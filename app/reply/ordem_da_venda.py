"""Ordem da venda (regra dura do chairman, 06/nov/2026): acolher, engajar, depois o preço.

Duas travas deterministicas sobre a resposta do agente, sem LLM juiz:

1. ``preco_antes_do_engajamento``: na PRIMEIRA resposta da conversa, valor em reais so
   aparece se a pessoa ja deu o contexto (disse para quem e, mandou pedido, quer so o
   valor, pediu varios itens ou deu a intencao de marcar).
2. ``oferta_junto_do_preco``: valor e oferta de agendar na mesma mensagem, sem a pessoa
   ter dado a intencao de marcar. Preco, perguntas, e so depois a oferta.

Ligada por tenant (``ORDEM_DA_VENDA_TENANTS``). Quem decide e o codigo, nao o prompt.
"""

from __future__ import annotations

import re
import unicodedata
import zlib
from typing import Literal

Violacao = Literal["preco_antes_do_engajamento", "oferta_junto_do_preco", "sem_insistencia"]

_PRECO = re.compile(r"R\$\s*\d|\d+(?:[.,]\d{2})?\s*reais", re.I)
_CONTEXTO = re.compile(
    r"\b(?:pra mim|para mim|pra minha|para minha|pro meu|para o meu|meu|minha|meus|minhas|"
    r"filho|filha|filhos|mae|pai|marido|esposa|esposo|bebe|crianca|neto|neta|avo|sogra|sogro|"
    r"irma|irmao|tia|tio)\b"
)
_SO_VALOR = re.compile(r"\b(?:so|apenas|somente)\b[^.?!]{0,25}\b(?:valor|preco|quanto)\b")
_PEDIDO = re.compile(r"\[(?:imagem|documento|pdf)\]|pedido medico|requisicao|receita")
_INTENCAO = re.compile(r"\b(?:marcar|agendar|marque|agende|remarcar)\b|^(?:sim|quero|pode|bora|vamos|fechado)\W*$")
_OFERTA = re.compile(r"\b(?:gostaria de|quer|posso|vamos|podemos|deseja|que tal)\b[^.?!]{0,40}\b(?:agendar|marcar|agendamento)\b")
_INDECISAO = re.compile(
    r"\b(?:vou pensar|deixa eu pensar|preciso pensar|vou ver|depois eu vejo|vou analisar|vou decidir|"
    r"vou conversar|mais tarde eu|qualquer coisa eu chamo)\b"
)
_RECUSA = re.compile(r"\b(?:agora nao|nao quero|nao tenho interesse|nao e o momento|sem interesse|nao me mande|pare de)\b")
_ITENS = (
    r"hemograma", r"ultrassonografia|ultrassom", r"raio x|raio-x", r"mamografia", r"tomografia",
    r"densitometria", r"endoscopia", r"consulta", r"vacina", r"exame",
)


def _norm(texto: str) -> str:
    sem_acento = "".join(c for c in unicodedata.normalize("NFKD", texto) if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9\[\]\s-]", " ", sem_acento.casefold()).strip()


def tem_preco(texto: str) -> bool:
    return bool(_PRECO.search(texto or ""))


def _varios_itens(turno: str) -> bool:
    return sum(1 for padrao in _ITENS if re.search(padrao, turno)) >= 2


def violacao_da_ordem(
    resposta: str, turno_do_cliente: str, historico: tuple[tuple[str, str], ...] = ()
) -> Violacao | None:
    """Qual regra a resposta quebra, ou ``None``. Puro: so texto, sem banco nem modelo."""
    turno = _norm(turno_do_cliente)
    if _INDECISAO.search(turno) and not _RECUSA.search(turno) and "?" not in (resposta or ""):
        return "sem_insistencia"
    if not tem_preco(resposta):
        return None
    intencao = bool(_INTENCAO.search(turno))
    if _OFERTA.search(_norm(resposta)) and not intencao:
        return "oferta_junto_do_preco"
    primeira = not any(papel == "assistant" for papel, _ in historico)
    if not primeira:
        return None
    if intencao or _CONTEXTO.search(turno) or _SO_VALOR.search(turno) or _PEDIDO.search(turno) or _varios_itens(turno):
        return None
    return "preco_antes_do_engajamento"


