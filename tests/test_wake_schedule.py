"""Wake so na queda: pausa com a 002 saudavel, retoma na queda confirmada."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
from pydantic import SecretStr

from app import failover, wake_schedule


def _cfg(**kw):
    base = dict(WAKE_AUTO=True, QSTASH_TOKEN=SecretStr("tok"), QSTASH_URL="https://q.test", WAKE_SCHEDULE_ID="standby-wake")
    base.update(kw)
    return SimpleNamespace(**base)


def _roda(coro_fn, handler):
    async def corre():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await coro_fn(c)

    return asyncio.run(corre())


def test_retomar_chama_a_api_com_o_token():
    vistos = []

    def h(r):
        vistos.append((r.method, str(r.url), r.headers["authorization"]))
        return httpx.Response(200)

    assert _roda(lambda c: wake_schedule.retomar(_cfg(), cliente=c), h)
    assert vistos == [("POST", "https://q.test/v2/schedules/standby-wake/resume", "Bearer tok")]


def test_pausar_usa_pause():
    vistos = []
    _roda(lambda c: wake_schedule.pausar(_cfg(), cliente=c), lambda r: vistos.append(r.url.path) or httpx.Response(200))
    assert vistos == ["/v2/schedules/standby-wake/pause"]


def test_desligado_ou_sem_credencial_nao_chama():
    def h(r):
        raise AssertionError("nao devia chamar")

    for cfg in (_cfg(WAKE_AUTO=False), _cfg(QSTASH_TOKEN=None), _cfg(QSTASH_URL="")):
        assert _roda(lambda c, cfg=cfg: wake_schedule.retomar(cfg, cliente=c), h) is False


def test_falha_da_api_nao_levanta():
    assert _roda(lambda c: wake_schedule.pausar(_cfg(), cliente=c), lambda r: httpx.Response(500)) is False

    def erro(r):
        raise httpx.ConnectError("x")

    assert _roda(lambda c: wake_schedule.pausar(_cfg(), cliente=c), erro) is False


def test_queda_confirmada_retoma_o_wake(monkeypatch):
    chamadas = []

    async def sonda_fora(url, **kw):
        return failover.Sonda(False, 3, 503, None)

    async def retomar(settings, **kw):
        chamadas.append("resume")
        return True

    monkeypatch.setattr(failover, "sondar", sonda_fora)
    monkeypatch.setattr(wake_schedule, "retomar", retomar)
    failover.zerar_cooldown()
    r = asyncio.run(failover.tratar_callback(probe_url="https://x", alert_url=None, cooldown_s=60, callback={}, settings=_cfg()))
    assert r["status"] == "alert" and r["wake_retomado"] is True and chamadas == ["resume"]


def test_alarme_falso_nao_retoma(monkeypatch):
    async def sonda_ok(url, **kw):
        return failover.Sonda(True, 1, 200, None)

    async def retomar(settings, **kw):
        raise AssertionError("nao devia retomar")

    monkeypatch.setattr(failover, "sondar", sonda_ok)
    monkeypatch.setattr(wake_schedule, "retomar", retomar)
    r = asyncio.run(failover.tratar_callback(probe_url="https://x", alert_url=None, cooldown_s=60, callback={}, settings=_cfg()))
    assert r["status"] == "false_alarm"
