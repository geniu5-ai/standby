"""Fatos da conversa derivados por REGRA, sem LLM (24/out/2026).

Entradas que o status_workflow ja tem: a fala do paciente, a resposta enviada e a acao (send_text/handoff).
Pedir fatos na resposta da Julia piorou as regras duras da clínica (1/200 -> 4/195) e o extrator por LLM
errou o que mais importa ("ok" virou pergunta aberta). Regex e deterministico, barato e nao toca o pool.
Conservador de proposito: na duvida, nenhum fato. "ok", "obrigada" e emoji de aceite nunca geram fato.
"""

from __future__ import annotations

import re
import unicodedata

from app.contracts import FatoConversa
from app.status.rules import is_deferral_or_closing

_ADIAR = re.compile(
    r"\b(?:(?:vou|vamos|vo) (?:falar|conversar|ver|combinar|consultar) com (?:meu|minha|o|a|os|as|ele|ela)\b"
    r"|(?:te |lhe )?aviso (?:depois|quando|mais tarde)|me d[aeu] um tempo|preciso (?:pensar|ver)|vou (?:pensar|ver depois))"
)
_REMARCAR = re.compile(r"\b(?:remarc|reagend|outro horario|trocar o (?:dia|horario)|nao vou (?:poder|conseguir|amanha|hoje|ir)|nao posso ir|nao consigo ir)")
_ATRASO = re.compile(r"\b(?:vou (?:me )?(?:atrasar|chegar)|to (?:presa|preso|atrasad[ao])|estou (?:presa|preso|atrasad[ao])|chegar umas? \d|atrasad[ao])")
_RETORNO = re.compile(r"\b(?:me (?:lembra|lembre|avisa|chama|liga)\b|lembra(?:r)? (?:me )?(?:de )?(?:amanha|hoje)?\s*(?:as|umas)?\s*\d)")
_RECUSA = re.compile(r"\b(?:nao quero|nao vou querer|ja disse que nao|nao tenho interesse|desisti|nao preciso mais|pode (?:cancelar|parar))\b")
_ACEITE = re.compile(r"\b(?:pode ser|fechado|combinado|confirmo|quero (?:esse|o primeiro|o segundo|a primeira|a segunda)|esse mesmo|isso mesmo)\b")
_TEM_HORARIO = re.compile(r"\b(?:\d{1,2}\s*(?:h|:)\s*\d{0,2}|segunda|terca|quarta|quinta|sexta|sabado|domingo)\b")
_PROMESSA = re.compile(r"\b(?:confirm(?:ar|o|a) com a equipe|ja te retorno|retorno (?:em breve|logo)|equipe (?:confirma|retorna|verifica|te)|vou (?:verificar|checar|ver com))\b")
# Aceita modificador de tom de pele, ZWJ, sinais de genero e seletor de variacao (🤷🏾, 🤷🏼‍♀️).
_SO_INTERROGACAO = re.compile(r"^[\s?!.\u2753\u2754\U0001F937\U0001F914\uff1f\U0001F3FB-\U0001F3FF\u200d\u2640\u2642\ufe0f]*$")


def _norm(texto: str) -> str:
    s = unicodedata.normalize("NFKD", texto.lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def derivar_fatos(fala_paciente: str, resposta: str, acao: str = "send_text") -> list[FatoConversa]:
    paciente, julia = _norm(fala_paciente), _norm(resposta)
    fatos: list[FatoConversa] = []

    def add(tipo: str, detalhe: str = "") -> None:
        if tipo not in {f.tipo for f in fatos}:
            fatos.append(FatoConversa(tipo=tipo, detalhe=detalhe[:120]))  # type: ignore[arg-type]

    if _SO_INTERROGACAO.match(fala_paciente) and re.search(r"[?❓❔\U0001F937\U0001F914？]", fala_paciente):
        add("pergunta_aberta", "pergunta sem texto")
    if _RECUSA.search(paciente):
        add("recusa", fala_paciente)
    if _ADIAR.search(paciente) or is_deferral_or_closing(fala_paciente):
        add("adiamento", fala_paciente)
    if _REMARCAR.search(paciente):
        add("remarcacao", fala_paciente)
    if _ATRASO.search(paciente):
        add("atraso", fala_paciente)
    if _RETORNO.search(paciente):
        add("pedido_retorno", fala_paciente)
    if _ACEITE.search(paciente) and _TEM_HORARIO.search(paciente + " " + julia):
        add("agendamento", resposta)
    if acao == "handoff" or _PROMESSA.search(julia):
        add("promessa", resposta)
    return fatos
