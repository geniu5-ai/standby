"""Rede de seguranca do Next sem DBOS (31/out/2026): nada fica preso porque um worker morreu.

O DBOS recuperava workflow ``PENDING`` no boot. Aqui o que importa vive em tabelas, entao dois
varredores a cada minuto olham o banco e re-agendam o que ficou no meio:

- ``despacha_outbox_pendente``: linha do outbox ``pending`` (ou ``claimed`` com lease vencido) e
  sem ``provider_message_id`` ha mais de ``OUTBOX_PENDENTE_APOS_S``. O despacho e idempotente
  (lease + chave unica), entao repetir nunca duplica envio.
- ``retoma_turnos_presos``: turno ``claimed``/``processing`` ha mais de ``TURNO_PRESO_APOS_S`` sem
  nenhum job da conversa vivo. Antes ele so era retomado quando o paciente escrevia de novo.

- ``retoma_followups_orfaos`` (06/nov/2026): ciclo de follow-up ``scheduled`` sem nenhum job vivo
  (o ``follow_up_send`` esgotou o ``RETRY_CADEIA``, o job sumiu ou o worker caiu no meio). Re-agenda o
  proximo despertar a partir do ultimo recibo ``confirmed``; a regra de parar, a janela e o recibo
  continuam valendo no despertar, entao repetir nunca duplica envio.

Todos so agendam; quem faz o trabalho e o mesmo codigo do caminho normal.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.contracts import EvolutionWebhook, OutboxCommand, TurnRelease
from app.db import get_engine
from app.identity import build_trusted_ingress
from app.identity_primitives import stable_id
from app.jobs import AUX_QUEUE, CRON_QUEUE, RETRY_CADEIA, defer_once, jobs
from app.settings import get_settings

logger = logging.getLogger(__name__)

OUTBOX_PENDENTE_APOS_S = 120
TURNO_PRESO_APOS_S = 180
LIMITE_POR_VARREDURA = 50
FOLLOWUP_ORFAO_APOS_S = 900  # ciclo scheduled sem job vivo ha 15 min: ninguem vai acordar
FOLLOWUP_ORFAO_ATE_S = 3 * 24 * 3600  # nao ressuscita ciclo com mais de 3 dias

_OUTBOX_PENDENTE_SQL = text(
    """
    /* reaper.outbox_pendente */
    SELECT tenant_id, turn_id, ordinal, instance, destination_jid, action, kind, idempotency_key,
           delivery_mode, payload
    FROM genius_next.outbox
    WHERE provider_message_id IS NULL
      AND (
        (delivery_status = 'pending' AND created_at < now() - make_interval(secs => :apos))
        OR (delivery_status = 'claimed' AND lease_expires_at < now())
      )
    ORDER BY created_at, ordinal
    LIMIT :limite
    """
)

_TURNOS_PRESOS_SQL = text(
    """
    /* reaper.turnos_presos */
    SELECT t.id AS turn_id, t.tenant_id, t.provider, t.instance, t.remote_jid, t.last_sequence,
           (SELECT e.raw_payload FROM genius_next.events AS e
             WHERE e.claimed_turn_id = t.id ORDER BY e.conversation_sequence DESC LIMIT 1) AS payload
    FROM genius_next.turns AS t
    WHERE t.status IN ('claimed', 'processing')
      AND t.claimed_at < now() - make_interval(secs => :apos)
    ORDER BY t.claimed_at
    LIMIT :limite
    """
)

_FOLLOWUP_ORFAOS_SQL = text(
    """
    /* reaper.followup_orfaos */
    SELECT l.tenant_id::text AS tenant_id, l.origin_turn_id,
           (SELECT j.args FROM procrastinate_jobs j
             WHERE j.task_name IN ('follow_up_wake', 'follow_up_send')
               AND j.args ? 'target_24h'
               AND j.args->>'tenant_id' = l.tenant_id::text
               AND j.args->>'origin_turn_id' = l.origin_turn_id
             ORDER BY j.id DESC LIMIT 1) AS args,
           COALESCE((SELECT max(r.attempt) FROM genius_next.follow_up_attempt_receipts r
                      WHERE r.tenant_id = l.tenant_id AND r.origin_turn_id = l.origin_turn_id
                        AND r.status = 'confirmed'), 0) AS ultimo_confirmado
    FROM genius_next.follow_up_lifecycles l
    WHERE l.state = 'scheduled'
      AND l.updated_at < now() - make_interval(secs => :apos)
      AND l.updated_at > now() - make_interval(secs => :ate)
      AND NOT EXISTS (
            SELECT 1 FROM procrastinate_jobs j
             WHERE j.task_name IN ('follow_up_start', 'follow_up_wake', 'follow_up_send', 'follow_up_close')
               AND j.status IN ('todo', 'doing')
               AND j.args->>'tenant_id' = l.tenant_id::text
               AND j.args->>'origin_turn_id' = l.origin_turn_id)
    ORDER BY l.updated_at
    LIMIT :limite
    """
)

_FOLLOWUP_TOCA_SQL = text(
    """
    /* reaper.followup_toca */
    UPDATE genius_next.follow_up_lifecycles SET updated_at = now()
    WHERE tenant_id::text = :tenant_id AND origin_turn_id = :origin_turn_id AND state = 'scheduled'
    """
)

_JOB_VIVO_DA_CONVERSA_SQL = text(
    """
    /* reaper.job_vivo */
    SELECT 1 FROM procrastinate_jobs
    WHERE lock = :conversa AND status IN ('todo', 'doing')
    LIMIT 1
    """
)


def command_from_row(row: Any) -> OutboxCommand:
    """Remonta o comando de envio a partir da linha do outbox (o payload guarda o resto)."""
    payload = row["payload"] or {}
    return OutboxCommand(
        tenant_id=row["tenant_id"],
        turn_id=row["turn_id"],
        ordinal=row["ordinal"],
        instance=row["instance"],
        destination_jid=row["destination_jid"],
        action=row["action"],
        kind=row["kind"],
        text=payload.get("text") or " ",
        media_base64=payload.get("media_base64"),
        mime_type=payload.get("mime_type"),
        idempotency_key=row["idempotency_key"],
        delivery_mode=row["delivery_mode"],
        purpose=payload.get("purpose", "reply"),
        follow_up=payload.get("follow_up"),
        template=payload.get("template"),
    )


def release_from_row(row: Any) -> TurnRelease:
    """Remonta o ``TurnRelease`` de um turno preso a partir do ultimo evento dele."""
    event = build_trusted_ingress(
        EvolutionWebhook.model_validate(row["payload"]),
        tenant_id=row["tenant_id"],
        provider=row["provider"],
    )
    return TurnRelease(event=event, through_sequence=int(row["last_sequence"]))


def _rows(sql: Any, **params: Any) -> list[Any]:
    with Session(get_engine(get_settings().DATABASE_URL)) as session:
        return list(session.execute(sql, params).mappings().all())


def _rows_exec(sql: Any, **params: Any) -> None:
    with Session(get_engine(get_settings().DATABASE_URL)) as session:
        session.execute(sql, params)
        session.commit()


def _conversa_tem_job_vivo(conversation_id: str) -> bool:
    return bool(_rows(_JOB_VIVO_DA_CONVERSA_SQL, conversa=conversation_id))


@jobs.task(name="outbox_dispatch", queue=AUX_QUEUE, retry=RETRY_CADEIA)
def outbox_dispatch_job(command: dict[str, Any]) -> None:
    from app.durable.outbox import dispatch_outbox_workflow

    dispatch_outbox_workflow(OutboxCommand.model_validate(command))


def despacha_outbox_pendente() -> int:
    """Agenda o despacho das linhas esquecidas. Mesmo turno = mesmo lock: a ordem dos blocos vale."""
    agendados = 0
    for row in _rows(
        _OUTBOX_PENDENTE_SQL, apos=OUTBOX_PENDENTE_APOS_S, limite=LIMITE_POR_VARREDURA
    ):
        try:
            command = command_from_row(row)
        except Exception:
            logger.warning("outbox pendente invalido: %s", row["idempotency_key"], exc_info=True)
            continue
        if defer_once(
            outbox_dispatch_job,
            lock=stable_id("outbox-turn", command.tenant_id, command.turn_id),
            queueing_lock=command.idempotency_key,
            command=command.model_dump(mode="json"),
        ):
            agendados += 1
            logger.warning("outbox pendente re-agendado: turno %s ordinal %s", command.turn_id, command.ordinal)
    return agendados


def retoma_turnos_presos() -> int:
    """Re-agenda o turno de quem ficou sem resposta porque o worker morreu no meio."""
    from app.durable.flow import _defer_turn

    retomados = 0
    for row in _rows(_TURNOS_PRESOS_SQL, apos=TURNO_PRESO_APOS_S, limite=LIMITE_POR_VARREDURA):
        try:
            release = release_from_row(row)
        except Exception:
            logger.warning("turno preso nao remontado: %s", row["turn_id"], exc_info=True)
            continue
        if _conversa_tem_job_vivo(release.event.conversation_id):
            continue
        if _defer_turn(release):
            retomados += 1
            logger.warning("turno preso retomado: %s", row["turn_id"])
    return retomados


def retoma_followups_orfaos() -> int:
    """Re-agenda o proximo despertar de ciclo de follow-up que ficou sem nenhum job vivo.

    O proximo passo sai do ultimo recibo ``confirmed``: sem recibo, a tentativa 1; com as 4
    confirmadas, o fechamento do ciclo. O despertar revalida tudo (paciente respondeu, equipe
    assumiu, janela do provedor) antes de pedir envio, e o recibo impede mandar duas vezes.
    """
    from app.durable.followup import (
        FOLLOWUP_V2_WAITS_SECONDS,
        _schedule_wake,
        follow_up_close_job,
    )

    retomados = 0
    for row in _rows(
        _FOLLOWUP_ORFAOS_SQL,
        apos=FOLLOWUP_ORFAO_APOS_S,
        ate=FOLLOWUP_ORFAO_ATE_S,
        limite=LIMITE_POR_VARREDURA,
    ):
        args = row["args"]
        if not isinstance(args, dict):
            logger.warning("follow-up orfao sem contexto de job: %s", row["origin_turn_id"])
            continue
        ctx = {k: v for k, v in args.items() if k not in ("attempt", "delivery_mode")}
        ultimo = int(row["ultimo_confirmado"] or 0)
        try:
            if ultimo >= len(FOLLOWUP_V2_WAITS_SECONDS):
                defer_once(
                    follow_up_close_job,
                    queueing_lock=stable_id("fu-close", row["tenant_id"], row["origin_turn_id"]),
                    **ctx,
                )
            else:
                _schedule_wake(ctx, ultimo + 1, 0)
            _rows_exec(_FOLLOWUP_TOCA_SQL, tenant_id=row["tenant_id"], origin_turn_id=row["origin_turn_id"])
        except Exception:
            logger.warning("follow-up orfao nao retomado: %s", row["origin_turn_id"], exc_info=True)
            continue
        retomados += 1
        logger.warning("follow-up orfao retomado: %s (proxima tentativa %s)", row["origin_turn_id"], ultimo + 1)
    return retomados


@jobs.periodic(cron="*/30 * * * *")  # Neon free: a cada 30 min (Next: a cada minuto)
@jobs.task(name="reaper", queue=CRON_QUEUE)
def reaper(timestamp: int) -> None:
    for varredura in (despacha_outbox_pendente, retoma_turnos_presos, retoma_followups_orfaos):
        try:
            varredura()
        except Exception:
            logger.exception("reaper: %s falhou", varredura.__name__)
