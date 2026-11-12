"""Fila e relogios do Next sem DBOS: Procrastinate 3.10 sobre o Postgres do proprio Next (31/out/2026).

Um job e uma linha em ``procrastinate_jobs``. O que o DBOS fazia por replay, o Next faz por duas
regras: (1) a idempotencia vem das chaves unicas do banco, nunca da memoria do worker; (2) o estado
que precisa sobreviver a uma queda vive em tabelas (turns, outbox, follow_up_lifecycles) ou nos
argumentos do proximo job.

Filas (cada uma com o seu worker dentro do servico, ``jobs_worker`` no lifespan):
- ``turn``: turnos ao vivo e envios de follow-up. ``lock`` = conversa: um por vez, prioridade
  ``TURN_PRIORITY_LIVE`` na frente de ``TURN_PRIORITY_FOLLOWUP`` (no Procrastinate MAIOR sai antes,
  ao contrario do DBOS).
- ``aux``: midia, auditoria de status, resumo, Rescue e Manager.
- ``cron``: relogios (``@jobs.periodic``, UTC, cinco campos) e os backups.

Um cron e um ``@jobs.periodic`` em cima de um ``@jobs.task``: a cada minuto cheio o worker insere
o job do horario e o ``UNIQUE (task_name, periodic_id, defer_timestamp)`` de
``procrastinate_periodic_defers`` garante um job por horario, mesmo com dois workers.

Regras de uso:
- Corpo sincrono roda numa thread do worker e pode chamar ``@transaction``.
- ``defer_once`` e a forma de criar job que pode ser pedido duas vezes: ``queueing_lock`` recusa o
  segundo enquanto o primeiro espera.
- NUNCA agende no futuro um job com ``lock``: ele segura a conversa ate rodar. Quem espera
  (follow-up) acorda sem lock; so o envio leva lock. Unica excecao: o debounce do turno, que segura
  a conversa por poucos segundos de proposito.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import procrastinate
import psycopg_pool
from procrastinate import builtin_tasks
from procrastinate.exceptions import AlreadyEnqueued
from sqlalchemy.engine import make_url

from app.settings import get_settings

logger = logging.getLogger(__name__)

CRON_QUEUE = "cron"
TURN_QUEUE = "turn"
AUX_QUEUE = "aux"
# No Procrastinate MAIOR numero sai primeiro. O follow-up nunca passa na frente do paciente.
TURN_PRIORITY_LIVE = 10
TURN_PRIORITY_FOLLOWUP = 0

# fila -> (concorrencia, segundos entre consultas ao banco). Job agendado so acorda na consulta,
# entao a fila do turno consulta a cada 1 s (o debounce de 8 s depende disso).
# Neon free: qualquer consulta reinicia o timer de 5 min para suspender o compute, e o plano grátis
# tem 100 CU-h/mês. Por isso o polling é lento: a fila acorda por NOTIFY (conexão direta, sem -pooler);
# o polling é só a rede de segurança. Não baixe estes números sem refazer a conta de CU-h.
# Neon grátis suspende após 5 min sem consulta. Cada poll é uma consulta: com polling curto o banco
# nunca dorme e estoura os 100 CU-h/mês. A fila acorda por NOTIFY (conexão direta); o poll é só rede de
# segurança, e por isso é longo. Turno: 60 s de atraso máximo se o NOTIFY falhar.
WORKERS: dict[str, tuple[int, float]] = {
    TURN_QUEUE: (16, 60.0),
    AUX_QUEUE: (6, 300.0),
    CRON_QUEUE: (2, 600.0),
}
JOBS_RETENTION_HOURS = 7 * 24
# Tarefas que seguram uma cadeia (follow-up, envio, Manager): uma falha de banco ou de rede por um
# instante nao pode matar a cadeia para sempre. Seguras por serem idempotentes (recibo/outbox).
RETRY_CADEIA = procrastinate.RetryStrategy(max_attempts=5, exponential_wait=10)
_SHUTDOWN_GRACE_S = 10.0

# Modulos que registram tasks; o worker os importa ao abrir.
TASK_MODULES = [
    "app.agent.lesson_scheduler",
    "app.agent.lead_summary",
    "app.durable.pendentes",
    "app.durable.manager_tick",
    "app.durable.manager",
    "app.durable.reaper",
    "app.durable.flow",
    "app.durable.followup",
    "app.durable.media",
    "app.durable.summary_backfill",
    "app.status.workflow",
]


def _conninfo() -> str:
    url = make_url(get_settings().DATABASE_URL).set(drivername="postgresql")
    # Neon: o host -pooler (PgBouncer) não entrega LISTEN/NOTIFY. Conexão direta = mesmo banco.
    host = url.host or ""
    if "-pooler." in host:
        url = url.set(host=host.replace("-pooler.", ".", 1))
    return url.render_as_string(hide_password=False)


def _pool(**kwargs: Any) -> psycopg_pool.AsyncConnectionPool:
    # Lido so ao abrir: importar este modulo nao exige DATABASE_URL.
    return psycopg_pool.AsyncConnectionPool(conninfo=_conninfo(), **kwargs)


jobs = procrastinate.App(
    connector=procrastinate.PsycopgConnector(pool_factory=_pool, min_size=1, max_size=6),
    import_paths=TASK_MODULES,
    # Padrao do Procrastinate: tick atrasado mais de 10 min e perdido, e um restart perto de
    # 13:00 UTC pularia a varredura do dia. 3 h recupera so o ultimo tick de cada relogio.
    periodic_defaults={"max_delay": 3 * 3600},
)


def _options(
    lock: str | None,
    queueing_lock: str | None,
    priority: int | None,
    schedule_in: int | float | None,
) -> dict[str, Any]:
    opts: dict[str, Any] = {}
    if lock is not None:
        opts["lock"] = lock
    if queueing_lock is not None:
        opts["queueing_lock"] = queueing_lock
    if priority is not None:
        opts["priority"] = priority
    if schedule_in is not None:
        opts["schedule_in"] = {"seconds": max(0, int(schedule_in))}
    return opts


def _submit(task: Any, opts: dict[str, Any], kwargs: dict[str, Any]) -> None:
    task.configure(**opts).defer(**kwargs)


def defer_once(
    task: Any,
    *,
    lock: str | None = None,
    queueing_lock: str | None = None,
    priority: int | None = None,
    schedule_in: int | float | None = None,
    **kwargs: Any,
) -> bool:
    """Cria o job; ``False`` quando ja havia um igual esperando (``queueing_lock``). Sincrono."""
    try:
        _submit(task, _options(lock, queueing_lock, priority, schedule_in), kwargs)
    except AlreadyEnqueued:
        return False
    return True


@jobs.periodic(cron="17 * * * *")
@jobs.task(name="limpa_jobs_antigos", queue=CRON_QUEUE, pass_context=True)
async def limpa_jobs_antigos(context: procrastinate.JobContext, timestamp: int) -> None:
    """Apaga jobs concluidos com mais de 7 dias; os que falharam ficam para investigar."""
    await builtin_tasks.remove_old_jobs(context, max_hours=JOBS_RETENTION_HOURS)


@jobs.periodic(cron="*/30 * * * *")
@jobs.task(name="reenfileira_travados", queue=CRON_QUEUE)
async def reenfileira_travados(timestamp: int) -> None:
    """Job ``doing`` de worker que morreu (deploy, kill -9) volta para a fila.

    Sem isto o DBOS recuperava o workflow no boot; aqui nada recupera sozinho. Tudo o que roda
    em job e idempotente pelas chaves do banco, entao repetir e seguro.
    """
    for job in list(await jobs.job_manager.get_stalled_jobs()):
        logger.warning("job travado reenfileirado: %s #%s", job.task_name, job.id)
        await jobs.job_manager.retry_job(job)


@asynccontextmanager
async def jobs_worker() -> AsyncIterator[None]:
    """Abre a fila e roda um worker por fila enquanto o servico estiver de pe."""
    async with jobs.open_async():
        workers = [
            asyncio.create_task(
                jobs.run_worker_async(
                    queues=[queue],
                    name=f"worker-{queue}",
                    concurrency=concurrency,
                    fetch_job_polling_interval=polling,
                    install_signal_handlers=False,
                    shutdown_graceful_timeout=_SHUTDOWN_GRACE_S,
                ),
                name=f"procrastinate-{queue}",
            )
            for queue, (concurrency, polling) in WORKERS.items()
        ]
        try:
            yield
        finally:
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
