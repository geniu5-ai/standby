"""Trava por codigo sobre a sugestao do Manager no modo follow (26/out/2026).

Medido em sombra na clínica (docs/specs/vendedor-manager-prompts.md): o Manager manda texto
livre em 24 de 24 conversas fora da janela de 24 h e manda chamar o paciente em 8 de 21
conversas em que a divida e da clinica. Quando existir executor, o LLM so escolhe angulo e
dica; canal, se chama e quando vem daqui e de ``plan.py``. Funcao pura, nao envia nada.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.state.plan import Acao, JANELA_24H_MIN

ANGULOS = {"reciprocidade", "compromisso", "escolha_limitada", "perda", "prova", "porta_aberta", "continuidade"}
DICA_MAX = 200


@dataclass(frozen=True)
class FollowDecidido:
    chamar: bool
    canal: str  # "texto_livre" | "template" | "nenhum"
    angulo: str | None
    dica: str
    motivo_trava: str  # vazio quando a sugestao do Manager passou sem correcao


def aplicar_trava(acao: Acao, sugestao: dict[str, Any] | None) -> FollowDecidido:
    """Combina o plano do codigo (``acao``) com a sugestao do Manager (``sugestao``)."""
    s = sugestao or {}
    if not acao.acao.startswith("follow_"):
        return FollowDecidido(False, "nenhum", None, "", f"estado/acao {acao.acao} nao e follow")
    m = acao.minutos
    if m is None:
        return FollowDecidido(False, "nenhum", None, "", "sem horario da ultima fala")
    if s.get("chamar") is False:
        return FollowDecidido(False, "nenhum", None, "", "Manager disse para nao chamar")
    angulo = s.get("angulo") if s.get("angulo") in ANGULOS else None
    dica = str(s.get("dica") or "")[:DICA_MAX]
    if m >= JANELA_24H_MIN:
        corrigido = s.get("canal") not in (None, "template")
        return FollowDecidido(True, "template", angulo, dica, "janela de 24h fechada: so template" if corrigido else "")
    return FollowDecidido(True, "texto_livre", angulo, dica, "" if s.get("canal") in (None, "texto_livre") else "janela aberta: texto livre")
