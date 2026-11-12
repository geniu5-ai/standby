"""Agenda do Manager: um tick unico le o que venceu em ``manager_items``.

20/out/2026 (docs/specs/manager-agenda.md). Em vez de um agendamento
por tarefa, compromissos com hora marcada viram linhas com ``due_at`` e um
unico tick, a cada 60 s, so faz a leitura e dispara um workflow por linha
vencida, com ID estavel (o mesmo item nunca roda duas vezes, nem em replay).
O trabalho de verdade mora no workflow do item, nunca no tick.

Tipo implementado: ``loss_silencio`` -- o follow-up esgotou as tentativas do
provedor e o lead seguiu calado. O item revalida tudo na hora (o lead pode ter
voltado a falar, a equipe pode ter assumido) e so entao aplica o loss pela
mesma transacao do follow-up, que tambem entrega o lead ao Rescue.

O tick tambem substitui o agendamento de 20 s da devolucao da equipe
(``crew_in_sweep``): 4.315 workflows/dia, 27 devolucoes em 7 dias. Agora sao
1.440/dia para os dois servicos. As duas partes rodam isoladas por try/except:
a falha de uma nunca trava a outra (paciente preso em ``crew`` e o pior caso).
A agenda so despacha com ``MANAGER_AGENDA_TENANTS`` preenchido.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text

from app.db import sql_session, transaction
from app.durable.crew_in_sweep import release_stale_crew_in
from app.durable.followup import (
    follow_up_max_attempts,
    mark_loss_and_queue_for_rescue_v2_txn,
    should_continue_follow_up,
)
from app.durable.manager import mark_manager_item_txn
from app.durable.rescue_intake import record_rescue_intake_txn
from app.identity_primitives import stable_id
from app.jobs import AUX_QUEUE, CRON_QUEUE, defer_once, jobs
from app.settings import get_settings

logger = logging.getLogger(__name__)

# Cinco campos, UTC: a cada minuto cheio (o Procrastinate nao tem segundos).
MANAGER_TICK_CRON = "*/30 * * * *"  # Neon free: a cada 30 min (ver app/jobs.py)
# Teto de itens disparados por tick; o resto fica para o proximo minuto.
MANAGER_TICK_LIMIT = 50
# Tipos que o tick executa. Os outros kinds de manager_items (promessa,
# assistencia, indicacao, triagem) seguem com os workflows proprios.
AGENDA_KINDS = ("loss_silencio",)

_DUE_ITEMS_SQL = text(
    """
    /* manager.agenda.due */
    SELECT item_key, tenant_id
    FROM genius_next.manager_items
    WHERE state = 'open'
      AND due_at IS NOT NULL
      AND due_at <= now()
      AND kind = ANY(:kinds)
    ORDER BY due_at
    LIMIT :limit
    """
)


@transaction()
def due_agenda_items_txn(limit: int = MANAGER_TICK_LIMIT) -> list[tuple[str, str]]:
    """(item_key, tenant_id) dos itens vencidos; o filtro por tenant fica no despacho."""
    rows = sql_session().execute(
        _DUE_ITEMS_SQL, {"kinds": list(AGENDA_KINDS), "limit": limit}
    ).all()
    return [(row[0], row[1]) for row in rows]


_LOAD_AGENDA_ITEM_SQL = text(
    """
    /* manager.agenda.load */
    SELECT item_key, tenant_id, phone, remote_jid, instance, provider,
           origin_turn_id, kind, state, payload->>'last_attempt' AS last_attempt
    FROM genius_next.manager_items
    WHERE item_key = :item_key
    """
)


@transaction()
def load_agenda_item_txn(item_key: str) -> dict[str, Any] | None:
    row = sql_session().execute(
        _LOAD_AGENDA_ITEM_SQL, {"item_key": item_key}
    ).mappings().first()
    return dict(row) if row else None


def agenda_item_workflow_id(item_key: str) -> str:
    return stable_id("manager-agenda-run", item_key)


def manager_agenda_item_workflow(item_key: str) -> str:
    """Executa um item vencido. Devolve o desfecho (para log e teste).

    Se este corpo falhar, o item nao pode ficar ``open`` para o tick redisparar
    a cada minuto. Por isso a falha fecha o item como ``stopped``
    (motivo ``erro``) e grita no log, em vez de deixa-lo ``open`` para sempre.
    O lead fica como estava (``open``): o estado seguro.
    """
    item = load_agenda_item_txn(item_key)
    if item is None or item.get("state") != "open":
        return "ignorado"
    try:
        if item.get("kind") == "loss_silencio":
            return _loss_silencio(item_key, item)
        return "ignorado"
    except Exception as exc:
        logger.exception("manager_agenda: item %s falhou; lead segue open", item_key)
        try:
            mark_manager_item_txn(item_key, "stopped", {"motivo": "erro", "erro": type(exc).__name__})
        except Exception:
            logger.exception("manager_agenda: item %s nao pode ser fechado", item_key)
        return "erro"


def _loss_silencio(item_key: str, item: dict[str, Any]) -> str:
    tenant_id = item["tenant_id"]
    origin_turn_id = item["origin_turn_id"]
    if not should_continue_follow_up(tenant_id, item["phone"], origin_turn_id):
        # O lead voltou a falar, a equipe assumiu ou o status ja mudou.
        mark_manager_item_txn(item_key, "stopped", {"motivo": "lead_ativo"})
        return "parado"
    last_attempt = int(item.get("last_attempt") or follow_up_max_attempts(item["provider"]))
    marked = mark_loss_and_queue_for_rescue_v2_txn(tenant_id, origin_turn_id, last_attempt)
    # Sempre: a transacao nao faz nada sem o loss atomico, e uma queda aqui nao perde o Rescue.
    record_rescue_intake_txn(tenant_id, origin_turn_id)
    mark_manager_item_txn(item_key, "done", {"loss": marked})
    return "loss" if marked else "sem_loss"


@jobs.task(name="manager_agenda_item", queue=AUX_QUEUE)
def manager_agenda_item_job(item_key: str) -> None:
    manager_agenda_item_workflow(item_key)


def dispatch_due_agenda_items() -> int:
    """Dispara um workflow por item vencido (ID estavel). So com a agenda ligada.

    O tenant e checado por item: item gravado e depois tenant retirado da flag
    nao executa (fica ``open`` ate a flag voltar), nem aplica loss em clinica
    que nao optou.
    """
    settings = get_settings()
    if not settings.MANAGER_AGENDA_TENANTS.strip():
        return 0
    disparados = 0
    for item_key, tenant_id in due_agenda_items_txn():
        if not settings.manager_agenda_enabled(tenant_id):
            continue
        defer_once(
            manager_agenda_item_job,
            lock=agenda_item_workflow_id(item_key),
            queueing_lock=agenda_item_workflow_id(item_key),
            item_key=item_key,
        )
        disparados += 1
    if disparados:
        logger.info("manager_tick: %d item(ns) da agenda disparado(s)", disparados)
    return disparados


def run_tick() -> dict[str, int]:
    """Um tick: devolucao da equipe e agenda, cada uma isolada da falha da outra."""
    resultado = {"crew_liberadas": 0, "agenda_disparados": 0, "falhas": 0}
    liberadas: list[dict[str, Any]] = []
    try:
        liberadas = release_stale_crew_in()
        resultado["crew_liberadas"] = len(liberadas)
    except Exception:
        resultado["falhas"] += 1
        logger.exception("manager_tick: devolucao da equipe falhou")
    try:
        # 03/nov/2026: fala escrita durante a marcacao foi descartada; ao devolver a conversa,
        # o Manager de pendencias a aborda na hora em vez de esperar a varredura de 30 min.
        from app.durable.pendentes import abordar_liberados

        abordar_liberados(liberadas)
    except Exception:
        resultado["falhas"] += 1
        logger.exception("manager_tick: abordagem dos devolvidos falhou")
    try:
        resultado["agenda_disparados"] = dispatch_due_agenda_items()
    except Exception:
        resultado["falhas"] += 1
        logger.exception("manager_tick: despacho da agenda falhou")
    return resultado


@jobs.periodic(cron=MANAGER_TICK_CRON)
@jobs.task(name="manager_tick", queue=CRON_QUEUE)
def manager_tick(timestamp: int) -> dict[str, int]:
    return run_tick()
