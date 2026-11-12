"""Guardrail de saida do agente de resposta (pydantic-ai-harness, 02/nov/2026).

Roda dentro do ``reply_agent`` (``app/reply/agent.py``), sobre a saida tipada
(``AgentReply`` e subclasses), antes de o texto virar ``ReplyDraft``:

1. ``sem_segredos`` — reescreve credencial (chave de API, token, JWT, chave
   privada) que aparecer no texto.  O agente nao tem segredo no contexto, entao
   isto e rede barata, nao defesa principal.
2. ``valores_confirmados`` — todo valor em reais citado tem de existir na
   entrada do modelo (mesma regra do vigia de ``app/reply/price_guard.py``).
   Na primeira vez o modelo refaz a resposta com a instrucao de nao inventar
   valor; se insistir, a saida e bloqueada (``OutputBlocked``) e quem chama
   devolve o handoff de valor nao confirmado.

O que NAO fica aqui, de proposito: avaliacao de qualidade (escopo, tom,
fidelidade ao catalogo) por LLM juiz.  Isso e monitoramento, roda fora do
caminho do cliente, e nao pode atrasar nem bloquear resposta.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai_harness.guardrails import GuardrailResult, OutputGuardrail, detectors

from app.reply.ordem_da_venda import INSTRUCAO_POR_VIOLACAO, violacao_da_ordem
from app.reply.price_guard import valores_fora_da_entrada

logger = logging.getLogger(__name__)

# Quantas vezes o modelo pode refazer uma resposta com valor fora da entrada
# antes do bloqueio.  Cada refacao e mais uma chamada ao modelo (~1,5 s no
# gpt-pool), entao uma so: o cliente espera por ela.
VALOR_RETRIES = 1

VALOR_RETRY_INSTRUCAO = (
    "Sua resposta citou valor em reais que nao esta nas informacoes da empresa "
    "nem na conversa: {valores}. Reescreva usando somente valores que aparecem "
    "nas informacoes da empresa. Se o valor pedido nao estiver la, diga que "
    "depende da avaliacao, sem citar numero."
)

VALOR_BLOQUEADO = "valor_nao_confirmado"


@lru_cache(maxsize=1)
def _detector_de_segredos():
    return detectors.secret_data()


def sem_segredos(output: Any) -> GuardrailResult:
    """Reescreve credencial no texto, mantendo o tipo da saida (``model_copy``)."""
    texto = getattr(output, "text", None)
    if not isinstance(texto, str):
        return GuardrailResult.allow()
    veredito = _detector_de_segredos()(texto)
    if veredito.action != "replace":
        return GuardrailResult.allow()
    logger.warning("guardrail de saida: credencial reescrita na resposta do agente")
    return GuardrailResult.replace(output.model_copy(update={"text": veredito.replacement}))


def valores_confirmados(ctx: RunContext[Any], output: Any) -> GuardrailResult:
    """Valor em reais fora da entrada: refaz uma vez, depois bloqueia."""
    deps = ctx.deps
    if not getattr(deps, "vigiar_valores", False):
        return GuardrailResult.allow()
    if getattr(output, "action", None) != "send_text":
        return GuardrailResult.allow()
    fora = valores_fora_da_entrada(output.text, getattr(deps, "fontes_de_valor", ()))
    if not fora:
        return GuardrailResult.allow()
    if ctx.retry < VALOR_RETRIES:
        logger.info(
            "guardrail de saida: %d valor(es) fora da entrada em %s; modelo refaz",
            len(fora),
            getattr(deps, "tenant_id", "?"),
        )
        valores = ", ".join(f"R$ {v.replace('.', ',')}" for v in fora)
        return GuardrailResult.retry(VALOR_RETRY_INSTRUCAO.format(valores=valores))
    logger.warning(
        "guardrail de saida: valor fora da entrada persistiu em %s; resposta bloqueada",
        getattr(deps, "tenant_id", "?"),
    )
    return GuardrailResult.block(VALOR_BLOQUEADO)


def ordem_da_venda(ctx: RunContext[Any], output: Any) -> GuardrailResult:
    """Acolher, engajar, depois o preco: refaz uma vez; se insistir, deixa passar (nunca bloqueia)."""
    deps = ctx.deps
    if not getattr(deps, "vigiar_ordem", False) or getattr(output, "action", None) != "send_text":
        return GuardrailResult.allow()
    violacao = violacao_da_ordem(
        output.text, getattr(deps, "turno_do_cliente", ""), getattr(deps, "historico", ())
    )
    if violacao is None:
        return GuardrailResult.allow()
    if ctx.retry < VALOR_RETRIES:
        logger.info("guardrail de saida: %s em %s; modelo refaz", violacao, getattr(deps, "tenant_id", "?"))
        return GuardrailResult.retry(INSTRUCAO_POR_VIOLACAO[violacao])
    logger.warning("guardrail de saida: %s persistiu em %s; resposta mantida", violacao, getattr(deps, "tenant_id", "?"))
    return GuardrailResult.allow()


def guardrail_de_saida() -> OutputGuardrail:
    """Cadeia na ordem: segredo reescrito antes de conferir valores."""
    return OutputGuardrail(guard=[sem_segredos, valores_confirmados, ordem_da_venda])
