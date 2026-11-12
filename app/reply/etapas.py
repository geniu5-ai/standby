"""A etapa da conversa, computada em codigo em vez de inferida pelo modelo.

Por que existe
--------------
O prompt do tenant descreve um protocolo de 7 etapas e manda o modelo obedecer
a etapa em que a conversa esta.  Descobrir a etapa, porem, e' justamente o que
um modelo com ``thinking=False`` nao faz: ele nao tem estado e nao raciocina,
entao recomputa de cabeca, a cada turno, uma coisa que o historico ja sabe.

O sintoma foi medido em 28/ago/2026, oito vezes.  Na pergunta fria de preco
("quanto custa a flex?") o modelo precisa inventar uma pergunta de qualificacao
e escolhe sempre o unico eixo que o dominio oferece entre "protese flexivel" e
"valor": dente.  Sete reescritas de prompt (v18, v19, persuasao v4 e v5, v22,
v23, v24) nao moveram o numero.  A oitava tentativa — injetar o estado no
contexto do turno — moveu pouco, e o motivo e' de posicao: o contexto e' o
bloco de DADO, que por construcao nao tem autoridade de instrucao
(``agents/prompter/library/2026-manual-operacional-de-prompt-e-contexto.md``,
secao 3).  Aqui o estado volta como instrucao de verdade, emitida por
``@reply_agent.instructions``, no fim do prompt montado.

O que este modulo NAO faz
-------------------------
Nao modela as 7 etapas.  Modela as tres situacoes que sao (a) computaveis com
confianca alta a partir do texto e (b) onde o modelo erra sozinho.  O meio da
conversa — compadecer, mostrar o que esta em jogo, oferecer — nao e' computavel
por regex e nao esta quebrado: o redteam passa limpo em ``dor``, ``recuo seco``
e ``desconfia de golpe``.  Nesses casos ``andamento`` emite string vazia e quem
manda continua sendo o protocolo do prompt.  Regra marginal em cima de
comportamento que ja funciona tem valor esperado negativo
(``docs/2026-08-28-estado-da-arte-prompt.md``, proposicao 1).
"""

from __future__ import annotations

import re
from typing import Literal

from app.status.rules import _normalize

Etapa = Literal["abertura", "preco", "fechamento", "andamento"]

# Reaproveitado do node ``Briefing`` do flow n8n HHmfcsmJhirSVoE9, em producao
# ha meses no Genius Sell — o chairman escreveu e validou este padrao contra
# trafego real de WhatsApp brasileiro.  Roda sobre texto ja normalizado (sem
# acento, minusculo), por isso as alternativas acentuadas do original saem.
_PRECO = re.compile(
    r"\bquanto\s+(?:custa|fica|e|sai|ta|esta)\b"
    r"|\bqual\s+(?:e|o)\s+o\s+(?:preco|valor)\b"
    r"|\b(?:preco|valor|investimento|orcamento)\b"
    r"|\br\s*\$?\s*\d+",
)

# Intencao de marcar.  Deliberadamente frouxo: errar para ``fechamento`` custa
# uma mensagem que oferece dois dias; errar para ``andamento`` custa o funil
# nao fechar, que e' o achado de maior impacto do documento consolidado (secao
# 9, item 1).
_FECHAMENTO = re.compile(
    r"\b(?:bora|vamos|quero|posso|podemos|da\s+pra|gostaria\s+de)\s+"
    r"(?:marcar|agendar|remarcar)\b"
    r"|\bmarcar\s+(?:uma|um|a|o)?\s*(?:consulta|avaliacao|horario)\b"
    r"|\btem\s+(?:vaga|horario|hora)\b"
    r"|\bquando\s+(?:posso|pode|da)\s+(?:ir|marcar|ser)\b",
)

