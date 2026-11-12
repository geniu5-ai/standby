"""Tarefas Procrastinate do auto-aprendizado. Fila própria (``learn``), tudo desligado por padrão.

Como cada recurso do Procrastinate 3.10 entra aqui (docs "advanced"):
- ``learn`` (fila própria, concorrência 2, prioridade -10): aprendizado nunca disputa vez com turno;
- ``queueing_lock`` por tenant: no máximo UMA rodada esperando por tenant (o tick pode repetir);
- ``lock`` por tenant: nunca duas rodadas do mesmo tenant ao mesmo tempo. Só vale porque a rodada
  é agendada para já: lock com ``schedule_in`` seguraria o tenant até rodar (regra de ``app/jobs.py``);
- ``retry`` com estratégia própria (``retry.py``): só erro transitório, espera exponencial;
- ``task_middleware`` por tarefa (``middleware.py``): span e log sem conteúdo;
- abort: o operador manda abortar, o ``CancelledError`` fecha a rodada como ``aborted``;
- periódico (``@jobs.periodic``): o tick é barato e roda na fila ``cron``; ``max_delay`` herdado do app.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import procrastinate
from procrastinate.exceptions import AlreadyEnqueued

from app.jobs import CRON_QUEUE, LEARN_QUEUE, jobs
from app.learning import repo
from app.learning.llm import LLMOmniRoute
from app.learning.middleware import instrumenta_job
from app.learning.models import Insight
from app.learning.reflect import LLM
from app.learning.retry import LEARN_PRIORITY, RETRY_LEARNING
from app.learning.runner import run_reflection
from app.settings import Settings, get_settings

logger = logging.getLogger(__name__)


def tenants_permitidos(settings: Settings) -> list[str]:
    """Lista explícita. Vazio = nenhum tenant (nunca 'todos'): ligar um tenant é decisão consciente."""
    return [t.strip() for t in settings.LEARNING_TENANTS.split(",") if t.strip()]


class ArmazemPostgres:
    """Grava insights como ``proposed`` (nunca ativos). Implementa o protocolo de ``store.py``."""

    def __init__(self, run_id: int) -> None:
        self._run_id = run_id

    async def gravar(self, tenant_id: str, insights: list[Insight]) -> int:
        return await asyncio.to_thread(repo.gravar_insights, tenant_id, self._run_id, insights)


def _llm(settings: Settings) -> LLM:
    chave = settings.OMNIROUTE_API_KEY
    if chave is None:
        raise RuntimeError("OMNIROUTE_API_KEY ausente")
    return LLMOmniRoute(
        base_url=settings.OMNIROUTE_BASE_URL,
        api_key=chave.get_secret_value(),
        model=settings.LEARNING_MODEL,
        timeout=settings.LEARNING_TIMEOUT_SECONDS,
        max_tokens=settings.LEARNING_MAX_TOKENS,
    )


@jobs.periodic(cron="23 */6 * * *")
@jobs.task(name="learning_tick", queue=CRON_QUEUE)
async def learning_tick(timestamp: int) -> None:
    """A cada 6 h: para cada tenant habilitado com movimento, pede UMA rodada (idempotente)."""
    settings = get_settings()
    if not settings.LEARNING_ENABLED:
        return
    permitidos = set(tenants_permitidos(settings))
    if not permitidos:
        return
    com_movimento = await asyncio.to_thread(
        repo.tenants_com_movimento, settings.LEARNING_WINDOW_HOURS, settings.LEARNING_MIN_OUTCOMES
    )
    for tenant in (t for t in com_movimento if t in permitidos):
        try:
            await learning_reflect.configure(
                lock=f"learning:{tenant}", queueing_lock=f"learning:{tenant}", priority=LEARN_PRIORITY
            ).defer_async(tenant_id=tenant)
        except AlreadyEnqueued:
            logger.info("learning_tick: rodada de %s ja esta na fila", tenant)


@jobs.task(
    name="learning_reflect",
    queue=LEARN_QUEUE,
    retry=RETRY_LEARNING,
    pass_context=True,
    task_middleware=[instrumenta_job],
)
async def learning_reflect(
    context: procrastinate.JobContext,
    tenant_id: str,
    janela_horas: int | None = None,
) -> dict[str, Any]:
    """Uma rodada de REFLECT de um tenant. Os argumentos do job são só JSON (tenant e janela)."""
    settings = get_settings()
    horas = janela_horas or settings.LEARNING_WINDOW_HOURS
    run_id = await asyncio.to_thread(
        repo.iniciar_run, tenant_id, context.job.id, horas, settings.LEARNING_DRY_RUN, settings.LEARNING_MODEL
    )
    outcomes_n = insights_n = salvos = 0
    status, erro = "ok", None
    try:
        if not settings.LEARNING_ENABLED or tenant_id not in tenants_permitidos(settings):
            status = "skipped"
            return {"run_id": run_id, "status": status}
        outcomes = await asyncio.to_thread(repo.coletar_outcomes, tenant_id, horas, settings.LEARNING_MAX_OUTCOMES)
        outcomes_n = len(outcomes)
        if outcomes_n < settings.LEARNING_MIN_OUTCOMES:
            status = "skipped"
            return {"run_id": run_id, "status": status, "outcomes": outcomes_n}
        rel = await run_reflection(
            tenant_id,
            outcomes,
            _llm(settings),
            ArmazemPostgres(run_id),
            dry_run=settings.LEARNING_DRY_RUN,
        )
        insights_n, salvos = len(rel.insights), rel.gravados
        return {"run_id": run_id, "status": status, "outcomes": outcomes_n, "insights": insights_n, "salvos": salvos}
    except asyncio.CancelledError:
        # Abort pedido pelo operador (cancel_job_by_id(abort=True)): fecha a rodada e deixa abortar.
        status, erro = "aborted", "CancelledError"
        raise
    except Exception as exc:
        status, erro = "failed", type(exc).__name__  # so o tipo: a mensagem pode ter trecho de conversa
        raise
    finally:
        await asyncio.shield(
            asyncio.to_thread(repo.finalizar_run, run_id, status, outcomes_n, insights_n, salvos, erro)
        )


@jobs.periodic(cron="41 * * * *")
@jobs.task(name="learning_promote", queue=LEARN_QUEUE, queueing_lock="learning_promote")
async def learning_promote(timestamp: int) -> None:
    """De hora em hora: insight APROVADO por humano vira lição (e só então chega ao agente)."""
    if not get_settings().LEARNING_ENABLED:
        return
    n = await asyncio.to_thread(repo.promover_aprovados)
    if n:
        logger.info("learning_promote: %d insight(s) viraram licao", n)
