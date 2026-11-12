"""Tenant-safe durable event, turn, and outbox persistence."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from app.contracts import (
    TURN_ID_PATTERN,
    AgentDecision,
    ConversationControlApplyResult,
    ConversationControlRequest,
    EvolutionWebhook,
    FollowUpDeliveryContext,
    OutboxCommand,
    TemplateCommand,
    TrustedIngress,
)
from app.db import sql_session, transaction
from app.durable.tenant_registry import manager_jid_with_session
from app.identity import normalize_phone, stable_id
from app.identity_primitives import direct_jids_equivalent
from app.security.guard import GuardAssessment
from app.settings import get_settings

logger = logging.getLogger(__name__)


class EventIdentityConflict(RuntimeError):
    """One tenant-scoped provider message ID was reused with different content."""


class TurnStateConflict(RuntimeError):
    """The claimed turn is absent, stale, or belongs to another identity."""


class ClientStateMissing(RuntimeError):
    """The trusted tenant/client row required for a decision does not exist."""


class OutboxWriteFailed(RuntimeError):
    """An outbox insert returned no durable row."""


class OutboxStateConflict(RuntimeError):
    """An outbox lease or terminal transition did not match durable state."""


class OutboundEventPersistenceFailed(RuntimeError):
    """A sent customer reply could not be represented in the event log."""


class ConversationControlConflict(RuntimeError):
    """One tenant-scoped control ID was reused with different request bytes."""


class PersistEventResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    inserted: bool
    conversation_id: str = Field(pattern=TURN_ID_PATTERN)
    sequence: int = Field(ge=1)
    is_new_client: bool = False


class ClaimedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_row_id: int = Field(ge=1)
    message_id: str = Field(min_length=1)
    # A sequencia da conversa pode ser reconstruida depois de uma recuperacao
    # de banco. O hash original distingue eventos novos que voltaram ao mesmo
    # numero de sequencia de um turno historico.
    event_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    sequence: int = Field(ge=1)
    message_type: str = Field(min_length=1)
    direction: Literal["inbound", "outbound"]
    text_content: str | None = None
    sanitized_payload: EvolutionWebhook


class ClaimedTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    turn_id: str = Field(pattern=TURN_ID_PATTERN)
    conversation_id: str = Field(pattern=TURN_ID_PATTERN)
    tenant_id: str
    provider: str
    instance: str
    remote_jid: str
    first_sequence: int = Field(ge=1)
    last_sequence: int = Field(ge=1)
    event_count: int = Field(ge=1, le=100)
    events: list[ClaimedEvent] = Field(min_length=1, max_length=100)


class ClaimTurnResult(BaseModel):
    """One finite claimed batch plus a bounded continuation signal."""

    model_config = ConfigDict(extra="forbid")

    turn: ClaimedTurn | None
    has_more: bool

    @model_validator(mode="after")
    def continuation_requires_a_claimed_turn(self) -> Self:
        if self.turn is None and self.has_more:
            raise ValueError("has_more requires a claimed turn")
        return self


class ClaimedOutbox(BaseModel):
    """One leased, validated outbound command loaded from domain storage."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    outbox_id: int = Field(ge=1)
    tenant_id: str = Field(min_length=1, max_length=128)
    turn_id: str = Field(pattern=TURN_ID_PATTERN)
    ordinal: int = Field(ge=0, le=99)
    instance: str = Field(min_length=1, max_length=128)
    destination_jid: str = Field(min_length=1, max_length=512)
    action: Literal["send_text", "send_media", "handoff", "send_template"]
    kind: Literal["customer", "team"] = "customer"
    text: str = Field(min_length=1, max_length=1_024)
    media_base64: str | None = Field(default=None, max_length=2_000_000)
    mime_type: str | None = Field(default=None, max_length=64)
    idempotency_key: str = Field(pattern=TURN_ID_PATTERN)
    delivery_mode: Literal["record_only", "live"]
    purpose: Literal["reply", "follow_up", "handoff"] = "reply"
    follow_up: FollowUpDeliveryContext | None = None
    # Só o action ``send_template`` carrega isto (lido do payload JSONB).
    template: TemplateCommand | None = None
    # Provider da rota (tenant_registry), resolvido no claim; nao faz parte do
    # comando canonico. Sem linha no registry cai em "evolution" (comportamento
    # historico).
    provider: str = Field(default="evolution", min_length=1, max_length=64)
    lease_owner: str | None = Field(default=None, max_length=512)
    attempts: int = Field(ge=1)

    @model_validator(mode="after")
    def follow_up_context_matches_purpose(self) -> Self:
        if self.purpose == "follow_up" and self.follow_up is None:
            raise ValueError("follow-up claimed outbox requires origin context")
        if self.purpose != "follow_up" and self.follow_up is not None:
            raise ValueError("normal claimed outbox must not carry follow-up context")
        return self


class OutboxFinalization(BaseModel):
    """Sanitized terminal outcome; raw provider responses never enter the DB."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    status: Literal["recorded", "sent", "failed", "uncertain"]
    provider_message_id: str | None = Field(default=None, max_length=512)
    provider_result: dict[str, str | int | bool | None] = Field(default_factory=dict)

    @model_validator(mode="after")
    def sent_requires_provider_identity(self) -> Self:
        if self.status == "sent" and not self.provider_message_id:
            raise ValueError("sent delivery requires provider message ID")
        if self.status in {"recorded", "failed"} and self.provider_message_id is not None:
            raise ValueError(
                "recorded or failed delivery cannot store a provider message ID"
            )
        return self


class SecurityEventRecord(BaseModel):
    """One payload-free guard audit row tied back to existing durable event IDs."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    tenant_id: str = Field(min_length=1, max_length=128)
    phone: str = Field(min_length=5, max_length=64)
    conversation_id: str = Field(pattern=TURN_ID_PATTERN)
    turn_id: str = Field(pattern=TURN_ID_PATTERN)
    event_id: int = Field(ge=1)
    stage: Literal["input", "output"]
    scanner: str = Field(min_length=1, max_length=128)
    scanner_version: str | None = Field(default=None, max_length=64)
    decision: Literal["allow", "block", "unavailable"]
    score: float = Field(default=0.0, ge=0.0, le=1.0)
    reason_code: str = Field(min_length=1, max_length=128)


_EVENT_LOOKUP_SQL = text(
    """
    /* event.lookup */
    SELECT
        event_hash,
        conversation_sequence,
        direction,
        event_type,
        ingest_workflow_id,
        instance,
        message_type,
        remote_jid,
        text_content
    FROM genius_next.events
    WHERE tenant_id = :tenant_id
      AND provider = :provider
      AND message_id = :message_id
    """
)

_CONVERSATION_ENSURE_SQL = text(
    """
    /* conversation.ensure */
    INSERT INTO genius_next.conversations (
        tenant_id, provider, instance, remote_jid
    ) VALUES (
        :tenant_id, :provider, :instance, :remote_jid
    )
    ON CONFLICT (tenant_id, provider, instance, remote_jid) DO NOTHING
    """
)

_CONVERSATION_LOCK_SQL = text(
    """
    /* conversation.lock */
    SELECT next_sequence
    FROM genius_next.conversations
    WHERE tenant_id = :tenant_id
      AND provider = :provider
      AND instance = :instance
      AND remote_jid = :remote_jid
    FOR UPDATE
    """
)

_CONVERSATION_ADVANCE_SQL = text(
    """
    /* conversation.advance */
    UPDATE genius_next.conversations
    SET next_sequence = :next_sequence,
        updated_at = now()
    WHERE tenant_id = :tenant_id
      AND provider = :provider
      AND instance = :instance
      AND remote_jid = :remote_jid
    """
)

_EVENT_INSERT_SQL = text(
    """
    /* event.insert */
    INSERT INTO genius_next.events (
        message_id, tenant_id, phone, instance, message_type, direction,
        text_content, push_name, raw_payload, provider, remote_jid,
        event_type, event_hash, conversation_sequence, ingest_workflow_id
    ) VALUES (
        :message_id, :tenant_id, :phone, :instance, :message_type, :direction,
        :text_content, :push_name, :sanitized_payload, :provider, :remote_jid,
        :event_type, :event_hash, :conversation_sequence, :ingest_workflow_id
    )
    """
).bindparams(bindparam("sanitized_payload", type_=JSONB))

_OUTBOUND_EVENT_INSERT_SQL = text(
    """
    /* event.insert.outbound */
    INSERT INTO genius_next.events (
        message_id, tenant_id, phone, instance, message_type, direction,
        text_content, push_name, raw_payload, provider, remote_jid,
        event_type, event_hash, conversation_sequence, ingest_workflow_id
    ) VALUES (
        :message_id, :tenant_id, :phone, :instance, :message_type, :direction,
        :text_content, :push_name, :sanitized_payload, :provider, :remote_jid,
        :event_type, :event_hash, :conversation_sequence, :ingest_workflow_id
    )
    ON CONFLICT (tenant_id, provider, message_id) DO NOTHING
    """
).bindparams(bindparam("sanitized_payload", type_=JSONB))

_CLIENT_UPSERT_SQL = text(
    """
    /* client.upsert */
    INSERT INTO genius_next.clients (
        tenant_id, phone, instance, push_name, last_seen
    ) VALUES (
        :tenant_id, :phone, :instance, :push_name, now()
    )
    ON CONFLICT (tenant_id, phone) DO UPDATE SET
        instance = EXCLUDED.instance,
        push_name = COALESCE(EXCLUDED.push_name, genius_next.clients.push_name),
        last_seen = EXCLUDED.last_seen,
        updated_at = now()
    RETURNING xmax
    """
)

_CLIENT_HAS_SUMMARY_SQL = text(
    """
    /* client.has_summary */
    SELECT metadata->>'summary' AS summary
    FROM genius_next.clients
    WHERE tenant_id = :tenant_id
      AND phone = :phone
    """
)

_CLIENT_RECORD_SUMMARY_SQL = text(
    """
    /* client.summary_record */
    UPDATE genius_next.clients
    SET summary_attempted_at = now(),
        metadata = COALESCE(metadata, '{}'::jsonb) || CASE
            WHEN CAST(:summary AS text) IS NULL
              OR btrim(CAST(:summary AS text)) = '' THEN '{}'::jsonb
            ELSE jsonb_build_object('summary', CAST(:summary AS text))
        END
    WHERE tenant_id = :tenant_id
      AND phone = :phone
    RETURNING phone
    """
)

_CLAIMABLE_EVENTS_SQL = text(
    """
    /* event.claimable */
    SELECT
        id AS event_row_id,
        message_id,
        event_hash,
        conversation_sequence AS sequence,
        message_type,
        direction,
        text_content,
        raw_payload AS sanitized_payload
    FROM genius_next.events
    WHERE tenant_id = :tenant_id
      AND provider = :provider
      AND instance = :instance
      AND remote_jid = :remote_jid
      AND direction = 'inbound'
      AND claimed_turn_id IS NULL
      AND conversation_sequence <= :through_sequence
    ORDER BY conversation_sequence
    LIMIT :fetch_limit
    FOR UPDATE
    """
)

# Turno que um job anterior reivindicou e nao concluiu (deploy, kill -9). Como so ha um job de
# turno por conversa (lock), qualquer turno ainda claimed/processing e orfao e deve ser retomado:
# sem isto o paciente ficaria sem resposta e o turno preso para sempre.
_INFLIGHT_TURN_SQL = text(
    """
    /* turn.inflight */
    SELECT id AS turn_id, first_sequence, last_sequence, event_count
    FROM genius_next.turns
    WHERE tenant_id = :tenant_id
      AND provider = :provider
      AND instance = :instance
      AND remote_jid = :remote_jid
      AND status IN ('claimed', 'processing')
    ORDER BY first_sequence
    LIMIT 1
    """
)

