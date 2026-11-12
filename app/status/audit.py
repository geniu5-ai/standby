"""Contrato puro do modulo de status — pipeline rule -> embedding -> LLM residual.

Evidencia de estado terminal so pode vir da propria fala do cliente. A
resposta do agente e contexto/compromisso, nunca prova de desfecho.

``open`` e o default, sem evidencia. ``win``/``loss`` sao decididos por regra
(``app.status.rules``), com embedding e LLM como camadas consultivas que
nunca decidem sozinhas. ``block`` NAO existe neste modulo — e decisao do
guard de conteudo sobre o trafego, nao da fala do cliente, e por isso nunca
aparece no vocabulario de saida daqui.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from collections.abc import Sequence
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_serializer,
    field_validator,
    model_validator,
)

from app.contracts import AgentStatusSignal
from app.settings import Settings
from app.status.rules import (
    detect_status_by_rule,
    is_trivial_message,
    llm_loss_vetado,
)

logger = logging.getLogger(__name__)


def _secret_value(secret: SecretStr | None) -> str:
    if secret is None:
        raise ValueError("required API key is not configured")
    return secret.get_secret_value()


CustomerStatus = Literal["open", "win", "loss"]
# "agent" (20/out/2026): open decidido pelo voto do agente principal, sem juiz.
EvidenceSource = Literal[
    "rule", "embedding", "llm", "default", "agent_reply", "customer", "agent"
]
SpeakerRole = Literal["customer", "assistant"]
# Papeis que o juiz enxerga na transcricao. "equipe" nasce de uma mensagem do
# assistente com o prefixo "[equipe] " (fala humana do app), nunca de StatusInput.
JudgeRole = Literal["customer", "assistant", "equipe"]

EMBED_SCORE_URL = "http://127.0.0.1:8721/score"
# Calibrado com 25 casos rotulados a mao contra o genius-embed (22/ago/2026).
# O e5-small comprime TUDO na faixa 0.81-0.98, entao o limiar 0.75 original nao
# filtrava nada: "Oi, tudo bem?" batia 0.886 em "loss" e marcava lead vivo como
# perdido -- e a trigger 015 torna loss quase irreversivel. Em 0.90 + margem 0.02
# a amostra da 21/25 com ZERO falso positivo; os 4 erros restantes sao falsos
# negativos, que caem de proposito na camada 3 (LLM). Falso negativo custa uma
# chamada de LLM; falso positivo mata um lead. Recalibrar quando houver dado novo.
EMBED_SCORE_THRESHOLD = 0.90
EMBED_SCORE_MARGIN = 0.02
EMBED_SCORE_TIMEOUT_SECONDS = 1.5

# ── Camada 3 (juiz LLM) — 13/out/2026, spec do CEO ─────────────────────────
# O juiz so pode emitir estado terminal quando consegue PROVAR o desfecho com
# citacoes que existem na transcricao. Os limiares separam "afirmou" de
# "tem certeza": win exige aceite com horario fechado, loss exige recusa
# inequivoca (hesitacao nunca e loss, e loss pausa a automacao).
STATUS_JUDGE_WIN_MIN_CONFIDENCE = 0.75
STATUS_JUDGE_LOSS_MIN_CONFIDENCE = 0.90
# 200 estourava com customer_quote + schedule_quote + agent_commitment
# (JSON truncado -> parse falha -> open). 300 cobre o payload completo.
STATUS_JUDGE_MAX_TOKENS = 300
# Historico entregue ao juiz: o contrato de entrada ja limita a 20, mas o
# corte fica aqui para valer tambem em entradas montadas fora do workflow.
STATUS_JUDGE_HISTORY_LIMIT = 20
# Mensagem da equipe no historico vem marcada com este prefixo (app/agent/memory).
TEAM_MESSAGE_PREFIX = "[equipe] "

# 27/ago/2026: a antiga chave "off" saiu daqui. Opt-out do cliente e loss (as
# frases que estavam em "off" viraram parte das ancoras de loss); spam/abuso
# nunca e sugerido por embedding, e decisao do guard sobre trafego.
ANCHOR_PHRASES: dict[str, list[str]] = {
    "win": [
        "confirmado, vou fechar",
        "quero agendar",
        "pode marcar",
        "vamos fechar hoje",
        "me passa os proximos horarios",
    ],
    # Only unambiguous explicit refusal -- hesitation/deferral ('vou pensar',
    # 'depois eu vejo', 'vou ver com a familia') must NEVER match loss here.
    # A lead who is undecided is still a live lead; only an explicit 'no' (or
    # silence through all 4 follow-up attempts, handled separately by
    # follow_up_v2 without ever touching this classifier) is loss.
    "loss": [
        "nao tenho interesse",
        "nao quero mais",
        "nao, obrigado",
        "decidi nao seguir com o tratamento",
        "nao vou continuar",
        "pare de mandar mensagem",
        "nao quero mais contato",
        "me tira da lista",
        "nao me mande mais mensagem",
        "remova meu numero",
    ],
}


class StatusInput(BaseModel):
    """Papeis serializados de uma classificacao: historico, fala do lead, resposta do agente."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    history: list[tuple[SpeakerRole, str]] = Field(default_factory=list)
    current_lead: str = Field(min_length=1, max_length=4_096)
    # 13/out/2026: turno auditado sem resposta do agente (supressao por controle
    # humano ou turno trivial) legitimo — o default "" e o valor de trabalho.
    agent_reply: str = Field(default="", max_length=2_048)

    @field_validator("history")
    @classmethod
    def _validate_history_roles(cls, value: object) -> object:
        if isinstance(value, list):
            for entry in value:
                if not isinstance(entry, (tuple, list)) or len(entry) != 2:
                    raise ValueError("history entries must be (role, text) pairs")
                if entry[0] not in {"customer", "assistant"}:
                    raise ValueError("history roles must be customer or assistant")
        return value

    @field_serializer("history")
    def _serialize_history(self, value: list[tuple[SpeakerRole, str]]) -> list[dict[str, str]]:
        return [{"role": role, "text": text} for role, text in value]


