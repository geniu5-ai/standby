"""Redação do rascunho comercial com Pydantic AI.

Substitui o envelope JSON montado à mão em :mod:`app.reply.llm`, onde o
``output_schema`` era *descrito em texto* dentro do prompt e a resposta era
parseada na unha — origem do ``finish_reason=length`` que derrubou turnos em
23 e 24/ago/2026.  Aqui o contrato de saída é ``output_type`` tipado: o
framework valida e repete a chamada quando o modelo devolve torto.

Divisão de responsabilidade, que é a razão de a dupla casar:

* **Pydantic AI** cuida do contrato com o modelo — instruções dinâmicas,
  saída tipada, retry de validação.
* **Procrastinate** cuida da execução durável: cada turno é um job e a
  chamada ao modelo acontece dentro dele.  Quem decide a bifurcação continua
  sendo o Python em :mod:`app.durable.flow`.

Por isso :func:`draft_reply_with_agent` **não** é um job separado: ela já é
chamada de dentro do job do turno.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic_ai import Agent, PromptedOutput, RunContext
from pydantic_ai.exceptions import ModelAPIError, UnexpectedModelBehavior

from app.contracts import FATO_TIPOS, AgentStatusSignal, FatoConversa
from app.reply.guardrails import guardrail_de_saida
from deepeval.tracing import observe as _deepeval_observe
from deepeval.tracing.context import update_current_trace as _update_trace

logger = logging.getLogger(__name__)


# (comentário, não docstring: o docstring vira descrição no schema que o modelo lê)
# O contrato de saída, agora tipado em vez de descrito em prosa.
class AgentReply(BaseModel):

    model_config = ConfigDict(extra="forbid")

    action: Literal["send_text", "handoff"]
    text: str = Field(min_length=1, max_length=2_048)


# (comentário, não docstring: o docstring vira descrição no schema que o modelo lê)
# Voto como o modelo escreve: tolerante de proposito.
#
# Maiuscula, valor desconhecido ou chave a mais nunca derrubam o turno
# (o paciente espera a resposta, nao o voto): tudo que nao for win/loss vira
# open. O contrato estrito e :class:`AgentStatusSignal`.
#
class SinalDoAgente(BaseModel):

    model_config = ConfigDict(extra="ignore")

    status: str | None = "open"
    citacao_cliente: str | None = ""
    citacao_horario: str | None = ""

    def para_contrato(self) -> AgentStatusSignal:
        status = (self.status or "").strip().lower()
        return AgentStatusSignal(
            status=status if status in ("win", "loss") else "open",
            citacao_cliente=(self.citacao_cliente or "")[:1_024],
            citacao_horario=(self.citacao_horario or "")[:1_024],
        )


# (comentário, não docstring: o docstring vira descrição no schema que o modelo lê)
# Saida com o voto de status (20/out/2026, specs/status-no-agente.md).
#
# So usada por tenant em ``STATUS_AGENT_*``: os demais seguem com o schema
# de :class:`AgentReply`, sem um token a mais no prompt.
#
class AgentReplyComStatus(AgentReply):

    model_config = ConfigDict(extra="ignore")

    # None = o modelo nao votou: o turno segue so com o juiz. Nunca vira um
    # "open" implicito, que pularia o juiz e vetaria loss (revisao Kimi).
    status: SinalDoAgente | None = None


class FatoDoAgente(BaseModel):
    """Fato como o modelo escreve: tolerante (tipo desconhecido e descartado)."""

    model_config = ConfigDict(extra="ignore")

    tipo: str | None = ""
    detalhe: str | None = ""

    def para_contrato(self) -> FatoConversa | None:
        tipo = (self.tipo or "").strip().lower()
        if tipo not in FATO_TIPOS:
            return None
        return FatoConversa(tipo=tipo, detalhe=(self.detalhe or "")[:240])  # type: ignore[arg-type]


# (comentário, não docstring: o docstring vira descrição no schema que o modelo lê)
# Saida com fatos (23/out/2026). So para tenant em FATOS_SHADOW_TENANTS.
class AgentReplyComFatos(AgentReplyComStatus):

    fatos: list[FatoDoAgente] | None = None


INSTRUCAO_FATOS = (
    'Alem da resposta, preencha o campo "fatos" (lista, pode ser vazia) com o que '
    "ACONTECEU nesta troca, sem opiniao sobre o desfecho da venda. Tipos: "
    '"pergunta_aberta" (a pessoa perguntou algo que voce nao respondeu agora), '
    '"promessa" (voce ou a equipe vai retornar), "agendamento" (dia e horario '
    'combinados, escreva o horario em detalhe), "remarcacao", "atraso", "recusa" '
    '(recusa explicita), "adiamento" (a pessoa vai decidir depois: escreva o prazo '
    'combinado em detalhe), "pedido_retorno" (hora em que a pessoa pediu retorno), '
    '"insistencia_feita" (voce fez a insistencia unica depois de uma recusa), '
    '"encerramento". Um "?" ou emoji isolado e "pergunta_aberta". "Ok" ou '
    '"obrigada" sozinhos nao sao fato. Os fatos nunca aparecem no texto enviado.'
)


# Mesma definicao do juiz (app/status/audit.py) e do STATUS: do prompt antigo
# do Genius Sell (promptfoo 07/ago/2026, 17/18). A citacao e verificada pelo
# codigo contra a transcricao; sem ela o voto terminal vale open.
INSTRUCAO_STATUS = (
    'Alem da resposta, preencha o campo "status" com o desfecho comercial da '
    "conversa ate agora:\n"
    '- "win": o cliente ACEITOU um agendamento com dia e horario definidos (ele '
    "escolheu e foi confirmado, ou foi proposto e ele concordou), ou fechou ou "
    'pagou. Exemplos depois de um horario combinado: "Pode ser terca as 14h", '
    '"Confirmado", "Vou comparecer".\n'
    '- "loss": o cliente recusou de forma explicita ou pediu para parar de '
    'receber mensagens. "Vou pensar" ou "depois eu vejo" NAO e loss.\n'
    '- "open": qualquer outro caso, inclusive intencao ("quero marcar") sem dia '
    "e horario aceitos. Na duvida, open.\n"
    'Para win e loss, copie em "citacao_cliente" uma fala LITERAL do cliente que '
    'prova o desfecho. Para win, copie em "citacao_horario" o trecho LITERAL, de '
    "qualquer pessoa da conversa, com o dia ou horario combinado. Para open, "
    "deixe as duas vazias. O status nunca aparece no texto enviado ao cliente."
)


@dataclass
class ReplyDeps:
    """Contexto de uma redação."""

    tenant_id: str
    business_prompt: str
    contract_rules: tuple[str, ...]
    # Chaves que a ferramenta ``abrir`` aceita, com titulo para o modelo saber
    # o que pedir.  Vazio = tenant sem catalogo, e a ferramenta so recusa.
    catalog: tuple[tuple[str, str], ...] = ()
    # Catalogo inteiro (chave, titulo, conteudo) quando a estrategia do tenant
    # e ``inline``.  Preenchido, o modelo ja recebe o conteudo e nao precisa
    # chamar ferramenta nenhuma — que e o ponto: cada chamada de ferramenta e
    # uma ida a mais ao provedor, em serie, e o cliente espera por ela.
    catalogo_inline: tuple[tuple[str, str, str], ...] = ()
    # Consultas feitas nesta execucao; a ferramenta usa para aplicar o teto.
    consultas: list[str] = field(default_factory=list)
    # Exa (pesquisar_web) liberado para este tenant via tenant_registry.web_search
    # (migration-069). Cada busca custa ~3k tokens + ida ao provedor, por isso
    # opt-in por tenant e teto de 1 por turno (abaixo).
    web_search: bool = False
    # Web searches (Exa) feitas nesta execucao; teto de 1 por turno, seguindo o
    # mesmo padrao de ReplyDeps.consultas (catalogo).
    web_search_consultas: list[str] = field(default_factory=list)
    # A etapa da conversa, computada em codigo (app/reply/etapas.py) em vez de
    # inferida pelo modelo. "andamento" nao injeta nada: e' o default seguro,
    # entao quem nao computa etapa continua com o comportamento de antes.
    etapa: str = "andamento"
    # --- Manager (task #8801; settings.MANAGER_AGENT_TENANTS) -----
    # False = padrao: as ferramentas proativas (raciocinar) nem aparecem para o
    # modelo, pelo mesmo `prepare` usado por web_search/knowledge. Nada muda no
    # turno de quem nao esta na flag.
    manager: bool = False
    # Ultimas falas da conversa (papel, texto) para a rota densa (B) montar o
    # prompt denso sem uma segunda leitura de banco. Ja limitada por
    # _reply_context (HISTORY_TOTAL_MAX_CHARS), entao continua serializavel.
    historico: tuple[tuple[str, str], ...] = ()
    # Telefone normalizado do paciente do turno (ferramentas da agenda; vazio fora delas).
    phone: str = ""
    # Chamadas a rota densa neste turno; teto MANAGER_REASONING_MAX_PER_TURN.
    raciocinios: list[str] = field(default_factory=list)
    # Guardrail de valores (app/reply/guardrails.py): liga so quando a entrada
    # e a fonte completa (catalogo inline, sem busca na web nem raciocinio), a
    # mesma condicao do vigia.  ``fontes_de_valor`` sao os textos da entrada
    # onde um valor em reais pode legitimamente aparecer.
    vigiar_valores: bool = False
    fontes_de_valor: tuple[str, ...] = ()
    # Ordem da venda (guardrail): fala do cliente neste turno e liga por tenant.
    turno_do_cliente: str = ""
    vigiar_ordem: bool = False


# ``defer_model_check`` evita tocar em settings no import: o modelo real chega
# em ``run(model=...)``, então este módulo importa num ambiente sem credencial
# (teste, CI) sem explodir.
# ``PromptedOutput`` em vez do modo padrao (tool call ``final_result``).  Medido
# em 26/ago/2026 com o prompt real deste tenant, duas rodadas:
#
#   tool calling (padrao)  2 requests  5.439 tokens de entrada  ~5s
#   JSON pedido no prompt  1 request   2.350 tokens de entrada  ~3s
#
# [Medido com deepseek-v4-flash, antes da troca de 05/nov/2026] o modelo erra a chamada de ferramenta, o Pydantic rejeita e ele
# refaz — um retry em todo turno.  E o schema das ferramentas ainda custa ~3k
# tokens de entrada por chamada.  Pedindo JSON no prompt ele acerta de primeira.
#
# Isto NAO abre mao do Pydantic AI: o contrato continua tipado, a validacao
# continua acontecendo e o retry continua existindo como rede.  Muda so como o
# modelo e convidado a responder — que era onde o framework brigava com o modelo.
#
# ``NativeOutput`` (structured outputs do provedor) foi testado e recusado pela
# lib: "Native structured output is not supported by this model".
reply_agent = Agent(
    "openai-chat:gpt-4o-mini",  # 05/nov/2026: DeepSeek fora; a chamada real passa REPLY_AGENT_MODEL
    output_type=PromptedOutput(AgentReply),
    deps_type=ReplyDeps,
    name="genius-next-reply",
    # ``retries=2`` e a revalidacao da saida tipada do Pydantic AI (modelo que erra o
    # formato refaz). Erro 500/timeout do provedor e outra coisa: tem retentativa propria em
    # ``_run_com_retentativa``; sem ela o turno morria e o cliente ficava sem resposta.
    #
    # Retentar a chamada ao modelo e seguro: e leitura, nao efeito colateral.  No
    # pior caso paga-se o token duas vezes.  E o oposto de ``send_text_step``, que
    # nao retenta de proposito, porque reenviar e mandar duas mensagens para o
    # cliente.
    #
    # 3 tentativas, 1s de intervalo dobrando: pior caso ~3s de espera adicional,
    # dentro do orcamento do turno.
    retries=2,
    defer_model_check=True,
    # Guardrail de saida (02/nov/2026, substitui o sidecar llm-guard): segredo
    # reescrito e valor em reais fora da entrada refeito uma vez, depois
    # bloqueado.  Detalhe em app/reply/guardrails.py.
    capabilities=[guardrail_de_saida()],
    # [Medido com deepseek-v4-flash, antes da troca] o modelo *pensava* antes de responder, e o pensamento e a maior
    # fatia da espera do cliente.  Medido em 26/ago/2026, 8 perguntas x 2
    # rodadas com o prompt e o catalogo reais deste tenant:
    #
    #   baseline (pensando)   media 5,2s   pior caso 14,3s   436 tokens de saida
    #   thinking=False        media 1,4s   pior caso  1,9s    39 tokens de saida
    #
    # A aderencia as regras nao caiu — subiu de 13/16 para 14/16 acoes certas,
    # e o unico handoff de urgencia perdido foi do lado que pensava.  Baixar o
    # esforco em vez de desligar e PIOR que o baseline (minimal 4,7s, low 5,5s):
    # nao ha meio termo util aqui, ou pensa ou nao pensa.
    #
    # Vale para ESTE modelo com ESTE prompt: a decisao do turno e escolher uma
    # frase dentro de um catalogo que ja veio inteiro no contexto, nao raciocinar.
    # Trocou o modelo por um de raciocinio, remeça antes de manter a linha.
    model_settings={"thinking": False},
)


# Politica do modelo: 3 tentativas, 1 s dobrando (pior caso ~3 s de espera).
# Um 500 ou timeout do provedor nao pode matar o turno. Retentar e seguro: chamar o modelo e
# leitura, no pior caso paga-se o token duas vezes (ao contrario de reenviar mensagem).
MODELO_TENTATIVAS = 3
MODELO_ESPERA_S = 1.0


def _run_com_retentativa(entrada: str, **kwargs: Any) -> Any:
    for tentativa in range(1, MODELO_TENTATIVAS + 1):
        try:
            return reply_agent.run_sync(entrada, **kwargs)
        except (ModelAPIError, httpx.TransportError) as exc:
            if tentativa == MODELO_TENTATIVAS:
                raise
            logger.warning("modelo falhou (tentativa %d): %r", tentativa, exc)
            time.sleep(MODELO_ESPERA_S * 2 ** (tentativa - 1))
    raise AssertionError("inalcancavel")


@reply_agent.instructions
def instrucoes_do_negocio(ctx: RunContext[ReplyDeps]) -> str:
    """O prompt do tenant, vindo do Postgres — dinâmico a cada execução.

    Era o campo ``business`` de um envelope; agora é instrução de verdade.
    """
    return ctx.deps.business_prompt


@reply_agent.instructions
def instrucoes_do_catalogo(ctx: RunContext[ReplyDeps]) -> str:
    """Lista as chaves que a ferramenta ``abrir`` aceita nesta execucao.

    Sem isto o modelo tem a ferramenta mas nao sabe o que existe — que e
    exatamente o estado de hoje: 19 chaves cadastradas e zero chamadas em 24h.
    """
    from app.reply.tools.knowledge import (
        catalogo_como_instrucao,
        catalogo_inline_como_instrucao,
    )

    if ctx.deps.catalogo_inline:
        return catalogo_inline_como_instrucao(ctx.deps.catalogo_inline)
    return catalogo_como_instrucao(ctx.deps.catalog)


@_deepeval_observe(type='agent')
def draft_reply_with_agent(
    turn: Any,
    history: Any,
    *,
    summary: str | None = None,
) -> tuple[Any, str]:
    """Redige um turno pelo agente. Mesma assinatura de ``llm.draft_reply_step``.

    **Não** transforme isto em job separado: já roda dentro do job do turno.

    Os helpers de contexto ainda vêm de :mod:`app.reply.llm` de propósito: o
    contexto já está correto e testado, e reescrevê-lo agora misturaria duas
    mudanças na mesma fase. Eles saem de lá na Fase 4 da SPEC_PYDANTIC_AI.
    """
    from sqlalchemy.orm import Session

    from app.contracts import ReplyDraft
    from app.durable.tenant_registry import get_registry_engine
    from app.identity_primitives import normalize_phone
    from app.reply.llm import _reply_context, _STRUCTURAL_RULES
    from app.reply.prompt_profiles import active_prompt_for
    from app.reply.manager import manager_enabled
    from app.settings import get_settings

    settings = get_settings()
    _update_trace(
        thread_id=getattr(turn, "conversation_id", None),
        user_id=(f"{turn.tenant_id}:{turn.remote_jid}" if getattr(turn, "tenant_id", None) else None),
    )
    business = active_prompt_for(turn.tenant_id, "reply_normal") or ""

    with Session(get_registry_engine(settings.DATABASE_URL)) as session:
        from app.agent.memory import get_client_profile, get_conversation_history

        phone = normalize_phone(turn.remote_jid)
        pg_history = get_conversation_history(
            session,
            tenant_id=turn.tenant_id,
            phone=phone,
            limit=settings.HISTORY_MAX_MESSAGES,
            max_chars=settings.HISTORY_TOTAL_MAX_CHARS,
            include_team=settings.HISTORY_INCLUDE_TEAM_MESSAGES,
        )
        try:
            perfil = get_client_profile(session, tenant_id=turn.tenant_id, phone=phone)
        except Exception:
            logger.warning("client profile indisponivel", exc_info=True)
            perfil = None
        from app.context.campaigns import campanhas_vigentes
        from app.context.knowledge import list_catalog, load_full_catalog
        from app.durable.tenant_registry import (
            knowledge_strategy_with_session,
            web_search_with_session,
        )

        catalogo = tuple(list_catalog(session, tenant_id=turn.tenant_id))
        # Campanha nao e catalogo: so entra se vigente hoje (nunca levanta).
        campanhas = campanhas_vigentes(session, turn.tenant_id)
        licoes = _licoes_do_turno(session, turn, settings)
        estrategia = knowledge_strategy_with_session(
            session, tenant_id=turn.tenant_id
        )
        web_search = web_search_with_session(
            session, tenant_id=turn.tenant_id
        )
        inline: tuple[tuple[str, str, str], ...] = ()
        if estrategia == "inline":
            inline = tuple(
                load_full_catalog(
                    session,
                    tenant_id=turn.tenant_id,
                    max_chars=settings.KNOWLEDGE_INLINE_MAX_CHARS,
                )
            )
            if not inline and catalogo:
                # Catalogo grande demais para o prompt: volta para a ferramenta
                # em vez de truncar. Este log e o gatilho para mover o tenant
                # para a estrategia "selecao".
                logger.warning(
                    "catalogo de %s nao coube inline; usando ferramenta",
                    turn.tenant_id,
                )

    context = _reply_context(
        turn,
        history,
        pg_history=pg_history,
        client_profile=perfil,
        max_chars=settings.HISTORY_TOTAL_MAX_CHARS,
    )
    if context is None:
        raise RuntimeError("turno sem conteudo para redigir")
    # Antes de _fontes_de_valor: o preco da campanha e fonte legitima de valor.
    from app.context.campaigns import incluir_no_contexto

    incluir_no_contexto(context, campanhas)
    if licoes:
        context["lessons"] = licoes
    if settings.ordem_da_venda_enabled(turn.tenant_id):
        from app.reply.ordem_da_venda import etapa_da_venda

        context["etapa_da_venda"] = etapa_da_venda(
            " ".join(str(m.get("text", "")) for m in context.get("turn", []) if m.get("role") == "customer"),
            tuple((str(i.get("role", "")), str(i.get("text", ""))) for i in context.get("history") or [] if isinstance(i, dict)),
            chave=phone,
        )
    if summary is not None:
        context.setdefault("client", {})["summary"] = summary

    # A etapa sai do MESMO texto que o modelo vai ler, nao de uma leitura
    # paralela do turno: se as duas divergissem, a instrucao descreveria uma
    # conversa diferente da que esta no contexto.
    from app.reply.etapas import etapa_do_turno

    fala_do_cliente = " ".join(
        mensagem.get("text", "")
        for mensagem in context.get("turn", [])
        if mensagem.get("role") == "customer"
    )
    etapa = etapa_do_turno(
        fala_do_cliente, tem_historico=bool(context.get("history"))
    )
    logger.info("turno de %s na etapa %s", turn.tenant_id, etapa)

    # etapa nao mais passa pro agente: instrucoes_da_etapa foi removida
    # (doutrina unica reply_normal v31 ja carrega o protocolo inteiro).
    # Etapa fica como telemetria via logger acima.
    com_status = settings.status_agent_enabled(turn.tenant_id)
    com_fatos = settings.fatos_enabled(turn.tenant_id)
    from pydantic_ai_harness.guardrails import OutputBlocked

    try:
        resposta = _run_agent(
            com_status=com_status,
            com_fatos=com_fatos,
            phone=phone,
            tenant_id=turn.tenant_id,
            business_prompt=business,
            contract_rules=tuple(_STRUCTURAL_RULES),
            catalog=catalogo,
            catalogo_inline=inline,
            context=context,
            model=settings.REPLY_AGENT_MODEL,
            output_max_tokens=settings.OUTPUT_MAX_TOKENS,
            web_search=web_search,
            manager=manager_enabled(turn.tenant_id),
            historico=tuple(
                (str(item.get("role", "")), str(item.get("text", "")))
                for item in context.get("history") or []
                if isinstance(item, dict)
            ),
            fontes_de_valor=_fontes_de_valor(business, inline, context),
        )
    except OutputBlocked:
        # Guardrail de saida: o modelo insistiu em valor fora da entrada mesmo
        # depois de refazer.  Mesmo destino do vigia: handoff sem preco.
        from app.contracts import ReplyDraft
        from app.reply.price_guard import VALOR_NAO_CONFIRMADO_TEXT

        return (
            ReplyDraft(
                action="handoff",
                text=VALOR_NAO_CONFIRMADO_TEXT,
                llm_model=settings.REPLY_AGENT_MODEL,
            ),
            "open",
        )
    barrado = _vigia_de_valores(
        resposta,
        tenant_id=turn.tenant_id,
        business=business,
        inline=inline,
        context=context,
        web_search=web_search,
        manager=manager_enabled(turn.tenant_id),
    )
    if barrado is not None:
        # A resposta foi trocada por handoff: o voto que veio com ela tambem
        # nao vale (mesma regra de _safe_output_guard_draft em flow.py).
        return barrado, "open"
    # Juiz por turno: agenda em fundo e segue. Desligado por padrao (TURN_JUDGE_ENABLED).
    from app.reply import turn_judge

    turn_judge.schedule(settings=settings, tenant_id=turn.tenant_id, turn=turn, resposta=resposta.text)
    # "open" mantém o contrato de draft_reply_step: a classificação de status
    # é outra peça (app/status/) e este caminho não decide status nenhum.
    #
    # 27/ago/2026: era "new", vocabulário morto desde a migration 032. O
    # Literal de AgentDecision.customer_status virou open/win/loss/block e
    # este retorno passou a quebrar TODO turno do motor de agente com
    # ValidationError — depois de o modelo já ter respondido e sido pago.
    # A suíte não pegou porque os testes mockam draft_reply_step com o mesmo
    # valor morto; o custo real foi o cliente ver "digitando..." e nunca a
    # resposta.
    return (
        ReplyDraft(
            action=resposta.action,
            text=resposta.text,
            llm_model=settings.REPLY_AGENT_MODEL,
            status_signal=(
                resposta.status.para_contrato()
                if isinstance(resposta, AgentReplyComStatus)
                and resposta.status is not None
                else None
            ),
            fatos=_fatos_do_agente(resposta),
        ),
        "open",
    )


def _licoes_do_turno(session: Any, turn: Any, settings: Any) -> list[str]:
    """Licoes APROVADAS (``genius_next.lessons``) para a ultima fala do cliente. Nunca levanta.

    So para tenants em ``LESSONS_IN_AGENT_TENANTS`` (vazio = nenhum). Licao que cita valor em reais e
    descartada: o contexto inteiro e fonte legitima do guardrail de preco, entao uma licao com preco
    "autorizaria" esse valor na resposta.
    """
    lista = {t.strip() for t in str(getattr(settings, "LESSONS_IN_AGENT_TENANTS", "") or "").split(",") if t.strip()}
    if turn.tenant_id not in lista:
        return []
    try:
        from app.agent.lessons import search_lessons
        from app.reply.price_guard import valores

        ultima = ""
        for event in reversed(getattr(turn, "events", None) or []):
            texto = (getattr(event, "text_content", None) or "").strip()
            if texto:
                ultima = texto
                break
        if not ultima:
            return []
        achadas = search_lessons(session, tenant_id=turn.tenant_id, segmento=None, query=ultima, limit=3)
        licoes = [li.content for li in achadas.lessons if not valores(li.content)]
        logger.info("licoes no agente: tenant=%s achadas=%d usadas=%d", turn.tenant_id, len(achadas.lessons), len(licoes))
        return licoes
    except Exception:  # noqa: BLE001 - licao e dica opcional: nunca derruba o turno
        logger.warning("licoes indisponiveis para o turno", exc_info=False)
        return []


def _fatos_do_agente(resposta: AgentReply) -> list[FatoConversa]:
    """Fatos validos da resposta, no maximo 8; qualquer defeito vira lista vazia."""
    if not isinstance(resposta, AgentReplyComFatos) or not resposta.fatos:
        return []
    fatos = [f.para_contrato() for f in resposta.fatos[:8]]
    return [f for f in fatos if f is not None]


def _entrada_e_fonte_completa(
    inline: tuple[tuple[str, str, str], ...], *, web_search: bool, manager: bool
) -> bool:
    """A entrada do modelo e a fonte completa de valores: catalogo inline e
    nenhuma ferramenta que traga texto novo (busca na web, raciocinio).

    Condicao unica do vigia e do guardrail de valores, para os dois nunca
    divergirem sobre quando opinar.
    """
    return bool(inline) and not web_search and not manager


def _fontes_de_valor(
    business: str,
    inline: tuple[tuple[str, str, str], ...],
    context: dict[str, Any],
) -> tuple[str, ...]:
    """Textos da entrada onde um valor em reais pode aparecer legitimamente."""
    return (
        business,
        *(f"{titulo} {conteudo}" for _chave, titulo, conteudo in inline),
        json.dumps(context, ensure_ascii=False),
    )


def _vigia_de_valores(
    resposta: AgentReply,
    *,
    tenant_id: str,
    business: str,
    inline: tuple[tuple[str, str, str], ...],
    context: dict[str, Any],
    web_search: bool,
    manager: bool,
) -> Any | None:
    """Handoff sem preço quando a resposta cita valor que o modelo não viu.

    Devolve o ``ReplyDraft`` de handoff, ou ``None`` quando a resposta segue.
    Melhor passar a conversa para a equipe do que arriscar um valor inventado.
    Só vale com catálogo inline e sem ferramenta que traga texto novo (busca na
    web, raciocínio proativo): fora disso a entrada não é a fonte completa.
    """
    if resposta.action != "send_text" or not _entrada_e_fonte_completa(
        inline, web_search=web_search, manager=manager
    ):
        return None
    from app.contracts import ReplyDraft
    from app.reply.price_guard import (
        VALOR_NAO_CONFIRMADO_TEXT,
        valores_fora_da_entrada,
    )
    from app.settings import get_settings

    fora = valores_fora_da_entrada(
        resposta.text, _fontes_de_valor(business, inline, context)
    )
    if not fora:
        return None
    logger.warning(
        "vigia de valores: %d valor(es) fora da entrada em %s; resposta vira handoff",
        len(fora),
        tenant_id,
    )
    return ReplyDraft(
        action="handoff",
        text=VALOR_NAO_CONFIRMADO_TEXT,
        llm_model=get_settings().REPLY_AGENT_MODEL,
    )


def _run_agent(
    *,
    tenant_id: str,
    business_prompt: str,
    contract_rules: tuple[str, ...],
    catalog: tuple[tuple[str, str], ...],
    context: dict[str, Any],
    model: str,
    output_max_tokens: int,
    catalogo_inline: tuple[tuple[str, str, str], ...] = (),
    web_search: bool = False,
    manager: bool = False,
    historico: tuple[tuple[str, str], ...] = (),
    com_status: bool = False,
    com_fatos: bool = False,
    phone: str = "",
    fontes_de_valor: tuple[str, ...] = (),
) -> AgentReply:
    """Uma execução do agente, com a retentativa de :func:`_run_com_retentativa`.

    Levanta ``OutputBlocked`` quando o guardrail de saida bloqueia a resposta
    (valor em reais fora da entrada depois da refacao): quem chama decide o
    fallback.
    """
    from app.settings import get_settings as _settings

    turno_do_cliente = " ".join(
        str(m.get("text", "")) for m in context.get("turn", []) if m.get("role") == "customer"
    )
    deps = ReplyDeps(
        turno_do_cliente=turno_do_cliente,
        vigiar_ordem=_settings().ordem_da_venda_enabled(tenant_id),
        tenant_id=tenant_id,
        business_prompt=business_prompt,
        contract_rules=contract_rules,
        catalog=catalog,
        catalogo_inline=catalogo_inline,
        web_search=web_search,
        manager=manager,
        historico=historico,
        phone=phone,
        vigiar_valores=_entrada_e_fonte_completa(
            catalogo_inline, web_search=web_search, manager=manager
        ),
        fontes_de_valor=fontes_de_valor,
    )
    model_settings = dict(reply_agent.model_settings or {})
    model_settings["max_tokens"] = output_max_tokens
    from app.settings import get_settings

    temperature = get_settings().REPLY_AGENT_TEMPERATURE
    if temperature is not None:
        model_settings["temperature"] = temperature
    entrada = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
    if com_status or com_fatos:
        # Fatos e voto compartilham o mesmo schema tolerante: o que nao foi pedido
        # fica None e o turno segue como antes.
        instrucoes = " ".join(
            x for x in (INSTRUCAO_STATUS if com_status else "", INSTRUCAO_FATOS if com_fatos else "") if x
        )
        try:
            return _run_com_retentativa(
                entrada,
                deps=deps,
                model=model,
                model_settings=model_settings,
                output_type=PromptedOutput(AgentReplyComFatos if com_fatos else AgentReplyComStatus),
                instructions=instrucoes,
            ).output
        except (UnexpectedModelBehavior, ValidationError):
            # O voto nunca custa a resposta: refaz sem o campo de status e o
            # status_workflow segue so com o juiz neste turno.
            logger.warning(
                "saida com voto de status falhou para %s; resposta sem voto",
                tenant_id,
                exc_info=True,
            )
    resultado = _run_com_retentativa(
        entrada,
        deps=deps,
        model=model,
        model_settings=model_settings,
    )
    return resultado.output
