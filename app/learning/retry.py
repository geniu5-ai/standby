"""Estratégia de retry do auto-aprendizado (``BaseRetryStrategy`` do Procrastinate 3.10).

Por que não ``RetryStrategy(max_attempts=..., exponential_wait=...)`` pronto:
- só erro **transitório** vale repetir (LLM fora, 429, banco caiu por um instante). Erro de
  configuração ou de validação repetido só enche a fila;
- cada repetição desce a prioridade: aprendizado nunca disputa vez com turno ao vivo;
- uma rodada que o operador mandou abortar nunca é repetida (o Procrastinate já garante).
"""

from __future__ import annotations

from procrastinate import BaseRetryStrategy, RetryDecision
from procrastinate.jobs import Job
from sqlalchemy.exc import OperationalError

from app.learning.llm import LLMTransitorio

LEARN_PRIORITY = -10          # abaixo do follow-up (0) e do turno ao vivo (10)
LEARN_PRIORITY_FLOOR = -50    # cada repeticao desce um pouco, ate aqui

TRANSITORIOS: tuple[type[BaseException], ...] = (LLMTransitorio, OperationalError, TimeoutError, ConnectionError)


class LearningRetry(BaseRetryStrategy):
    max_attempts = 4
    espera_base_s = 30
    espera_max_s = 1800

    def get_retry_decision(self, *, exception: BaseException, job: Job) -> RetryDecision | None:
        if job.attempts >= self.max_attempts:
            return None
        if not isinstance(exception, TRANSITORIOS):
            return None
        espera = min(self.espera_base_s * (2**job.attempts), self.espera_max_s)
        return RetryDecision(
            retry_in={"seconds": espera},
            priority=max(job.priority - 1, LEARN_PRIORITY_FLOOR),
        )


RETRY_LEARNING = LearningRetry()
