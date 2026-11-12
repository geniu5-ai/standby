"""Middleware de tarefa do auto-aprendizado (assíncrono: roda no loop, junto de tarefas ``async``).

Registrado **por tarefa** (``task_middleware=[...]``), nunca no worker inteiro: o worker do Next é
compartilhado com turno e envio, e o middleware global mexeria no caminho do cliente.

Regras (docs do Procrastinate): chamar ``call_next()`` e devolver o resultado; deixar a exceção
subir (suprimir marcaria o job como sucesso e pularia retry e abort); limpeza em ``finally``.
Nada de conteúdo: só ids, duração e o tipo da exceção.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from app import observability

logger = logging.getLogger(__name__)


async def instrumenta_job(call_next: Any, context: Any, worker: Any) -> Any:
    job = context.job
    tenant = (job.task_kwargs or {}).get("tenant_id")
    inicio = time.perf_counter()
    resultado = "ok"
    with observability.message_span(
        "learning.job", task=context.task.name, tenant_id=tenant, job_id=job.id, tentativa=job.attempts
    ):
        try:
            return await call_next()
        except BaseException as exc:
            resultado = type(exc).__name__
            raise
        finally:
            logger.info(
                "learning job=%s task=%s tenant=%s tentativa=%s resultado=%s ms=%d",
                job.id,
                context.task.name,
                tenant,
                job.attempts,
                resultado,
                int((time.perf_counter() - inicio) * 1000),
            )