_TURN_EVENTS_SQL = text(
    """
    /* turn.events */
    SELECT
        id AS event_row_id,
        message_id,
        event_hash,
        conversation_sequence AS sequence,
        message_type,
        direction,
        text_content,
        raw_payload AS sanitized_payload
    FROM genius_next.events
    WHERE claimed_turn_id = :turn_id
    ORDER BY conversation_sequence
    """
)

_TURN_INSERT_SQL = text(
    """
    /* turn.insert */
    INSERT INTO genius_next.turns (
        id, tenant_id, provider, instance, remote_jid,
        first_sequence, last_sequence, event_count, status
    ) VALUES (
        :turn_id, :tenant_id, :provider, :instance, :remote_jid,
        :first_sequence, :last_sequence, :event_count, 'claimed'
    )
    ON CONFLICT (id) DO NOTHING
    """
)

_EVENT_MARK_CLAIMED_SQL = text(
    """
    /* event.mark_claimed */
    UPDATE genius_next.events
    SET claimed_turn_id = :turn_id
    WHERE id IN :event_ids
      AND claimed_turn_id IS NULL
    """
).bindparams(bindparam("event_ids", expanding=True))

_TURN_COMPLETE_SQL = text(
    """
    /* turn.complete */
    UPDATE genius_next.turns
    SET status = 'completed',
        decision = :decision,
        completed_at = now(),
        error = NULL
    WHERE id = :turn_id
      AND tenant_id = :tenant_id
      AND provider = :provider
      AND instance = :instance
      AND remote_jid = :remote_jid
      AND first_sequence = :first_sequence
      AND last_sequence = :last_sequence
      AND event_count = :event_count
      AND status IN ('claimed', 'processing')
    RETURNING id
    """
).bindparams(bindparam("decision", type_=JSONB))

_CLIENT_APPLY_DECISION_SQL = text(
    """
    /* client.apply_decision */
    UPDATE genius_next.clients
    SET status = CASE
            -- block e absorvente: nao e cliente (ataque/spam via guard), so
            -- acao humana explicita (control API) muda. win/loss SAO reais
            -- desfechos de cliente e podem transicionar entre si com
            -- evidencia nova (cliente muda de ideia) -- mas nunca voltam a
            -- 'open' por ruido do classificador best-effort.
            WHEN status = 'block' THEN status
            WHEN status IN ('win', 'loss') AND :customer_status = 'open' THEN status
            ELSE :customer_status
        END,
        automation_state = CASE
            WHEN :needs_human THEN 'crew'
            -- 27/ago/2026: achado por revisao externa (Kimi K3), verificado.
            -- Sem esta linha, um cliente em atendimento humano (crew)
            -- cujo turno seguinte o regex classifica como loss (falso
            -- positivo comum: "nao tenho interesse NO PLANO X" e recusa de
            -- uma oferta, nao do atendimento) perdia a posse pro
            -- classificador silenciosamente -- a conversa some da fila do
            -- operador sem ninguem decidir isso.  crew nunca sai por ruido
            -- de regra: so por acao humana (control API) ou pelo prazo de
            -- inatividade da equipe (_CREW_IN_AUTO_RELEASE_SQL).
            WHEN automation_state = 'crew' THEN automation_state
            WHEN (
                CASE
                    WHEN status = 'block' THEN status
                    WHEN status IN ('win', 'loss') AND :customer_status = 'open' THEN status
                    ELSE :customer_status
                END
            ) IN ('loss', 'block') THEN 'loss'
            ELSE automation_state
        END,
        -- 18/set/2026: a coluna booleana e a bandeira documentada de
        -- "equipe acionada" (docs/core/04-MODULOS-CREW-STATUS.md) e o
        -- control API a mantem em lockstep com automation_state='crew'.
        -- crew_in_at e o prazo da marcacao: NULL = marcacao sem prazo (so sai
        -- por clique humano: release/finalize); preenchido = o sweep a devolve
        -- ao bot 4min30 depois do ultimo ato da equipe (_CREW_IN_AUTO_RELEASE_SQL).
        -- 08/out/2026 (ordem do chairman): o handoff automatico ganha prazo
        -- quando a decisao diz que a marcacao expira (AgentDecision.crew_expires:
        -- tenant em HANDOFF_AUTO_RELEASE_TENANTS e causa fora da rota fixa de
        -- seguranca).  Rota fixa (emergencia, medicamento) e tenants fora da
        -- lista seguem como antes: crew_in_at NULL, fica com a equipe.
        crew = CASE WHEN :needs_human THEN true ELSE crew END,
        crew_in_at = CASE WHEN :needs_human AND :crew_expires THEN now()
                          WHEN :needs_human THEN NULL
                          ELSE crew_in_at END,
        last_intent = :last_intent,
        metadata = COALESCE(metadata, '{}'::jsonb) || :memory_updates,
        updated_at = now()
    WHERE tenant_id = :tenant_id
      AND phone = :phone
    RETURNING phone
    """
).bindparams(bindparam("memory_updates", type_=JSONB))

_CONTROL_LOOKUP_SQL = text(
    """
    /* control.lookup */
    SELECT request_hash
    FROM genius_next.conversation_control_events
    WHERE tenant_id = :tenant_id AND control_id = :control_id
    """
)

_CONTROL_INSERT_SQL = text(
    """
    /* control.insert */
    INSERT INTO genius_next.conversation_control_events
        (tenant_id, control_id, phone, actor_id, action, terminal_status, request_hash, request)
    VALUES
        (:tenant_id, :control_id, :phone, :actor_id, :action, :terminal_status, :request_hash, :request)
    ON CONFLICT (tenant_id, control_id) DO NOTHING
    RETURNING control_id
    """
).bindparams(bindparam("request", type_=JSONB))

_CONTROL_APPLY_CLIENT_SQL = text(
    """
    /* control.apply_client */
    UPDATE genius_next.clients
    SET automation_state = CASE WHEN :action = 'crew_in' THEN 'crew'
                                WHEN :action IN ('release', 'reopen') THEN 'open'
                                ELSE 'loss' END,
        -- 18/set/2026: finalize tambem limpa a posse.  Antes ele caia no
        -- ELSE e preservava crew=true/crew_in_at, entao o sweep de
        -- auto-release reabria para o bot uma conversa ja finalizada
        -- (loss/block) assim que o timer estourasse.
        crew = CASE WHEN :action = 'crew_in' THEN true ELSE false END,
        crew_in_at = CASE WHEN :action = 'crew_in' THEN now() ELSE NULL END,
        status = CASE WHEN :action = 'finalize' THEN :terminal_status ELSE status END,
        last_intent = CASE WHEN :action = 'crew_in' THEN 'handoff'
                           WHEN :action IN ('release', 'reopen') THEN 'reply'
                           ELSE 'loss' END,
        updated_at = now()
    WHERE tenant_id = :tenant_id AND phone = :phone
    RETURNING phone, automation_state, instance
    """
)

_CONTROL_LATEST_REMOTE_JID_SQL = text(
    """
    /* control.latest_remote_jid */
    SELECT remote_jid
    FROM genius_next.events
    WHERE tenant_id = :tenant_id AND phone = :phone
    ORDER BY received_at DESC
    LIMIT 1
    """
)

_CREW_IN_TEAM_ACTIVE_SQL = text(
    """
    /* control.crew_in_team_active */
    SELECT (
        -- 1) Ja saiu aviso de Assumir para este contato na janela (falha
        --    terminal nao conta: o paciente nunca recebeu).
        EXISTS (
            SELECT 1
            FROM genius_next.outbox o
            WHERE o.tenant_id = :tenant_id
              AND o.destination_jid IN (
                    SELECT ev.remote_jid
                    FROM genius_next.events ev
                    WHERE ev.tenant_id = :tenant_id AND ev.phone = :phone
              )
              AND starts_with(o.turn_id, 'crew-in-disclosure:')
              AND o.delivery_status <> 'failed'
              AND o.created_at > now() - make_interval(mins => :minutes)
        )
        -- 2) Envio humano pelo app (POST /v1/webapp/send, tenants Zernio).
        OR EXISTS (
            SELECT 1
            FROM genius_next.webapp_sends w
            WHERE w.tenant_id = :tenant_id
              AND w.phone = :phone
              AND w.status <> 'failed'
              AND w.created_at > now() - make_interval(mins => :minutes)
        )
        -- 3) Mensagem de saida que nao veio do bot: digitada no celular /
        --    inbox do provedor (chega pelo webhook, ingest_workflow_id
        --    'ingest:...') ou enviada pelo app (message_id em webapp_sends).
        --    Toda saida do bot tem linha no outbox com o mesmo
        --    provider_message_id; essa nunca conta como humana.
        OR EXISTS (
            SELECT 1
            FROM genius_next.events e
            WHERE e.tenant_id = :tenant_id
              AND e.phone = :phone
              AND e.direction = 'outbound'
              AND e.received_at > now() - make_interval(mins => :minutes)
              AND (
                    NOT starts_with(e.ingest_workflow_id, 'outbound:')
                 OR EXISTS (
                        SELECT 1 FROM genius_next.webapp_sends w2
                        WHERE w2.tenant_id = e.tenant_id
                          AND w2.message_id = e.message_id
                    )
              )
              AND NOT EXISTS (
                    SELECT 1 FROM genius_next.outbox o2
                    WHERE o2.tenant_id = e.tenant_id
                      AND o2.provider_message_id = e.message_id
              )
        )
    ) AS team_active
    """
)

# Mensagem humana da equipe para o contato: evento ``outbound`` (alias ``e``)
# que NAO saiu do nosso outbox -- a mesma regra de ``sent_by='human_manual'``
# em app/agent/memory.py.  Cobre o funcionario escrevendo pelo WhatsApp oficial
# (fromMe: Zernio ``message.sent`` fora da API, Evolution ``send.message``) e o
# envio pelo App (POST /v1/webapp/send, gravado por
# ``record_manual_outbound_event_with_session``).  Resposta da IA, follow-up e
# aviso de Assumir saem do outbox e nunca contam.  Usado pelo sweep (renova a
# marcacao) e pelo follow-up (humano escreveu depois da resposta de origem).
HUMAN_TEAM_OUTBOUND_PREDICATE = """
          e.direction = 'outbound'
          AND NOT EXISTS (
              SELECT 1
              FROM genius_next.outbox AS o
              WHERE o.tenant_id = e.tenant_id
                AND o.provider_message_id IS NOT NULL
                AND o.provider_message_id = e.message_id
          )
"""

# Paciente esperando resposta: a ultima mensagem do paciente (o inbound mais
# recente do contato) nao foi respondida por ninguem -- o turno dela foi parado
# pela marcacao ('suppressed_by_control'), ainda nao terminou ou falhou -- e
# nenhum humano da equipe escreveu depois dela.  Revisao de 08/out/2026: sem
# esta regra a pergunta feita durante a marcacao ficava sem resposta de
# ninguem (o turno ja tinha sido descartado, o sweep devolvia a conversa e o
# follow-up parava porque o paciente tinha respondido), e a conversa sumia da
# fila Humano do App.  Alias ``c`` = genius_next.clients.
_PATIENT_WAITING_PREDICATE = f"""
      EXISTS (
          SELECT 1
          FROM (
              SELECT p.received_at, p.claimed_turn_id
              FROM genius_next.events AS p
              WHERE p.tenant_id = c.tenant_id
                AND p.phone = c.phone
                AND p.direction = 'inbound'
              ORDER BY p.received_at DESC, p.id DESC
              LIMIT 1
          ) AS last_in
          LEFT JOIN genius_next.turns AS t
            ON t.id = last_in.claimed_turn_id AND t.tenant_id = c.tenant_id
          WHERE (
                t.id IS NULL
                OR t.status <> 'completed'
                OR t.decision ->> 'action' = 'suppressed_by_control'
            )
            AND NOT EXISTS (
                SELECT 1
                FROM genius_next.events AS e
                WHERE e.tenant_id = c.tenant_id
                  AND e.phone = c.phone
                  AND e.received_at > last_in.received_at
                  AND {HUMAN_TEAM_OUTBOUND_PREDICATE}
            )
      )
"""

