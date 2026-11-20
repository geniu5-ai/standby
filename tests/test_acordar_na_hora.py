"""Standby: job criado com atraso avisa o worker na hora em que vence (poll longo nao o deixa esperando)."""

from __future__ import annotations

import json
from typing import ClassVar

import pytest
from procrastinate.exceptions import AlreadyEnqueued

from app import jobs as jobs_mod


class _TimerFalso:
    criados: ClassVar[list] = []

    def __init__(self, intervalo, funcao, args=()):
        self.intervalo, self.funcao, self.args = intervalo, funcao, args
        self.daemon = False
        self.iniciado = False
        _TimerFalso.criados.append(self)

    def start(self):
        self.iniciado = True


class _TarefaFalsa:
    queue = "turn"


@pytest.fixture(autouse=True)
def _timers(monkeypatch):
    _TimerFalso.criados = []
    monkeypatch.setattr(jobs_mod.threading, "Timer", _TimerFalso)
    return _TimerFalso


def _defer(monkeypatch, retorno=77, erro=None, **kw):
    def falso(task, opts, kwargs):
        if erro:
            raise erro
        return retorno

    monkeypatch.setattr(jobs_mod, "_submit", falso)
    return jobs_mod.defer_once(_TarefaFalsa(), **kw)


def test_job_com_atraso_curto_agenda_dois_avisos_nas_horas_certas(monkeypatch, _timers):
    assert _defer(monkeypatch, schedule_in=8) is True
    assert [t.intervalo for t in _timers.criados] == [9.0, 14.0]
    for t in _timers.criados:
        assert t.iniciado and t.daemon  # daemon: nao segura o encerramento do processo
        assert t.funcao is jobs_mod._notificar_fila and t.args == ("turn", 77)


@pytest.mark.parametrize("atraso", [None, 0, -3, 901, 86400])
def test_sem_atraso_ou_atraso_longo_nao_agenda_aviso(monkeypatch, _timers, atraso):
    _defer(monkeypatch, schedule_in=atraso)
    assert _timers.criados == []  # atraso longo: vale o wake do QStash


def test_limite_de_15_min_ainda_agenda(monkeypatch, _timers):
    _defer(monkeypatch, schedule_in=900)
    assert len(_timers.criados) == 2


def test_job_repetido_nao_agenda_aviso_e_devolve_false(monkeypatch, _timers):
    assert _defer(monkeypatch, erro=AlreadyEnqueued("ja existe"), schedule_in=8) is False
    assert _timers.criados == []


def test_notificar_fila_manda_o_mesmo_notify_do_procrastinate(monkeypatch):
    chamadas = []

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params):
            chamadas.append((sql, params))

    import psycopg

    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: Conn())
    monkeypatch.setattr(jobs_mod, "_conninfo", lambda: "postgresql://x/y")
    jobs_mod._notificar_fila("turn", 77)
    (sql, params), = chamadas
    assert "pg_notify" in sql
    assert params[0] == "procrastinate_queue_v1#turn"  # canal que o worker escuta
    assert json.loads(params[1]) == {"type": "job_inserted", "job_id": 77}


def test_falha_no_aviso_nao_derruba_nada(monkeypatch, caplog):
    import psycopg

    def quebra(*a, **k):
        raise OSError("sem rede")

    monkeypatch.setattr(psycopg, "connect", quebra)
    monkeypatch.setattr(jobs_mod, "_conninfo", lambda: "postgresql://x/y")
    jobs_mod._notificar_fila("turn", None)  # nao levanta
    assert "aviso de job vencido falhou" in caplog.text