# Perguntas de abertura rotativas (genericas, valem para qualquer vertical). O codigo sorteia
# de forma deterministica por conversa e por numero de respostas ja dadas, para nao repetir.
ABERTURAS: tuple[str, ...] = (
    "me fala um pouco do seu caso pra eu te direcionar melhor",
    "o que te fez procurar a gente agora?",
    "conta pra mim o que voce esta precisando",
    "voce ja tem pedido medico ou esta comecando a pesquisar?",
    "me conta como esta a situacao, que eu te ajudo da melhor forma",
    "e algo que voce ja vinha pensando em resolver?",
)


def _abertura(chave: str, historico: tuple[tuple[str, str], ...]) -> str:
    ja_respondidas = sum(1 for papel, _ in historico if papel == "assistant")
    return ABERTURAS[(zlib.crc32(chave.encode("utf-8")) + ja_respondidas) % len(ABERTURAS)]


def _preco_ja_dado(historico: tuple[tuple[str, str], ...]) -> bool:
    return any(papel == "assistant" and tem_preco(texto) for papel, texto in historico)


def etapa_da_venda(
    turno_do_cliente: str, historico: tuple[tuple[str, str], ...] = (), chave: str = ""
) -> dict[str, bool | str]:
    """Estado da venda calculado em codigo (nao pelo modelo) e entregue como dado no contexto.

    ``preco_liberado``: a pessoa ja deu o contexto, ja houve resposta anterior, quer so o valor,
    mandou pedido, pediu varios itens ou deu a intencao de marcar.
    ``oferta_de_agendamento_liberada``: a pessoa deu a intencao de marcar, ou o valor ja foi dito
    e a conversa seguiu depois dele (ha fala do cliente apos a mensagem com o valor).
    """
    turno = _norm(turno_do_cliente)
    intencao = bool(_INTENCAO.search(turno))
    primeira = not any(papel == "assistant" for papel, _ in historico)
    contexto = bool(
        intencao or _CONTEXTO.search(turno) or _SO_VALOR.search(turno) or _PEDIDO.search(turno) or _varios_itens(turno)
    )
    depois_do_valor = False
    visto = False
    for papel, texto in historico:
        if papel == "assistant" and tem_preco(texto):
            visto = True
        elif visto and papel in ("customer", "user"):
            depois_do_valor = True
    estado: dict[str, bool | str] = {
        "preco_liberado": (not primeira) or contexto,
        "oferta_de_agendamento_liberada": intencao or depois_do_valor,
    }
    if not estado["preco_liberado"]:
        estado["pergunta_de_abertura"] = _abertura(chave, historico)
    return estado


INSTRUCAO_POR_VIOLACAO: dict[str, str] = {
    "sem_insistencia": (
        "A pessoa esta indecisa ou vai pensar. Nao se despeca: reescreva com carinho, sem pressao e sem "
        "urgencia inventada, e termine com uma pergunta leve que ajude a decidir (o que esta pesando? "
        "ficou alguma duvida?)."
    ),
    "preco_antes_do_engajamento": (
        "Esta e a primeira resposta e a pessoa ainda nao contou o caso dela. Reescreva sem citar valor "
        "nem forma de pagamento: acolha em uma frase calorosa e convide a pessoa a contar um pouco do "
        "caso dela (varie a pergunta). O valor vem na proxima mensagem."
    ),
    "oferta_junto_do_preco": (
        "Nao ofereca agendamento na mesma mensagem em que da o valor. Reescreva mantendo o valor e "
        "troque a oferta por uma pergunta de conversa (para quando ela precisa? ficou alguma duvida?)."
    ),
}
