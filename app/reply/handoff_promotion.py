"""Promoção determinística de "vou encaminhar à equipe" para handoff real.

Problema medido (tenant clínica): o modelo responde "Recebi seus dados,
a equipe vai confirmar o dia e o horário" ou "vou encaminhar você para a
recepção" com ``action="send_text"``. Só ``action="handoff"`` avisa o gestor
(``manager_jid``) e entrega o contato à equipe; com ``send_text`` ninguém é
avisado e o paciente fica esperando.

Aqui, em código e sem LLM: se a RESPOSTA do modelo declara que a equipe /
recepção / atendimento vai assumir, confirmar, retornar, verificar ou receber o
caso, o draft vira ``handoff`` com o MESMO texto. O handoff resultante segue o
caminho já existente em ``app/durable/flow.py`` (manager_jid, zernio, fallback).

Escopo por tenant: ``Settings.HANDOFF_PROMOTE_TENANTS`` (vírgula, padrão vazio =
desligado). Tenants fora da lista: comportamento atual intacto.

Gatilho x informação: citar a equipe numa informação ("nossa equipe tem pediatra
de segunda a sexta", "a equipe atende de segunda a sexta") NÃO promove. Promove
quando há um verbo de ENCAMINHAMENTO ou de PROMESSA (passando pra equipe, vai
confirmar, retorna, entram em contato, vou confirmar com a equipe...). Ofertas e
perguntas ("quer que eu passe os dados pra equipe?") ainda não encaminharam e
também não promovem.

Normalização igual à de ``safety_routes`` (armadilha #17 de docs/TRAPS.md): sem
acento, casefold, só letras — nenhum padrão depende de símbolo.
"""

from __future__ import annotations

import logging
import re
import unicodedata

from app.contracts import ReplyDraft
from app.settings import get_settings

logger = logging.getLogger(__name__)


def _normalize(text: str) -> str:
    """Casefold + remove acento + só letras (mesmo critério de safety_routes)."""
    nfkd = unicodedata.normalize("NFKD", text)
    sem_acento = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"[^a-z]+", " ", sem_acento.casefold()).strip()


# --- Sujeito/destino: a equipe -------------------------------------------------
_ART = r"(?:(?:a|o|as|os|nossa|nosso|minha|uma|um|essa|nessa|na|no|da|do) )?"
_SUBJ = (
    _ART
    + r"(?:equipe|recepcao|atendimento|time|atendente|secretaria|setor|"
    r"pessoal (?:do|da) (?:kyrios|clinica|atendimento|recepcao)|"
    r"(?:pessoa|alguem) (?:d[ao] )?(?:equipe|recepcao|atendimento|time))"
    r"(?: (?:do|da|de) (?:kyrios|clinica|atendimento|agendamento|recepcao))?"
)

# Pessoa e verbo de encaminhamento (1ª pessoa / gerúndio / passado).
_FORWARD_VERBS = (
    r"(?:passando|passar|passei|passo|repassando|repassar|repassei|repasso|"
    r"encaminhando|encaminhar|encaminhei|encaminho|transferindo|transferir|"
    r"transferi|transfiro|direcionando|direcionar|direcionei|direciono|"
    r"enviando|enviei|envio)"
)
_NOTIFY_VERBS = (
    r"(?:avisar|avisando|avisei|aviso|chamar|chamando|chamei|chamo|acionar|"
    r"acionando|acionei|aciono|notificar|notificando|notifiquei|notifico)"
)
_CHECK_VERBS = (
    r"(?:confirmar|confirmo|verificar|verifico|checar|checo|consultar|consulto|"
    r"alinhar|alinho|ver|falar|fala|conversar|combinar|perguntar|pergunto)"
)

