"""REFLECT: transforma outcomes recentes em modelos mentais. Assíncrono, LLM injetado (testável sem rede).

Porta do ``rag_reflect.py`` do Dev, com as correções que aquele job aprendeu da forma mais cara:
- o JSON do LLM vem em cerca ```json: parse do 1º ``[`` ao último ``]``, não regex não-guloso;
- ``finish_reason=length`` trunca o array: o parse falhando devolve lista vazia e **registra** o motivo;
- validação por insight: um item ruim não derruba o lote inteiro.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from typing import Protocol

from pydantic import ValidationError

from app.learning.models import Insight, Outcome

logger = logging.getLogger(__name__)

MAX_OUTCOMES_POR_LOTE = 20
MAX_CHARS_POR_OUTCOME = 900


class LLM(Protocol):
    async def completar(self, prompt: str) -> str: ...


def build_prompt(outcomes: Sequence[Outcome]) -> str:
    """Prompt de REFLECT em português, com os mesmos quatro focos do job do Dev."""
    blocos = []
    for o in list(outcomes)[:MAX_OUTCOMES_POR_LOTE]:
        nota = "sem nota" if o.nota_juiz is None else f"{o.nota_juiz:.2f}"
        motivo = f"\nMotivo do handoff: {o.motivo_handoff}" if o.motivo_handoff else ""
        blocos.append(
            f"--- turno {o.turno_id} | desfecho={o.desfecho} | juiz={nota}{motivo}\n"
            f"Cliente: {o.texto_cliente[:MAX_CHARS_POR_OUTCOME]}\n"
            f"Resposta: {o.resposta[:MAX_CHARS_POR_OUTCOME]}"
        )
    corpo = "\n\n".join(blocos)
    return (
        "Você analisa conversas já resolvidas de um assistente comercial. Encontre padrões que um "
        "próximo atendimento deveria saber. Foque em: (1) padrões que se repetem em vários turnos; "
        "(2) respostas que o juiz pontuou mal e por quê; (3) handoffs que poderiam ter sido evitados "
        "ou que foram necessários; (4) regras operacionais que funcionam ou falham.\n\n"
        "Cada item: titulo (até 80 caracteres), corpo (2 a 5 frases), camada (mundo, experiencia ou "
        "modelo_mental), tags (2 a 4), evidencias (ids dos turnos que sustentam o item).\n"
        "Não invente fato fora dos turnos. Se não houver padrão, devolva [].\n"
        "Responda SOMENTE com um array JSON.\n\n"
        f"Turnos:\n{corpo}\n"
    )


def parse_insights(texto: str) -> list[Insight]:
    """Extrai o array JSON da resposta e valida item a item. Nunca levanta."""
    limpo = re.sub(r"```[a-zA-Z]*", "", texto or "").strip()
    inicio, fim = limpo.find("["), limpo.rfind("]")
    if inicio == -1 or fim <= inicio:
        logger.warning("reflect: resposta sem array JSON (tamanho=%d)", len(texto or ""))
        return []
    try:
        bruto = json.loads(limpo[inicio : fim + 1])
    except json.JSONDecodeError:
        logger.warning("reflect: array JSON invalido (possivel truncamento)")
        return []
    if not isinstance(bruto, list):
        return []
    aceitos: list[Insight] = []
    for item in bruto:
        try:
            aceitos.append(Insight.model_validate(item))
        except ValidationError:
            logger.info("reflect: insight descartado por validacao")
    return aceitos


async def reflect(outcomes: Sequence[Outcome], llm: LLM) -> list[Insight]:
    """Um lote de outcomes -> insights validados. Lote vazio nem chama o LLM."""
    if not outcomes:
        return []
    texto = await llm.completar(build_prompt(outcomes))
    return parse_insights(texto)
