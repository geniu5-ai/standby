"""Juiz por turno (DeepEval), em tempo real mas FORA do caminho da resposta.

Regras:
- Desligado por padrao (``TURN_JUDGE_ENABLED``). Ligado, julga uma fracao dos turnos
  (``TURN_JUDGE_SAMPLE_RATE``) numa thread de fundo; o cliente nunca espera por ele.
- Fila pequena e limitada: se estiver cheia, o turno e descartado, nunca enfileira sem fim.
- Nada de conteudo no log: so tenant, turno, nota, limiar, passou/nao e o nome da metrica.
  Nem a mensagem do cliente, nem a resposta, nem o prompt saem daqui.
- Telemetria do DeepEval desligada antes de ele ser importado: ela envia eventos anonimos
  para um servico de analytics de terceiros (PostHog, EUA). Para dados de paciente, nao.
- Usa o gateway OmniRoute de producao (mesmo provedor do agente): nenhum terceiro novo.
"""

from __future__ import annotations

import logging
import os
import random
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

logger = logging.getLogger(__name__)

_MAX_PENDENTES = 8
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="turn-judge")
_vagas = threading.BoundedSemaphore(_MAX_PENDENTES)


def _tenants(valor: str) -> set[str]:
    return {t.strip() for t in str(valor or "").split(",") if t.strip()}


def _texto_do_cliente(turn: Any) -> str:
    partes = []
    for ev in getattr(turn, "events", None) or []:
        if getattr(ev, "direction", None) == "inbound" and getattr(ev, "text_content", None):
            partes.append(str(ev.text_content))
    return "\n".join(partes)


def schedule(*, settings: Any, tenant_id: str, turn: Any, resposta: str) -> bool:
    """Agenda o julgamento do turno. Nunca levanta excecao e nunca bloqueia.

    Devolve ``True`` se o turno entrou na fila.
    """
    try:
        if not getattr(settings, "TURN_JUDGE_ENABLED", False):
            return False
        if tenant_id not in _tenants(getattr(settings, "TURN_JUDGE_TENANTS", "")):
            return False  # lista vazia = nenhum tenant
        if random.random() >= float(settings.TURN_JUDGE_SAMPLE_RATE):
            return False
        pergunta = _texto_do_cliente(turn)
        if not pergunta or not resposta:
            return False
        if not _vagas.acquire(blocking=False):
            logger.info("turn_judge: fila cheia, turno descartado tenant=%s", tenant_id)
            return False
        try:
            _executor.submit(
                _julgar_e_liberar,
                settings,
                tenant_id,
                str(getattr(turn, "turn_id", "")),
                pergunta,
                resposta,
            )
        except RuntimeError:
            _vagas.release()
            return False
        return True
    except Exception:  # noqa: BLE001 - juiz nunca derruba o turno
        logger.warning("turn_judge: falha ao agendar", exc_info=False)
        return False


def _julgar_e_liberar(settings: Any, tenant_id: str, turn_id: str, pergunta: str, resposta: str) -> None:
    try:
        _julgar(settings, tenant_id, turn_id, pergunta, resposta)
    except Exception as exc:  # noqa: BLE001
        # So o tipo do erro: a mensagem pode conter trecho de resposta do modelo.
        logger.warning("turn_judge: falha no julgamento tenant=%s (%s)", tenant_id, type(exc).__name__)
    finally:
        _vagas.release()


def _julgar(settings: Any, tenant_id: str, turn_id: str, pergunta: str, resposta: str) -> None:
    # Antes do import do deepeval: ele le a variavel na criacao das settings.
    os.environ.setdefault("DEEPEVAL_TELEMETRY_OPT_OUT", "1")
    from deepeval.metrics import TurnRelevancyMetric
    from deepeval.models import OpenAIModel
    from deepeval.test_case import ConversationalTestCase, Turn

    chave = settings.OMNIROUTE_API_KEY
    if chave is None:
        return
    juiz = OpenAIModel(
        model=settings.TURN_JUDGE_MODEL,
        api_key=chave.get_secret_value(),
        base_url=settings.OMNIROUTE_BASE_URL,
        temperature=0,
        generation_kwargs={"max_completion_tokens": 512},
    )
    caso = ConversationalTestCase(
        turns=[Turn(role="user", content=pergunta), Turn(role="assistant", content=resposta)]
    )
    limiar = float(settings.TURN_JUDGE_THRESHOLD)
    metrica = TurnRelevancyMetric(threshold=limiar, model=juiz, async_mode=False)
    metrica.measure(caso)
    nota = metrica.score
    if nota is not None:
        _gravar_nota(tenant_id, turn_id, float(nota), limiar, settings.TURN_JUDGE_MODEL)
    logger.info(
        "turn_judge tenant=%s turno=%s metrica=TurnRelevancy nota=%.2f limiar=%.2f passou=%s",
        tenant_id,
        turn_id,
        float(nota or 0.0),
        limiar,
        bool(nota is not None and nota >= limiar),
    )


def _gravar_nota(tenant_id: str, turn_id: str, nota: float, limiar: float, modelo: str) -> None:
    """Guarda a nota para o auto-aprendizado (app/learning). Sem tabela ou com erro: so registra o tipo."""
    try:
        from app.learning.repo import METRICA_PADRAO, gravar_nota

        gravar_nota(tenant_id, turn_id, METRICA_PADRAO, nota, limiar, modelo)
    except Exception as exc:  # noqa: BLE001 - persistir a nota nunca derruba o juiz
        logger.warning("turn_judge: nota nao gravada (%s)", type(exc).__name__)
