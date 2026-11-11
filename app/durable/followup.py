"""Durable follow-up workflows: gentle check-in after customer silence.

Two workflows with different roles:
- ``follow_up_v2_workflow`` — sleeps between attempts, runs OUTSIDE turn_queue,
  only enqueues the fast send workflow.
- ``send_follow_up_v2_turn_workflow`` — drafts and dispatches one follow-up
  message, runs INSIDE turn_queue (concurrency=1, partitioned), so it has
  real mutual exclusion with any conversation_turn_workflow for the same
  tenant:remote_jid.

As esperas de 24h/48h das tentativas 3 e 4 vivem em ``procrastinate_jobs``
(``schedule_in``), nao na memoria do worker: sobrevivem a deploy e a queda.
"""

import logging
from datetime import datetime, time, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import text as _sql_text
from sqlalchemy.orm import Session

from app.agent.memory import get_conversation_history
from app.contracts import FollowUpDeliveryContext, OutboxCommand, ReplyDraft
from app.db import sql_session, transaction
from app.durable.outbox import dispatch_outbox_batch_workflow
from app.durable.persistence import (
    HUMAN_TEAM_OUTBOUND_PREDICATE,
    crew_release_window_seconds,
    insert_outbox_command_txn,
    release_expired_crew_marking_with_session,
)
from app.durable.rescue_intake import record_rescue_intake_txn
from app.durable.tenant_registry import get_registry_engine
from app.identity_primitives import stable_id
from app.jobs import (
    AUX_QUEUE,
    TURN_PRIORITY_FOLLOWUP,
    RETRY_CADEIA,
    TURN_QUEUE,
    defer_once,
    jobs,
)
from app.context.campaigns import (
    CHAVE_CONTEXTO as CHAVE_CAMPANHAS,
    campanhas_vigentes as campanhas_vigentes_hoje,
    formatar_para_contexto as formatar_campanhas,
)
from app.reply.llm import _build_system_prompt, _data_atual_negocio, _secret_value
from app.reply.split import split_human_reply
from app.settings import get_settings
from app.status.rules import is_deferral_or_closing, is_farewell, is_objection

logger = logging.getLogger(__name__)

# V2 follow-up schedule. As tentativas 1 e 2 usam estes sleeps; a tentativa 3
# (indice 2) e SUBSTITUIDA pelo alvo de 23 h de FOLLOWUP_24H_* (o valor 24 h
# fica so como marcador do tamanho da tupla) e a 4 (indice 3) mantem o sleep de
# 48 h com a janela comercial.
FOLLOWUP_V2_WAITS_SECONDS: tuple[int, int, int, int] = (
    5 * 60,
    30 * 60,  # 15/out/2026 (chairman): o 2o acompanhamento era 10 min, cedo demais
    24 * 60 * 60,
    48 * 60 * 60,
)
FOLLOWUP_V2_GRACE_SECONDS = 2 * 60 * 60

# 17/out/2026 (ordem do chairman): a tentativa de 1 dia nao usa mais o sleep
# sequencial de 24 h. Ela sai 23 h depois da ultima fala do paciente -- ANTES de
# fechar a janela de 24 h da Meta, para poder reabrir a conversa se o lead
# responder -- e sempre dentro da janela 09:00-20:00 propria desta tentativa
# (business_end da politica segue 18:00 para as demais tentativas). As tentativas
# 1 (5 min) e 2 (30 min) NAO mudam (mesma ordem).
FOLLOWUP_24H_ATTEMPT = 3
FOLLOWUP_24H_TARGET_SECONDS = 23 * 60 * 60
FOLLOWUP_24H_WINDOW_START = time(9, 0)
FOLLOWUP_24H_WINDOW_END = time(20, 0)
FOLLOWUP_24H_UNREACHABLE_REASON = "janela_inalcancavel"

# Cloud API do WhatsApp (via Zernio): texto livre so dentro de 24 h da ultima
# mensagem do paciente. 17/out/2026: a tentativa de 1 dia agora sai as 23 h,
# dentro da janela (ver FOLLOWUP_24H_*), entao volta a caber nesse provedor; a
# de 48 h continua fora da janela da Meta e segue desligada aqui.
FOLLOWUP_MAX_ATTEMPTS_BY_PROVIDER: dict[str, int] = {"zernio": FOLLOWUP_24H_ATTEMPT}


def follow_up_max_attempts(provider: str) -> int:
    """Quantas tentativas o provedor suporta (Evolution: todas)."""
    return FOLLOWUP_MAX_ATTEMPTS_BY_PROVIDER.get(provider, len(FOLLOWUP_V2_WAITS_SECONDS))


def follow_up_partition_key(tenant_id: str, provider: str, instance: str, remote_jid: str) -> str:
    """Mesma chave dos turnos normais (app/identity.py): exclusao mutua real por conversa."""
    return stable_id("conversation", tenant_id, provider, instance, remote_jid)

# 21/set/2026: restaurado o estilo de regras por tentativa que a V1 tinha
# (removido na migração pra V2, 21/set mais cedo, achado revisando o
# histórico a pedido do chairman -- "aquilo levou muito tempo pra ser
# afinado"). #1 e #2 são as regras originais da V1, praticamente intactas
# (só ajustado o texto de #2 pra não dizer mais "última tentativa", já que a
# V2 tem 4 tentativas, não 2). #3 e #4 são novas -- a V1 nunca teve 24h/48h.
FOLLOWUP_RULE_ATTEMPT_1 = (
    "Este é o 1º follow-up: um toque leve e caloroso, poucos minutos depois "
    "do silêncio, que retoma o que ESTE cliente pediu (veja history). O "
    "cliente NÃO recusou: apenas ainda não respondeu. Mostre que você "
    "lembrou dele e deixe o próximo passo fácil, com insistência educada e "
    "sem cobrança. Como passou pouco tempo, não faça pressão de agenda. "
    "Escreva do zero, nunca um modelo genérico, nunca uma frase que "
    "serviria para qualquer pessoa."
)

# Attempt 2 (+15min total): commitment-consistency angle (Cialdini) -- the
# customer reached out first, so a reply is a natural continuation of what
# THEY started, not a new ask from us. State the PRINCIPLE only: do not
# dictate literal phrasing (an earlier version of this rule said "a single
# word or emoji is fine" and the model echoed that near-verbatim every
# time -- specific wording in the rule becomes the model's output wording).
FOLLOWUP_RULE_ATTEMPT_2 = (
    "Este é o 2º follow-up. Traga um MOTIVO NOVO para decidir, diferente do "
    "primeiro toque, usando um ângulo do repertório do perfil e algo "
    "específico e real que ESTE cliente disse (veja history). Não invente "
    "escassez nem urgência: qualquer menção a horários limitados ou agenda "
    "cheia só vale se o perfil do tenant mandar e for verdade para esta "
    "conversa. Ofereça verificar com a equipe em forma de pergunta. Calor "
    "humano, insistência leve e educada, pedido fácil de responder. Se o "
    "cliente já recusou ou apontou um obstáculo (distância, preço, tempo) em "
    "qualquer ponto desta conversa, não envie isto: apenas confirme que viu "
    "a mensagem anterior."
)

# Attempt 3 (+24h, next business day): a full day passed -- do not just
# repeat attempts #1/#2 with different words, that reads as nagging. Bring
# something the first two messages did not: a different detail from the
# conversation, or a small piece of new value (answer a question they
# likely have, mention something relevant). Still zero pressure.
FOLLOWUP_RULE_ATTEMPT_3 = (
    "Este é o 3º follow-up, enviado cerca de um dia depois — pode ser o "
    "último contato antes de a janela do canal fechar. NÃO repita com "
    "outras palavras o pedido das tentativas anteriores. Traga algo NOVO "
    "de valor para a pessoa decidir: uma informação útil e verdadeira, a "
    "antecipação de uma dúvida provável dela ou um convite para conhecer a "
    "empresa. Não use escassez nem urgência de agenda. Feche "
    "com um próximo passo concreto e fácil, em forma de pergunta (por "
    "exemplo escolher entre dois caminhos), e deixe a porta aberta sem "
    "culpa e sem cobrança; nunca termine só com \"fico à disposição\". Se o "
    "cliente já "
    "recusou em qualquer ponto desta conversa, não envie isto: apenas "
    "confirme que viu a mensagem anterior."
)

# Attempt 4 (+48h, final before the lead is marked lost): the graceful
# close. Never add urgency or scarcity pressure at the end of a silence —
# that reads as desperate and burns goodwill on the way out. Give the
# customer a dignified, no-pressure exit while leaving the door open.
FOLLOWUP_RULE_ATTEMPT_4 = (
    "Este é o 4º e último follow-up, logo antes de este lead ser marcado "
    "como perdido. Parta do que ESTE cliente disse de fato antes (veja "
    "history) — cite algo específico e real, nunca uma frase genérica. "
    "NÃO acrescente urgência nem escassez — uma última mensagem que "
    "aperta mais soa desesperada e gasta a boa vontade na saída. Dê ao "
    "cliente um jeito elegante e sem pressão de responder ou de deixar "
    "para lá — deixe explícito que não há problema de nenhum lado e que a "
    "porta continua aberta quando ele quiser. Continue caloroso e breve. "
    "Se o cliente já recusou em qualquer ponto desta conversa, não envie "
    "isto: apenas confirme que viu a mensagem anterior."
)

FOLLOWUP_RULES_BY_ATTEMPT = {
    1: FOLLOWUP_RULE_ATTEMPT_1,
    2: FOLLOWUP_RULE_ATTEMPT_2,
    3: FOLLOWUP_RULE_ATTEMPT_3,
    4: FOLLOWUP_RULE_ATTEMPT_4,
}


