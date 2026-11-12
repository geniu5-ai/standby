"""Rede de seguranca do Next sem DBOS (31/out/2026): nada fica preso porque um worker morreu.

O DBOS recuperava workflow ``PENDING`` no boot. Aqui o que importa vive em tabelas, entao dois
varredores a cada minuto olham o banco e re-agendam o que ficou no meio:

- ``despacha_outbox_pendente``: linha do outbox ``pending`` (ou ``claimed`` com lease vencido) e
  sem ``provider_message_id`` ha mais de ``OUTBOX_PENDENTE_APOS_S``. O despacho e idempotente
  (lease + chave unica), entao repetir nunca duplica envio.
- ``retoma_turnos_presos``: turno ``claimed``/``processing`` ha mais de ``TURNO_PRESO_APOS_S`` sem
  nenhum job da conversa vivo. Antes ele so era retomado quando o paciente escrevia de novo.

Ambos so agendam; quem faz o trabalho e o mesmo codigo do caminho normal.
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


@jobs.periodic(cron="*/30 * * * *")  # Neon free: a cada 30 min
@jobs.task(name="reaper", queue=CRON_QUEUE)
def reaper(timestamp: int) -> None:
    for varredura in (despacha_outbox_pendente, retoma_turnos_presos):
        try:
            varredura()
        except Exception:
            logger.exception("reaper: %s falhou", varredura.__name__)