# 08/out/2026 (ordem do chairman): a marcacao de dono humano
# (automation_state='crew'; "Humano" no App, "equipe" para o chairman; o nome
# antigo human_owned morreu na migration 032 e o CHECK do banco so aceita
# open/crew/loss) volta sozinha quando TODAS valem (alias ``c`` = clients):
#   1. tem prazo: crew_in_at preenchido.  NULL = marcacao sem prazo, que so sai
#      por clique humano: rota fixa de seguranca (emergencia, medicamento),
#      handoff automatico de tenant fora de HANDOFF_AUTO_RELEASE_TENANTS e as
#      linhas anteriores a esta lei (nenhuma volta sozinha no 1o tick).
#      Excecao herdada do sweep anterior: crew=false com crew_in_at NULL e o
#      estado inconsistente do TRAPS #9 e continua sendo devolvido;
#   2. o inicio da marcacao (crew_in_at) tem :window_seconds (270 s) ou mais;
#   3. nenhuma mensagem humana da equipe para o contato nesse intervalo (renova);
#   4. nenhum envio pelo App (webapp_sends) nesse intervalo: cobre o instante
#      entre o clique em enviar e o registro do evento outbound;
#   5. o paciente nao esta esperando resposta (_PATIENT_WAITING_PREDICATE): com
#      pergunta pendente a conversa fica na fila Humano ate alguem responder.
_CREW_RELEASE_ELIGIBLE = f"""
      c.automation_state = 'crew'
      AND (
          (
              c.crew_in_at IS NOT NULL
              AND c.crew_in_at <= now() - (:window_seconds * interval '1 second')
          )
          OR (c.crew = false AND c.crew_in_at IS NULL)
      )
      AND NOT EXISTS (
          SELECT 1
          FROM genius_next.events AS e
          WHERE e.tenant_id = c.tenant_id
            AND e.phone = c.phone
            AND e.received_at > now() - (:window_seconds * interval '1 second')
            AND {HUMAN_TEAM_OUTBOUND_PREDICATE}
      )
      AND NOT EXISTS (
          SELECT 1
          FROM genius_next.webapp_sends AS w
          WHERE w.tenant_id = c.tenant_id
            AND w.phone = c.phone
            AND w.updated_at > now() - (:window_seconds * interval '1 second')
      )
      -- 16/out/2026: NAO ha mais "paciente esperando segura a marcacao". Com ele,
      -- a conversa ficava travada para sempre: a paciente esperava a equipe, a equipe
      -- nao respondia, e a Julia nao falava porque a conversa seguia em crew (caso
      -- Venarya, 16/out). Agora, passada a janela (270 s) sem nenhum humano escrever, a
      -- conversa volta ao bot, que responde a ultima pergunta. Quem tem humano escrevendo
      -- continua com a equipe (guardas de equipe acima).
"""

# Destino da devolucao, igual ao release manual para status open/win: bot de
# volta (automation 'open', last_intent 'reply') e status preservado -- win/open
# e estado normal (cliente que comprou, nunca finalizado).  loss/block voltam
# para a finalizacao (automation 'loss'), que o sweep nunca reabre.
_CREW_RELEASE_AUTOMATION_STATE = (
    "CASE WHEN c.status IN ('open', 'win') THEN 'open' ELSE 'loss' END"
)
_CREW_RELEASE_LAST_INTENT = (
    "CASE WHEN c.status IN ('open', 'win') THEN 'reply' ELSE 'loss' END"
)
_CREW_RELEASE_SET = f"""
    SET automation_state = {_CREW_RELEASE_AUTOMATION_STATE},
        crew = false,
        crew_in_at = NULL,
        last_intent = {_CREW_RELEASE_LAST_INTENT},
        updated_at = now()
"""

_CREW_IN_AUTO_RELEASE_SQL = text(
    f"""
    /* crew_in.auto_release_sweep */
    UPDATE genius_next.clients AS c
    {_CREW_RELEASE_SET}
    WHERE {_CREW_RELEASE_ELIGIBLE}
    RETURNING c.tenant_id, c.phone, c.automation_state
    """
)

# Mesma regra, para um contato so: o follow-up (send_follow_up_v2_turn_workflow)
# devolve a marcacao vencida na hora do disparo em vez de depender do relogio
# de 20 s do sweep (restart, fila).  RETURNING = estado depois da devolucao.
_CREW_RELEASE_ONE_SQL = text(
    f"""
    /* crew_in.release_one */
    UPDATE genius_next.clients AS c
    {_CREW_RELEASE_SET}
    WHERE c.tenant_id = :tenant_id
      AND c.phone = :phone
      AND {_CREW_RELEASE_ELIGIBLE}
    RETURNING c.status, c.last_intent, c.automation_state
    """
)

# Leitura sem escrita da mesma decisao (o step do follow-up nao grava): o
# estado que o contato teria depois da devolucao, ou nenhuma linha.
_CREW_RELEASE_ONE_PREVIEW_SQL = text(
    f"""
    /* crew_in.release_one_preview */
    SELECT c.status,
           {_CREW_RELEASE_LAST_INTENT} AS last_intent,
           {_CREW_RELEASE_AUTOMATION_STATE} AS automation_state
    FROM genius_next.clients AS c
    WHERE c.tenant_id = :tenant_id
      AND c.phone = :phone
      AND {_CREW_RELEASE_ELIGIBLE}
    """
)


def crew_release_window_seconds(settings: Any | None = None) -> float:
    """Janela de inatividade da equipe, em segundos (4,5 min = 270 s)."""
    resolved = settings if settings is not None else get_settings()
    return float(resolved.CREW_IN_AUTO_RELEASE_MINUTES) * 60.0


def release_expired_crew_marking_with_session(
    session: Session,
    *,
    tenant_id: str,
    phone: str,
    window_seconds: float,
    apply: bool,
) -> dict[str, Any] | None:
    """Devolve (``apply=True``) ou so avalia a marcacao vencida de um contato.

    Mesma regra do sweep (``_CREW_RELEASE_ELIGIBLE``).  Retorna o estado do
    cliente depois da devolucao (status, last_intent, automation_state) ou
    ``None`` quando a marcacao ainda vale (ou nao ha marcacao).
    """
    if not window_seconds > 0:
        raise ValueError("crew release window must be positive")
    statement = _CREW_RELEASE_ONE_SQL if apply else _CREW_RELEASE_ONE_PREVIEW_SQL
    row = session.execute(
        statement,
        {
            "tenant_id": tenant_id,
            "phone": phone,
            "window_seconds": float(window_seconds),
        },
    ).mappings().one_or_none()
    return dict(row) if row is not None else None


_CONTROL_RESULT_STATE = {
    "crew_in": "crew",
    "finalize": "loss",
    "release": "open",
    "reopen": "open",
}

_CONTROL_CANCEL_LIFECYCLES_SQL = text(
    """
    /* control.cancel_lifecycles */
    UPDATE genius_next.follow_up_lifecycles
    SET state = 'stopped', updated_at = now()
    WHERE tenant_id = :tenant_id AND phone = :phone AND state = 'scheduled'
    """
)

_AUTOMATION_STATE_SQL = text(
    """
    /* control.automation_state */
    SELECT automation_state
    FROM genius_next.clients
    WHERE tenant_id = :tenant_id AND phone = :phone
    """
)

_TURN_STOPPED_BY_CONTROL_SQL = text(
    """
    /* control.turn_stop */
    UPDATE genius_next.turns
    SET status = 'completed',
        decision = jsonb_build_object('action', 'suppressed_by_control'),
        completed_at = now(), error = NULL
    WHERE id = :turn_id AND tenant_id = :tenant_id
      AND status IN ('claimed', 'processing')
    """
)

_TURN_STOPPED_AS_TRIVIAL_SQL = text(
    """
    /* trivial.turn_stop */
    UPDATE genius_next.turns
    SET status = 'completed',
        decision = jsonb_build_object('action', 'suppressed_trivial'),
        completed_at = now(), error = NULL
    WHERE id = :turn_id AND tenant_id = :tenant_id
      AND status IN ('claimed', 'processing')
    """
)

_TURN_FAILED_SQL = text(
    """
    /* turn.fail */
    UPDATE genius_next.turns
    SET status = 'failed',
        completed_at = now(),
        error = :error
    WHERE id = :turn_id
      AND tenant_id = :tenant_id
      AND provider = :provider
      AND instance = :instance
      AND remote_jid = :remote_jid
      AND first_sequence = :first_sequence
      AND last_sequence = :last_sequence
      AND event_count = :event_count
      AND status IN ('claimed', 'processing')
    RETURNING id
    """
)

_OUTBOX_INSERT_SQL = text(
    """
    /* outbox.insert */
    INSERT INTO genius_next.outbox (
        topic, payload, tenant_id, turn_id, ordinal, instance,
        destination_jid, action, kind, idempotency_key, delivery_mode
    ) VALUES (
        :topic, :payload, :tenant_id, :turn_id, :ordinal, :instance,
        :destination_jid, :action, :kind, :idempotency_key, :delivery_mode
    )
    RETURNING id
    """
).bindparams(bindparam("payload", type_=JSONB))


# Envio sintetico (follow-up, Manager, espera do turno): a mesma tentativa pode chegar de novo
# apos uma queda, e a chave unica do outbox ja diz que a linha existe.
_OUTBOX_INSERT_IDEMPOTENT_SQL = text(
    str(_OUTBOX_INSERT_SQL).rstrip().removesuffix("RETURNING id")
    + "ON CONFLICT (tenant_id, idempotency_key) DO NOTHING\n    RETURNING id"
).bindparams(bindparam("payload", type_=JSONB))


_SECURITY_EVENT_INSERT_SQL = text(
    """
    /* security_event.insert */
    INSERT INTO genius_next.security_events (
        tenant_id, phone, conversation_id, turn_id, event_id,
        stage, scanner, scanner_version, decision, score, reason_code
    ) VALUES (
        :tenant_id, :phone, :conversation_id, :turn_id, :event_id,
        :stage, :scanner, :scanner_version, :decision, :score, :reason_code
    )
    RETURNING id
    """
)

_OUTBOX_CLAIM_SQL = text(
    """
    /* outbox.claim */
    UPDATE genius_next.outbox
    SET delivery_status = 'claimed',
        lease_owner = :lease_owner,
        lease_expires_at = now() + (:lease_seconds * interval '1 second'),
        attempts = attempts + 1
    WHERE tenant_id = :tenant_id
      AND idempotency_key = :idempotency_key
      AND delivery_status = 'pending'
    RETURNING
        id AS outbox_id, tenant_id, turn_id, ordinal, instance,
        destination_jid, action, kind, payload ->> 'text' AS text,
        payload ->> 'media_base64' AS media_base64,
        payload ->> 'mime_type' AS mime_type,
        COALESCE(payload ->> 'purpose', 'reply') AS purpose,
        payload -> 'follow_up' AS follow_up,
        payload -> 'template' AS template,
        idempotency_key, delivery_mode, lease_owner, attempts,
        COALESCE((
            SELECT r.provider
            FROM genius_next.tenant_registry AS r
            WHERE r.tenant_id = genius_next.outbox.tenant_id
              AND r.instance = genius_next.outbox.instance
            ORDER BY r.provider
            LIMIT 1
        ), 'evolution') AS provider
    """
)

