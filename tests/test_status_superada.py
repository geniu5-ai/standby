"""Auditoria de status superada por outra mais nova: não é erro. Cliente ausente: continua sendo erro."""

from __future__ import annotations

import pytest

from app.durable import persistence as p


class Resultado:
    def __init__(self, valor):
        self._valor = valor

    def scalar_one_or_none(self):
        return self._valor


class SessaoFake:
    """Devolve respostas na ordem: insert, aplicação do cliente, existência do cliente."""

    def __init__(self, *respostas):
        self._respostas = list(respostas)
        self.sql_visto = []

    def execute(self, sql, params=None):
        self.sql_visto.append(sql)
        return Resultado(self._respostas.pop(0))


class Resultado_status:
    def model_dump(self, mode="json"):
        return {"customer_status": "open", "confidence": 0.9, "evidence_source": "default", "evidence": None}


ARGS = dict(tenant_id="t1", turn_id="turn:1", phone="5511900000000", model="m", source_sequence=2)


def test_auditoria_superada_nao_levanta_erro():
    sessao = SessaoFake(1, None, 1)  # insert ok, aplicacao sem linha (superada), cliente existe
    assert p.apply_status_with_session(sessao, result=Resultado_status(), **ARGS) is False


def test_cliente_inexistente_continua_sendo_erro():
    sessao = SessaoFake(1, None, None)  # insert ok, aplicacao sem linha, cliente tambem nao existe
    with pytest.raises(p.ClientStateMissing):
        p.apply_status_with_session(sessao, result=Resultado_status(), **ARGS)


def test_aplicacao_normal_continua_verdadeira():
    sessao = SessaoFake(1, 1)
    assert p.apply_status_with_session(sessao, result=Resultado_status(), **ARGS) is True
