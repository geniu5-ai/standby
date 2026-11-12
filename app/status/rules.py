"""Regex determinístico: única camada autoritativa do módulo de status.

Duas famílias de padrão, ambas evidência da própria fala do cliente:
``_EXPLICIT_WIN_PATTERNS`` (confirmação real de negócio) e ``_LOSS_PATTERNS`` +
``_OPT_OUT_PATTERNS`` (recusa ou pedido para parar de receber mensagem — as
duas são formas de ``loss``, não duas categorias diferentes: quem pede pra
parar de falar com a empresa É um cliente perdido, não um ataque).

``block`` nunca nasce daqui. Spam e abuso não são evidência de fala de
cliente — são decisão do guard de conteúdo, fora deste módulo.

27/ago/2026, segunda rodada de revisão externa (GLM-5.3 + Kimi K3, sobre o
código JÁ implementado): dois achados sérios corrigidos aqui.

1. O gatilho de recusa ("não quero mais", "não tenho interesse") só olhava
   o que vinha DEPOIS do gatilho para decidir se havia sinal positivo
   ("continuação"). "Podemos marcar pra hoje? Não quero mais esperar." —
   o sinal de win vem ANTES do "não quero mais", e o código antigo via só
   " esperar" depois, sem continuação — loss. Lead mais quente possível
   marcado como perdido. Fix: quando um sinal de win explícito existe em
   QUALQUER lugar do texto, o resultado vira ambíguo (None), não loss —
   consistente com a filosofia do módulo ("narrow regex, ambiguous falls to
   consultive layers"), e mais seguro que arriscar win errado por posição.

2. ``_LOSS_PATTERNS`` ("não tenho interesse") nunca teve o mesmo tratamento
   de continuação que ``_NO_MORE_LOSS_PATTERN`` já tinha — inconsistência
   interna achada por ambas as revisões. "Não tenho interesse em consulta
   avulsa, quero fechar o pacote de 10 sessões" batia só em LOSS_PATTERNS e
   ignorava o win logo ali. Mesmo fix se aplica aos dois gatilhos agora.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence

_NON_ALNUM_RE = re.compile(r"[^0-9a-z]+")
_MESSAGE_TOKEN = r"(?:mensag(?:em|ens)|msgs?)"
_NO_MORE_LOSS_PATTERN = re.compile(r"\bnao\s+quero\s+mais\b")
_BOOKING_TARGET = (
    r"(?:(?:um|uma|o|a)\s+)?"
    r"(?:horario|consulta|avaliacao|atendimento|visita|sessao|"
    r"procedimento|demonstracao|reserva)"
)
_BOOKING_TIME = (
    r"(?:pra|para)\s+(?:hoje|amanha|"
    r"(?:segunda|terca|quarta|quinta|sexta|sabado|domingo)(?:\s+feira)?|"
    r"(?:de\s+)?(?:manha|tarde|noite))"
)
_EXPLICIT_WIN_PATTERNS = [
    # 27/ago/2026: exige referencia explicita de dia/horario, nao mais so o
    # alvo do agendamento. Achado por revisao externa (GLM-5.3) e verificado:
    # "quero marcar uma consulta" (intencao pura, sem confirmar nada) disparava
    # win terminal antes desta correcao — false-positivo grave, porque win
    # terminal para o follow-up do lead. "tem vaga pra hoje?" continua fora
    # (nao usa este verbo), "quero marcar pra quinta-feira" continua win.
    #
    # NOTA (segunda revisao, 27/ago): "quero marcar pra quinta" ainda e'
    # PEDIDO, nao confirmacao de vaga — continua sendo o mesmo bug de classe
    # so que mais estreito. O redesign de verdade (win como sinal fraco +
    # integracao de agenda que confirma win de fato) fica registrado como
    # decisao de escopo pendente, nao resolvido por este regex.
    re.compile(
        rf"\b(?:quero|gostaria\s+de|pode|vamos|podemos)\s+"
        rf"(?:marcar|agendar)\s+(?:{_BOOKING_TARGET}\s+)?{_BOOKING_TIME}\b"
    ),
    re.compile(
        r"\bconfirmo(?:\s+(?:o|a))?\s+"
        r"(?:horario|consulta|agendamento|reserva)\b"
        r"(?!\s+d[ae]\s+reuniao\s+interna\b)"
    ),
    re.compile(
        r"\baceito(?:\s+(?:a|o))?\s+(?:proposta|orcamento|condicao)\b"
        r"(?!\s+(?:do|da|de)\s+(?:concorrente|outr[oa](?:\s+empresa)?))"
    ),
    re.compile(r"\bquero\s+comprar\b(?!\s+(?:tempo|briga|problema)\b)"),
    re.compile(
        r"\bquero\s+fechar\s+(?:(?:o|a|um|uma)\s+)?"
        r"(?:negocio|contrato|pedido|compra|proposta|plano|pacote|"
        r"servico|tratamento|acordo)\b"
    ),
]
_POSITIVE_CONTINUATION_PATTERNS = [
    re.compile(r"\bpode\s+(?:marcar|agendar)\b"),
    re.compile(r"\b(?:vamos|podemos)\s+(?:marcar|agendar)\b"),
    re.compile(r"\bquero\s+sim\b"),
    re.compile(r"\bquero\s+(?:marcar|agendar|comprar|fechar)\b"),
    *_EXPLICIT_WIN_PATTERNS,
]
_NEGATED_WIN_PATTERNS = [
    re.compile(r"\b(?:nao|nem)\s+quero\s+(?:sim|marcar|agendar|comprar|fechar)\b"),
    re.compile(r"\b(?:nao|nem)\s+(?:pode|podemos|vamos)\s+(?:marcar|agendar)\b"),
    re.compile(r"\b(?:nao|nem)\s+(?:confirmo|aceito)\b"),
]
_CONDITIONAL_WIN_PATTERNS = [
    re.compile(r"\b(?:mas\s+)?so\s+se\b"),
    re.compile(r"\bdependendo\s+d[aeo]\b"),
]


def _normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    without_marks = "".join(char for char in decomposed if not unicodedata.combining(char))
    simplified = _NON_ALNUM_RE.sub(" ", without_marks.casefold())
    return " ".join(simplified.split())


# 27/ago/2026: eram "_OFF_PATTERNS" e devolviam "off". Renomeados porque um
# cliente pedindo pra parar de receber mensagem NAO e ataque — e cliente
# desistindo, ou seja, loss. "off"/"block" ficou reservado para o guard de
# conteudo (spam/abuso), que nunca decide a partir da fala do cliente.
#
# Cada padrao ganhou "(?!\s+\w)" antes do \b final: achado real (revisao
# externa GLM-5.3, verificado por execucao) — sem isso "nao me manda mais
# audio/foto/link" disparava a mesma frase que "nao me manda mais mensagem",
# porque o token de mensagem e opcional e re.search para no prefixo que
# casou, ignorando o resto da frase. Cliente que so quer parar de receber UM
# tipo de conteudo nao pode virar opt-out total.
#
# 27/ago/2026, segunda rodada (Kimi K3): o mesmo lookahead cria falso
# negativo em opt-out real com continuacao educada — "nao me manda mais
# mensagem, obrigado" nao casava mais, porque "obrigado" e' uma palavra
# depois. Aceito como troca deliberada: falso negativo aqui cai pra
# embedding/LLM (que tem ancora "nao me mande mais mensagem"); falso
# positivo:silenciava cliente que so queria parar de receber UM tipo de
# conteudo. E "para de mandar" (sem acento, "para" em vez de "pare") e'
# grafia coloquial comum — adicionado par(?:a|e) para cobrir as duas.
_OPT_OUT_PATTERNS = [
    re.compile(
        rf"\bnao\s+me\s+mand(?:a|e)\s+mais"
        rf"(?:\s+{_MESSAGE_TOKEN})?(?:\s+(?:por\s+favor|pfv))?(?!\s+\w)\b"
    ),
    re.compile(r"\bnao\s+me\s+manda\s+mais\s+nada\b"),
    re.compile(r"\bme\s+remov[ae]\s+da\s+lista(?:\s+(?:por\s+favor|pfv))?\b"),
    re.compile(
        rf"\bpar(?:a|e)\s+de\s+mandar(?:\s+{_MESSAGE_TOKEN})?(?:\s+pra\s+mim)?(?!\s+\w)\b"
    ),
    re.compile(rf"\bnao\s+quero\s+mais\s+receber(?:\s+{_MESSAGE_TOKEN})?(?!\s+\w)\b"),
    re.compile(r"\bme\s+tira\s+dessa\s+lista\b"),
    re.compile(r"\bremov[ae]\s+meu\s+numero\b"),
]

_LOSS_PATTERNS = [
    re.compile(r"\bnao\s+tenho(?:\s+mais)?\s+interesse\b"),
    re.compile(r"\bja\s+(?:tenho\s+outro|fechei\s+com\s+outro)\b"),
    re.compile(r"\bnao\s+vou\s+fechar(?:\s+nao)?\b"),
    re.compile(r"\bdesisti\b"),
]

# Intentionally narrow: only explicit opt-out / refusal language belongs here.
# Typos with missing semantic tokens, ambiguous deferrals ("vou pensar",
# "depois vejo"), sarcasm, and messy voice-note transcripts still fall through
# to embeddings/LLM so the regex layer stays precise.

# 27/ago/2026: recusa de UM tipo de conteudo ("nao quero mais audio") nao e
# desistencia total -- mesma familia do fix dos _OPT_OUT_PATTERNS, achada
# numa segunda revisao (GLM-5.3) num caminho diferente do regex.  Ancorado
# no INICIO do tail (logo apos "nao quero mais"), pra nao interferir com o
# resto da frase.
_CONTENT_TYPE_TOKEN = (
    r"(?:audio|audios|foto|fotos|imagem|imagens|video|videos|"
    r"sticker|figurinha|figurinhas|link|links|promocao|promocoes)"
)
_NO_MORE_CONTENT_SPECIFIC_AT_START = re.compile(
    rf"^\s*(?:receber\s+)?{_CONTENT_TYPE_TOKEN}\b"
)


def _positive_continuation_after_no_more(normalized: str) -> bool:
    match = _NO_MORE_LOSS_PATTERN.search(normalized)
    if match is None:
        return False
    tail = normalized[match.end():]
    if any(pattern.search(tail) for pattern in _NEGATED_WIN_PATTERNS):
        return False
    return any(pattern.search(tail) for pattern in _POSITIVE_CONTINUATION_PATTERNS)


def _has_explicit_win_signal(normalized: str) -> bool:
    if any(pattern.search(normalized) for pattern in _NEGATED_WIN_PATTERNS):
        return False
    if any(pattern.search(normalized) for pattern in _CONDITIONAL_WIN_PATTERNS):
        return False
    return any(pattern.search(normalized) for pattern in _EXPLICIT_WIN_PATTERNS)


def _has_positive_signal_anywhere(normalized: str) -> bool:
    """Sinal de win em QUALQUER lugar do texto, nao so depois do gatilho de recusa.

    27/ago/2026: achado por duas revisoes externas independentes (GLM-5.3 e
    Kimi K3). "Podemos marcar pra hoje? Nao quero mais esperar." tem o sinal
    de win ANTES do gatilho de recusa -- o codigo antigo so olhava o texto
    DEPOIS do "nao quero mais"/"nao tenho interesse" (a logica de
    "continuacao"), entao via so " esperar" e concluia loss. Lead mais quente
    possivel marcado como perdido, automacao pausada, follow-up morto.

    A fila nao decide ordem (nao sabemos se o win veio antes ou depois da
    recusa por acidente de digitacao vs mudanca real de ideia) -- entao
    quando os dois sinais coexistem no MESMO texto, o resultado correto e
    ambiguo (None), nao loss nem win. Cai pra embedding/LLM, que ve o texto
    inteiro com contexto. Loss e quase irreversivel (pausa automacao,
    trigger bloqueia regressao); errar pro lado de "nao decidir" e mais
    barato que errar matando um lead quente.

    A negacao precisa valer aqui tambem -- "nem quero comprar" contem
    literalmente "quero comprar" (um _POSITIVE_CONTINUATION_PATTERNS), mas
    esta NEGADO.  Bug real pego no proprio teste de regressao desta correcao.
    """
    if any(pattern.search(normalized) for pattern in _NEGATED_WIN_PATTERNS):
        return False
    if _has_explicit_win_signal(normalized):
        return True
    return any(pattern.search(normalized) for pattern in _POSITIVE_CONTINUATION_PATTERNS)


# Camada "pseudo-IA": determinismo local, zero rede, antes de embedding+LLM.
# O pipeline (rule -> embedding -> LLM residual, ver app/status/audit.py) chama
# o LLM em TODO turno cujo texto nao bate em detect_status_by_rule -- inclusive
# "obrigada!", "kkkkk", "👍". Nenhuma dessas carrega evidencia de status;
# gastar uma chamada de modelo nelas e' custo sem sinal. Escopo deliberadamente
# estreito (so o que foi pedido: agradecimento, risada, so-emoji) -- nao cobre
# "ok"/"sim"/"blz" porque essas podem confirmar um agendamento sugerido pelo
# agente ("terca ou quinta?" -> "sim"), e so o LLM residual ve o historico
# completo pra resolver isso. Ampliar aqui exige ver caso real, nao suposicao.
_AGRADECIMENTO_TRIVIAL = re.compile(r"^(?:muito\s+)?obrigad[ao]s?$|^obg$|^vlw$|^valeu$")
_RISADA_TRIVIAL = re.compile(r"^(?:k{2,}|(?:rs){2,}|(?:ha){2,}|(?:he){2,})+$")


# Interrogacao (inclusive a de largura total) ou emoji de duvida: pedem resposta.
_PERGUNTA_ISOLADA = re.compile("[?\uff1f\u2753\u2754\U0001F937\U0001F914]")


def is_trivial_message(text: str) -> bool:
    """Agradecimento, risada ou so emoji/pontuacao -- sem sinal de status.

    ``_normalize`` ja reduz emoji e pontuacao a espaco (so mantem [0-9a-z] pos
    NFKD): uma mensagem so de emoji vira string vazia aqui, o que a classe
    "so emoji" pedida cobre de graca, sem regex de emoji nenhuma.
    """
    # 23/out/2026: "?" e emoji de duvida SAO a pergunta do paciente, nao ruido. Antes
    # o texto normalizado ficava vazio e o turno era suprimido sem a Julia ler (9 de 20
    # conversas abertas da clínica com a ultima fala do paciente sem resposta).
    if _PERGUNTA_ISOLADA.search(text):
        return False
    normalizado = _normalize(text)
    if not normalizado:
        return True
    return bool(
        _AGRADECIMENTO_TRIVIAL.match(normalizado) or _RISADA_TRIVIAL.match(normalizado)
    )


# Escopo mais largo que is_trivial_message: usada pra decidir se o TURNO gera
# resposta nenhuma (join/debounce, antes do agente), nao so pra classificacao
# de status. Adiciona confirmacoes curtas puras (ok/blz/beleza/certo/aham/uhum/
# show/top) ao que ja e' trivial. "sim"/"nao" ficam de FORA de proposito: sao
# isolados demais e tem chance real de confirmar algo que o agente perguntou no
# turno anterior ("terca ou quinta?" -> "sim") -- matar a resposta ali deixaria a
# conversa sem confirmacao visivel. "ok" sozinho tem o mesmo risco e entra assim
# mesmo porque foi pedido explicitamente pelo chairman; se aparecer caso real
# ruim (ok como confirmacao de agendamento), tirar da lista e' uma linha.
_CONFIRMACAO_CURTA = re.compile(
    r"^(?:ok|blz|beleza|certo|aham|uhum|show|top)$"
)


# 15/out/2026 (chairman): quem adia ou encerra ("sem pressa", "quando o dinheiro
# entrar", "depois eu vejo") fechou a conversa por ora. Follow-up depois disso
# vira cobranca: uma paciente recebeu 8 mensagens em 90 min. Normalizado como o
# resto do modulo (sem acento, so letras e digitos, minusculas).
#
# 17/out/2026 (incidente Olga ...3096, task #8959): a lista nao cobria
# "amanha, ou outro dia falo com vcs" (sem "eu" e com "ou outro dia" no meio)
# nem "nao e o momento agora" -- dois adiamentos reais que deixaram o follow-up
# insistir (6 mensagens em 43 min). Fixes: "eu" virou opcional em "amanha",
# "outro dia <verbo>" entrou como familia propria e "nao e (o) momento" /
# "agora nao e (a) hora" / "nao e (a) hora ainda" cobrem a recusa de agora.
_ADIAMENTO = re.compile(
    r"\b(?:"
    r"sem pressa"
    r"|quando (?:o )?dinheiro (?:entrar|cair|sair)"
    r"|quando (?:eu )?(?:puder|der|receber|tiver)"
    r"|quando (?:for|chegar) (?:a )?(?:hora|o dia)"
    r"|depois (?:eu )?(?:vejo|volto|chamo|passo|falo|mando|te chamo|te falo|retorno|converso)"
    r"|(?:falo|converso) (?:com (?:voce|vc|vcs|voces) )?depois"
    r"|(?:eu )?(?:vou|vo) ver (?:e|direitinho|com|aqui|depois)"
    r"|(?:vou|vo) pensar"
    r"|mais tarde eu"
    r"|amanha (?:eu )?(?:vejo|volto|chamo|passo|vou|falo|te chamo|te falo)"
    r"|outro dia (?:eu )?(?:vejo|volto|chamo|passo|falo|mando|te chamo|te falo|retorno|converso)(?: com (?:voce|vc|vcs|voces))?"
    r"|(?:semana|mes) que vem"
    r"|fico no aguardo"
    r"|(?:pode|podes) deixar"
    r"|deixa que eu (?:chamo|falo|vejo|aviso)"
    r"|(?:eu )?(?:te )?aviso (?:depois|quando)"
    r"|(?:agora|no momento|por enquanto) nao"
    r"|nao e (?:o )?momento"
    r"|agora nao e (?:a )?hora"
    r"|nao e (?:a )?hora ainda"
    r"|nao tenho interesse"
    r"|fica pra (?:depois|outra|outro dia)"
    r"|vou (?:amanha|depois|semana)"
    r")\b"
)


# 03/nov/2026 (incidente "Vix" / "Muito longe", vacina de meningite): o paciente
# (responsavel por um paciente) respondeu "Vix" e "Muito longe" -- uma OBJECAO de
# distancia -- e nenhum padrao a reconhecia; o follow-up reabriu um ciclo inteiro e
# ainda usou uma frase de culpa ("outras familias decidem mesmo morando mais
# longe"). Objecao de distancia, custo ou tempo e uma recusa branda: quem a faz
# nao esta "decidindo ainda", esta dizendo que o obstaculo o impede. Tratada como
# adiamento/encerramento (pausa o follow-up) e NUNCA como loss (``llm_loss_vetado``
# usa ``is_deferral_or_closing``): a pessoa pode voltar se a equipe resolver o
# obstaculo, e quem decide isso e o atendimento humano, nao o regex.
#
# Falsos positivos que este bloco evita de proposito:
# - Pergunta nunca e objecao ("Fica longe da rodoviaria?", "Onde fica?", "Tá
#   caro?"): '?' no texto original (salvo o tira-duvida "né?"), pergunta de
#   endereco e palavra interrogativa no inicio derrubam o sinal.
# - "longe d<ponto de referencia>" ("fica longe da rodoviaria") e' pedido de
#   localizacao, nao recusa. So vale quando o ponto e a propria pessoa/casa/aqui.
# - "nao posso ir amanha / de manha / nessa semana" e' conflito de horario, nao
#   recusa do servico: continua com o follow-up (o LLM/atendimento remarca).
_SEM_PONTO_DE_REFERENCIA = (
    r"(?!\s+d[aeo]s?\s+(?!(?:mim|casa|minha|meu|aqui|onde|nos)\b)\w)"
)
_QUANDO_NAO_E_RECUSA = (
    r"(?!\s+(?:hoje|amanha|segunda|terca|quarta|quinta|sexta|sabado|domingo|dia|"
    r"nesse|nessa|neste|nesta|essa|esse|ate|no|na|as|de\s+manha|a\s+tarde|"
    r"de\s+tarde|de\s+noite|a\s+noite|semana|mes|agora|sozinh[ao])\b)"
)
_OBJECAO_DISTANCIA = [
    re.compile(rf"\b(?:muito|mt|mto|bem|tao|meio|demais)\s+longe\b{_SEM_PONTO_DE_REFERENCIA}"),
    re.compile(r"\bmor(?:o|a|amos|ando)\s+(?:(?:muito|bem|meio)\s+)?longe\b"),
    re.compile(
        r"\b(?:e|eh|ta|esta|fica|ficou|ficando)\s+"
        rf"(?:(?:muito|bem|meio|bastante|um\s+pouco)\s+)?longe\b{_SEM_PONTO_DE_REFERENCIA}"
    ),
    re.compile(r"\blonge\s+(?:demais|de\s+mais|pra\s+mim|para\s+mim|de\s+mim|de\s+casa|da\s+minha\s+casa|daqui)\b"),
    re.compile(
        r"\bnao\s+(?:da|tem\s+como|tenho\s+como|teria\s+como|consigo|posso|vou\s+conseguir)\s+"
        rf"(?:(?:pra|para|de)\s+)?(?:ir|chegar|comparecer|vir|me\s+deslocar|deslocar)\b{_QUANDO_NAO_E_RECUSA}"
    ),
    re.compile(r"\bmor(?:o|amos|a)\s+(?:em\s+outr[ao]\s+(?:cidade|estado|pais)|fora\s+da\s+cidade|no\s+interior)\b"),
    re.compile(r"\boutro\s+lado\s+da\s+cidade\b"),
]
_OBJECAO_CUSTO = [
    re.compile(r"\b(?:muito|mt|mto|bem|meio|demais|bastante|tao)\s+(?:caro|carissimo|salgado|puxado)\b"),
    re.compile(
        r"\b(?:e|eh|ta|esta|fica|ficou|achei|acho|achamos|achou)\s+"
        r"(?:(?:muito|mt|mto|bem|meio|bastante|um\s+pouco)\s+)?(?:caro|carissimo|salgado|puxado)\b"
    ),
    re.compile(r"\bcaro\s+(?:demais|de\s+mais|pra\s+mim|para\s+mim)\b"),
    re.compile(r"\bnao\s+(?:tenho|tem|temos)\s+(?:como\s+pagar|condicoes|condicao|dinheiro|grana|verba)\b"),
    re.compile(r"\bsem\s+condicoes\b"),
    re.compile(r"\bnao\s+(?:posso|consigo|vou\s+conseguir)\s+pagar\b"),
    re.compile(r"\bnao\s+cabe(?:m)?\s+no\s+(?:meu\s+)?(?:orcamento|bolso)\b"),
    re.compile(r"\bfora\s+d[oa]\s+(?:meu|minha)\s+(?:orcamento|realidade|bolso|alcance)\b"),
    re.compile(r"\bpesad[oa]\s+(?:pro|para\s+o|no)\s+bolso\b"),
]
_OBJECAO_TEMPO = [
    re.compile(
        r"\b(?:nao\s+tenho|nao\s+tem|nao\s+temos|to\s+sem|estou\s+sem|ando\s+sem|fico\s+sem|sem)\s+"
        r"(?:muito\s+|nenhum\s+)?tempo\b"
    ),
    re.compile(r"\bnao\s+tenho\s+(?:muita\s+)?disponibilidade\b"),
]
_OBJECOES = [*_OBJECAO_DISTANCIA, *_OBJECAO_CUSTO, *_OBJECAO_TEMPO]
# "Vix"/"Vixe" sozinho: a reacao de desanimo/surpresa da resposta do incidente.
# So a mensagem inteira (nunca no meio de uma frase com conteudo).
_OBJECAO_INTERJEICAO = re.compile(r"^(?:vix+|vixe+|vixi+|vish+)(?:\s+(?:maria|nossa|senhora|que\s+longe))?$")
_PERGUNTA_DE_LOCALIZACAO = re.compile(
    r"^(?:onde|aonde|qual|quais|quanto|quantos|como|quando)\b"
    r"|\bonde\s+(?:fica|e|eh|ficam)\b|\bendereco\b|\bcomo\s+(?:eu\s+)?(?:chego|chegar)\b"
)
_TIRA_DUVIDA_FINAL = re.compile(r"\bn[eé]\s*[?？]*\s*$")


def _e_pergunta(text: str, normalizado: str) -> bool:
    if _PERGUNTA_DE_LOCALIZACAO.search(normalizado):
        return True
    if re.search(r"[?？]", text):
        # "Muito longe, né?" afirma; so a interrogacao real faz pergunta.
        return _TIRA_DUVIDA_FINAL.search(text.casefold().strip()) is None
    return False


def is_objection(text: str) -> bool:
    """Objecao de distancia, custo ou tempo ("Muito longe", "tá caro", "não tenho tempo").

    Nao e loss e nao e opt-out: e' o sinal de que o follow-up automatico nao deve
    insistir nem comparar a pessoa com terceiros. Pergunta nunca conta.
    """
    if not text or not text.strip():
        return False
    normalizado = _normalize(text)
    if not normalizado:
        return False
    if _OBJECAO_INTERJEICAO.match(normalizado):
        return True
    if _e_pergunta(text, normalizado):
        return False
    return any(pattern.search(normalizado) for pattern in _OBJECOES)


# 03/nov/2026: "Ok obrigada" (despedida) tambem fechou a conversa mas o follow-up
# disparou logo depois. Despedida = a mensagem INTEIRA e' agradecimento/tchau com,
# no maximo, palavras de enfeite (ok, tá bom, muito, pela atenção...). Qualquer
# palavra fora da lista (um pedido, uma duvida, "vou marcar") nao e' despedida --
# nessa duvida o follow-up segue como antes. "ok" sozinho continua nao sendo
# despedida: pode confirmar um horario que o agente propos.
_DESPEDIDA_NUCLEO = re.compile(
    r"\b(?:obrigad[ao]+s?|brigad[ao]+s?|obg|obgd[ao]?|vlw|valeu+|agradeco|grato|grata|"
    r"tchau|xau|flw|falou|fechou|ate\s+(?:mais|logo|breve|a\s+proxima))\b"
)
_DESPEDIDA_ENFEITE = frozenset(
    "ok okk okay oks certo certinho tudo bem ta tah bom boa blz beleza entao ai muito mt mto "
    "mesmo viu de nada por pela pelo pelas pelos atencao ajuda informacao informacoes retorno "
    "esclarecimento esclarecimentos gentileza disponibilidade tambem igualmente voces voce vc vcs "
    "a o as os ate mais logo depois dia tarde noite amem deus abencoe abraco abracos bjs bj "
    "beijos doutor doutora dr dra moca rapaz show top massa perfeito otimo".split()
)


def is_farewell(text: str) -> bool:
    """Despedida/agradecimento final ("ok obrigada", "valeu", "tá bom, obrigado")."""
    if not text or not text.strip() or re.search(r"[?？]", text):
        return False
    normalizado = _normalize(text)
    if len(normalizado.split()) > 10 or not _DESPEDIDA_NUCLEO.search(normalizado):
        return False
    resto = _DESPEDIDA_NUCLEO.sub(" ", normalizado).split()
    return all(token in _DESPEDIDA_ENFEITE for token in resto)


def encerra_follow_up(text: str) -> bool:
    """A fala fecha o ciclo de follow-up: adiamento, objecao ou despedida."""
    return is_deferral_or_closing(text) or is_farewell(text)


def is_deferral_or_closing(text: str) -> bool:
    """O paciente adiou, recusou de leve ou encerrou por ora: o follow-up nao deve insistir.

    Frase de adiamento ou objecao de distancia/custo/tempo (``is_objection``);
    "ok" ou "obrigada" sozinhos e perguntas continuam como antes (quem pergunta
    nao esta adiando). Despedida e' ``is_farewell`` (so a ultima fala, ver
    ``encerra_follow_up``).
    """
    if not text or not text.strip():
        return False
    normalizado = _normalize(text)
    if not normalizado:
        return False
    if re.search(r"\b(?:quanto|qual|quais|tem vaga|tem horario|quero marcar|como (?:faco|marco))\b", normalizado):
        return False
    return bool(_ADIAMENTO.search(normalizado)) or is_objection(text)


# 06/nov/2026 (chairman): so RECUSA FIRME encerra o follow-up. "Vou pensar", "depois eu vejo" e
# objecao (longe, caro, sem tempo) continuam com insistencia leve; "agora nao", "nao tenho interesse"
# e opt-out ("nao me manda mais") encerram com educacao.
_RECUSA_FIRME = re.compile(
    r"\b(?:"
    r"(?:agora|no momento|por enquanto) nao"
    r"|nao e (?:o )?momento"
    r"|agora nao e (?:a )?hora"
    r"|nao e (?:a )?hora ainda"
    r"|nao tenho interesse|sem interesse|nao me interessa"
    r"|nao quero|nao vou (?:querer|fazer|marcar)"
    r")\b"
)


def is_refusal(text: str) -> bool:
    """Recusa firme ("agora não", "não tenho interesse", "não me manda mais"): o follow-up encerra.

    Adiamento ("vou pensar"), objecao e pergunta nao sao recusa: a conversa segue.
    """
    if not text or not text.strip():
        return False
    normalizado = _normalize(text)
    if not normalizado or _e_pergunta(text, normalizado):
        return False
    if any(pattern.search(normalizado) for pattern in _OPT_OUT_PATTERNS):
        return True
    return bool(_RECUSA_FIRME.search(normalizado))


def is_trivial_no_reply(text: str) -> bool:
    """Emoji-so, risada, agradecimento ou confirmacao curta -- turno sem resposta.

    Mais largo que ``is_trivial_message`` (que so decide status). Nao decide
    status nenhum -- so se o agente deve ou nao gerar uma resposta pra esta
    mensagem isolada. A decisao de suprimir o TURNO (que pode ter varias
    mensagens juntadas pelo join do debounce) exige que TODAS as mensagens do
    turno passem aqui, uma a uma -- ver conversation_turn_workflow.
    """
    if is_trivial_message(text):
        return True
    normalizado = _normalize(text)
    return bool(_CONFIRMACAO_CURTA.match(normalizado))


def detect_status_by_rule(text: str) -> str | None:
    """``win`` | ``loss`` | ``None``. Nunca ``open`` nem ``block``.

    ``open`` e a ausencia de sinal (o chamador decide o default).  ``block`` e
    decisao do guard, nao desta funcao — spam/abuso nao e lido da fala do
    cliente, e sim do comportamento do trafego.
    """
    normalized = _normalize(text)

    for pattern in _OPT_OUT_PATTERNS:
        if pattern.search(normalized):
            return "loss"

    # 27/ago/2026: sinal de win em qualquer lugar do texto torna o gatilho de
    # recusa ambiguo -- checado ANTES dos dois gatilhos abaixo, que antes
    # decidiam loss olhando so o que vinha depois de si mesmos.
    if _has_positive_signal_anywhere(normalized):
        if _NO_MORE_LOSS_PATTERN.search(normalized) or any(
            p.search(normalized) for p in _LOSS_PATTERNS
        ):
            return None

    match_no_more = _NO_MORE_LOSS_PATTERN.search(normalized)
    if match_no_more is not None:
        tail = normalized[match_no_more.end():]
        if _NO_MORE_CONTENT_SPECIFIC_AT_START.search(tail.lstrip()):
            pass  # recusa de UM tipo de conteudo -- ambiguo, nao decide sozinho
        elif not _positive_continuation_after_no_more(normalized):
            return "loss"

    for pattern in _LOSS_PATTERNS:
        if pattern.search(normalized):
            return "loss"

    if _has_explicit_win_signal(normalized):
        return "win"

    return None


_NAO_SOZINHO = re.compile(r"^(?:nao)(?:\s+nao)*$")
_RECUSA_DE_CONTEUDO = re.compile(
    rf"\bnao\s+(?:me\s+mand(?:a|e)|quero)\s+mais\s+(?:receber\s+)?{_CONTENT_TYPE_TOKEN}\b"
)


def llm_loss_vetado(text: str) -> bool:
    """Fala que NUNCA prova loss, venha o loss do juiz ou do agente (20/out/2026).

    Bateria hard (121 casos): todos os falsos loss restantes do voto eram
    destes tres tipos -- "Nao" sozinho (resposta a uma pergunta, nao recusa
    do servico), recusa de UM tipo de conteudo ("nao me manda mais audio") e
    adiamento ("falo com voces depois", ja reconhecido pelo follow-up). A
    regra de loss explicito (``detect_status_by_rule``) roda antes e continua
    decidindo "nao tenho interesse" e opt-out.
    """
    normalizado = _normalize(text)
    if not normalizado:
        return False
    return bool(
        _NAO_SOZINHO.match(normalizado)
        or _RECUSA_DE_CONTEUDO.search(normalizado)
        or is_deferral_or_closing(text)
    )


def is_opt_out(text: str) -> bool:
    """True quando o cliente pede explicitamente para parar de ser contatado.

    Reusa ``_OPT_OUT_PATTERNS`` — não inventa nenhum regex novo. O separador
    entre ``opt_out`` (parada explícita, terminal no funil de genius_maps.leads)
    e ``perdido`` (recusa sem pedir parada) é exatamente este sinal explícito.
    """
    normalized = _normalize(text)
    return any(pattern.search(normalized) for pattern in _OPT_OUT_PATTERNS)


# ── Sinal de ma-fe (log-only, fase 1) ───────────────────────────────────────
# Lead que nunca diz um "nao" explicito (nao bate em _LOSS_PATTERNS/
# _OPT_OUT_PATTERNS acima), nao e agressivo a ponto de justificar handoff, nao
# manda spam/link -- mas esta claramente so testando o bot ou descarregando
# frustracao, sem nenhuma intencao real de compra. Ser humano e' emocional:
# parte real do trafego vai ser gente descarregando raiva, nao lead de
# verdade (achado registrado em 22/set/2026, sessao de design do contrato
# hibrido prompt/flow).
#
# Desenhado com consultoria externa (HY4, 22/set/2026) depois de descartar a
# alternativa obvia -- estender ANCHOR_PHRASES["loss"] do embedding
# (app/status/audit.py). Descartado porque: (1) o embedding (e5) comprime
# score entre 0.81-0.98, e uma ancora vaga de "trote" colidiria com "esse
# preco e um roubo" (cliente real, irritado, nao troll); (2) o embedding
# julga UM turno sem historico, e ma-fe e' propriedade de TRAJETORIA, nao de
# mensagem isolada; (3) "loss" no classificador de negocio e quase
# irreversivel (trigger de protecao no Postgres) -- perigoso demais basear
# nisso um sinal ainda sem precisao medida.
#
# FASE 1 (este commit): so loga via bad_faith_signal(), zero mutacao de
# estado, zero efeito em automation_state/loss/Rescue. So depois de medir
# precisao contra amostra real e' que se decide promover pra alguma acao —
# ver docs/TASKS.md.
_BUSINESS_LEXICON_TOKENS = frozenset({
    "preco", "valor", "reais", "gratis", "investimento", "parcela", "parcelar",
    "desconto", "pagamento", "plano",
    "consulta", "avaliacao", "atendimento", "agendar", "marcar", "horario",
    "vaga", "procedimento", "tratamento", "sessao", "pacote",
    "duvida", "pergunta", "informacao", "ajuda",
    "clinica", "escritorio", "consultorio",
    "advogado", "advogada", "dentista", "dr", "dra", "doutor", "doutora",
    "processo", "beneficio", "inss", "direito", "caso",
})

# Provocacao explicita ao bot/atendimento -- deliberadamente estreito (xingamento
# direto ou acusacao de ser roteiro/trote), nunca tom hostil generico. Tom
# hostil sozinho ("isso e um absurdo") nao entra aqui: cliente real irritado
# com preco/demora tambem fala assim, e a funcao exige lexico de negocio
# ausente ALEM disso pra sinalizar.
_PROVOCACAO_RE = re.compile(
    r"\b(?:burr[ao]|idiota|estupid[ao]|imbecil|otari[ao]|babaca|inutil|"
    r"lixo|porcaria|merda)\b"
    r"|\bbot\s+(?:burro|idiota|fake|programado)\b"
    r"|\b(?:isso\s+e|so)\s+(?:um\s+)?(?:trote|zoeira|brincadeira|pegadinha)\b"
    r"|\bresposta\s+(?:pronta|decorada|robotizada)\b"
)


def bad_faith_signal(customer_texts: Sequence[str]) -> bool:
    """Sinal (so log, ver nota acima) de lead sem proposito de compra na conversa.

    ``customer_texts`` e a fala do cliente em ordem (historico + turno atual).
    Nunca decide por 1 mensagem isolada -- exige pelo menos 3. Exige AUSENCIA
    de vocabulario de negocio na maioria das mensagens (>=70%) E (repeticao
    ou provocacao) -- tom hostil sozinho nunca basta, e ausencia de lexico
    sozinha (lead so manda audio curto/emoji) tambem nao.
    """
    if len(customer_texts) < 3:
        return False
    normalizados = [_normalize(texto) for texto in customer_texts if texto]
    if len(normalizados) < 3:
        return False
    off_topic = sum(
        1
        for texto in normalizados
        if not any(token in texto.split() for token in _BUSINESS_LEXICON_TOKENS)
    )
    if (off_topic / len(normalizados)) < 0.7:
        return False
    repeticao = len(set(normalizados)) <= len(normalizados) / 2
    provocacao = any(_PROVOCACAO_RE.search(texto) for texto in normalizados)
    return repeticao or provocacao


def _autoteste() -> None:
    trivial_sem_resposta_extra = [
        "ok", "Ok", "blz", "beleza", "certo", "aham", "uhum", "show", "top",
    ]
    nao_trivial_sem_resposta = [
        "sim", "nao", "sim, pode marcar", "certo, mas quanto custa?",
    ]
    for texto in trivial_sem_resposta_extra:
        assert is_trivial_no_reply(texto), f"deveria ser trivial (sem resposta): {texto!r}"
        assert not is_trivial_message(texto), (
            f"{texto!r} nao deveria ser trivial pra STATUS (so pra resposta)"
        )
    for texto in nao_trivial_sem_resposta:
        assert not is_trivial_no_reply(texto), f"nao deveria suprimir resposta: {texto!r}"

    trivial = [
        "obrigada!!!", "Obrigado", "muito obrigada", "obg", "vlw", "valeu",
        "kkkkkk", "kkkk", "rsrsrs", "hahaha", "hehe",
        "👍", "👍👍", "...", "!!", "🙂",
    ]
    nao_trivial = [
        "obrigada, vou pensar", "quero marcar", "kkkk mas quanto custa?",
        "sim", "ok", "blz", "nao quero mais", "quanto custa a flex?",
        "obrigada por avisar, mas nao vou fechar",
    ]
    for texto in trivial:
        assert is_trivial_message(texto), f"deveria ser trivial: {texto!r}"
    for texto in nao_trivial:
        assert not is_trivial_message(texto), f"nao deveria ser trivial: {texto!r}"

    # bad_faith_signal -- casos que DEVEM disparar
    assert bad_faith_signal(["kkkkkk", "kkkkkk", "kkkkkk"]), "repeticao pura deveria disparar"
    assert bad_faith_signal(
        ["oi", "vc e burro mesmo", "bot idiota nao entende nada"]
    ), "provocacao explicita deveria disparar"
    assert bad_faith_signal(
        ["isso e um trote", "so pra ver se voce responde", "kkkk bot burro"]
    ), "trote explicito + provocacao deveria disparar"

    # bad_faith_signal -- casos que NAO devem disparar
    assert not bad_faith_signal(["oi", "quanto custa"]), "menos de 3 mensagens nunca dispara"
    assert not bad_faith_signal(
        ["esse preco e um roubo", "voces sao um bando de ladrao", "isso e um absurdo"]
    ), "cliente real irritado com preco (tem lexico de negocio) nao pode disparar"
    assert not bad_faith_signal(
        ["oi", "quanto custa a avaliacao", "e qual o horario de amanha"]
    ), "conversa normal de negocio nao pode disparar"
    assert not bad_faith_signal(
        ["bom dia", "tudo bem", "como vai"]
    ), "fora de assunto sem repeticao/provocacao nao basta sozinho"

    print(
        f"autoteste ok: {len(trivial) + len(nao_trivial)} casos triviais + "
        "7 casos bad_faith_signal"
    )


if __name__ == "__main__":
    _autoteste()