_OUTBOX_RECLAIM_SQL = text(
    """
    /* outbox.reclaim */
    UPDATE genius_next.outbox
    SET delivery_status = 'pending',
        lease_owner = NULL,
        lease_expires_at = NULL
    WHERE tenant_id = :tenant_id
      AND idempotency_key = :idempotency_key
      AND delivery_status = 'claimed'
      AND lease_expires_at < now()
      AND provider_message_id IS NULL
    RETURNING
        id AS outbox_id, tenant_id, turn_id, ordinal, instance,
        destination_jid, action, kind, payload ->> 'text' AS text,
        payload ->> 'media_base64' AS media_base64,
        payload ->> 'mime_type' AS mime_type,
        COALESCE(payload ->> 'purpose', 'reply') AS purpose,
        payload -> 'follow_up' AS follow_up,
        payload -> 'template' AS template,
        idempotency_key, delivery_mode, lease_owner, attempts,
        COALESCE((
            SELECT r.provider
            FROM genius_next.tenant_registry AS r
            WHERE r.tenant_id = genius_next.outbox.tenant_id
              AND r.instance = genius_next.outbox.instance
            ORDER BY r.provider
            LIMIT 1
        ), 'evolution') AS provider
    """
)


_OUTBOX_FINALIZE_SQL = text(
    """
    /* outbox.finalize */
    UPDATE genius_next.outbox
    SET delivery_status = :delivery_status,
        provider_message_id = :provider_message_id,
        provider_result = :provider_result,
        dispatched_at = now(),
        lease_owner = NULL,
        lease_expires_at = NULL
    WHERE id = :outbox_id
      AND tenant_id = :tenant_id
      AND idempotency_key = :idempotency_key
      AND delivery_status = 'claimed'
      AND lease_owner = :lease_owner
    RETURNING id
    """
).bindparams(bindparam("provider_result", type_=JSONB))

_OUTBOX_ROUTE_REGISTERED_SQL = text(
    """
    /* outbox.route_registered */
    SELECT tenant_id
    FROM genius_next.tenant_registry
    WHERE provider = :provider
      AND tenant_id = :tenant_id
      AND instance = :instance
      AND enabled = true
    """
)


def _outbox_payload(command: OutboxCommand) -> dict[str, Any]:
    """Persist command purpose and only its trusted delivery context in JSONB."""
    payload: dict[str, Any] = {
        "action": command.action,
        "text": command.text,
        "purpose": command.purpose,
    }
    if command.media_base64 is not None:
        payload.update(
            media_base64=command.media_base64,
            mime_type=command.mime_type,
        )
    if command.follow_up is not None:
        payload["follow_up"] = command.follow_up.model_dump(mode="json")
    if command.template is not None:
        payload["template"] = command.template.model_dump(mode="json")
    return payload


def _identity_params(event: TrustedIngress) -> dict[str, Any]:
    identity = event.identity
    return {
        "tenant_id": event.tenant_id,
        "provider": identity.provider,
        "instance": identity.instance,
        "remote_jid": identity.remote_jid,
        "message_id": identity.message_id,
    }


def client_has_summary_with_session(
    session: Session,
    *,
    tenant_id: str,
    phone: str,
) -> bool:
    summary = session.execute(
        _CLIENT_HAS_SUMMARY_SQL,
        {"tenant_id": tenant_id, "phone": phone},
    ).scalar_one_or_none()
    return isinstance(summary, str) and bool(summary.strip())



def record_client_summary_with_session(
    session: Session,
    *,
    tenant_id: str,
    phone: str,
    summary: str | None,
) -> None:
    updated = session.execute(
        _CLIENT_RECORD_SUMMARY_SQL,
        {"tenant_id": tenant_id, "phone": phone, "summary": summary},
    ).scalar_one_or_none()
    if updated is None:
        raise ClientStateMissing("trusted client row does not exist")


