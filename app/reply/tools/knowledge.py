"""A ferramenta ``abrir(chave)``: consulta a base de conhecimento do tenant.

Substitui o tool escrito a mao em :mod:`app.reply.llm` (``_knowledge_tool_schema``,
``_parse_tool_arguments``, ``_knowledge_tool_result``).  A diferenca que importa
nao e estetica: la os argumentos vinham como string JSON e, quando chegavam
tortos, ``_parse_tool_arguments`` devolvia ``None`` **em silencio** — a consulta
virava "documento nao encontrado" e ninguem ficava sabendo.  Aqui o Pydantic
valida a assinatura e cada chamada aparece no resultado da execucao, entao da
para responder a pergunta que hoje e impossivel: o modelo consultou ou inventou?
"""

from __future__ import annotations

import logging

from pydantic_ai import RunContext
from pydantic_ai.tools import ToolDefinition
from sqlalchemy.orm import Session

from app.context.knowledge import open_knowledge
from app.durable.tenant_registry import get_registry_engine
from app.reply.agent import ReplyDeps, reply_agent
from app.security.guard import GuardDisposition, scan_invisible_text
from app.settings import get_settings

logger = logging.getLogger(__name__)

NAO_ENCONTRADO = "documento nao encontrado"
INDISPONIVEL = "documento indisponivel"
LIMITE_ATINGIDO = "limite de consultas atingido"

# Mesmo teto do caminho legado (llm.MAX_TOOL_CALLS): duas consultas bastam para
# responder e impedem que o modelo entre em loop de leitura.  Virou default de
# chave em 26/ago/2026 (``KNOWLEDGE_MAX_CONSULTAS``): cada consulta e uma ida a
# mais ao provedor, entao esse numero e latencia direta.
MAX_CONSULTAS = 2


def _ler_documento(tenant_id: str, chave: str) -> str | None:
    """Le um documento do catalogo dentro do job do turno.

    A leitura vai direto ao Postgres: e repetida se o job for retentado, entao
    o documento nunca vem de cache do worker.
    """
    settings = get_settings()
    with Session(get_registry_engine(settings.DATABASE_URL)) as session:
        return open_knowledge(session, tenant_id=tenant_id, chave=chave)


def _so_quando_nao_veio_inline(
    ctx: RunContext[ReplyDeps], definicao: ToolDefinition
) -> ToolDefinition | None:
    """Esconde ``abrir`` do modelo quando o catalogo ja foi para o prompt.

    Nao basta parar de listar as chaves: enquanto a ferramenta existe na
    definicao, o modelo tenta usa-la.  Medido em 26/ago/2026 na pergunta de
    preco, com o conteudo ja inline: 3 requests com a ferramenta registrada, 1
    sem ela.  Ferramenta visivel e um convite, e o modelo aceita.

    Devolver ``None`` retira a ferramenta *daquela execucao* — o codigo dela
    continua registrado e volta sozinho no tenant cujo catalogo nao coube.
    """
    return None if ctx.deps.catalogo_inline else definicao


@reply_agent.tool(prepare=_so_quando_nao_veio_inline)
def abrir(ctx: RunContext[ReplyDeps], chave: str) -> str:
    """Abre um documento do catalogo e devolve o conteudo aprovado.

    Use antes de afirmar preco, agenda, endereco, procedimento, disponibilidade
    ou prova social.  ``chave`` precisa ser uma das listadas no catalogo.
    """
    deps = ctx.deps
    permitidas = {c for c, _ in deps.catalog}
    if chave not in permitidas:
        return NAO_ENCONTRADO

    deps.consultas.append(chave)
    if len(deps.consultas) > get_settings().KNOWLEDGE_MAX_CONSULTAS:
        return LIMITE_ATINGIDO

    conteudo = _ler_documento(deps.tenant_id, chave)
    if conteudo is None:
        return NAO_ENCONTRADO

    # Deliberadamente NAO o scanner do llm-guard.  Medido em 22/ago/2026: aquele
    # scanner e treinado em *mensagens* hostis, entao um documento comercial
    # declarativo fica fora da distribuicao e ele bloqueia tudo — uma tabela de
    # precos pontuou 1.0 para "prompt_injection", o horario 0.5, o endereco 0.6.
    # Tres de tres documentos legitimos bloqueados, o que transformava toda
    # consulta em "documento indisponivel".  genius_next.knowledge e escrito
    # pelo proprio tenant: mesmo nivel de confianca do system prompt, nao de
    # entrada de cliente.  O ataque real aqui e instrucao escondida em Unicode
    # invisivel, e scan_invisible_text pega exatamente isso — deterministico,
    # sem rede, sem falso positivo em texto comum.
    if scan_invisible_text(conteudo).disposition == GuardDisposition.BLOCK.value:
        logger.warning("documento %s carrega texto invisivel; retido", chave)
        return INDISPONIVEL
    return conteudo


def catalogo_como_instrucao(catalog: tuple[tuple[str, str], ...]) -> str:
    """Lista as chaves disponiveis; sem isso o modelo nao sabe o que pedir."""
    if not catalog:
        return ""
    linhas = "\n".join(f"- {chave}: {titulo}" for chave, titulo in catalog)
    return (
        "Catalogo disponivel via abrir(chave). Abra a chave pertinente antes de "
        f"afirmar preco, agenda, endereco ou procedimento:\n{linhas}"
    )


def catalogo_inline_como_instrucao(
    catalogo: tuple[tuple[str, str, str], ...],
) -> str:
    """O catalogo inteiro colado no prompt, em vez de buscado por ferramenta.

    Contraintuitivo mas medido: para catalogo pequeno isto gasta MENOS token que
    a ferramenta, porque cada chamada de ferramenta reenvia prompt, historico e
    tudo que ja foi trocado.  Tres idas ao provedor custam tres vezes a base;
    uma ida com o catalogo dentro custa a base + o catalogo.

    Formato seco de proposito — ficha, nao prosa.  Texto declarativo curto e
    mais facil de citar sem deformar do que paragrafo bem escrito.
    """
    if not catalogo:
        return ""
    blocos = "\n\n".join(
        f"[{chave}] {titulo}\n{conteudo}" for chave, titulo, conteudo in catalogo
    )
    return (
        "Informacoes verificadas da empresa. Sao a unica fonte para qualquer "
        "dado da clinica — preco, agenda, endereco, procedimento, forma de "
        "pagamento. Use exatamente o que esta aqui e nao complete com "
        "suposicao. O que nao estiver aqui, voce nao sabe.\n"
        "Valores: de o preco escrito do item que a pessoa pediu, na ordem do "
        "atendimento (acolher, engajar, depois o preco). "
        "Endereco, horario e formas de pagamento sao de uso livre.\n\n"
        f"{blocos}"
    )