# Cada texto e' curto de proposito: entra no fim do prompt montado, onde
# concorre com 5 mil chars de negocio + catalogo.  Instrucao longa nesta
# posicao dilui a propria autoridade que a posicao da.
_INSTRUCOES: dict[str, str] = {
    "abertura": (
        "Situacao desta mensagem: e o primeiro contato dela e voce nao sabe nada "
        "do caso. Esta resposta e so recepcao — mostre que leu o que ela disse e "
        "abra espaco pra ela falar. Nada de tratamento, nada de valor, nada sobre "
        "a clinica."
    ),
    # 29/ago/2026: doutrina invertida (ordem do chairman, inspirada no prompt
    # provado do Genius Sell/n8n -- "pergunta de preco nao deve ser bloqueada
    # nem desviada"). Ate aqui a etapa proibia o numero e mandava qualificar
    # primeiro; medido na pratica (conversa real, mesmo dia) que isso vira
    # loop -- o bot reformula a mesma pergunta em turnos seguidos e recusa
    # dar o preco mesmo quando a cliente insiste. Sem gatekeeping, o problema
    # de repeticao que motivou a tentativa anterior (rastrear se ja
    # perguntou) deixou de existir por definicao -- removido em vez de
    # mantido morto. A licao antiga (nao nomear "dente" na proibicao)
    # continua valendo aqui por outro motivo: quando NAO houver valor pra
    # aquele item no catalogo, a saida e' dizer que depende de avaliacao,
    # nunca inventar nem pedir detalhe clinico.
    "preco": (
        "Situacao desta mensagem: ela perguntou preco. Comece com uma frase "
        "gentil que mostre que voce entendeu o pedido. Siga as regras de valores "
        "das instrucoes da empresa: diga o valor escrito do item que ela pediu, "
        "ou faca antes a unica pergunta que essas regras pedirem. Nao puxe "
        "agendamento, nao peca dados e nao fale de outro item nesta mensagem. "
        "Se o item nao tiver valor escrito, siga a regra de confirmar com a "
        "equipe; nunca invente numero."
    ),
    # 29/ago/2026: nao oferecer mais dia/periodo como se ja estivesse certo --
    # ordem do chairman, achado ao vivo: "vou ver se tem um horario" em vez de
    # "prefere terca ou quinta" (que presume disponibilidade que nao existe).
    # Mesma correcao aplicada no protocolo (reply_normal v25) -- precisa estar
    # nos dois lugares, essa instrucao entra por ultimo no prompt montado e
    # vence por recencia se ficar so na v25.
    # 29/ago/2026, segunda correcao no mesmo dia: "vou verificar e volto" e'
    # o MESMO erro conhecido desde o Genius Sell (produto anterior) -- o bot
    # promete, ninguem confirma depois, a conversa morre. Sem integracao real
    # de agenda (Google Calendar, fora de escopo por ora), a saida e' nao
    # prometer: perguntar a preferencia dela e devolver a confirmacao pra
    # equipe, nao pro bot.
    "fechamento": (
        "Situacao desta mensagem: ela sinalizou que quer marcar. Siga as regras "
        "de agendamento das instrucoes da empresa. Nao confirme dia nem horario "
        "como certos: a equipe confirma. Nunca prometa verificar e voltar por "
        "conta propria."
    ),
    "andamento": "",
}


def etapa_do_turno(
    texto: str,
    *,
    tem_historico: bool,
) -> Etapa:
    """A etapa desta mensagem, a partir do texto do turno e do historico.

    ``preco`` vence ``abertura`` de proposito: a pergunta fria de preco na
    primeira mensagem e' exatamente o caso que quebra, e as duas instrucoes se
    contradiriam ("nada de valor" x "antes do numero vem uma pergunta").
    """
    normalizado = _normalize(texto)
    if _PRECO.search(normalizado):
        return "preco"
    if not tem_historico:
        return "abertura"
    if _FECHAMENTO.search(normalizado):
        return "fechamento"
    return "andamento"


def instrucao_da_etapa(etapa: str) -> str:
    """O texto injetado no prompt para esta etapa. Vazio = protocolo do prompt."""
    return _INSTRUCOES.get(etapa, "")


def _autoteste() -> None:
    casos: list[tuple[str, bool, Etapa]] = [
        # o caso que motivou o modulo
        ("ola, quanto custa a flex?", False, "preco"),
        ("quanto ta a protese flexivel?", False, "preco"),
        ("so me diz o valor", True, "preco"),
        ("qual e o preco da protese?", True, "preco"),
        ("me passa o orcamento", True, "preco"),
        ("fica R$ 900?", True, "preco"),
        # abertura: primeiro contato sem pergunta de preco
        ("boa tarde, vim pelo anuncio da flex", False, "abertura"),
        ("oi", False, "abertura"),
        ("vi no instagram a protese flexivel", False, "abertura"),
        # fechamento so depois que a conversa existe
        ("bora marcar, pode ser essa semana?", True, "fechamento"),
        ("quero agendar", True, "fechamento"),
        ("tem vaga pra quinta?", True, "fechamento"),
        # andamento: o meio da conversa nao e' mexido
        ("to com muita dor, nao aguento mais", True, "andamento"),
        ("ah nao sei nao... vou pensar", True, "andamento"),
        ("isso nao e golpe nao?", True, "andamento"),
        ("perdi dois dentes do lado esquerdo", True, "andamento"),
        # preco vence fechamento quando os dois aparecem: o defeito medido
        # esta no preco, e dar o valor cedo e' o erro mais caro dos dois
        ("bora marcar, quanto custa?", True, "preco"),
    ]
    for texto, tem_historico, esperado in casos:
        obtido = etapa_do_turno(texto, tem_historico=tem_historico)
        assert obtido == esperado, f"{texto!r} -> {obtido}, esperado {esperado}"
    # a etapa que nao injeta nada precisa continuar nao injetando nada
    assert instrucao_da_etapa("andamento") == ""
    assert instrucao_da_etapa("inexistente") == ""
    assert instrucao_da_etapa("preco").startswith("Situacao")

    print(f"autoteste ok: {len(casos)} casos")


if __name__ == "__main__":
    _autoteste()