def _canonical_payload_json(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


_CREW_IN_DISCLOSURE_TEXT = (
    "A partir de agora, {actor_id} da nossa equipe vai continuar seu atendimento."
)
_ACTOR_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


_ACTOR_NAME_RE = re.compile(r"^[A-Za-zÀ-ÖØ-öø-ÿ]+(?: [A-Za-zÀ-ÖØ-öø-ÿ]+){0,3}$")
_ACTOR_TITLES = frozenset({"dr.", "dra.", "sr.", "sra.", "prof.", "profa."})
_ACTOR_INTERNAL_WORDS = frozenset(
    {"chairman", "ceo", "admin", "user", "bot", "test", "teste", "crew", "system"}
)


def _crew_in_actor_label(actor_id: str | None) -> str:
    """Quem assumiu, como o paciente deve ler: so nome proprio, nunca id tecnico.

    Lista de permissao (incidente de 16/out: "chairman-eugenio-camargo" chegou a
    um paciente porque o filtro anterior so barrava UUID e "@"). So vira nome o
    que parece nome de pessoa: letras e espacos, ate 4 palavras, sem hifen,
    digito, underscore ou palavra interna. Qualquer outro valor vira "uma pessoa".
    """
    label = " ".join((actor_id or "").split())
    words = label.split()
    if words and words[0].lower() in _ACTOR_TITLES:
        label_body = " ".join(words[1:])
    else:
        label_body = label
    if not label_body or not _ACTOR_NAME_RE.match(label_body):
        return "uma pessoa"
    if any(w.lower() in _ACTOR_INTERNAL_WORDS for w in label.split()):
        return "uma pessoa"
    return label


def _crew_already_talking(*, tenant_id: str, phone: str) -> bool:
    """A equipe ja esta conversando com o contato? Entao o aviso nao sai.

    Sim quando, dentro de ``CREW_IN_DISCLOSURE_QUIET_MINUTES``, ja saiu um
    aviso de Assumir ou houve mensagem humana da equipe (envio pelo app ou
    digitada no celular/inbox). Sem isso, cada clique em Assumir -- e o
    auto-release de ``CREW_IN_AUTO_RELEASE_MINUTES`` faz o operador clicar de
    novo -- repetia "A partir de agora, ..." no meio da conversa.
    """
    minutes = get_settings().CREW_IN_DISCLOSURE_QUIET_MINUTES
    if minutes <= 0:
        return False
    active = sql_session().execute(
        _CREW_IN_TEAM_ACTIVE_SQL,
        {"tenant_id": tenant_id, "phone": phone, "minutes": minutes},
    ).scalar_one_or_none()
    return bool(active)


def _build_crew_in_disclosure(
    *,
    tenant_id: str,
    phone: str,
    instance: str,
    control_id: str,
    actor_id: str,
) -> OutboxCommand | None:
    """One customer-facing notice that a human took over -- best effort.

    Skipped when the team is already talking to the contact (see
    ``_crew_already_talking``): the notice is for the hand-over moment only.

    Looked up by the client's own most recent ``events.remote_jid`` instead
    of reconstructing ``f"{phone}@s.whatsapp.net"``: a ``@lid`` (LinkedID)
    contact has no real phone number, so guessing the JID from ``phone``
    would misroute or silently fail for those clients.
    """
    row = sql_session().execute(
        _CONTROL_LATEST_REMOTE_JID_SQL,
        {"tenant_id": tenant_id, "phone": phone},
    ).mappings().first()
    if row is None or not row["remote_jid"]:
        logger.warning(
            "crew_in disclosure skipped: no known remote_jid for %s/%s",
            tenant_id,
            phone,
        )
        return None
    if _crew_already_talking(tenant_id=tenant_id, phone=phone):
        logger.info(
            "crew_in disclosure skipped: team already talking to %s/...%s",
            tenant_id,
            phone[-4:],
        )
        return None
    turn_id = stable_id("crew-in-disclosure", tenant_id, phone, control_id)
    return OutboxCommand(
        tenant_id=tenant_id,
        turn_id=turn_id,
        ordinal=0,
        instance=instance,
        destination_jid=row["remote_jid"],
        action="send_text",
        kind="customer",
        text=_CREW_IN_DISCLOSURE_TEXT.format(actor_id=_crew_in_actor_label(actor_id)),
        idempotency_key=stable_id("outbox", turn_id, "0", "send_text"),
        delivery_mode=get_settings().DELIVERY_MODE,
        purpose="handoff",
    )


@transaction(isolation_level="SERIALIZABLE")
def apply_conversation_control_txn(
    request: ConversationControlRequest,
    request_hash: str,
) -> tuple[ConversationControlApplyResult, OutboxCommand | None]:
    """Apply one operator instruction once and cancel active automation.

    The request is immutable at this boundary: a reused control ID with a
    different request hash is an explicit conflict, never a last-write-wins
    state mutation. Returns the result plus a customer disclosure command
    (crew_in only, first application only -- the control_id dedup above
    already guarantees this branch runs at most once per control_id, so no
    extra idempotency guard is needed on the outbox insert itself; and only
    when the team is not already talking to the contact, so a second
    Assumir with a new control_id stays silent) for the caller to dispatch
    after commit.
    """
    params = request.model_dump(mode="json")
    params["request_hash"] = request_hash
    inserted = sql_session().execute(_CONTROL_INSERT_SQL, {**params, "request": params}).scalar_one_or_none()
    if inserted is None:
        existing = sql_session().execute(
            _CONTROL_LOOKUP_SQL,
            {"tenant_id": request.tenant_id, "control_id": request.control_id},
        ).mappings().one_or_none()
        if existing is None or existing["request_hash"] != request_hash:
            raise ConversationControlConflict("control ID already has a different request")
        state = _CONTROL_RESULT_STATE[request.action]
        return ConversationControlApplyResult(outcome="noop", automation_state=state), None

    updated = sql_session().execute(_CONTROL_APPLY_CLIENT_SQL, params).mappings().one_or_none()
    if updated is None:
        raise ClientStateMissing("trusted client row does not exist")
    if request.action != "release":
        # Devolver a conversa para a automacao nao deve matar follow-up agendado;
        # so tomar o controle ou finalizar cancela.
        sql_session().execute(
            _CONTROL_CANCEL_LIFECYCLES_SQL,
            {"tenant_id": request.tenant_id, "phone": request.phone},
        )
    disclosure: OutboxCommand | None = None
    if request.action == "crew_in":
        disclosure = _build_crew_in_disclosure(
            tenant_id=request.tenant_id,
            phone=request.phone,
            instance=updated["instance"],
            control_id=request.control_id,
            actor_id=request.actor_id,
        )
        if disclosure is not None:
            sql_session().execute(
                _OUTBOX_INSERT_SQL,
                {
                    "topic": "reply.created",
                    "payload": _outbox_payload(disclosure),
                    **disclosure.model_dump(mode="json"),
                },
            )
    result = ConversationControlApplyResult(
        outcome="applied", automation_state=updated["automation_state"]
    )
    return result, disclosure


@transaction()
def auto_release_stale_crew_in_txn(window_seconds: float) -> list[dict[str, str]]:
    """Single atomic conditional UPDATE -- no read-then-write, no TOCTOU.

    A concurrent write to the same client row (a crew_in that refreshed
    crew_in_at, a decision, a release) is never overwritten by a stale view:
    under the SERIALIZABLE default of ``@transaction`` the conflict aborts one
    side with 40001 and the whole function is retried.  Regra completa em
    ``_CREW_RELEASE_ELIGIBLE``.
    """
    if not window_seconds > 0:
        raise ValueError("crew release window must be positive")
    rows = sql_session().execute(
        _CREW_IN_AUTO_RELEASE_SQL, {"window_seconds": float(window_seconds)}
    ).mappings().all()
    return [dict(r) for r in rows]



_REOPEN_IF_LOSS_SQL = text(
    """
    /* client.reopen_if_loss */
    UPDATE genius_next.clients
    SET status = 'open', automation_state = 'open', updated_at = now()
    WHERE tenant_id = :tenant_id AND phone = :phone
      AND status = 'loss' AND automation_state = 'loss'
    RETURNING phone
    """
)


@transaction(isolation_level="SERIALIZABLE")
def reopen_if_loss_txn(tenant_id: str, phone: str) -> bool:
    """Cliente perdido que fala de novo reabre a conversa sozinho.

    27/ago/2026: antes disso, loss so voltava a open por acao humana
    explicita (action='release') -- um cliente que mudou de ideia e mandou
    mensagem de novo ficava em silencio ate alguem notar e liberar na mao.
    So dispara quando o estado e EXATAMENTE loss/loss -- nunca reabre
    crew (decisao de operador) nem block (spam/abuso, guard-driven;
    so acao humana libera). E o cliente quem reabre, nunca a empresa: a
    mensagem dele e a evidencia.
    """
    reopened = sql_session().execute(
        _REOPEN_IF_LOSS_SQL, {"tenant_id": tenant_id, "phone": phone}
    ).scalar_one_or_none()
    return reopened is not None


_REOPEN_IF_WIN_SQL = text(
    """
    /* client.reopen_if_win */
    UPDATE genius_next.clients
    SET status = 'open', automation_state = 'open', crew = false, updated_at = now()
    WHERE tenant_id = :tenant_id AND phone = :phone
      AND status = 'win' AND automation_state = 'loss'
    RETURNING phone
    """
)


@transaction(isolation_level="SERIALIZABLE")
def reopen_if_win_txn(tenant_id: str, phone: str) -> bool:
    """Cliente win que volta a falar reabre a conversa como open.

    21/set/2026: cliente que ja comprou (status=win) pode querer comprar
    outro produto ou retomar conversacao. Quando manda mensagem nova,
    volta automaticamente a status=open para o bot conversar. O cliente
    e quem reabre, nunca a empresa: a mensagem dele e a evidencia de
    intencao de compra nova. So dispara quando o estado e EXATAMENTE
    win/loss -- nunca reabre crew (decisao de operador) nem block
    (spam/abuso; so acao humana libera).
    """
    reopened = sql_session().execute(
        _REOPEN_IF_WIN_SQL, {"tenant_id": tenant_id, "phone": phone}
    ).scalar_one_or_none()
    return reopened is not None


@transaction(isolation_level="SERIALIZABLE")
def automation_allowed_for_turn_txn(tenant_id: str, phone: str) -> bool:
    """Fail closed before an automated workflow can draft a customer reply."""
    state = sql_session().execute(
        _AUTOMATION_STATE_SQL, {"tenant_id": tenant_id, "phone": phone}
    ).scalar_one_or_none()
    return state == "open"


@transaction(isolation_level="SERIALIZABLE")
def client_has_summary_txn(tenant_id: str, phone: str) -> bool:
    return client_has_summary_with_session(
        sql_session(),
        tenant_id=tenant_id,
        phone=phone,
    )



@transaction(isolation_level="SERIALIZABLE")
def record_client_summary_txn(
    tenant_id: str,
    phone: str,
    summary: str | None,
) -> None:
    record_client_summary_with_session(
        sql_session(),
        tenant_id=tenant_id,
        phone=phone,
        summary=summary,
    )


@transaction(isolation_level="SERIALIZABLE")
def automation_allowed_for_delivery_txn(
    tenant_id: str,
    normalized_destination: str,
) -> bool:
    """Last durable authority check for a live customer reply."""
    state = sql_session().execute(
        _AUTOMATION_STATE_SQL,
        {"tenant_id": tenant_id, "phone": normalized_destination},
    ).scalar_one_or_none()
    return state == "open"


@transaction(isolation_level="SERIALIZABLE")
def stop_turn_for_control_txn(turn_id: str, tenant_id: str) -> None:
    """Release a claimed turn without AI work after operator ownership begins."""
    sql_session().execute(
        _TURN_STOPPED_BY_CONTROL_SQL, {"turn_id": turn_id, "tenant_id": tenant_id}
    )


@transaction(isolation_level="SERIALIZABLE")
def stop_turn_as_trivial_txn(turn_id: str, tenant_id: str) -> None:
    """Release a claimed turn without AI work: every customer message in the
    batch was trivial (thanks, laughter, emoji-only, bare ok/blz-style ack).

    Mirrors stop_turn_for_control_txn exactly -- same shape, different
    decision.action -- so downstream readers of turns.decision already know
    how to handle a completed turn with no reply.
    """
    sql_session().execute(
        _TURN_STOPPED_AS_TRIVIAL_SQL, {"turn_id": turn_id, "tenant_id": tenant_id}
    )


def fail_turn_with_session(
    session: Session,
    *,
    turn: ClaimedTurn,
    error: str,
) -> None:
    """Persist a terminal domain failure for one already-claimed turn."""
    failed_turn_id = session.execute(
        _TURN_FAILED_SQL,
        {
            'turn_id': turn.turn_id,
            'tenant_id': turn.tenant_id,
            'provider': turn.provider,
            'instance': turn.instance,
            'remote_jid': turn.remote_jid,
            'first_sequence': turn.first_sequence,
            'last_sequence': turn.last_sequence,
            'event_count': turn.event_count,
            'error': error,
        },
    ).scalar_one_or_none()
    if failed_turn_id is None:
        raise TurnStateConflict(
            'turn is absent, stale, or belongs to another identity'
        )


@transaction(isolation_level='SERIALIZABLE')
def fail_turn_txn(turn: ClaimedTurn, error: str) -> None:
    fail_turn_with_session(sql_session(), turn=turn, error=error)


def _existing_result(
    existing: dict[str, Any] | None,
    event: TrustedIngress,
) -> PersistEventResult | None:
    if existing is None:
        return None
    if existing["event_hash"] != event.event_hash:
        # A sent reply is first represented by the durable outbox finalizer and
        # may later arrive again as Evolution's messages.upsert echo.  Their
        # envelopes (and hashes) differ, but the provider identity and visible
        # message are the same.  Reconcile only that narrow outbound case.
        same_route = (
            existing.get("direction") == "outbound"
            and event.direction == "outbound"
            and existing.get("instance") == event.identity.instance
            and direct_jids_equivalent(
                str(existing.get("remote_jid") or ""),
                event.identity.remote_jid,
            )
        )
        same_content = existing.get("text_content") == event.text_content
        same_audio = (
            existing.get("message_type") == "audioMessage"
            and event.sanitized_payload.data.message_type == "audioMessage"
        )
        recorded_by_outbox = str(
            existing.get("ingest_workflow_id") or ""
        ).startswith("outbound:")
        if not (recorded_by_outbox and same_route and (same_content or same_audio)):
            raise EventIdentityConflict(
                "provider event identity reused with different payload"
            )
    return PersistEventResult(
        inserted=False,
        conversation_id=event.conversation_id,
        sequence=existing["conversation_sequence"],
    )


def _is_same_outbound_event(
    existing: dict[str, Any] | None,
    *,
    claimed: ClaimedOutbox,
) -> bool:
    """Return whether a provider identity already represents this exact reply."""
    if existing is None:
        return False
    same_route = (
        existing.get("direction") == "outbound"
        and existing.get("instance") == claimed.instance
        and direct_jids_equivalent(
            str(existing.get("remote_jid") or ""), claimed.destination_jid
        )
    )
    same_content = existing.get("text_content") == claimed.text
    same_audio = (
        claimed.action == "send_media"
        and existing.get("message_type") == "audioMessage"
    )
    return same_route and (same_content or same_audio)


def persist_event_with_session(
    session: Session,
    event: TrustedIngress,
) -> PersistEventResult:
    """Persist/dedupe one event while allocating a tenant-safe sequence."""
    identity_params = _identity_params(event)
    existing = session.execute(_EVENT_LOOKUP_SQL, identity_params).mappings().first()
    result = _existing_result(existing, event)
    if result is not None:
        return result

    session.execute(_CONVERSATION_ENSURE_SQL, identity_params)
    locked = session.execute(_CONVERSATION_LOCK_SQL, identity_params).mappings().first()
    if locked is None:
        raise RuntimeError("conversation lock row was not created")

    # Recheck under the same conversation lock before consuming sequence.  In
    # SERIALIZABLE Postgres, a concurrent insert may instead force a retry.
    existing = session.execute(_EVENT_LOOKUP_SQL, identity_params).mappings().first()
    result = _existing_result(existing, event)
    if result is not None:
        return result

    sequence = int(locked["next_sequence"])
    session.execute(
        _CONVERSATION_ADVANCE_SQL,
        {**identity_params, "next_sequence": sequence + 1},
    )
    sanitized_payload = event.sanitized_payload.model_dump(
        by_alias=True, exclude_none=True, mode="json"
    )
    payload_data = event.sanitized_payload.data
    session.execute(
        _EVENT_INSERT_SQL,
        {
            **identity_params,
            "phone": event.phone,
            "message_type": payload_data.message_type,
            "direction": event.direction,
            "text_content": event.text_content,
            "push_name": payload_data.push_name,
            "sanitized_payload": sanitized_payload,
            "event_type": event.identity.event_type,
            "event_hash": event.event_hash,
            "conversation_sequence": sequence,
            "ingest_workflow_id": event.ingest_workflow_id,
        },
    )
    # ``RETURNING xmax`` reuses the classic Postgres insert-vs-update trick
    # from the legacy n8n flow: xmax is 0 only for a freshly inserted row, so
    # it tells us this was the client's very first appearance in
    # genius_next.clients.  Compared in Python (not `(xmax = 0) AS bool` in
    # SQL): returning the raw integer avoids a bare boolean scalar in the
    # result, which once crashed with "TypeError: 'bool' object is not
    # subscriptable" (see PG task #3121).
    upserted = session.execute(
        _CLIENT_UPSERT_SQL,
        {
            "tenant_id": event.tenant_id,
            "phone": event.phone,
            "instance": event.identity.instance,
            "push_name": payload_data.push_name,
        },
    ).scalar_one_or_none()
    is_new_client = upserted == 0
    if (
        event.direction == "inbound"
        and event.identity.event_type == "messages.upsert"
        and get_settings().compromissos_shadow_enabled(event.tenant_id)
    ):
        _abrir_compromisso_sombra(session, event, sequence)
    return PersistEventResult(
        inserted=True,
        conversation_id=event.conversation_id,
        sequence=sequence,
        is_new_client=is_new_client,
    )


def _abrir_compromisso_sombra(session: Session, event: TrustedIngress, sequence: int) -> None:
    """Compromisso resposta_devida na MESMA transacao do evento (app/state/compromissos.py).

    Sombra: um savepoint isola a escrita; qualquer falha aqui e logada e a entrada
    da mensagem segue intacta.
    """
    from app.state.compromissos import abrir_resposta_devida

    try:
        with session.begin_nested():
            abrir_resposta_devida(
                session,
                tenant_id=event.tenant_id,
                phone=event.phone,
                remote_jid=event.identity.remote_jid,
                instance=event.identity.instance,
                provider=event.identity.provider,
                geracao=sequence,
            )
    except Exception:
        logger.warning("compromisso sombra nao aberto para %s", event.tenant_id, exc_info=True)


@transaction(isolation_level="SERIALIZABLE")
def persist_event_txn(event: TrustedIngress) -> PersistEventResult:
    """Transaction wrapper for durable event ingestion."""
    return persist_event_with_session(sql_session(), event)


def claim_turn_with_session(
    session: Session,
    event: TrustedIngress,
    *,
    through_sequence: int,
    max_events: int,
) -> ClaimTurnResult:
    """Claim one finite ordered inbound batch under the conversation lock."""
    if through_sequence < 1:
        raise ValueError("through_sequence must be positive")
    if not 1 <= max_events <= 100:
        raise ValueError("max_events must be between 1 and 100")
    identity_params = _identity_params(event)
    locked = session.execute(_CONVERSATION_LOCK_SQL, identity_params).mappings().first()
    if locked is None:
        raise RuntimeError("conversation does not exist")
    rows = (
        session.execute(
            _CLAIMABLE_EVENTS_SQL,
            {
                **identity_params,
                "through_sequence": through_sequence,
                "fetch_limit": max_events + 1,
            },
        )
        .mappings()
        .all()
    )
    if not rows:
        # Nenhum evento novo a reivindicar: pode haver um turno orfao de uma
        # execucao anterior que morreu no meio (deploy, kill -9). Recupera-o.
        inflight = session.execute(_INFLIGHT_TURN_SQL, identity_params).mappings().first()
        if inflight is not None:
            retomados = (
                session.execute(_TURN_EVENTS_SQL, {"turn_id": inflight["turn_id"]}).mappings().all()
            )
            return ClaimTurnResult(
                turn=ClaimedTurn(
                    turn_id=inflight["turn_id"],
                    conversation_id=event.conversation_id,
                    tenant_id=event.tenant_id,
                    provider=event.identity.provider,
                    instance=event.identity.instance,
                    remote_jid=event.identity.remote_jid,
                    first_sequence=inflight["first_sequence"],
                    last_sequence=inflight["last_sequence"],
                    event_count=inflight["event_count"],
                    events=[ClaimedEvent.model_validate(row) for row in retomados],
                ),
                has_more=False,
            )
        return ClaimTurnResult(turn=None, has_more=False)

    has_more = len(rows) > max_events
    claimed_events = [ClaimedEvent.model_validate(row) for row in rows[:max_events]]
    first_sequence = claimed_events[0].sequence
    last_sequence = claimed_events[-1].sequence
    event_hashes = tuple(claimed.event_hash for claimed in claimed_events)
    if any(event_hash is None for event_hash in event_hashes):
        raise RuntimeError("claimed event is missing its immutable hash")
    turn_id = stable_id(
        "turn",
        event.conversation_id,
        first_sequence,
        last_sequence,
        *event_hashes,
    )
    turn_params = {
        **identity_params,
        "turn_id": turn_id,
        "first_sequence": first_sequence,
        "last_sequence": last_sequence,
        "event_count": len(claimed_events),
    }
    session.execute(_TURN_INSERT_SQL, turn_params)
    session.execute(
        _EVENT_MARK_CLAIMED_SQL,
        {
            "turn_id": turn_id,
            "event_ids": [claimed.event_row_id for claimed in claimed_events],
        },
    )
    return ClaimTurnResult(
        turn=ClaimedTurn(
            turn_id=turn_id,
            conversation_id=event.conversation_id,
            tenant_id=event.tenant_id,
            provider=event.identity.provider,
            instance=event.identity.instance,
            remote_jid=event.identity.remote_jid,
            first_sequence=first_sequence,
            last_sequence=last_sequence,
            event_count=len(claimed_events),
            events=claimed_events,
        ),
        has_more=has_more,
    )


# O debounce do Procrastinate junta a rajada num turno so: o claim le tudo o que ja foi gravado.
CLAIM_ALL_SEQUENCE = 2**62


@transaction(isolation_level="SERIALIZABLE")
def claim_turn_txn(
    event: TrustedIngress,
    through_sequence: int,
    max_events: int | None = None,
) -> ClaimTurnResult:
    """Transaction wrapper using the configured finite claim limit."""
    resolved_limit = (
        get_settings().TURN_MAX_EVENTS if max_events is None else max_events
    )
    return claim_turn_with_session(
        sql_session(),
        event,
        through_sequence=through_sequence,
        max_events=resolved_limit,
    )


def _complete_turn_and_apply_decision(
    session: Session,
    *,
    turn: ClaimedTurn,
    phone: str,
    decision: AgentDecision,
) -> None:
    if phone != normalize_phone(turn.remote_jid):
        raise ValueError("client phone does not match claimed turn")

    completed_turn_id = session.execute(
        _TURN_COMPLETE_SQL,
        {
            "turn_id": turn.turn_id,
            "tenant_id": turn.tenant_id,
            "provider": turn.provider,
            "instance": turn.instance,
            "remote_jid": turn.remote_jid,
            "first_sequence": turn.first_sequence,
            "last_sequence": turn.last_sequence,
            "event_count": turn.event_count,
            "decision": decision.model_dump(mode="json"),
        },
    ).scalar_one_or_none()
    if completed_turn_id is None:
        raise TurnStateConflict("turn is absent, stale, or belongs to another identity")

    updated_phone = session.execute(
        _CLIENT_APPLY_DECISION_SQL,
        {
            "tenant_id": turn.tenant_id,
            "phone": phone,
            "customer_status": decision.customer_status,
            "needs_human": decision.needs_human,
            "crew_expires": decision.crew_expires,
            "last_intent": decision.intent,
            "memory_updates": decision.memory_updates,
        },
    ).scalar_one_or_none()
    if updated_phone is None:
        raise ClientStateMissing("trusted client row does not exist")


def _validate_outbox_command_routing(
    session: Session,
    *,
    turn: ClaimedTurn,
    command: OutboxCommand,
) -> None:
    """Validate customer and team routes against trusted durable state."""
    if command.instance != turn.instance:
        raise ValueError("outbox command routing does not match claimed turn")

    if command.kind == "customer":
        expected_destination = turn.remote_jid
    else:
        expected_destination = manager_jid_with_session(
            session, tenant_id=turn.tenant_id
        )
        if expected_destination is None:
            raise ValueError("team outbox command has no registered manager route")

    if command.destination_jid != expected_destination:
        raise ValueError("outbox command routing does not match claimed turn")


def record_decision_with_session(
    session: Session,
    *,
    turn: ClaimedTurn,
    phone: str,
    decision: AgentDecision,
    command: OutboxCommand,
) -> OutboxCommand:
    """Atomically complete a turn, mutate client state, and append its outbox row."""
    expected_key = stable_id(
        "outbox",
        turn.tenant_id,
        turn.turn_id,
        command.ordinal,
        command.action,
    )
    if command.tenant_id != turn.tenant_id or command.turn_id != turn.turn_id:
        raise ValueError("outbox command does not belong to claimed turn")
    _validate_outbox_command_routing(session, turn=turn, command=command)
    if command.idempotency_key != expected_key:
        raise ValueError("outbox idempotency key is not canonical")

    _complete_turn_and_apply_decision(
        session,
        turn=turn,
        phone=phone,
        decision=decision,
    )

    outbox_id = session.execute(
        _OUTBOX_INSERT_SQL,
        {
            "topic": "reply.created",
            "payload": _outbox_payload(command),
            **command.model_dump(mode="json"),
        },
    ).scalar_one_or_none()
    if outbox_id is None:
        raise OutboxWriteFailed("outbox insert returned no row")
    return command


@transaction(isolation_level="SERIALIZABLE")
def record_decision_txn(
    turn: ClaimedTurn,
    phone: str,
    decision: AgentDecision,
    command: OutboxCommand,
) -> OutboxCommand:
    """Wrapper making state completion and outbox append one commit."""
    return record_decision_with_session(
        sql_session(),
        turn=turn,
        phone=phone,
        decision=decision,
        command=command,
    )


def record_decision_batch_with_session(
    session: Session,
    *,
    turn: ClaimedTurn,
    phone: str,
    decision: AgentDecision,
    commands: list[OutboxCommand],
) -> list[OutboxCommand]:
    """Atomically complete a turn, mutate client state, and append N outbox rows.

    The batch mirrors ``record_decision_with_session`` for multi-block human
    output: the turn is completed exactly once, the client state is updated
    exactly once, and every command (ordinals 0..N-1, contiguous, unique)
    is appended in the same SERIALIZABLE commit.
    """
    if not commands:
        raise ValueError("outbox batch must not be empty")

    seen_ordinals: set[int] = set()
    for command in commands:
        expected_key = stable_id(
            "outbox",
            turn.tenant_id,
            turn.turn_id,
            command.ordinal,
            command.action,
        )
        if command.tenant_id != turn.tenant_id or command.turn_id != turn.turn_id:
            raise ValueError("outbox command does not belong to claimed turn")
        _validate_outbox_command_routing(session, turn=turn, command=command)
        if command.idempotency_key != expected_key:
            raise ValueError("outbox idempotency key is not canonical")
        if command.ordinal in seen_ordinals:
            raise ValueError("outbox batch ordinals must be unique")
        seen_ordinals.add(command.ordinal)

    if sorted(seen_ordinals) != list(range(len(commands))):
        raise ValueError("outbox batch ordinals must be contiguous from 0")

    _complete_turn_and_apply_decision(
        session,
        turn=turn,
        phone=phone,
        decision=decision,
    )

    for command in commands:
        outbox_id = session.execute(
            _OUTBOX_INSERT_SQL,
            {
                "topic": "reply.created",
                "payload": _outbox_payload(command),
                **command.model_dump(mode="json"),
            },
        ).scalar_one_or_none()
        if outbox_id is None:
            raise OutboxWriteFailed("outbox insert returned no row")
    return commands


def record_decision_without_outbox_with_session(
    session: Session,
    *,
    turn: ClaimedTurn,
    phone: str,
    decision: AgentDecision,
) -> None:
    """Complete a turn and mutate client state without appending any outbox row."""
    _complete_turn_and_apply_decision(
        session,
        turn=turn,
        phone=phone,
        decision=decision,
    )


@transaction(isolation_level="SERIALIZABLE")
def record_decision_without_outbox_txn(
    turn: ClaimedTurn,
    phone: str,
    decision: AgentDecision,
) -> None:
    record_decision_without_outbox_with_session(
        sql_session(),
        turn=turn,
        phone=phone,
        decision=decision,
    )


def _split_scanner_version(scanner: str) -> tuple[str, str | None]:
    base, sep, version = scanner.rpartition("@")
    if not sep:
        return scanner, None
    return base or scanner, version or None


def record_security_event_with_session(
    session: Session,
    *,
    tenant_id: str,
    phone: str,
    conversation_id: str,
    turn_id: str,
    assessment: GuardAssessment,
) -> list[SecurityEventRecord]:
    """Persist payload-free guard findings linked to existing event IDs only."""
    rows: list[SecurityEventRecord] = []
    event_refs = tuple(dict.fromkeys(assessment.event_refs))
    for event_ref in event_refs:
        event_id = int(event_ref)
        for finding in assessment.findings:
            scanner, scanner_version = _split_scanner_version(finding.scanner)
            row = SecurityEventRecord(
                tenant_id=tenant_id,
                phone=phone,
                conversation_id=conversation_id,
                turn_id=turn_id,
                event_id=event_id,
                stage=assessment.stage,
                scanner=scanner,
                scanner_version=scanner_version,
                decision=assessment.disposition,
                score=finding.score,
                reason_code=finding.reason_code,
            )
            inserted = session.execute(
                _SECURITY_EVENT_INSERT_SQL,
                row.model_dump(mode="json"),
            ).scalar_one_or_none()
            if inserted is None:
                raise RuntimeError("security event insert returned no row")
            rows.append(row)
    return rows


@transaction(isolation_level="SERIALIZABLE")
def record_security_event_txn(
    tenant_id: str,
    phone: str,
    conversation_id: str,
    turn_id: str,
    assessment: GuardAssessment,
) -> list[SecurityEventRecord]:
    return record_security_event_with_session(
        sql_session(),
        tenant_id=tenant_id,
        phone=phone,
        conversation_id=conversation_id,
        turn_id=turn_id,
        assessment=assessment,
    )


@transaction(isolation_level="SERIALIZABLE")
def record_security_input_block_txn(
    turn: ClaimedTurn,
    phone: str,
    assessment: GuardAssessment,
) -> None:
    decision = AgentDecision(
        intent="security_guard",
        action="suppressed",
        confidence=1.0,
        customer_status="block",
        needs_human=False,
    )
    record_decision_without_outbox_with_session(
        sql_session(),
        turn=turn,
        phone=phone,
        decision=decision,
    )
    record_security_event_with_session(
        sql_session(),
        tenant_id=turn.tenant_id,
        phone=phone,
        conversation_id=turn.conversation_id,
        turn_id=turn.turn_id,
        assessment=assessment,
    )


@transaction(isolation_level="SERIALIZABLE")
def record_output_guard_block_txn(
    turn: ClaimedTurn,
    phone: str,
    decision: AgentDecision,
    assessment: GuardAssessment,
) -> None:
    record_decision_without_outbox_with_session(
        sql_session(),
        turn=turn,
        phone=phone,
        decision=decision,
    )
    record_security_event_with_session(
        sql_session(),
        tenant_id=turn.tenant_id,
        phone=phone,
        conversation_id=turn.conversation_id,
        turn_id=turn.turn_id,
        assessment=assessment,
    )


@transaction(isolation_level="SERIALIZABLE")
def record_guard_decision_batch_txn(
    turn: ClaimedTurn,
    phone: str,
    decision: AgentDecision,
    commands: list[OutboxCommand],
    assessment: GuardAssessment,
) -> list[OutboxCommand]:
    """Record a guarded fallback decision plus its audit trail in one commit."""
    recorded = record_decision_batch_with_session(
        sql_session(),
        turn=turn,
        phone=phone,
        decision=decision,
        commands=commands,
    )
    record_security_event_with_session(
        sql_session(),
        tenant_id=turn.tenant_id,
        phone=phone,
        conversation_id=turn.conversation_id,
        turn_id=turn.turn_id,
        assessment=assessment,
    )
    return recorded


@transaction(isolation_level="SERIALIZABLE")
def insert_outbox_command_txn(command: OutboxCommand) -> None:
    """Insert one outbox row directly, with no turn/client bookkeeping.

    For synthetic sends (e.g. follow-up check-ins) that are not tied to a
    real claimed turn in genius_next.turns -- record_decision_batch_txn
    requires turn.turn_id to already exist there with status
    claimed/processing, which is untrue for a follow-up send.
    """
    # Sem linha devolvida = a tentativa ja estava no outbox (repeticao apos queda): nada a fazer.
    sql_session().execute(
        _OUTBOX_INSERT_IDEMPOTENT_SQL,
        {
            "topic": "reply.created",
            "payload": _outbox_payload(command),
            **command.model_dump(mode="json"),
        },
    )


@transaction(isolation_level="SERIALIZABLE")
def record_decision_batch_txn(
    turn: ClaimedTurn,
    phone: str,
    decision: AgentDecision,
    commands: list[OutboxCommand],
) -> list[OutboxCommand]:
    """Wrapper making turn completion, client update, and N outbox rows one commit."""
    return record_decision_batch_with_session(
        sql_session(),
        turn=turn,
        phone=phone,
        decision=decision,
        commands=commands,
    )


@transaction(isolation_level="SERIALIZABLE")
def apply_manager_handoff_txn(
    *, tenant_id: str, phone: str, crew_expires: bool
) -> bool:
    """Marca a conversa como da equipe SEM turno — triagem proativa que escalou.

    Reusa EXATAMENTE o SQL de marcação do handoff normal
    (``_CLIENT_APPLY_DECISION_SQL``): mesma transição de
    ``automation_state``/``crew``/``crew_in_at`` e o mesmo
    ``last_intent='handoff'``. Não duplica a lógica de marcação — o handoff do
    turno passa por este mesmo SQL dentro de ``record_decision_batch_txn``.
    ``crew_expires`` segue a lei de 08/out (``handoff_marking_expires``).
    Devolve True quando a linha do cliente existia e foi atualizada.
    """
    updated = sql_session().execute(
        _CLIENT_APPLY_DECISION_SQL,
        {
            "tenant_id": tenant_id,
            "phone": phone,
            "customer_status": "open",
            "needs_human": True,
            "crew_expires": bool(crew_expires),
            "last_intent": "handoff",
            "memory_updates": {},
        },
    ).scalar_one_or_none()
    return updated is not None


def claim_outbox_with_session(
    session: Session,
    *,
    command: OutboxCommand,
    lease_owner: str,
    lease_seconds: int = 60,
) -> ClaimedOutbox | None:
    """Lease one pending canonical command; terminal rows can never be reclaimed."""
    if not lease_owner or len(lease_owner) > 512:
        raise ValueError("invalid outbox lease owner")
    if not 1 <= lease_seconds <= 300:
        raise ValueError("outbox lease must be between 1 and 300 seconds")
    row = (
        session.execute(
            _OUTBOX_CLAIM_SQL,
            {
                "tenant_id": command.tenant_id,
                "idempotency_key": command.idempotency_key,
                "lease_owner": lease_owner,
                "lease_seconds": lease_seconds,
            },
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    claimed = ClaimedOutbox.model_validate(row)
    expected = command.model_dump(mode="json")
    actual = claimed.model_dump(
        mode="json", exclude={"outbox_id", "lease_owner", "attempts", "provider"}
    )
    if actual != expected:
        raise OutboxStateConflict("leased outbox row does not match canonical command")
    return claimed


@transaction(isolation_level="SERIALIZABLE")
def claim_outbox_txn(
    command: OutboxCommand,
    lease_owner: str,
    lease_seconds: int = 60,
) -> ClaimedOutbox | None:
    return claim_outbox_with_session(
        sql_session(),
        command=command,
        lease_owner=lease_owner,
        lease_seconds=lease_seconds,
    )

def reclaim_outbox_with_session(
    session: Session,
    *,
    command: OutboxCommand,
) -> ClaimedOutbox | None:
    """Reclaim one stuck outbox row: claimed with expired lease and never sent.

    Rows with delivery_status='claimed' AND lease_expires_at < now() AND
    provider_message_id IS NULL are reset to 'pending' so they can be
    claimed again. Rows that were already sent (provider_message_id set)
    are left untouched to avoid duplicate delivery.
    """
    row = (
        session.execute(
            _OUTBOX_RECLAIM_SQL,
            {
                "tenant_id": command.tenant_id,
                "idempotency_key": command.idempotency_key,
            },
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    return ClaimedOutbox.model_validate(row)


@transaction(isolation_level="SERIALIZABLE")
def reclaim_outbox_txn(
    command: OutboxCommand,
) -> ClaimedOutbox | None:
    return reclaim_outbox_with_session(
        sql_session(),
        command=command,
    )


def outbox_route_registered_with_session(
    session: Session,
    *,
    tenant_id: str,
    instance: str,
    provider: str = "evolution",
) -> bool:
    """Confirm the outbound instance still belongs to the command tenant."""
    resolved = session.execute(
        _OUTBOX_ROUTE_REGISTERED_SQL,
        {"tenant_id": tenant_id, "instance": instance, "provider": provider},
    ).scalar_one_or_none()
    return resolved == tenant_id


@transaction(isolation_level="SERIALIZABLE")
def outbox_route_registered_txn(
    tenant_id: str, instance: str, provider: str = "evolution"
) -> bool:
    return outbox_route_registered_with_session(
        sql_session(), tenant_id=tenant_id, instance=instance, provider=provider
    )


def record_outbound_event_with_session(
    session: Session,
    *,
    claimed: ClaimedOutbox,
    provider_message_id: str,
) -> bool:
    """Ensure one sent customer reply exists under its provider identity."""
    message_id = provider_message_id
    identity_params = {
        "tenant_id": claimed.tenant_id,
        "provider": claimed.provider,
        "instance": claimed.instance,
        "remote_jid": claimed.destination_jid,
        "message_id": message_id,
    }
    existing = session.execute(_EVENT_LOOKUP_SQL, identity_params).mappings().first()
    if existing is not None:
        if _is_same_outbound_event(existing, claimed=claimed):
            return True
        raise OutboundEventPersistenceFailed(
            "outbound provider identity already belongs to another event"
        )

    locked = session.execute(_CONVERSATION_LOCK_SQL, identity_params).mappings().first()
    if locked is None:
        raise OutboundEventPersistenceFailed(
            "conversation lock is missing for outbound event"
        )

    existing = session.execute(_EVENT_LOOKUP_SQL, identity_params).mappings().first()
    if existing is not None:
        if _is_same_outbound_event(existing, claimed=claimed):
            return True
        raise OutboundEventPersistenceFailed(
            "outbound provider identity already belongs to another event"
        )

    sequence = int(locked["next_sequence"])
    session.execute(
        _CONVERSATION_ADVANCE_SQL,
        {**identity_params, "next_sequence": sequence + 1},
    )
    if claimed.action == "send_media":
        message = {
            "audioMessage": {"mimetype": claimed.mime_type or "audio/mpeg"}
        }
        message_type = "audioMessage"
    else:
        message = {"conversation": claimed.text}
        message_type = "conversation"
    payload = EvolutionWebhook.model_validate(
        {
            "event": "send.message",
            "instance": claimed.instance,
            "data": {
                "key": {
                    "id": message_id,
                    "remoteJid": claimed.destination_jid,
                    "fromMe": True,
                },
                "message": message,
                "messageType": message_type,
            },
        }
    )
    sanitized_payload = payload.model_dump(
        by_alias=True,
        exclude_none=True,
        mode="json",
    )
    event_hash = hashlib.sha256(
        _canonical_payload_json(sanitized_payload)
    ).hexdigest()
    session.execute(
        _OUTBOUND_EVENT_INSERT_SQL,
        {
            **identity_params,
            "phone": normalize_phone(claimed.destination_jid),
            "message_type": payload.data.message_type,
            "direction": "outbound",
            "text_content": claimed.text,
            "push_name": None,
            "sanitized_payload": sanitized_payload,
            "event_type": "send.message",
            "event_hash": event_hash,
            "conversation_sequence": sequence,
            "ingest_workflow_id": stable_id("outbound", claimed.idempotency_key),
        },
    )
    inserted = session.execute(_EVENT_LOOKUP_SQL, identity_params).mappings().first()
    if not _is_same_outbound_event(inserted, claimed=claimed):
        raise OutboundEventPersistenceFailed(
            "outbound event insert did not preserve the sent reply"
        )
    return True


def finalize_outbox_with_session(
    session: Session,
    *,
    claimed: ClaimedOutbox,
    outcome: OutboxFinalization,
) -> OutboxFinalization:
    """Commit exactly one terminal state owned by the active lease."""
    if outcome.status == "sent" and claimed.kind == "customer" and claimed.text:
        provider_message_id = outcome.provider_message_id
        if not provider_message_id:
            raise OutboundEventPersistenceFailed(
                "sent customer reply is missing provider identity"
            )
        # Event + terminal outbox state share one SERIALIZABLE transaction.
        # A failure rolls both back so the job can be retried safely.
        record_outbound_event_with_session(
            session,
            claimed=claimed,
            provider_message_id=provider_message_id,
        )
    finalized_id = session.execute(
        _OUTBOX_FINALIZE_SQL,
        {
            "outbox_id": claimed.outbox_id,
            "tenant_id": claimed.tenant_id,
            "idempotency_key": claimed.idempotency_key,
            "lease_owner": claimed.lease_owner,
            "delivery_status": outcome.status,
            "provider_message_id": outcome.provider_message_id,
            "provider_result": outcome.provider_result,
        },
    ).scalar_one_or_none()
    if finalized_id != claimed.outbox_id:
        raise OutboxStateConflict("outbox lease no longer owns terminal transition")
    return outcome


@transaction(isolation_level="SERIALIZABLE")
def finalize_outbox_txn(
    claimed: ClaimedOutbox, outcome: OutboxFinalization
) -> OutboxFinalization:
    return finalize_outbox_with_session(
        sql_session(), claimed=claimed, outcome=outcome
    )


_STATUS_INSERT_SQL = text(
    """
    /* status_audits.insert */
    INSERT INTO genius_next.status_audits (
        tenant_id, turn_id, phone, source_sequence, customer_status,
        agent_commitment, confidence, evidence_source, model, audited_at
    ) VALUES (
        :tenant_id, :turn_id, :phone, :source_sequence, :customer_status,
        :agent_commitment, :confidence, :evidence_source, :model,
        COALESCE(CAST(:audited_at AS timestamptz), now())
    )
    ON CONFLICT (tenant_id, turn_id) DO NOTHING
    RETURNING turn_id
    """
)

# 20/out/2026: so roda quando StatusResult.evidence vem preenchido, ou seja,
# tenant em STATUS_AGENT_*. Com as flags vazias esta SQL nunca executa, e a
# coluna (migration 081) pode nao existir ainda.
_STATUS_EVIDENCE_SQL = text(
    """
    /* status_audits.evidence */
    UPDATE genius_next.status_audits
    SET evidence = CAST(:evidence AS jsonb)
    WHERE tenant_id = :tenant_id AND turn_id = :turn_id
    """
)

_STATUS_CLIENT_APPLY_SQL = text(
    """
    /* client.apply_status */
    UPDATE genius_next.clients AS c
    SET status = CASE
            WHEN c.status = 'block' THEN c.status
            WHEN c.status IN ('win', 'loss')
                 AND :customer_status = 'open' THEN c.status
            ELSE :customer_status
        END,
        automation_state = CASE
            -- 27/ago/2026: mesma protecao de _CLIENT_APPLY_DECISION_SQL --
            -- crew nunca sai por classificador, so por acao humana.
            WHEN automation_state = 'crew' THEN automation_state
            WHEN (
                CASE
                    WHEN c.status = 'block' THEN c.status
                    WHEN c.status IN ('win', 'loss')
                         AND :customer_status = 'open' THEN c.status
                    ELSE :customer_status
                END
            ) IN ('loss', 'block') THEN 'loss'
            ELSE automation_state
        END,
        metadata = COALESCE(c.metadata, '{}'::jsonb) || jsonb_build_object(
            'status_audit', jsonb_build_object(
                -- 29/ago/2026: IndeterminateDatatype nao era so' o agent_commitment
                -- (Python None). Qualquer parametro usado SO' aqui dentro de
                -- jsonb_build_object, sem outro contexto tipado na query, tem o
                -- mesmo problema -- Postgres nao infere tipo de generico/ANY sem
                -- cast explicito. customer_status fica sem CAST porque tambem
                -- aparece nas comparacoes de c.status acima, que ja tipam ele.
                'turn_id', CAST(:turn_id AS text),
                'customer_status', :customer_status,
                'agent_commitment', CAST(:agent_commitment AS text),
                'confidence', CAST(:confidence AS double precision),
                'evidence_source', CAST(:evidence_source AS text),
                'model', CAST(:model AS text)
            )
        ),
        updated_at = now()
    WHERE c.tenant_id = :tenant_id
      AND c.phone = :phone
      AND NOT EXISTS (
          -- 11/nov/2026 (alerta sales_status_classificador): o guarda antigo
          -- comparava a sequencia do audit novo com a maior sequencia gravada do
          -- mesmo phone, mas a sequencia e POR CONVERSA e reinicia quando a
          -- conversa/instancia e recriada (instancia 'genius' religada em 02/nov).
          -- Com audits antigos do mesmo phone ate a seq 103, todo audit novo era
          -- "stale", o job falhava com ClientStateMissing e o rollback descartava
          -- a propria linha do audit — turno ficava sem auditoria. A ordem real
          -- entre turnos e turns.created_at (monotona entre epocas e entre
          -- instancias do mesmo phone): so pula o apply quando o audit de um
          -- turno MAIS RECENTE ja existe.
          SELECT 1
          FROM genius_next.status_audits newer
          JOIN genius_next.turns nt
            ON nt.tenant_id = newer.tenant_id AND nt.id = newer.turn_id
          WHERE newer.tenant_id = :tenant_id
            AND newer.phone = :phone
            AND nt.created_at > (
                SELECT this_turn.created_at
                FROM genius_next.turns this_turn
                WHERE this_turn.tenant_id = :tenant_id
                  AND this_turn.id = :turn_id
            )
      )
    RETURNING c.phone
    """
)


# 11/nov/2026: diagnostico do ramo applied is None — clients sem linha
# (anomalia de integridade, merece destaque em log) ou audit de turno mais novo
# ja aplicado (corrida legitima de jobs, benigna). O guarda e o MESMO da
# _STATUS_CLIENT_APPLY_SQL, por turns.created_at: a sequencia por conversa
# reinicia quando a conversa/instancia e recriada e nao serve de ordem entre
# epocas.
_STATUS_APPLY_SKIP_SQL = text(
    """
    /* status.apply_skip_diag */
    SELECT CASE
        WHEN NOT EXISTS (
            SELECT 1 FROM genius_next.clients c
            WHERE c.tenant_id = :tenant_id AND c.phone = :phone
        ) THEN 'clients_ausente'
        WHEN EXISTS (
            SELECT 1
            FROM genius_next.status_audits newer
            JOIN genius_next.turns nt
              ON nt.tenant_id = newer.tenant_id AND nt.id = newer.turn_id
            WHERE newer.tenant_id = :tenant_id
              AND newer.phone = :phone
              AND nt.created_at > (
                  SELECT this_turn.created_at
                  FROM genius_next.turns this_turn
                  WHERE this_turn.tenant_id = :tenant_id
                    AND this_turn.id = :turn_id
              )
        ) THEN 'turno_mais_novo_ja_aplicado'
        ELSE 'desconhecido'
    END
    """
)

def _record_status_evidence(
    session: Session, *, tenant_id: str, turn_id: str, evidence: dict[str, Any]
) -> None:
    """Grava a evidencia num savepoint: se a migration 081 nao foi aplicada
    (flag ligada antes da coluna), so a evidencia se perde -- o status do
    cliente continua sendo aplicado na mesma transacao."""
    params = {
        "tenant_id": tenant_id,
        "turn_id": turn_id,
        "evidence": json.dumps(evidence, ensure_ascii=False),
    }
    begin_nested = getattr(session, "begin_nested", None)
    if begin_nested is None:
        session.execute(_STATUS_EVIDENCE_SQL, params)
        return
    try:
        with begin_nested():
            session.execute(_STATUS_EVIDENCE_SQL, params)
    except Exception:
        logger.warning(
            "status evidence nao gravada (migration 081 aplicada?) tenant=%s turn=%s",
            tenant_id,
            turn_id,
            exc_info=True,
        )


def apply_status_with_session(
    session: Session,
    *,
    tenant_id: str,
    turn_id: str,
    phone: str,
    result: Any,
    model: str | None,
    audited_at: str | None = None,
    source_sequence: int = 0,
) -> bool:
    """Persist one status audit and apply it only if it is still current.

    The unique turn key makes job retries idempotent.  The insert happens
    before the client update so any insert failure prevents domain mutation;
    the surrounding SERIALIZABLE transaction rolls both operations back.

    11/nov/2026: o apply em clients que casa zero linhas NAO falha mais o
    job. Audit de turno mais novo (ordem real = turns.created_at; a sequencia
    por conversa reinicia quando a conversa/instancia e recriada) ou clients
    sem linha apenas pulam o apply: a linha do audit fica gravada como
    evidencia e o job termina ok, em vez de falhar sem retry e descartar o
    proprio audit no rollback. Retorno: True quando um audit novo foi gravado
    (apply aplicado ou pulado), False quando o audit ja existia (idempotencia
    de replay).
    """
    if not tenant_id or not turn_id or not phone:
        raise ValueError("status audit identity is required")
    if source_sequence < 0:
        raise ValueError("status audit sequence must be non-negative")

    values = result.model_dump(mode="json")
    inserted = session.execute(
        _STATUS_INSERT_SQL,
        {
            "tenant_id": tenant_id,
            "turn_id": turn_id,
            "phone": phone,
            "source_sequence": source_sequence,
            "customer_status": values["customer_status"],
            "agent_commitment": values.get("agent_commitment"),
            "confidence": values.get("confidence", 0.0),
            "evidence_source": values.get("evidence_source", "default"),
            "model": model,
            "audited_at": audited_at,
        },
    ).scalar_one_or_none()
    if inserted is None:
        return False

    if values.get("evidence") is not None:
        _record_status_evidence(
            session,
            tenant_id=tenant_id,
            turn_id=turn_id,
            evidence=values["evidence"],
        )

    applied = session.execute(
        _STATUS_CLIENT_APPLY_SQL,
        {
            "tenant_id": tenant_id,
            "turn_id": turn_id,
            "phone": phone,
            "customer_status": values["customer_status"],
            "agent_commitment": values.get("agent_commitment"),
            "confidence": values.get("confidence", 0.0),
            "evidence_source": values.get("evidence_source", "default"),
            "model": model,
        },
    ).scalar_one_or_none()
    if applied is None:
        # 11/nov/2026: apply sem linha NAO e mais falha de job. Antes o
        # ClientStateMissing rolava a transacao SERIALIZABLE e descartava a
        # propria linha do audit (job sem retry = perda definitiva) — foi o que
        # abriu o alerta sales_status_classificador. A linha do audit e a
        # evidencia por turno: fica gravada; o apply em clients e derivado e
        # pode ser pulado com seguranca.
        motivo = session.execute(
            _STATUS_APPLY_SKIP_SQL,
            {"tenant_id": tenant_id, "phone": phone, "turn_id": turn_id},
        ).scalar_one_or_none()
        logger.warning(
            "status audit %s: apply em clients pulado (%s); linha do audit preservada",
            turn_id,
            motivo or "desconhecido",
        )
    return True


_INSERT_CONVERSATION_FACT_SQL = text(
    """
    /* facts.insert */
    INSERT INTO genius_next.conversation_facts
        (tenant_id, phone, turn_id, ordinal, tipo, detalhe)
    VALUES (:tenant_id, :phone, :turn_id, :ordinal, :tipo, :detalhe)
    ON CONFLICT (tenant_id, turn_id, ordinal) DO NOTHING
    """
)


def record_conversation_facts_with_session(
    session: Any,
    *,
    tenant_id: str,
    phone: str,
    turn_id: str,
    fatos: list[tuple[str, str]],
) -> int:
    """Append-only e idempotente no replay (UNIQUE tenant, turn, ordinal)."""
    gravados = 0
    for ordinal, (tipo, detalhe) in enumerate(fatos):
        gravados += session.execute(
            _INSERT_CONVERSATION_FACT_SQL,
            {
                "tenant_id": tenant_id,
                "phone": phone,
                "turn_id": turn_id,
                "ordinal": ordinal,
                "tipo": tipo,
                "detalhe": detalhe[:240],
            },
        ).rowcount
    return gravados


@transaction(isolation_level="SERIALIZABLE")
def record_conversation_facts_txn(
    tenant_id: str, phone: str, turn_id: str, fatos: list[tuple[str, str]]
) -> int:
    return record_conversation_facts_with_session(
        sql_session(), tenant_id=tenant_id, phone=phone, turn_id=turn_id, fatos=fatos
    )


@transaction(isolation_level="SERIALIZABLE")
def apply_status_txn(
    tenant_id: str,
    turn_id: str,
    phone: str,
    result: Any,
    model: str | None,
    audited_at: str | None = None,
    source_sequence: int = 0,
) -> bool:
    return apply_status_with_session(
        sql_session(),
        tenant_id=tenant_id,
        turn_id=turn_id,
        phone=phone,
        result=result,
        model=model,
        audited_at=audited_at,
        source_sequence=source_sequence,
    )