class FollowUpPolicy(BaseModel):
    """Tenant-owned scheduling policy, loaded durably before V2 begins."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str = Field(min_length=1, max_length=128)
    enabled: bool
    delivery_mode: Literal["record_only", "live"] = "record_only"
    timezone: str = "America/Sao_Paulo"
    business_start: time = time(9, 0)
    business_end: time = time(18, 0)
    business_weekdays: tuple[int, ...] = (0, 1, 2, 3, 4)

    @model_validator(mode="after")
    def validate_schedule(self) -> "FollowUpPolicy":
        """Reject policies that could crash or loop a durable scheduler."""
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("unknown follow-up timezone") from exc
        if not self.business_weekdays:
            raise ValueError("business weekdays cannot be empty")
        if any(day < 0 or day > 6 for day in self.business_weekdays):
            raise ValueError("business weekdays must use Python weekday values")
        if self.business_start >= self.business_end:
            raise ValueError("business hours must have a positive window")
        return self

    @classmethod
    def enabled_default(cls, tenant_id: str) -> "FollowUpPolicy":
        """Test fixture and migration default; production still loads PG policy."""
        return cls(tenant_id=tenant_id, enabled=True)


class FollowUpAttemptReceipt(BaseModel):
    """Durable result from a queued V2 attempt, never inferred from enqueue."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["confirmed", "stopped", "review_required"]
    command_count: int = Field(default=0, ge=0, le=99)
    delivery_mode: Literal["record_only", "live"] | None = None