class StatusResult(BaseModel):
    """Desfecho validado. Default ``open``; estados terminais exigem evidencia do cliente."""

    model_config = ConfigDict(extra="forbid")

    customer_status: CustomerStatus = "open"
    agent_commitment: str | None = Field(default=None, max_length=2_048)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence_source: EvidenceSource = "default"
    # 20/out/2026: voto do agente, citacoes do juiz e caminho da decisao.
    # So preenchido para tenant em STATUS_AGENT_*; vai para status_audits.evidence.
    evidence: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _terminal_requires_customer_evidence(self) -> "StatusResult":
        if (
            self.customer_status in {"win", "loss"}
            and self.evidence_source in {"agent_reply", "agent"}
        ):
            raise ValueError(
                "terminal status cannot be proven by the agent reply alone"
            )
        return self


def normalize_judge_text(text: str) -> str:
    """Normalizacao da camada 3: casefold, sem acento, espacos colapsados.

    Pontuacao e preservada de proposito: e' o que deixa "15:30" legivel e
    mantem a comparacao de citacao proxima do texto literal da transcricao.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    sem_acento = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(sem_acento.casefold().split())


# Pista de agendamento na fala de quem nao e o cliente (IA ou equipe).
# Caso real do 13/out/2026: "obrigada" do paciente chegava ao atalho trivial e
# virava open sem juiz, logo apos a equipe confirmar a consulta -- o atalho so
# pode valer quando as ultimas falas do outro lado nao tem marcacao.
_BOOKING_CUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # hora: "15h", "15 h", "15:30"
    re.compile(r"\b\d{1,2}\s*(?:h|:)\s*\d{0,2}"),
    re.compile(
        r"\b(?:segunda|terca|quarta|quinta|sexta|sabado|domingo)(?:[\s-]+feira)?\b"
    ),
    re.compile(r"\b(?:hoje|amanha)\b"),
    re.compile(r"\b(?:confirm|agend|marcad|consulta|horario)"),
)


def has_recent_booking_cue(history: Sequence[tuple[str, str]]) -> bool:
    """True quando uma das ultimas 3 falas que NAO sao do cliente marca agendamento.

    Publica de proposito: o atalho trivial de ``classify_status`` e a supressao
    trivial de ``app.durable.flow`` decidem com a mesma regra.
    """
    falas_nao_cliente = [texto for papel, texto in history if papel != "customer"]
    for texto in falas_nao_cliente[-3:]:
        if not texto:
            continue
        normalizado = normalize_judge_text(texto)
        if any(padrao.search(normalizado) for padrao in _BOOKING_CUE_PATTERNS):
            return True
    return False


def _judge_history_entry(role: str, text: str) -> dict[str, str]:
    """Fala da equipe marcada no historico vira papel ``equipe`` sem o prefixo."""
    if role == "assistant" and text.startswith(TEAM_MESSAGE_PREFIX):
        return {"role": "equipe", "text": text[len(TEAM_MESSAGE_PREFIX):]}
    return {"role": role, "text": text}


def build_status_context(
    status_input: StatusInput,
    *,
    embedding_hint: str | None = None,
) -> dict[str, object]:
    """Serializa papeis rotulados; a resposta nunca entra entre a fala do cliente."""
    context: dict[str, object] = {
        "history": [
            _judge_history_entry(role, text)
            for role, text in status_input.history[-STATUS_JUDGE_HISTORY_LIMIT:]
        ],
        "turn": [{"role": "customer", "text": status_input.current_lead}],
        "agent_reply": status_input.agent_reply,
    }
    if embedding_hint is not None:
        context["embedding_hint"] = embedding_hint
    return context


def _strip_markdown_fence(content: str) -> str:
    """27/ago/2026: LLMs adoram envolver JSON em ```json ... ``` mesmo quando
    pedido explicitamente objeto puro (achado por revisao externa, Kimi K3).
    Sem isto, o fence quebra json.loads e a classificacao vira "open" por
    default -- nao e crash, mas desperdica todo turno em que o modelo decora
    a resposta."""
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.lower().startswith("json"):
            stripped = stripped[4:]
    return stripped.strip()


def parse_status_llm_response(raw: str) -> dict[str, Any]:
    """Parse de resposta OpenAI-compativel, tolerando choices[0].message.content aninhado."""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"status response is not valid JSON: {exc}") from exc

    if isinstance(payload, dict) and "choices" in payload:
        choices = payload.get("choices") or []
        if choices:
            message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
            content = message.get("content", "") if isinstance(message, dict) else ""
            if isinstance(content, dict):
                return content
            if isinstance(content, str) and content.strip():
                parsed = json.loads(_strip_markdown_fence(content))
                if isinstance(parsed, dict):
                    return parsed
    if isinstance(payload, dict):
        return payload
    raise ValueError("status response has no parseable content")


def _safe_confidence(raw: Any) -> float:
    """27/ago/2026: achado por revisao externa (Kimi K3), verificado.

    ``float(parsed.get("confidence", 0.0) or 0.0)`` sem guarda: se o LLM
    devolver confidence como string nao-numerica ("high") ou objeto,
    ValueError/TypeError propaga de dentro do job de status -- o job falha,
    e retentado (mesmo erro de novo), o status nunca conclui e o turno fica
    sem status nenhum aplicado. candidate_status e evidence ja
    tinham whitelist; confidence era o unico campo sem parse defensivo."""
    try:
        return float(raw or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _classify_by_embedding(text: str, embed_client: Any) -> str | None:
    """Pergunta ao servico local de embedding; qualquer rotulo terminal e so palpite."""
    if not text:
        return None
    try:
        response = embed_client.post(
            EMBED_SCORE_URL,
            params={
                "threshold": EMBED_SCORE_THRESHOLD,
                "margin": EMBED_SCORE_MARGIN,
            },
            json={"text": text, "anchors": ANCHOR_PHRASES},
        )
        if getattr(response, "status_code", None) != 200:
            return None
        payload = response.json()
        best_label = payload.get("best_label")
        if best_label in {"win", "loss"}:
            return best_label
        return None
    except Exception:
        logger.warning("status embedding failed", exc_info=True)
        return None


def _judge_system_prompt() -> str:
    """Contrato do juiz (pt-BR) — 13/out/2026, spec do CEO.

    O prompt anterior so pedia "decida pelas palavras do cliente" e nem
    definia win/loss: o juiz nao tinha como emitir win quando a equipe
    propoe horario e o paciente so diz "pode ser". Agora win/loss/open tem
    definicao operacional e o juiz devolve as citacoes que o codigo valida.
    """
    return (
        "Voce e o juiz de status de uma conversa comercial. A transcricao tem "
        'papeis: "customer" (o cliente), "assistant" (a IA) e "equipe" (pessoa '
        "da equipe escrevendo pelo app). O campo agent_reply e a resposta que "
        "a IA acabou de enviar.\n\n"
        "Definicoes (obrigatorias):\n"
        "- win: o cliente CONCRETIZOU o objetivo comercial. Consulta, avaliacao "
        "ou servico AGENDADO com dia e horario definidos E aceitos pelo cliente "
        "(a equipe ou o agente propoe/confirma e o cliente concorda, ou o "
        "cliente escolhe e a equipe/agente confirma), ou o cliente aceitou "
        "proposta, pagou ou fechou.\n"
        "- NAO e win: intencao (\"quero marcar\"), pergunta de preco, de horario "
        "ou de vaga, \"vou pensar\", agente prometendo \"vou ver os horarios\", "
        "horario so sugerido sem aceite do cliente.\n"
        "- loss: recusa EXPLICITA do cliente ou pedido para parar de receber "
        "mensagem. Hesitacao ou adiamento (\"vou pensar\", \"depois eu vejo\") NAO "
        "e loss.\n"
        "- open: qualquer outro caso. Na duvida, open.\n\n"
        "Responda SOMENTE com JSON puro, sem markdown e sem texto fora do JSON:\n"
        '{"customer_status":"open|win|loss","confidence":0.0,"customer_quote":"",'
        '"schedule_quote":"","agent_commitment":null}\n\n'
        "- customer_status: exatamente \"win\", \"loss\" ou \"open\".\n"
        "- confidence: numero entre 0 e 1. win exige evidencia forte "
        f"(>= {STATUS_JUDGE_WIN_MIN_CONFIDENCE}); loss exige recusa inequivoca "
        f"(>= {STATUS_JUDGE_LOSS_MIN_CONFIDENCE}).\n"
        "- customer_quote: copia LITERAL de uma fala do cliente que aparece na "
        "transcricao, sem reescrever acento, maiuscula ou pontuacao. Vazio "
        '("") quando customer_status for open.\n'
        "- schedule_quote: copia LITERAL (de qualquer papel) com o dia, o "
        "horario ou o fechamento do acordo, tal como aparece na transcricao. "
        'Vazio ("") quando customer_status for open.\n'
        "- agent_commitment: promessa/compromisso do agente, ou null.\n"
        "Se houver embedding_hint, trate como indicio fraco: nunca decide sozinho."
    )


def _quote_verifies(raw_quote: object, speeches: Sequence[str]) -> bool:
    """Citacao existe na transcricao apos normalizar os dois lados."""
    if not isinstance(raw_quote, str):
        return False
    quote = normalize_judge_text(raw_quote)
    if not quote:
        return False
    return any(quote in speech for speech in speeches)


def _judge_verdict_holds(candidate: str, parsed: dict[str, Any], status_input: StatusInput) -> bool:
    """Valida a alegacao do juiz contra a transcricao (A4 da spec de 13/out/2026).

    Substitui o antigo rebaixamento por ``evidence_source`` autodeclarado, que
    rebaixava quase todo win para open porque o default do campo era "llm".
    """
    confidence = _safe_confidence(parsed.get("confidence"))
    fala_do_cliente = [
        normalize_judge_text(status_input.current_lead),
        *(
            normalize_judge_text(texto)
            for papel, texto in status_input.history
            if papel == "customer"
        ),
    ]
    if candidate == "win":
        if confidence < STATUS_JUDGE_WIN_MIN_CONFIDENCE:
            return False
        if not _quote_verifies(parsed.get("customer_quote"), fala_do_cliente):
            return False
        # schedule_quote pode vir de qualquer papel — inclusive da resposta do
        # agente deste turno, que o juiz tambem enxerga em agent_reply.
        qualquer_fala = [
            *fala_do_cliente,
            normalize_judge_text(status_input.agent_reply),
            *(
                normalize_judge_text(texto)
                for papel, texto in status_input.history
                if papel != "customer"
            ),
        ]
        return _quote_verifies(parsed.get("schedule_quote"), qualquer_fala)
    if candidate == "loss":
        return (
            confidence >= STATUS_JUDGE_LOSS_MIN_CONFIDENCE
            and _quote_verifies(parsed.get("customer_quote"), fala_do_cliente)
        )
    return True


def _booking_cue_in_turn(status_input: StatusInput) -> bool:
    """Pista de agendamento nas ultimas falas de IA/equipe, na resposta deste
    turno ou na propria fala do cliente. Sem pista, um ``open`` do agente
    dispensa o juiz (voto, 20/out/2026)."""
    if has_recent_booking_cue(
        [*status_input.history, ("assistant", status_input.agent_reply)]
    ):
        return True
    fala = normalize_judge_text(status_input.current_lead)
    if _CLOSING_CUE.search(fala):
        return True
    return any(padrao.search(fala) for padrao in _BOOKING_CUE_PATTERNS)


# Fechamento sem horario ("Fechado!", "Ja fiz o deposito") tambem leva ao juiz.
_CLOSING_CUE = re.compile(r"\b(?:fechad|fecho|pag|deposit|pix|comprovante|transferi)")


def _agent_signal_holds(signal: AgentStatusSignal, status_input: StatusInput) -> bool:
    """Voto terminal do agente so vale com as mesmas citacoes exigidas do juiz."""
    if signal.status == "open":
        return True
    return _judge_verdict_holds(
        signal.status,
        {
            # O agente nao da confianca; a prova e a citacao verificada.
            "confidence": 1.0,
            "customer_quote": signal.citacao_cliente,
            "schedule_quote": signal.citacao_horario,
        },
        status_input,
    )


def _with_evidence(
    result: StatusResult, record: bool, caminho: str, **extra: Any
) -> StatusResult:
    if not record:
        return result
    return result.model_copy(update={"evidence": {"caminho": caminho, **extra}})


def classify_status(
    status_input: StatusInput,
    *,
    settings: Settings,
    http: Any,
    embed_client: Any,
    model_override: str | None = None,
    agent_signal: AgentStatusSignal | None = None,
    vote: bool = False,
    record_evidence: bool = False,
    inicio_de_conversa: bool = False,
) -> StatusResult:
    """Roda regra -> embedding -> LLM residual sobre um dialogo pos-rascunho.

    - Camada de regra e autoritativa (so a fala do lead).
    - Camada de embedding e consultiva; um rotulo terminal nunca muta estado.
    - O LLM residual ve a transcricao rotulada e so mantem win/loss quando as
      citacoes que ele devolveu existem na transcricao e a confidence passa no
      limiar do estado; qualquer falha cai para ``open`` com
      ``evidence_source="default"`` (o compromisso do agente e preservado).

    Voto do agente (20/out/2026, ``docs/specs/status-no-agente.md``):
    ``agent_signal`` e o status que o agente principal deu junto da resposta.
    Com ``vote=False`` (sombra) ele so e gravado em ``evidence``. Com
    ``vote=True`` a regra e assimetrica, como mediu a bateria hard (121 casos):

    - agente ``open`` sem pista de agendamento/fechamento dispensa o juiz;
    - ``win`` do juiz vale sozinho: o agente vota ``open`` quando a equipe
      ainda vai confirmar, e exigir acordo derrubava 6 de 16 wins;
    - ``loss`` do juiz so vale se o agente tambem disser ``loss`` com citacao
      verificada e se a fala nao for vetada por regra (``llm_loss_vetado``:
      "Nao" sozinho, recusa de um tipo de conteudo, adiamento). O juiz sozinho
      deu 11 falsos loss na bateria hard. Loss pausa a automacao; na duvida,
      ``open``.

    ``record_evidence`` grava o caminho.
    """
    agente = agent_signal.model_dump() if agent_signal is not None else None
    agente_ok = (
        _agent_signal_holds(agent_signal, status_input)
        if agent_signal is not None
        else None
    )

    rule_status = detect_status_by_rule(status_input.current_lead)
    if rule_status is not None:
        return _with_evidence(
            StatusResult(
                customer_status=rule_status,
                confidence=1.0,
                evidence_source="rule",
            ),
            record_evidence,
            "regra",
            agente=agente,
            agente_citacao_ok=agente_ok,
        )

    # Camada pseudo-IA (app.status.rules.is_trivial_message): agradecimento,
    # risada ou so emoji nao carrega sinal de status -- default sem pagar a
    # chamada de embedding nem de LLM. So chega aqui quando a regra acima ja
    # devolveu None, entao nunca suprime um win/loss que a regra teria achado.
    # Excecao de 13/out/2026: com pista de agendamento recente do outro lado da
    # conversa, "obrigada" pode ser o aceite de uma confirmacao -- vai pro juiz.
    if is_trivial_message(status_input.current_lead) and not has_recent_booking_cue(
        status_input.history
    ):
        return _with_evidence(
            StatusResult(),
            record_evidence,
            "trivial",
            agente=agente,
            agente_citacao_ok=agente_ok,
        )

    # 31/out/2026: no comeco da conversa (o chamador conta no banco) o juiz nunca saiu de open
    # (clínica, 30 dias: 0 de 106 nos dois primeiros julgamentos). A regra acima ja rodou, entao
    # uma recusa explicita no primeiro turno continua virando loss; com pista de agendamento
    # o juiz roda mesmo assim.
    if (
        inicio_de_conversa
        and not _booking_cue_in_turn(status_input)
        and not has_recent_booking_cue(status_input.history)
    ):
        return _with_evidence(
            StatusResult(),
            record_evidence,
            "inicio_de_conversa",
            agente=agente,
            agente_citacao_ok=agente_ok,
        )

    if (
        vote
        and agent_signal is not None
        and agent_signal.status == "open"
        and not _booking_cue_in_turn(status_input)
    ):
        return _with_evidence(
            StatusResult(evidence_source="agent"),
            record_evidence,
            "agente_open_sem_pista",
            agente=agente,
            agente_citacao_ok=agente_ok,
        )

    # Embedding e um hint semantico apenas. Acumulado no contexto do LLM
    # residual em vez de decidir sozinho: um score vetorial local nao
    # distingue uma promessa do agente de uma intencao do cliente.
    embed_status = _classify_by_embedding(status_input.current_lead, embed_client)
    judge, parsed = _run_judge(
        status_input,
        settings=settings,
        http=http,
        model_override=model_override,
        embed_status=embed_status,
    )
    juiz = None
    if parsed is not None:
        juiz = {
            "status": parsed.get("customer_status"),
            "customer_quote": parsed.get("customer_quote"),
            "schedule_quote": parsed.get("schedule_quote"),
            "confidence": _safe_confidence(parsed.get("confidence")),
        }
    trilha = {
        "agente": agente,
        "agente_citacao_ok": agente_ok,
        "juiz": juiz,
        "juiz_aplicado": judge.customer_status,
        "embedding_hint": embed_status,
    }

    if not vote or agent_signal is None:
        return _with_evidence(judge, record_evidence, "juiz", **trilha)

    if judge.customer_status != "loss":
        caminho = "acordo" if judge.customer_status == agent_signal.status else "juiz"
        return _with_evidence(judge, record_evidence, caminho, **trilha)
    if agent_signal.status == "loss" and agente_ok and not llm_loss_vetado(
        status_input.current_lead
    ):
        return _with_evidence(judge, record_evidence, "acordo", **trilha)
    return _with_evidence(
        StatusResult(
            customer_status="open",
            agent_commitment=judge.agent_commitment,
            confidence=judge.confidence,
            evidence_source="default",
        ),
        record_evidence,
        "loss_sem_acordo",
        **trilha,
    )


def _run_judge(
    status_input: StatusInput,
    *,
    settings: Settings,
    http: Any,
    model_override: str | None,
    embed_status: str | None,
) -> tuple[StatusResult, dict[str, Any] | None]:
    """Camada 3. Devolve o resultado validado e a resposta crua do juiz
    (``None`` quando a chamada ou o parse falham)."""
    base_url = settings.STATUS_AUDIT_BASE_URL or settings.OMNIROUTE_BASE_URL
    model = model_override or settings.STATUS_AUDIT_MODEL or settings.OMNIROUTE_MODEL
    api_key = settings.STATUS_AUDIT_API_KEY or settings.OMNIROUTE_API_KEY

    messages = [
        {"role": "system", "content": _judge_system_prompt()},
        {
            "role": "user",
            "content": json.dumps(
                build_status_context(status_input, embedding_hint=embed_status),
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        },
    ]

    try:
        response = http.post(
            f"{base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {_secret_value(api_key)}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "temperature": 0,
                "stream": False,
                # 27/ago/2026: 50 estourava com agent_commitment nao-vazio
                # (achado por revisao externa) -- JSON truncado, parse falha,
                # default open (compromisso do agente se perde de graca).
                "max_tokens": STATUS_JUDGE_MAX_TOKENS,
                # 13/out/2026: o deepseek-v4-flash raciocina antes de responder e
                # o raciocinio consumia todo o max_tokens (finish=length, content
                # vazio, parse falha, open). Mesmo desligamento do resumo de
                # historico (history_summary.py) e do agente de resposta.
                "thinking": {"type": "disabled"},
                "messages": messages,
            },
        )
    except Exception:
        logger.warning("status LLM failed; defaulting to open", exc_info=True)
        return StatusResult(), None

    if getattr(response, "status_code", None) != 200:
        return StatusResult(), None

    try:
        parsed = parse_status_llm_response(response.text)
    except Exception:
        logger.warning("status LLM parse failed; defaulting to open", exc_info=True)
        return StatusResult(), None

    candidate_status = parsed.get("customer_status", "open")
    if candidate_status not in {"open", "win", "loss"}:
        candidate_status = "open"

    commitment = parsed.get("agent_commitment")
    if not isinstance(commitment, str) or not commitment.strip():
        commitment = None
    confidence = _safe_confidence(parsed.get("confidence"))

    if candidate_status in {"win", "loss"} and not _judge_verdict_holds(
        candidate_status, parsed, status_input
    ):
        # Alegacao terminal sem prova na transcricao: open com a confidence que
        # o juiz deu (pro observador ver quao "certo" ele estava) e o
        # compromisso do agente preservado como contexto.
        return (
            StatusResult(
                customer_status="open",
                agent_commitment=commitment,
                confidence=confidence,
                evidence_source="default",
            ),
            parsed,
        )

    return (
        StatusResult(
            customer_status=candidate_status,
            agent_commitment=commitment,
            confidence=confidence,
            evidence_source="llm",
        ),
        parsed,
    )
