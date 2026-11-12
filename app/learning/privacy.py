"""Redação de dados pessoais antes de o texto sair do banco para o LLM de reflexão.

O texto de um turno pode trazer telefone, CPF, e-mail e número de documento. A rodada trabalha com
padrões, não com pessoas: tudo isso vira marcador antes de montar o prompt. Falha para o lado seguro
(redige a mais, nunca a menos).
"""

from __future__ import annotations

import re

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_CPF = re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b")
_CNPJ = re.compile(r"\b\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}\b")
# 8+ digitos com separadores comuns: telefone, cartao, protocolo longo.
_NUMERO_LONGO = re.compile(r"\+?\d[\d\s().-]{6,}\d")


def redigir(texto: str | None) -> str:
    if not texto:
        return ""
    t = _EMAIL.sub("[email]", texto)
    t = _CNPJ.sub("[doc]", t)
    t = _CPF.sub("[doc]", t)
    t = _NUMERO_LONGO.sub("[numero]", t)
    return t.strip()