class FollowUpDeliveryPolicy(BaseModel):
    """Config snapshot consumed by V2's deterministic sender body."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    delivery_mode: Literal["record_only", "live"]
    max_blocks: int = Field(ge=1, le=99)
    target_chars: int = Field(ge=1, le=1024)


_FOLLOWUP_POLICY_SQL = _sql_text(
    """
    /* followup.v2_policy */
    SELECT tenant_id, enabled, delivery_mode, timezone, business_start, business_end, business_weekdays
    FROM genius_next.follow_up_policies
    WHERE tenant_id = :tenant_id
    """
)

_LIFECYCLE_ENSURE_SQL = _sql_text(
    """
    /* followup.v2_lifecycle_ensure */
    INSERT INTO genius_next.follow_up_lifecycles
        (tenant_id, origin_turn_id, phone, remote_jid, instance, origin_sequence, state)
    VALUES (:tenant_id, :origin_turn_id, :phone, :remote_jid, :instance, :origin_sequence, 'scheduled')
    ON CONFLICT (tenant_id, origin_turn_id) DO NOTHING
    """
)

_LIFECYCLE_SET_STATE_SQL = _sql_text(
    """
    /* followup.v2_lifecycle_state */
    UPDATE genius_next.follow_up_lifecycles
    SET state = :state, updated_at = now()
    WHERE tenant_id = :tenant_id
      AND origin_turn_id = :origin_turn_id
      AND state = 'scheduled'
    """
)

_ATTEMPT_RECEIPT_SQL = _sql_text(
    """
    /* followup.v2_attempt_receipt */
    INSERT INTO genius_next.follow_up_attempt_receipts
        (tenant_id, origin_turn_id, attempt, status, command_count, delivery_mode)
    VALUES (:tenant_id, :origin_turn_id, :attempt, :status, :command_count, :delivery_mode)
    ON CONFLICT (tenant_id, origin_turn_id, attempt) DO NOTHING
    """
)

# 17/out/2026: encerramento silencioso e auditavel do lifecycle quando a
# tentativa de 1 dia nao tem nenhum instante permitido entre a ultima fala e o
# alvo (janela_inalcancavel). Sem envio, sem alerta: so o motivo fica gravado.
_LIFECYCLE_STOP_WITH_REASON_SQL = _sql_text(
    """
    /* followup.v2_lifecycle_stop_reason */
    UPDATE genius_next.follow_up_lifecycles
    SET state = 'stopped', stop_reason = :reason, updated_at = now()
    WHERE tenant_id = :tenant_id
      AND origin_turn_id = :origin_turn_id
      AND state = 'scheduled'
    """
)

_MARK_LOSS_AND_RESCUE_V2_SQL = _sql_text(
    """
    /* followup.v2_mark_loss_rescue */
    WITH eligible AS (
        SELECT l.tenant_id, l.origin_turn_id, l.phone, l.remote_jid, l.instance
        FROM genius_next.follow_up_lifecycles AS l
        JOIN genius_next.clients AS c
          ON c.tenant_id = l.tenant_id AND c.phone = l.phone
        WHERE l.tenant_id = :tenant_id
          AND l.origin_turn_id = :origin_turn_id
          AND l.state = 'scheduled'
          AND c.status = 'open'
          AND c.automation_state = 'open'
          AND COALESCE(c.last_intent, '') <> 'handoff'
          AND EXISTS (
              SELECT 1 FROM genius_next.follow_up_attempt_receipts AS r
              WHERE r.tenant_id = l.tenant_id
                AND r.origin_turn_id = l.origin_turn_id
                AND r.attempt = :last_attempt
                AND r.status = 'confirmed'
          )
          AND NOT EXISTS (
              SELECT 1 FROM genius_next.events AS e
              WHERE e.tenant_id = l.tenant_id
                AND e.phone = l.phone
                AND e.direction = 'inbound'
                AND e.conversation_sequence > l.origin_sequence
          )
        FOR UPDATE OF l, c
    ), marked AS (
        UPDATE genius_next.clients AS c
        SET status = 'loss', automation_state = 'loss', updated_at = now()
        FROM eligible AS e
        WHERE c.tenant_id = e.tenant_id AND c.phone = e.phone
        RETURNING e.tenant_id, e.origin_turn_id, e.phone, e.remote_jid, e.instance
    ), queued AS (
        UPDATE genius_next.follow_up_lifecycles AS l
        SET state = 'rescue_queued', updated_at = now()
        FROM marked AS m
        WHERE l.tenant_id = m.tenant_id AND l.origin_turn_id = m.origin_turn_id
        RETURNING m.tenant_id, m.origin_turn_id, m.phone, m.remote_jid, m.instance
    )
    INSERT INTO genius_next.follow_up_candidates
        (tenant_id, phone, remote_jid, instance, origin_turn_id, handoff_key, status, audit)
    SELECT tenant_id, phone, remote_jid, instance, origin_turn_id,
           :handoff_key, 'queued', jsonb_build_object('source', 'followup_v2')
    FROM queued
    ON CONFLICT (tenant_id, handoff_key) DO NOTHING
    RETURNING phone
    """
)


def business_opening_wait_seconds(now: datetime, policy: FollowUpPolicy) -> int:
    """Return seconds to the next configured BRT business opening (no holidays)."""
    if not policy.enabled:
        return 0
    local_now = now.astimezone(ZoneInfo(policy.timezone))
    candidate_date = local_now.date()
    while True:
        opening = datetime.combine(candidate_date, policy.business_start, tzinfo=local_now.tzinfo)
        closing = datetime.combine(candidate_date, policy.business_end, tzinfo=local_now.tzinfo)
        if candidate_date.weekday() in policy.business_weekdays:
            if opening <= local_now < closing:
                return 0
            if local_now < opening:
                return int((opening - local_now).total_seconds())
        candidate_date += timedelta(days=1)


def follow_up_24h_target_instant(origin: datetime, policy: FollowUpPolicy) -> datetime | None:
    """Instant of the 1-day attempt: ``origin + 23 h`` inside the 09:00-20:00 window.

    ``origin`` is the patient's last message (the workflow starts right after
    it).  The target is only ever ANTICIPATED -- to the latest allowed instant
    that is still ``<= target`` and ``> origin`` on a configured business
    weekday -- never postponed, because after the 24 h Meta window only an
    approved template can reopen the conversation.  Returns ``None`` when no
    allowed instant exists in ``(origin, target]`` (``janela_inalcancavel``):
    the caller sends nothing and leaves a silent auditable event.
    """
    tz = ZoneInfo(policy.timezone)
    local_origin = origin.astimezone(tz)
    target = local_origin + timedelta(seconds=FOLLOWUP_24H_TARGET_SECONDS)
    day = target.date()
    origin_day = local_origin.date()
    while day >= origin_day:
        if day.weekday() in policy.business_weekdays:
            opening = datetime.combine(day, FOLLOWUP_24H_WINDOW_START, tzinfo=tz)
            closing = datetime.combine(day, FOLLOWUP_24H_WINDOW_END, tzinfo=tz)
            candidate = min(closing, target)
            if candidate >= opening and candidate > local_origin:
                return candidate
        day -= timedelta(days=1)
    return None


def follow_up_24h_target_step(policy: FollowUpPolicy) -> str | None:
    """Checkpoint wall-clock + window math for the 1-day attempt (None = unreachable)."""
    origin = datetime.now(ZoneInfo(policy.timezone))
    target = follow_up_24h_target_instant(origin, policy)
    return target.isoformat() if target is not None else None


def follow_up_wait_to_target_step(target_iso: str, policy: FollowUpPolicy) -> int:
    """Seconds from now until the 1-day target (0 when already due)."""
    target = datetime.fromisoformat(target_iso)
    now = datetime.now(ZoneInfo(policy.timezone))
    return max(0, int((target - now).total_seconds()))


def load_follow_up_policy_step(tenant_id: str) -> FollowUpPolicy | None:
    """Load the tenant policy once; absent or disabled must stop V2."""
    settings = get_settings()
    engine = get_registry_engine(settings.DATABASE_URL)
    with Session(engine) as session:
        row = session.execute(_FOLLOWUP_POLICY_SQL, {"tenant_id": tenant_id}).mappings().one_or_none()
    if row is None or not row["enabled"]:
        return None
    try:
        return FollowUpPolicy.model_validate(dict(row))
    except ValueError:
        logger.error("invalid follow-up policy; refusing to schedule", exc_info=True)
        return None


def follow_up_delivery_policy_step(
    delivery_mode: Literal["record_only", "live"],
) -> FollowUpDeliveryPolicy:
    """Checkpoint mutable delivery settings before the V2 sender builds commands."""
    settings = get_settings()
    return FollowUpDeliveryPolicy(
        delivery_mode="live" if delivery_mode == "live" and settings.DELIVERY_MODE == "live" else "record_only",
        max_blocks=settings.OUTPUT_SPLIT_MAX_BLOCKS,
        target_chars=settings.OUTPUT_SPLIT_TARGET_CHARS,
    )


@transaction(isolation_level="SERIALIZABLE")
def ensure_follow_up_v2_lifecycle_txn(
    tenant_id: str,
    remote_jid: str,
    phone: str,
    instance: str,
    origin_turn_id: str,
) -> bool:
    """Durably create the V2 lifecycle once before scheduling any attempt.

    ``True`` so para quem criou: e o que impede dois ciclos para o mesmo turno de origem
    (antes quem impedia era o ID estavel do workflow). ``False`` tambem quando a
    guarda de abertura (``motivo_para_nao_abrir_ciclo``) recusa o ciclo.
    """
    params = {
        "tenant_id": tenant_id,
        "origin_turn_id": origin_turn_id,
        "phone": phone,
        "remote_jid": remote_jid,
        "instance": instance,
        "origin_sequence": _origin_sequence(sql_session(), tenant_id, origin_turn_id),
    }
    # 03/nov/2026: guarda de ABERTURA do ciclo (objecao/despedida/adiamento na
    # ultima fala, teto de 24 h e 7 dias). Registra o ciclo como stopped com o
    # motivo e devolve False: nada e agendado.
    motivo = motivo_para_nao_abrir_ciclo(sql_session(), tenant_id, phone)
    if motivo is not None:
        sql_session().execute(_LIFECYCLE_ENSURE_STOPPED_SQL, {**params, "reason": motivo})
        return False
    created = sql_session().execute(_LIFECYCLE_ENSURE_SQL, params)
    return created.rowcount == 1


_RECEIPT_STATUS_SQL = _sql_text(
    """
    /* followup.v2_receipt_status */
    SELECT status FROM genius_next.follow_up_attempt_receipts
    WHERE tenant_id = :tenant_id AND origin_turn_id = :origin_turn_id AND attempt = :attempt
    """
)


@transaction(isolation_level="READ COMMITTED")
def follow_up_v2_receipt_status_txn(
    tenant_id: str, origin_turn_id: str, attempt: int
) -> str | None:
    """Recibo ja gravado da tentativa (repeticao do job apos queda continua dai)."""
    return sql_session().execute(
        _RECEIPT_STATUS_SQL,
        {"tenant_id": tenant_id, "origin_turn_id": origin_turn_id, "attempt": attempt},
    ).scalar_one_or_none()


@transaction(isolation_level="SERIALIZABLE")
def record_follow_up_v2_receipt_txn(
    tenant_id: str,
    origin_turn_id: str,
    attempt: int,
    receipt: FollowUpAttemptReceipt,
) -> None:
    """Persist the actual delivery outcome; uncertainty halts automatic loss."""
    sql_session().execute(
        _ATTEMPT_RECEIPT_SQL,
        {
            "tenant_id": tenant_id,
            "origin_turn_id": origin_turn_id,
            "attempt": attempt,
            "status": receipt.status,
            "command_count": receipt.command_count,
            "delivery_mode": receipt.delivery_mode,
        },
    )
    state = "review_required" if receipt.status == "review_required" else "stopped"
    if receipt.status != "confirmed":
        sql_session().execute(
            _LIFECYCLE_SET_STATE_SQL,
            {"tenant_id": tenant_id, "origin_turn_id": origin_turn_id, "state": state},
        )


@transaction(isolation_level="SERIALIZABLE")
def mark_loss_and_queue_for_rescue_v2_txn(
    tenant_id: str,
    origin_turn_id: str,
    last_attempt: int = len(FOLLOWUP_V2_WAITS_SECONDS),
) -> bool:
    """Atomically perform the silent new→loss transition and one Rescue handoff.

    ``last_attempt`` e a ultima tentativa que o provedor permite (20/out/2026):
    era fixo em 4, e o Zernio para na 3 (janela de 24 h da Meta), entao o loss
    por silencio nunca acontecia la. Quem chama pela agenda do Manager passa
    ``follow_up_max_attempts(provider)``.
    """
    handoff_key = stable_id("rescue-handoff", tenant_id, origin_turn_id)
    marked = sql_session().execute(
        _MARK_LOSS_AND_RESCUE_V2_SQL,
        {
            "tenant_id": tenant_id,
            "origin_turn_id": origin_turn_id,
            "handoff_key": handoff_key,
            "last_attempt": last_attempt,
        },
    ).scalar_one_or_none()
    return marked is not None


_SCHEDULE_LOSS_SILENCIO_SQL = _sql_text(
    """
    /* manager.agenda.loss_silencio */
    INSERT INTO genius_next.manager_items
        (item_key, tenant_id, phone, remote_jid, instance, provider,
         origin_turn_id, kind, question, state, payload, due_at)
    VALUES
        (:item_key, :tenant_id, :phone, :remote_jid, :instance, :provider,
         :origin_turn_id, 'loss_silencio', 'loss por silencio', 'open',
         jsonb_build_object('last_attempt', CAST(:last_attempt AS integer)),
         now() + make_interval(hours => CAST(:horas AS integer)))
    ON CONFLICT (item_key) DO NOTHING
    RETURNING item_key
    """
)


def loss_silencio_item_key(tenant_id: str, origin_turn_id: str) -> str:
    return stable_id("manager-agenda", tenant_id, "loss_silencio", origin_turn_id)


@transaction(isolation_level="SERIALIZABLE")
def schedule_loss_silencio_txn(
    tenant_id: str,
    phone: str,
    remote_jid: str,
    instance: str,
    provider: str,
    origin_turn_id: str,
) -> bool:
    """Agenda o loss por silencio na agenda do Manager (20/out/2026).

    So para tenant em MANAGER_AGENDA_TENANTS; fora dela nada muda (o ciclo
    termina como antes). Idempotente por ``item_key``: replay nao duplica.
    """
    settings = get_settings()
    if not settings.manager_agenda_enabled(tenant_id):
        return False
    inserted = sql_session().execute(
        _SCHEDULE_LOSS_SILENCIO_SQL,
        {
            "item_key": loss_silencio_item_key(tenant_id, origin_turn_id),
            "tenant_id": tenant_id,
            "phone": phone,
            "remote_jid": remote_jid,
            "instance": instance,
            "provider": provider,
            "origin_turn_id": origin_turn_id,
            "last_attempt": follow_up_max_attempts(provider),
            "horas": settings.MANAGER_LOSS_SILENCIO_HORAS,
        },
    ).scalar_one_or_none()
    return inserted is not None


@transaction(isolation_level="SERIALIZABLE")
def record_follow_up_24h_unreachable_txn(tenant_id: str, origin_turn_id: str) -> None:
    """Silent auditable event: the 1-day target has no allowed instant.

    Records the reason on the lifecycle and a stopped receipt for the attempt
    (the receipt is evidence it never reached the sender); nothing is sent and
    no one is alerted.  Both statements are idempotent under replay: the UPDATE
    only touches a ``scheduled`` row and the receipt uses ON CONFLICT.
    """
    sql_session().execute(
        _LIFECYCLE_STOP_WITH_REASON_SQL,
        {
            "tenant_id": tenant_id,
            "origin_turn_id": origin_turn_id,
            "reason": FOLLOWUP_24H_UNREACHABLE_REASON,
        },
    )
    sql_session().execute(
        _ATTEMPT_RECEIPT_SQL,
        {
            "tenant_id": tenant_id,
            "origin_turn_id": origin_turn_id,
            "attempt": FOLLOWUP_24H_ATTEMPT,
            "status": "stopped",
            "command_count": 0,
            "delivery_mode": None,
        },
    )


# ── SQL queries (V2 only) ────────────────────────────────────────────────

_LIFECYCLE_STATE_SQL = _sql_text(
    """
    /* followup.lifecycle_state */
    SELECT state
    FROM genius_next.follow_up_lifecycles
    WHERE tenant_id = :tenant_id
      AND origin_turn_id = :origin_turn_id
    """
)


# O paciente escreveu depois do turno de origem?
# 11/nov/2026: a comparacao era so por ``conversation_sequence``, sem olhar a conversa. O mesmo
# telefone pode ter mais de uma conversa no tenant (instancias/provedores), cada uma com o seu
# contador: uma conversa antiga com sequencia alta fazia TODO follow-up de uma conversa nova parar
# como "paciente respondeu" (visto no Standby: 28 mensagens antigas pelo Zernio paravam o ciclo da
# conversa de teste, sequencia 4). Agora:
#   - mesma conversa do turno de origem (provider, instance, remote_jid): por sequencia;
#   - outra conversa do mesmo telefone: por tempo, so o que chegou depois do turno de origem
#     (quem escreveu por outro canal depois continua parando o ciclo);
#   - sem a linha do turno de origem: regra antiga (qualquer conversa, por sequencia), que so
#     erra para o lado de NAO enviar.
_INBOUND_AFTER_ORIGIN_SQL = _sql_text(
    """
    /* followup.active_event_after */
    SELECT 1
    FROM genius_next.events AS e
    LEFT JOIN genius_next.turns AS t
      ON t.id = :origin_turn_id AND t.tenant_id = e.tenant_id
    WHERE e.tenant_id = :tenant_id
      AND e.phone = :phone
      AND e.direction = 'inbound'
      AND CASE
            WHEN t.id IS NULL THEN e.conversation_sequence > :origin_sequence
            WHEN e.provider IS NOT DISTINCT FROM t.provider
                 AND e.instance IS NOT DISTINCT FROM t.instance
                 AND e.remote_jid IS NOT DISTINCT FROM t.remote_jid
              THEN e.conversation_sequence > :origin_sequence
            ELSE e.received_at > t.created_at
          END
    LIMIT 1
    """
)

_CLIENT_STATE_SQL = _sql_text(
    """
    /* followup.client_status */
    SELECT status, last_intent, automation_state FROM genius_next.clients
    WHERE tenant_id = :tenant_id AND phone = :phone
    """
)

# Humano da equipe escreveu para o contato desde o inicio do turno de origem
# (turns.created_at, o claim): conta tambem a mensagem digitada enquanto a IA
# ainda redigia a resposta de origem (antes de completed_at).  Mesma definicao
# de "mensagem humana da equipe" que renova a marcacao no sweep
# (HUMAN_TEAM_OUTBOUND_PREDICATE): outbound que nao saiu do nosso outbox.  Por
# tempo, e nao por sequencia, porque o mesmo telefone pode ter mais de uma
# conversa (instancias/provedores) com contadores de sequencia independentes.
# Sem a linha do turno de origem o JOIN nao casa e a checagem nao para nada (as
# outras continuam valendo).
_TEAM_MESSAGE_AFTER_ORIGIN_SQL = _sql_text(
    f"""
    /* followup.team_message_after_origin */
    SELECT 1
    FROM genius_next.events AS e
    JOIN genius_next.turns AS t
      ON t.id = :origin_turn_id AND t.tenant_id = e.tenant_id
    WHERE e.tenant_id = :tenant_id
      AND e.phone = :phone
      AND e.received_at > t.created_at
      AND {HUMAN_TEAM_OUTBOUND_PREDICATE}
    LIMIT 1
    """
)


# Ultima fala do paciente (para ver se ele adiou ou encerrou). Por tempo, como
# _TEAM_MESSAGE_AFTER_ORIGIN_SQL: o mesmo telefone pode ter mais de uma conversa.
_LAST_INBOUND_TEXT_SQL = _sql_text(
    """
    /* followup.last_inbound_text */
    SELECT coalesce(text_content, '') FROM genius_next.events
    WHERE tenant_id = :tenant_id AND phone = :phone AND direction = 'inbound'
    ORDER BY received_at DESC, id DESC LIMIT 2
    """
)

# Follow-ups confirmados para o contato nas ultimas 24 h, somando ciclos.
_FOLLOWUPS_24H_SQL = _sql_text(
    """
    /* followup.confirmed_last_24h */
    SELECT count(*)
    FROM genius_next.follow_up_attempt_receipts AS r
    JOIN genius_next.follow_up_lifecycles AS l
      ON l.tenant_id = r.tenant_id AND l.origin_turn_id = r.origin_turn_id
    WHERE r.tenant_id = :tenant_id AND l.phone = :phone
      AND r.status = 'confirmed' AND r.recorded_at > now() - interval '24 hours'
    """
)


# Follow-ups confirmados para o contato nos ultimos 7 dias, somando ciclos.
# 03/nov/2026: o teto de 24 h sozinho deixava o contato receber 3 mensagens por
# dia, todo dia, enquanto ele respondesse (cada resposta abria um ciclo novo).
_FOLLOWUPS_7D_SQL = _sql_text(
    """
    /* followup.confirmed_last_7d */
    SELECT count(*)
    FROM genius_next.follow_up_attempt_receipts AS r
    JOIN genius_next.follow_up_lifecycles AS l
      ON l.tenant_id = r.tenant_id AND l.origin_turn_id = r.origin_turn_id
    WHERE r.tenant_id = :tenant_id AND l.phone = :phone
      AND r.status = 'confirmed' AND r.recorded_at > now() - interval '7 days'
    """
)

# Ciclo que nao chegou a abrir: fica registrado (stopped + motivo) para auditoria.
_LIFECYCLE_ENSURE_STOPPED_SQL = _sql_text(
    """
    /* followup.v2_lifecycle_ensure_stopped */
    INSERT INTO genius_next.follow_up_lifecycles
        (tenant_id, origin_turn_id, phone, remote_jid, instance, origin_sequence, state, stop_reason)
    VALUES (:tenant_id, :origin_turn_id, :phone, :remote_jid, :instance, :origin_sequence, 'stopped', :reason)
    ON CONFLICT (tenant_id, origin_turn_id) DO NOTHING
    """
)

# Limites (03/nov/2026): ver FOLLOWUP_MAX_PER_PHONE_24H / _7D em app/settings.py.
# - 24 h: no maximo 3 tentativas confirmadas por contato, somando ciclos.
# - 7 dias: no maximo 5 (um ciclo cheio de 3 mais 2 de um segundo ciclo).
# O teto vale na abertura do ciclo (``ensure_follow_up_v2_lifecycle_txn``) e a
# cada despertar/envio (``_should_continue``), entao jobs ja agendados antes
# deste deploy tambem sao contidos.
MOTIVO_FALA_ENCERRA = "fala_do_paciente_encerra"
MOTIVO_TETO_24H = "teto_24h"
MOTIVO_TETO_7D = "teto_7d"


def _fala_encerra_ciclo(ultimas: list[str]) -> bool:
    """A conversa terminou, do lado do paciente: adiamento, objecao ou despedida.

    ``ultimas`` vem de ``_LAST_INBOUND_TEXT_SQL`` (as duas falas mais recentes, a
    mais nova primeiro). Adiamento/objecao valem nas duas ("Depois falo com
    voce" seguido de "Ok" e adiamento); despedida ("ok obrigada") so na ultima:
    quem agradeceu e depois fez uma pergunta nao se despediu.
    """
    if any(t and is_deferral_or_closing(t) for t in ultimas):
        return True
    return bool(ultimas and ultimas[0] and is_farewell(ultimas[0]))


def _teto_de_envios_atingido(session: Session, tenant_id: str, phone: str) -> str | None:
    """Motivo do teto de follow-ups por contato (24 h, depois 7 dias), ou ``None``."""
    settings = get_settings()
    params = {"tenant_id": tenant_id, "phone": phone}
    sent_24h = session.execute(_FOLLOWUPS_24H_SQL, params).scalar_one_or_none() or 0
    if sent_24h >= settings.FOLLOWUP_MAX_PER_PHONE_24H:
        return MOTIVO_TETO_24H
    sent_7d = session.execute(_FOLLOWUPS_7D_SQL, params).scalar_one_or_none() or 0
    if sent_7d >= settings.FOLLOWUP_MAX_PER_PHONE_7D:
        return MOTIVO_TETO_7D
    return None


def motivo_para_nao_abrir_ciclo(session: Session, tenant_id: str, phone: str) -> str | None:
    """Por que um ciclo NOVO de follow-up nao deve abrir para este contato, ou ``None``.

    03/nov/2026 (incidente "Vix" / "Muito longe"): o ciclo reabria a cada resposta
    do paciente, mesmo sendo uma objecao ou despedida, e nao havia teto real de
    mensagens. Agora nao abre ciclo quando a ultima fala do paciente adia,
    objeta ou se despede, nem quando o contato ja atingiu o teto de 24 h / 7 dias.
    """
    ultimas = session.execute(
        _LAST_INBOUND_TEXT_SQL, {"tenant_id": tenant_id, "phone": phone}
    ).scalars().all()
    if _fala_encerra_ciclo(list(ultimas)):
        return MOTIVO_FALA_ENCERRA
    return _teto_de_envios_atingido(session, tenant_id, phone)


def _origin_sequence(session: Session, tenant_id: str, turn_id: str) -> int:
    """Return the last_sequence of the origin turn, defaulting to 0."""
    row = session.execute(
        _sql_text("SELECT last_sequence FROM genius_next.turns WHERE id = :turn_id AND tenant_id = :tenant_id"),
        {"tenant_id": tenant_id, "turn_id": turn_id},
    ).scalar_one_or_none()
    return int(row) if row is not None else 0


def _should_continue(
    session: Session,
    tenant_id: str,
    phone: str,
    origin_sequence: int,
    origin_turn_id: str,
    *,
    release_window_seconds: float | None = None,
    apply_crew_release: bool = False,
) -> bool:
    """Allow sends only for a silent, active, automation-owned client.

    Silent = no inbound after the origin turn AND no human team message since
    the origin turn started.  Automation-owned = open/open, not ``handoff``: a
    human owner marking still standing at fire time stops the follow-up.

    With ``release_window_seconds`` an expired marking (the sweep rule,
    ``_CREW_RELEASE_ELIGIBLE``) counts as already released, so the follow-up
    never depends on the 20 s sweep clock (restart, queue).  The read-only
    step only evaluates it; the queued transaction (``apply_crew_release``)
    performs the same release the sweep would, before sending.
    """
    state = session.execute(
        _LIFECYCLE_STATE_SQL,
        {"tenant_id": tenant_id, "origin_turn_id": origin_turn_id},
    ).scalar_one_or_none()
    if state is not None and state != "scheduled":
        return False

    active = session.execute(
        _INBOUND_AFTER_ORIGIN_SQL,
        {
            "tenant_id": tenant_id,
            "phone": phone,
            "origin_sequence": origin_sequence,
            "origin_turn_id": origin_turn_id,
        },
    ).scalar_one_or_none()
    if active is not None:
        return False  # customer responded

    # 15/out/2026: a ultima fala do paciente foi adiamento/encerramento ("sem
    # pressa", "quando o dinheiro entrar"): insistir vira cobranca.
    # 03/nov/2026: objecao (distancia/custo/tempo) e despedida ("ok obrigada") tambem.
    ultimas = session.execute(
        _LAST_INBOUND_TEXT_SQL, {"tenant_id": tenant_id, "phone": phone}
    ).scalars().all()
    if _fala_encerra_ciclo(list(ultimas)):
        return False

    # 15/out/2026: teto por contato em 24 h, somando ciclos (cada resposta do
    # paciente abria um ciclo novo de 2 tentativas). 03/nov/2026: e em 7 dias.
    if _teto_de_envios_atingido(session, tenant_id, phone) is not None:
        return False

    client = session.execute(
        _CLIENT_STATE_SQL,
        {"tenant_id": tenant_id, "phone": phone},
    ).mappings().one_or_none()
    if client is None:
        return False
    if client.get("automation_state") == "crew" and release_window_seconds:
        # 08/out/2026: marcacao de equipe com prazo vencido (mesma regra do
        # sweep) vale como devolvida; sem prazo ou ainda de pe, para.
        released = release_expired_crew_marking_with_session(
            session,
            tenant_id=tenant_id,
            phone=phone,
            window_seconds=release_window_seconds,
            apply=apply_crew_release,
        )
        if released is None:
            return False
        client = released
    if (
        client["status"] != "open"
        or client["last_intent"] == "handoff"
        or client.get("automation_state", "open") != "open"
    ):
        return False

    # 08/out/2026: a marcacao automatica de equipe com prazo nao mata mais o
    # follow-up -- ela expira em 4min30 e a decisao fica para a hora do
    # disparo.  Se alguem da equipe escreveu para o contato desde o inicio do
    # turno de origem, a conversa e dela: o follow-up para.
    team_wrote = session.execute(
        _TEAM_MESSAGE_AFTER_ORIGIN_SQL,
        {"tenant_id": tenant_id, "phone": phone, "origin_turn_id": origin_turn_id},
    ).scalar_one_or_none()
    if team_wrote is not None:
        return False

    return True


def should_continue_follow_up(
    tenant_id: str,
    phone: str,
    origin_turn_id: str,
) -> bool:
    """Checkpoint: reading whether follow-up should continue (never writes)."""
    settings = get_settings()
    engine = get_registry_engine(settings.DATABASE_URL)
    with Session(engine) as session:
        seq = _origin_sequence(session, tenant_id, origin_turn_id)
        return _should_continue(
            session,
            tenant_id,
            phone,
            seq,
            origin_turn_id,
            release_window_seconds=crew_release_window_seconds(settings),
        )


@transaction(isolation_level="SERIALIZABLE")
def should_continue_follow_up_txn(
    tenant_id: str,
    phone: str,
    origin_turn_id: str,
) -> bool:
    """Re-check inside turn_queue, with transactional consistency.

    An expired crew marking is released here, in this commit, before the
    follow-up drafts and sends -- the outbox re-checks automation_state.
    """
    seq = _origin_sequence(sql_session(), tenant_id, origin_turn_id)
    return _should_continue(
        sql_session(),
        tenant_id,
        phone,
        seq,
        origin_turn_id,
        release_window_seconds=crew_release_window_seconds(get_settings()),
        apply_crew_release=True,
    )





_JA_ENVIADAS_MAX = 6

# Repertório genérico de ângulos de persuasão (vale para qualquer tenant). Cada
# item diz a INTENÇÃO, nunca a frase: frase literal na regra vira frase do
# modelo. O código sorteia de forma determinística o ``angulo_da_vez`` por
# (tenant, contato, tentativa, quantas mensagens já saíram): sem isso o modelo
# volta sempre ao mesmo ângulo favorito e a conversa vira oito mensagens iguais.
FOLLOWUP_ANGULOS: tuple[tuple[str, str], ...] = (
    # 03/nov/2026: revisao de TODOS os angulos (incidente "Muito longe"): nenhum
    # pode apontar culpa, comparar a pessoa com terceiros ("outras familias/pessoas
    # fazem X mesmo com [obstaculo que ela citou]") nem contornar um obstaculo que
    # ela mesma apontou. Escassez e' recurso de ultimo caso (conversa avancada).
    ("horario_agenda", "RECURSO DE ÚLTIMO CASO, nunca padrão: só se o perfil do tenant autorizar e a conversa já estiver avançada (a pessoa disse o que quer e já recebeu a informação); nunca para quem só pediu informação nem depois de objeção. Urgência leve e verdadeira de agenda, sem número nem prazo"),
    ("valor_educativo", "explicar em termos gerais e sem número por que o que a pessoa busca importa, de forma educativa e sem prometer resultado; sem medo, sem culpa e sem comparar com outras pessoas"),
    ("facilidade", "reduzir o esforço: mostrar o menor próximo passo possível para encaminhar"),
    ("duvida_antecipada", "antecipar a dúvida provável (como funciona, o que levar, como é o primeiro passo) usando só fatos disponíveis"),
    ("reciprocidade", "dar algo útil antes de pedir: uma dica, uma informação ou um esclarecimento relevante para o caso dela, sem condicionar a nada"),
    ("compromisso", "retomar o que a própria pessoa pediu ou disse que ia fazer, como continuação natural do que ela começou"),
    ("escolha_pequena", "propor uma escolha mínima entre dois caminhos (período ou dia), nunca um horário exato"),
    ("motivo_pessoal", "ligar ao que a própria pessoa contou que quer resolver ou a quem ela cuida, usando só o que ela disse, sem apontar descuido nem culpa. PROIBIDO comparar com terceiros: nunca dizer que outras famílias/pessoas fazem X mesmo com [obstáculo que ela citou: distância, preço, tempo]. Exemplo NEGATIVO real, jamais escrever algo assim: \"Algumas famílias decidem proteger seus filhos contra a meningite mesmo morando mais longe\""),
    ("encolher_decisao", "baixar a barreira: o primeiro passo é pequeno (conversar, avaliar, tirar a dúvida), não a decisão final"),
    ("pergunta_curiosa", "uma pergunta aberta e curiosa sobre a situação dela, com eco do que ela disse, para reabrir a conversa"),
    ("validacao_do_passo", "reconhecer com sinceridade que ter procurado já foi um bom passo e que é natural decidir com calma"),
    ("autoridade_verdadeira", "citar um diferencial, profissional ou estrutura que conste nas informações da empresa, sem comparar com outras clínicas ou pessoas e sem usar isso para vencer uma objeção"),
    ("convite_conhecer", "convidar a conhecer a empresa pelo perfil, site ou conteúdo, apenas se o link constar nas informações do perfil"),
    ("oferta_de_verificar", "perguntar se ela quer que a equipe veja a disponibilidade da semana, em forma de pergunta e sem prometer"),
    ("campanha_vigente", "mencionar uma campanha de campanhas_vigentes, pelo que está escrito nela; se campanhas_vigentes não existir no contexto, não cite campanha e faça uma pergunta leve sobre o que a pessoa procura"),
)
_FOLLOWUP_ANGULOS_NAO_NA_1A = frozenset({"convite_conhecer", "campanha_vigente"})
# 22/out/2026 (incidente followup-contradiz-handoff, contato 9322): pressao de
# agenda e campanha fazem sentido para lead frio que ainda esta decidindo; para
# quem ja foi encaminhado para a equipe e ali esta esperando o retorno dela,
# viram cobranca (AGENTS.md regra 9 — o agente nao vira cobrador quando a falha
# e da equipe).  Ciclo nascido de handoff expirado nunca usa estes angulos.
_FOLLOWUP_ANGULOS_NAO_POS_HANDOFF = frozenset({"horario_agenda", "campanha_vigente"})
# 03/nov/2026: depois de QUALQUER objecao do paciente na conversa (distancia,
# custo, tempo, adiamento) saem os angulos de pressao ou de comparacao com
# terceiros. Sobram so os que acolhem, esclarecem ou perguntam.
_FOLLOWUP_ANGULOS_NAO_POS_OBJECAO = frozenset(
    {
        "horario_agenda",
        "campanha_vigente",
        "motivo_pessoal",
        "autoridade_verdadeira",
        "valor_educativo",
        "escolha_pequena",
    }
)
_FOLLOWUP_ANGULO_FINAL = ("porta_aberta", "fechar com elegância e sem pressão, deixando a porta aberta para quando ela quiser")


def angulo_da_vez(
    tenant_id: str,
    phone: str,
    attempt: int,
    ja_enviadas: int,
    *,
    pos_handoff: bool = False,
    pos_objecao: bool = False,
) -> tuple[str, str]:
    """Ângulo sugerido para esta mensagem, estável para a mesma conversa e variando entre elas.

    ``pos_handoff``: o ciclo nasceu de um encaminhamento à equipe que expirou
    sem resposta dela — exclui os ângulos de pressão/urgência.
    ``pos_objecao``: o paciente já objetou (distância, custo, tempo) ou adiou em
    algum ponto da conversa — exclui os ângulos de pressão e de comparação com
    terceiros (``_FOLLOWUP_ANGULOS_NAO_POS_OBJECAO``).
    """
    import hashlib

    if attempt >= len(FOLLOWUP_V2_WAITS_SECONDS):
        return _FOLLOWUP_ANGULO_FINAL
    semente = int(hashlib.sha256(f"{tenant_id}:{phone}".encode()).hexdigest()[:8], 16)
    base = semente + attempt * 5 + ja_enviadas * 3
    for passo in range(len(FOLLOWUP_ANGULOS)):
        angulo = FOLLOWUP_ANGULOS[(base + passo) % len(FOLLOWUP_ANGULOS)]
        # 5 min depois do silêncio ainda é cedo para convidar a conhecer a empresa.
        if attempt == 1 and angulo[0] in _FOLLOWUP_ANGULOS_NAO_NA_1A:
            continue
        if pos_handoff and angulo[0] in _FOLLOWUP_ANGULOS_NAO_POS_HANDOFF:
            continue
        if pos_objecao and angulo[0] in _FOLLOWUP_ANGULOS_NAO_POS_OBJECAO:
            continue
        return angulo
    return _FOLLOWUP_ANGULO_FINAL


def _pontas(texto: str, n: int = 4) -> tuple[str, str]:
    """Primeiras e últimas ``n`` palavras de uma mensagem (abertura e fecho)."""
    palavras = (texto or "").split()
    return " ".join(palavras[:n]), " ".join(palavras[-n:])


def _tokens_follow(texto: str) -> list[str]:
    import re
    import unicodedata

    decomp = unicodedata.normalize("NFKD", texto or "")
    sem = "".join(c for c in decomp if not unicodedata.combining(c)).casefold()
    return [t for t in re.split(r"[^a-z0-9]+", sem) if len(t) > 2]


def follow_up_repete(rascunho: str, anteriores: list[str]) -> bool:
    """O rascunho repete ou parafraseia alguma mensagem ja enviada ao contato?

    Deterministico: Jaccard de palavras >= 0.5 ou um trecho de 4 palavras igual.
    15/out/2026: o modelo repetia "sem pressa nenhuma", "fico por aqui" e
    aberturas inteiras para a mesma pessoa; silencio e melhor que repeticao.
    """
    novo = _tokens_follow(rascunho)
    if not novo:
        return False
    novo_set = set(novo)
    novo_4 = {tuple(novo[i : i + 4]) for i in range(len(novo) - 3)}
    for anterior in anteriores:
        velho = _tokens_follow(anterior)
        if not velho:
            continue
        velho_set = set(velho)
        uniao = novo_set | velho_set
        if uniao and len(novo_set & velho_set) / len(uniao) >= 0.5:
            return True
        if novo_4 & {tuple(velho[i : i + 4]) for i in range(len(velho) - 3)}:
            return True
    return False


_REGRA_ANTI_REPETICAO = (
            "ANTI-REPETIÇÃO (regra dura): mensagens_ja_enviadas lista o que você já enviou a esta "
            "pessoa; aberturas_ja_usadas e fechos_ja_usados mostram como elas começaram e terminaram. "
            "A nova mensagem deve diferir de TODAS em ângulo, abertura, fecho, estrutura da frase e "
            "tipo de pergunta: não comece nem termine de modo parecido, não reaproveite palavras-chave "
            "nem a mesma ideia com outras palavras, e não use sempre a mesma fórmula de convite. "
            "Use o angulo_da_vez como ângulo desta mensagem, adaptando ao que a pessoa disse; só troque "
            "por outro do repertório se ele não couber na conversa. A situação da conversa vence o "
            "ângulo: se a equipe ainda deve resposta, se a pessoa recusou ou adiou, ou se já agendou, "
            "siga o que o perfil manda para esse caso e ignore o ângulo. Teste final: se a mensagem coubesse "
            "igual numa conversa com outra pessoa, ou repetisse algo que ela já leu, reescreva."
)


# 22/out/2026 (incidente 9322): o follow-up que nasce de um handoff expirado
# tem de saber que a ultima fala da Julia JA prometeu retorno da equipe.  Sem
# isto o rascunho repetia pergunta que o paciente ja respondeu, prometia prazo
# de novo e, no attempt 2, usava angulo de pressao de agenda — virando
# "cobrador" pela falha da equipe (AGENTS.md regra 9).
_REGRA_POS_HANDOFF = (
    "PÓS-ENCAMINHAMENTO À EQUIPE (regra dura): a última sua nesta conversa já encaminhou "
    "o pedido para a equipe e prometeu o retorno dela; esta mensagem não é momento de vender. "
    "A pessoa está esperando a equipe, não decidindo se quer o serviço. Assim: não repita nem "
    "reformule a pergunta que ela já respondeu; não prometa prazo novo nem repita que a equipe "
    "vai retornar; não use urgência de agenda, escassez nem campanha. Se couber, ofereça só "
    "ajuda extra enquanto a equipe confirma (por exemplo, avisar se tiver mais alguma dúvida); "
    "se não houver nada útil a dizer, reconheça a espera em uma frase curta, sem nova promessa."
)


# 03/nov/2026 (incidente "Muito longe"): a terceira mensagem do ciclo disse que
# "algumas famílias decidem proteger seus filhos mesmo morando mais longe" para
# quem tinha acabado de dizer que era longe. Regra dura para todas as tentativas.
_REGRA_SEM_COMPARACAO = (
    "SEM COMPARAÇÃO NEM CULPA (regra dura): nunca compare a pessoa com terceiros, como "
    "\"outras famílias/pessoas fazem X mesmo com [obstáculo que ela citou]\", e nunca "
    "contorne um obstáculo que ela apontou (distância, preço, tempo) nem sugira descuido, "
    "medo ou culpa. Escassez de horários só se o perfil mandar, a conversa estiver avançada "
    "e for verdade. Campanha só se estiver em campanhas_vigentes; nunca escreva um mês por conta própria."
)

# Depois de objecao a conversa e' dela: reconhecer, nunca rebater.
_REGRA_POS_OBJECAO = (
    "PÓS-OBJEÇÃO (regra dura): em algum ponto desta conversa a pessoa apontou um obstáculo "
    "(distância, preço, tempo) ou adiou. Reconheça com respeito, sem rebater, sem repetir o "
    "convite e sem pressão de agenda, campanha ou comparação; se não houver nada útil e "
    "novo a oferecer, confirme em uma frase curta que a porta continua aberta."
)


def historico_tem_objecao(history: list[dict[str, str]] | None) -> bool:
    """Alguma fala do paciente no historico e' objecao ou adiamento (distancia, custo, tempo)."""
    for message in history or []:
        if message.get("role") not in ("user", "customer", "client"):
            continue
        texto = message.get("text") or ""
        if is_objection(texto) or is_deferral_or_closing(texto):
            return True
    return False


def follow_up_extra_rules(
    attempt: int, *, pos_handoff: bool = False, pos_objecao: bool = False
) -> list[str]:
    """Regras que o follow-up soma ao perfil do tenant: papel da tentativa e anti-repeticao.

    ``pos_handoff`` soma a regra dura de pós-encaminhamento (ciclo nascido de
    handoff cuja marcação de equipe expirou sem resposta — incidente 9322).
    """
    rules = [
        FOLLOWUP_RULES_BY_ATTEMPT.get(attempt, FOLLOWUP_RULE_ATTEMPT_1),
        _REGRA_ANTI_REPETICAO,
        _REGRA_SEM_COMPARACAO,
    ]
    if pos_objecao:
        rules.append(_REGRA_POS_OBJECAO)
    if pos_handoff:
        rules.append(_REGRA_POS_HANDOFF)
    return rules


def follow_up_context(
    tenant_id: str,
    phone: str,
    attempt: int,
    history: list[dict[str, str]],
    ja_enviadas: list[str],
    client_profile: dict[str, Any] | None,
    informacoes_da_empresa: str = "",
    *,
    pos_handoff: bool = False,
    data_atual: str | None = None,
    campanhas_vigentes: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Entrada JSON do rascunho do follow-up (mesma montagem em producao e na medicao).

    ``data_atual``: data de hoje e dia da semana (fuso America/Fortaleza, o mesmo
    helper do atendimento). Sem ela o follow-up citava campanha vencida e
    "outubro" em novembro (03/nov/2026).

    ``campanhas_vigentes``: itens ja formatados (``formatar_para_contexto``) das
    campanhas que valem hoje; vazio ou ``None`` = a chave nao aparece.

    ``pos_handoff``: o ciclo nasceu de um handoff cuja marcação de equipe
    expirou sem resposta — o contexto avisa o modelo e o ângulo da vez deixa
    de ser um de pressão (incidente 9322).
    """
    aberturas, fechos = zip(*(_pontas(t) for t in ja_enviadas)) if ja_enviadas else ((), ())
    pos_objecao = historico_tem_objecao(history)
    angulo_id, angulo_texto = angulo_da_vez(
        tenant_id, phone, attempt, len(ja_enviadas), pos_handoff=pos_handoff, pos_objecao=pos_objecao
    )
    context: dict[str, Any] = {
        "data_atual": data_atual or _data_atual_negocio(),
        "follow_up_attempt": attempt,
        "history": history or [],
        "mensagens_ja_enviadas": ja_enviadas,
        "aberturas_ja_usadas": list(aberturas),
        "fechos_ja_usados": list(fechos),
        "angulo_da_vez": {"id": angulo_id, "intencao": angulo_texto},
    }
    if pos_handoff:
        context["apos_encaminhamento_equipe"] = True
    if pos_objecao:
        context["apos_objecao_do_paciente"] = True
    if client_profile:
        context["client"] = client_profile
    if informacoes_da_empresa:
        context["informacoes_da_empresa"] = informacoes_da_empresa
    if campanhas_vigentes:
        context[CHAVE_CAMPANHAS] = campanhas_vigentes
    return context


def _informacoes_da_empresa(session: Session, tenant_id: str, settings: Any) -> str:
    """Catalogo do tenant (tabela knowledge) para o follow-up citar so fatos oficiais.

    22/out/2026: o follow-up nao via o catalogo, so o historico; preco, campanha,
    preparo e diferencial so apareciam se ja estivessem na conversa. Mesma regra
    do atendimento normal: estrategia ``inline`` entra inteira (cabe no teto);
    outra estrategia ou qualquer falha devolve vazio e o follow-up segue so com a
    conversa -- o catalogo nunca derruba o envio.
    """
    try:
        from app.context.knowledge import load_full_catalog
        from app.durable.tenant_registry import knowledge_strategy_with_session
        from app.reply.tools.knowledge import catalogo_inline_como_instrucao

        if knowledge_strategy_with_session(session, tenant_id=tenant_id) != "inline":
            return ""
        inline = load_full_catalog(
            session, tenant_id=tenant_id, max_chars=settings.KNOWLEDGE_INLINE_MAX_CHARS
        )
        return catalogo_inline_como_instrucao(tuple(inline)) if inline else ""
    except Exception:
        logger.warning("catalogo indisponivel no follow-up; segue so com a conversa", exc_info=True)
        return ""


def draft_follow_up_step(
    tenant_id: str,
    phone: str,
    attempt: int,
    origin_turn_id: str | None = None,
) -> ReplyDraft | None:
    """Checkpoint: the external LLM call for one follow-up message.

    15/out/2026: devolve ``None`` (nao envia) quando o modelo falha ou quando o
    rascunho repete uma mensagem ja enviada ao contato mesmo depois de uma
    segunda tentativa. Nao existe mais texto generico de reserva.

    22/out/2026 (incidente 9322, task #9190): com ``origin_turn_id``, le a
    decisao do turno de origem — se ele foi ``handoff``, o ciclo nasceu de uma
    marcacao de equipe que expirou sem resposta (o release de 16/out ja devolveu
    a conversa ao bot antes deste draft) e o rascunho recebe a regra dura de
    pos-encaminhamento, o sinal ``apos_encaminhamento_equipe`` no contexto e os
    angulos de pressao excluidos.  O sinal e recalculado aqui (nao guardado no
    lifecycle): a decisao do turno e imutavel e e a unica fonte — zero estado
    duplicado.  Parametro opcional: replays de workflows antigos (3 args) seguem
    validos, e o replay nao reexecuta steps ja gravados.

    21/set/2026: removed legacy drafting infrastructure (_draft_followup_http),
    kept the same reply agent pipeline with purpose='followup' tag.

    21/set/2026 (segunda correção, mesmo dia): restauradas as regras de
    persuasão por tentativa que a V1 tinha (removidas na primeira correção em
    favor de uma regra genérica única) -- achado revisando o histórico a
    pedido do chairman ("aquilo levou muito tempo pra ser afinado"). Ver
    FOLLOWUP_RULES_BY_ATTEMPT.
    """
    import json

    import httpx

    settings = get_settings()
    engine = get_registry_engine(settings.DATABASE_URL)

    client_profile: dict[str, Any] | None = None
    history: list[dict[str, str]] = []
    with Session(engine) as session:
        row = session.execute(
            _sql_text(
                """SELECT name, push_name, status, last_intent, tags, metadata
                   FROM genius_next.clients WHERE tenant_id=:tid AND phone=:ph"""
            ),
            {"tid": tenant_id, "ph": phone},
        ).mappings().first()
        if row:
            client_profile = {
                "name": row["name"] or row["push_name"],
                "status": row["status"],
                "last_intent": row["last_intent"],
                "tags": list(row["tags"] or []),
            }
        conversation = get_conversation_history(
            session,
            tenant_id=tenant_id,
            phone=phone,
            limit=settings.HISTORY_MAX_MESSAGES,
            max_chars=settings.HISTORY_TOTAL_MAX_CHARS,
            include_team=settings.HISTORY_INCLUDE_TEAM_MESSAGES,
        )
        if conversation.available:
            history = [{"role": msg.role, "text": msg.text} for msg in conversation.messages]
        informacoes_da_empresa = _informacoes_da_empresa(session, tenant_id, settings)
        # Nunca levanta: sem tabela ou com erro devolve lista vazia.
        campanhas = formatar_campanhas(campanhas_vigentes_hoje(session, tenant_id))
        pos_handoff = False
        if origin_turn_id:
            # A decisao do turno de origem e imutavel (turns.decision), ao
            # contrario de clients.last_intent que o release zera para 'reply'.
            origem_intent = session.execute(
                _sql_text(
                    """SELECT decision->>'intent' FROM genius_next.turns
                       WHERE id = :turn_id AND tenant_id = :tenant_id"""
                ),
                {"turn_id": origin_turn_id, "tenant_id": tenant_id},
            ).scalar_one_or_none()
            pos_handoff = origem_intent == "handoff"

    system_prompt = _build_system_prompt(
        tenant_id,
        purpose="followup",
        extra_rules=follow_up_extra_rules(
            attempt, pos_handoff=pos_handoff, pos_objecao=historico_tem_objecao(history)
        ),
    )

    ja_enviadas = [m["text"] for m in history if m.get("role") == "assistant"][-_JA_ENVIADAS_MAX:]
    context = follow_up_context(
        tenant_id,
        phone,
        attempt,
        history,
        ja_enviadas,
        client_profile,
        informacoes_da_empresa,
        pos_handoff=pos_handoff,
        campanhas_vigentes=campanhas,
    )

    def _gerar(temperatura: float, evitar: str | None) -> ReplyDraft | None:
        mensagens = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": json.dumps(context, ensure_ascii=False, separators=(",", ":")),
            },
        ]
        if evitar:
            mensagens.append(
                {
                    "role": "user",
                    "content": "Seu rascunho repetiu algo que já foi enviado: "
                    + json.dumps(evitar, ensure_ascii=False)
                    + ". Escreva uma mensagem diferente, com um ângulo novo.",
                }
            )
        try:
            http = httpx.Client(timeout=settings.REPLY_TIMEOUT_SECONDS)
            try:
                response = http.post(
                    f"{settings.OMNIROUTE_BASE_URL}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {_secret_value(settings.OMNIROUTE_API_KEY)}",
                        "Content-Type": "application/json",
                        "x-omniroute-compression": "off",
                    },
                    json={
                        "model": settings.OMNIROUTE_MODEL,
                        "temperature": temperatura,
                        "stream": False,
                        "max_tokens": settings.OUTPUT_MAX_TOKENS,
                        "response_format": {"type": "json_object"},
                        # 16/out/2026 (TRAPS #45/#61): com o raciocinio ligado o modelo gasta o
                        # max_tokens pensando e devolve finish_reason=length; o follow-up falhava
                        # em silencio (antes o texto generico de reserva escondia o erro).
                        "thinking": {"type": "disabled"},
                        "messages": mensagens,
                    },
                )
                if response.status_code != 200:
                    raise RuntimeError("follow-up draft failed")
                parsed = json.loads(response.text)
                choice = parsed.get("choices", [{}])[0]
                if choice.get("finish_reason") != "stop":
                    raise RuntimeError("follow-up draft invalid")
                return ReplyDraft.model_validate_json(choice["message"]["content"])
            finally:
                http.close()
        except Exception:
            logger.warning("follow-up draft failed; nada sera enviado", exc_info=True)
            return None

    draft = _gerar(0.8, None)
    if draft is None or draft.action != "send_text":
        return draft
    if not follow_up_repete(draft.text, ja_enviadas):
        return draft
    segundo = _gerar(0.9, draft.text)
    if segundo is None or segundo.action != "send_text":
        return segundo
    if follow_up_repete(segundo.text, ja_enviadas):
        logger.info("follow-up repetiria o que ja foi enviado; nada sera enviado (attempt=%s)", attempt)
        return None
    return segundo




def send_follow_up_v2_turn_workflow(
    tenant_id: str,
    remote_jid: str,
    phone: str,
    instance: str,
    attempt: int,
    origin_turn_id: str,
    delivery_mode: Literal["record_only", "live"] = "record_only",
) -> FollowUpAttemptReceipt:
    """One V2 attempt in the existing conversation partition.

    A queue insertion is not proof of delivery.  The parent V2 scheduler waits
    for this returned receipt, which only becomes ``confirmed`` after every
    outbox command has durably reached ``recorded`` or ``sent``.

    Repetir apos queda e seguro: recibo ja gravado devolve o mesmo resultado sem
    redigir nem enviar de novo, e o outbox absorve a mesma chave.
    """
    gravado = follow_up_v2_receipt_status_txn(tenant_id, origin_turn_id, attempt)
    if gravado is not None:
        return FollowUpAttemptReceipt(status=gravado)
    if not should_continue_follow_up_txn(tenant_id, phone, origin_turn_id):
        receipt = FollowUpAttemptReceipt(status="stopped")
        record_follow_up_v2_receipt_txn(tenant_id, origin_turn_id, attempt, receipt)
        return receipt

    draft = draft_follow_up_step(tenant_id, phone, attempt, origin_turn_id)
    if draft is None:
        # Modelo falhou ou o rascunho repetiria o que ja foi enviado: nao envia.
        receipt = FollowUpAttemptReceipt(status="stopped")
        record_follow_up_v2_receipt_txn(tenant_id, origin_turn_id, attempt, receipt)
        return receipt
    if draft.action != "send_text" or not draft.text.strip():
        receipt = FollowUpAttemptReceipt(status="review_required")
        record_follow_up_v2_receipt_txn(tenant_id, origin_turn_id, attempt, receipt)
        return receipt

    followup_turn_id = stable_id(
        "followup-v2", tenant_id, remote_jid, origin_turn_id, str(attempt)
    )
    delivery_policy = follow_up_delivery_policy_step(delivery_mode)
    texts = split_human_reply(
        draft.text,
        max_blocks=delivery_policy.max_blocks,
        target_chars=delivery_policy.target_chars,
    ) or [draft.text]
    commands = [
        OutboxCommand(
            tenant_id=tenant_id,
            turn_id=followup_turn_id,
            ordinal=ordinal,
            instance=instance,
            destination_jid=remote_jid,
            action="send_text",
            text=message,
            idempotency_key=stable_id(
                "outbox", tenant_id, followup_turn_id, ordinal, "send_text"
            ),
            delivery_mode=delivery_policy.delivery_mode,
            purpose="follow_up",
            follow_up=FollowUpDeliveryContext(phone=phone, origin_turn_id=origin_turn_id),
        )
        for ordinal, message in enumerate(texts)
    ]
    for command in commands:
        insert_outbox_command_txn(command)
    outcomes = dispatch_outbox_batch_workflow(commands)
    confirmed = bool(outcomes) and all(
        outcome is not None and outcome.status in {"recorded", "sent"}
        for outcome in outcomes
    )
    receipt = FollowUpAttemptReceipt(
        status="confirmed" if confirmed else "review_required",
        command_count=len(commands),
        delivery_mode=delivery_policy.delivery_mode,
    )
    record_follow_up_v2_receipt_txn(tenant_id, origin_turn_id, attempt, receipt)
    return receipt


# --------------------------------------------------------------------------- #
# Cadeia de jobs (substitui o workflow antigo que dormia 48 h + 2 h)
#
#   start -> wake(1) -> send(1) -> wake(2) -> send(2) -> wake(3) -> send(3) -> wake(4) -> send(4)
#                                                                                  -> close (graca)
#
# - ``wake`` acorda sem lock na hora certa, revalida e so entao pede o ``send``.
# - ``send`` leva o lock da conversa (nunca passa na frente de turno ao vivo) e, confirmado o
#   recibo, agenda o proximo ``wake``. O estado da cadeia e o recibo gravado + os argumentos do job.
# - Cada job e idempotente: ``queueing_lock`` por (origem, tentativa) e o recibo no banco.
# --------------------------------------------------------------------------- #


def _ctx(
    tenant_id: str,
    remote_jid: str,
    phone: str,
    instance: str,
    origin_turn_id: str,
    provider: str,
    target_24h: str | None,
) -> dict[str, Any]:
    return {
        "tenant_id": tenant_id,
        "remote_jid": remote_jid,
        "phone": phone,
        "instance": instance,
        "origin_turn_id": origin_turn_id,
        "provider": provider,
        "target_24h": target_24h,
    }


def _schedule_wake(ctx: dict[str, Any], attempt: int, delay: int) -> None:
    defer_once(
        follow_up_wake_job,
        queueing_lock=stable_id("fu-wake", ctx["tenant_id"], ctx["origin_turn_id"], attempt),
        schedule_in=delay,
        attempt=attempt,
        **ctx,
    )


def _wait_before(attempt: int, ctx: dict[str, Any], policy: FollowUpPolicy) -> int:
    """Segundos da tentativa anterior ate esta (mesma regra que o laco do workflow tinha)."""
    if attempt == FOLLOWUP_24H_ATTEMPT:
        target = ctx["target_24h"]
        return 0 if target is None else follow_up_wait_to_target_step(target, policy)
    wait = FOLLOWUP_V2_WAITS_SECONDS[attempt - 1]
    if attempt > FOLLOWUP_24H_ATTEMPT:
        # Depois do sono, a tentativa so sai em horario comercial.
        depois = datetime.now(ZoneInfo(policy.timezone)) + timedelta(seconds=wait)
        wait += business_opening_wait_seconds(depois, policy)
    return wait


def start_follow_up_v2(
    tenant_id: str,
    remote_jid: str,
    phone: str,
    instance: str,
    origin_turn_id: str,
    provider: str = "evolution",
) -> None:
    """Abre o ciclo: politica, lifecycle (uma vez por turno de origem) e o primeiro despertar.

    O alvo da tentativa de 1 dia e ancorado AQUI (logo apos a ultima fala do paciente), nao no
    fim da tentativa anterior, e viaja nos argumentos de cada job.
    """
    policy = load_follow_up_policy_step(tenant_id)
    if policy is None:
        return
    if not ensure_follow_up_v2_lifecycle_txn(
        tenant_id, remote_jid, phone, instance, origin_turn_id
    ):
        return
    target = (
        follow_up_24h_target_step(policy)
        if follow_up_max_attempts(provider) >= FOLLOWUP_24H_ATTEMPT
        else None
    )
    ctx = _ctx(tenant_id, remote_jid, phone, instance, origin_turn_id, provider, target)
    _schedule_wake(ctx, 1, FOLLOWUP_V2_WAITS_SECONDS[0])


@jobs.task(name="follow_up_start", queue=AUX_QUEUE, retry=RETRY_CADEIA)
def follow_up_start_job(**kwargs: Any) -> None:
    start_follow_up_v2(**kwargs)


def follow_up_wake(ctx: dict[str, Any], attempt: int) -> None:
    """Na hora da tentativa: encerra, ou revalida e pede o envio."""
    tenant_id, origin_turn_id = ctx["tenant_id"], ctx["origin_turn_id"]
    policy = load_follow_up_policy_step(tenant_id)
    if policy is None:
        return
    if attempt > follow_up_max_attempts(ctx["provider"]):
        # Janela de 24 h do provedor fechada: nao envia mais nada. Com a agenda do
        # Manager ligada, o silencio vira loss depois de MANAGER_LOSS_SILENCIO_HORAS.
        record_follow_up_v2_receipt_txn(
            tenant_id, origin_turn_id, attempt, FollowUpAttemptReceipt(status="stopped")
        )
        schedule_loss_silencio_txn(
            tenant_id, ctx["phone"], ctx["remote_jid"], ctx["instance"], ctx["provider"],
            origin_turn_id,
        )
        return
    if attempt == FOLLOWUP_24H_ATTEMPT and ctx["target_24h"] is None:
        # Nenhum instante permitido entre a ultima fala e o alvo: nao envia, nao alerta.
        record_follow_up_24h_unreachable_txn(tenant_id, origin_turn_id)
        return
    if not should_continue_follow_up(tenant_id, ctx["phone"], origin_turn_id):
        record_follow_up_v2_receipt_txn(
            tenant_id, origin_turn_id, attempt, FollowUpAttemptReceipt(status="stopped")
        )
        return
    defer_once(
        follow_up_send_job,
        lock=follow_up_partition_key(
            tenant_id, ctx["provider"], ctx["instance"], ctx["remote_jid"]
        ),
        priority=TURN_PRIORITY_FOLLOWUP,
        queueing_lock=stable_id("fu-send", tenant_id, origin_turn_id, attempt),
        attempt=attempt,
        delivery_mode=policy.delivery_mode,
        **ctx,
    )


@jobs.task(name="follow_up_wake", queue=AUX_QUEUE, retry=RETRY_CADEIA)
def follow_up_wake_job(attempt: int, **ctx: Any) -> None:
    follow_up_wake(ctx, attempt)


def follow_up_send(ctx: dict[str, Any], attempt: int, delivery_mode: str) -> None:
    """Envia a tentativa e, com recibo confirmado, agenda a proxima (ou a graca)."""
    receipt = send_follow_up_v2_turn_workflow(
        ctx["tenant_id"], ctx["remote_jid"], ctx["phone"], ctx["instance"], attempt,
        ctx["origin_turn_id"], delivery_mode,  # type: ignore[arg-type]
    )
    if receipt.status != "confirmed":
        return
    policy = load_follow_up_policy_step(ctx["tenant_id"])
    if policy is None:
        return
    if attempt >= len(FOLLOWUP_V2_WAITS_SECONDS):
        defer_once(
            follow_up_close_job,
            queueing_lock=stable_id("fu-close", ctx["tenant_id"], ctx["origin_turn_id"]),
            schedule_in=FOLLOWUP_V2_GRACE_SECONDS,
            **ctx,
        )
        return
    proxima = attempt + 1
    # Alem do limite do provedor o despertar roda na hora e fecha o ciclo.
    espera = (
        0
        if proxima > follow_up_max_attempts(ctx["provider"])
        else _wait_before(proxima, ctx, policy)
    )
    _schedule_wake(ctx, proxima, espera)


@jobs.task(name="follow_up_send", queue=TURN_QUEUE, retry=RETRY_CADEIA)
def follow_up_send_job(attempt: int, delivery_mode: str, **ctx: Any) -> None:
    follow_up_send(ctx, attempt, delivery_mode)


def follow_up_close(ctx: dict[str, Any]) -> None:
    """Fim da graca: sem resposta e sem equipe, loss atomico e entrega ao Rescue."""
    tenant_id, origin_turn_id = ctx["tenant_id"], ctx["origin_turn_id"]
    # A transacao repete esta checagem sob SERIALIZABLE: resposta ou handoff que corre na
    # fronteira da graca nunca vira loss.
    if not should_continue_follow_up(tenant_id, ctx["phone"], origin_turn_id):
        record_follow_up_v2_receipt_txn(
            tenant_id, origin_turn_id, 4, FollowUpAttemptReceipt(status="stopped")
        )
        return
    mark_loss_and_queue_for_rescue_v2_txn(tenant_id, origin_turn_id)
    # A transacao do Rescue nao faz nada sem o loss atomico acima; repetir e seguro.
    record_rescue_intake_txn(tenant_id, origin_turn_id)


@jobs.task(name="follow_up_close", queue=AUX_QUEUE, retry=RETRY_CADEIA)
def follow_up_close_job(**ctx: Any) -> None:
    follow_up_close(ctx)