# Verbos que a equipe CUMPRE (futuro/promessa) depois do sujeito.
_TEAM_FUTURE = (
    r"(?:ja |logo |em breve |tambem |entao )*"
    r"(?:vai|vao|ira|irao|vem) (?:ja |logo |em breve |tambem )*"
    r"(?:te |lhe |voce )?"
    r"(?:confirmar|verificar|checar|retornar|entrar em contato|assumir|receber|"
    r"responder|ligar|chamar|falar|dar (?:um )?retorno|cuidar|contatar|agendar|"
    r"analisar|avaliar|ver|passar|mandar|enviar|alinhar)"
)
_TEAM_SYNTH = (
    r"(?:confirmara|verificara|checara|retornara|assumira|recebera|respondera|"
    r"cuidara|contatara|agendara|ligara|entrara em contato|analisara|avaliara)"
)
_TEAM_PRESENT = (
    r"(?:retorna|retornam|assume|assumem|entra em contato|entram em contato|"
    r"da retorno|dao retorno|recebe seus dados|recebem seus dados|"
    r"ja recebeu|ja recebeu seus dados|confirma|confirmam|verifica|verificam)"
)
# Oração relativa: "a recepção, que verifica o horário".
_TEAM_RELATIVE = r"que (?:confirma|confirmam|verifica|verificam|checa|checam|retorna|retornam)"

_GAP = r"(?: \w+){0,2}? "

_PROMOTE_RES: tuple[re.Pattern[str], ...] = (
    # 1) "passando pra equipe", "vou encaminhar você para a recepção",
    #    "te transfiro pro atendimento"  (verbo de encaminhamento -> destino).
    re.compile(rf"\b{_FORWARD_VERBS}\b.{{0,70}}?\b(?:para|pra|pro|ao|a) {_SUBJ}\b"),
    re.compile(rf"\b{_FORWARD_VERBS}\b.{{0,70}}?\b(?:pro) (?:time|atendimento|setor)\b"),
    # 2) Passivo/particípio: "seus dados foram encaminhados para a equipe".
    re.compile(
        rf"\b(?:encaminhad|repassad|passad|enviad|transferid)[oa]s?\b.{{0,50}}?"
        rf"\b(?:para|pra|pro|ao|a|com) {_SUBJ}\b"
    ),
    # 3) Avisar/chamar/acionar a equipe (ação já tomada ou em curso).
    re.compile(rf"\b{_NOTIFY_VERBS} {_SUBJ}\b"),
    # 4) "vou confirmar isso com a equipe", "deixa eu verificar com a recepção".
    re.compile(
        rf"\b(?:vou|vamos|irei|deixa eu|deixe eu|preciso|vou so|vou ja|ja vou|"
        rf"estou|to|eu) (?:so |ja |agora |rapidinho |logo )*{_CHECK_VERBS}\b.{{0,45}}?"
        rf"\b(?:com|junto (?:a|com|de)|na|no|a) {_SUBJ}\b"
    ),
    re.compile(
        rf"\b(?:vou|vamos|irei) (?:so |ja |agora |logo )*pedir (?:para|pra|a|ao) {_SUBJ}\b"
    ),
    # 5) Sujeito equipe + promessa: "a equipe vai confirmar", "a recepção retorna".
    re.compile(rf"\b{_SUBJ}(?: que)?{_GAP}{_TEAM_FUTURE}\b"),
    re.compile(rf"\b{_SUBJ}(?: que)?{_GAP}{_TEAM_SYNTH}\b"),
    re.compile(rf"\b{_SUBJ}(?: que)?{_GAP}{_TEAM_PRESENT}\b"),
    re.compile(rf"\b{_SUBJ} {_TEAM_RELATIVE}\b"),
    # 5b) "quem confirma certinho é a nossa equipe", "quem verifica isso é a recepção".
    re.compile(
        rf"\bquem (?:confirma|confirmam|verifica|verificam|retorna|retornam)(?: \w+){{0,3}}? e (?:a |o )?{_SUBJ}\b"
    ),
    # 6) Com a equipe: "seus dados ja estao com a equipe", "deixei tudo com a recepção".
    re.compile(
        rf"\b(?:dados|tudo|isso|caso|pedido|solicitacao|cadastro|informacoes|ficha|"
        rf"contato|numero|agendamento)\b(?: \w+){{0,3}}? "
        rf"(?:esta|estao|ficou|ficaram|fica|ficam|deixei|deixado) (?:\w+ ){{0,2}}?com {_SUBJ}\b"
    ),
    re.compile(
        rf"\b(?:deixei|deixando) (?:\w+ ){{0,3}}?com {_SUBJ}\b"
    ),
    # 7) Plural da clínica: "entraremos em contato", "retornaremos", "daremos retorno".
    re.compile(
        r"\b(?:entraremos em contato|retornaremos|daremos (?:um )?retorno|"
        r"confirmaremos|verificaremos|checaremos|vamos entrar em contato|"
        r"vamos te retornar|vamos retornar|"
        r"te retornamos|te respondemos|entramos em contato em breve)\b"
    ),
)

# Ofertas e pedidos de permissão: ainda não encaminhou.
_OFFER_RE = re.compile(
    r"\b(?:posso|podemos|poderia|poderiamos|podia|gostaria|gostariam|"
    r"quer que|quer|queres|deseja|desejaria|prefere|preferia|"
    r"se quiser|se preferir|se precisar|se necessario|caso queira|caso precise|"
    r"caso prefira|caso deseje|se voce quiser|se desejar|voce pode|pode|podem|"
    r"basta|so preciso|e so|so me)\b"
)
# Negação do encaminhamento ("não vou passar pra equipe").
_NEGATION_RE = re.compile(
    r"\b(?:nao (?:vou|vai|vao|vamos|irei|posso|podemos|consigo|temos como|e possivel)|"
    r"sem (?:a |o )?(?:equipe|recepcao|atendimento|time))\b"
)
# Pergunta que começa por interrogativo: informação pedida, não promessa.
_QUESTION_START_RE = re.compile(
    r"^(?:qual|quais|quando|quanto|quantos|quantas|como|onde|que|voce|vc|tem|ha|"
    r"pode|podem|sera|seria|ja|precisa|prefere|quer|gostaria|deseja)\b"
)

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+|\n+")


def _sentence_is_forwarding(sentence: str) -> bool:
    normalized = _normalize(sentence)
    if not normalized:
        return False
    if _OFFER_RE.search(normalized) or _NEGATION_RE.search(normalized):
        return False
    if sentence.rstrip().endswith("?") and _QUESTION_START_RE.search(normalized):
        return False
    return any(pattern.search(normalized) for pattern in _PROMOTE_RES)


def declares_forwarding(text: str) -> bool:
    """True quando o texto da resposta declara que a equipe vai assumir o caso."""
    if not text or not text.strip():
        return False
    return any(
        _sentence_is_forwarding(sentence)
        for sentence in _SENTENCE_SPLIT_RE.split(text)
        if sentence and sentence.strip()
    )


def _promote_tenants() -> frozenset[str]:
    """Tenants com a promoção ativa. Padrão vazio = desligado para todos."""
    raw = getattr(get_settings(), "HANDOFF_PROMOTE_TENANTS", "") or ""
    return frozenset(t.strip() for t in raw.split(",") if t.strip())


def handoff_promotion_enabled(tenant_id: str) -> bool:
    return tenant_id in _promote_tenants()


def promote_forwarding_to_handoff(tenant_id: str, draft: ReplyDraft) -> ReplyDraft:
    """Converte ``send_text`` que promete encaminhamento em ``handoff``.

    Devolve o MESMO objeto quando não promove (o chamador pode usar ``is not``
    para saber se houve promoção). Log estruturado leva tenant e flag, nunca o
    texto do paciente nem o da resposta.
    """
    if draft.action != "send_text" or not handoff_promotion_enabled(tenant_id):
        return draft
    if not declares_forwarding(draft.text):
        return draft
    logger.info(
        "handoff_promoted",
        extra={
            "event": "handoff_promoted",
            "tenant_id": tenant_id,
            "promoted": True,
        },
    )
    return ReplyDraft(
        action="handoff",
        text=draft.text,
        llm_model=draft.llm_model,
        status_signal=draft.status_signal,
        fatos=draft.fatos,
    )
