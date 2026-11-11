"""Vigia da VPS 002 em modo alerta: sonda, aviso com cooldown e a rota /v1/control/qstash-failover."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.util
import time
from pathlib import Path

import httpx
import jwt
import pytest
from fastapi.testclient import TestClient

from app import failover

ATUAL = "sig_chave_atual_de_teste_000000000"
PROXIMA = "sig_chave_proxima_de_teste_00000000"
URL_FAILOVER = "https://standby.exemplo.test/v1/control/qstash-failover"
SONDA = "https://vps.exemplo.test/health"


def _cliente(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _sondar(handler, **kw):
    async def corre():
        async with _cliente(handler) as c:
            return await failover.sondar(SONDA, espera_s=0, cliente=c, **kw)

    return asyncio.run(corre())


def test_sonda_saudavel_exige_200_e_ok_no_corpo():
    sonda = _sondar(lambda r: httpx.Response(200, json={"status": "ok"}))
    assert sonda.ok and sonda.tentativas == 1 and sonda.ultimo_status == 200


def test_sonda_200_sem_ok_no_corpo_nao_e_saudavel():
    sonda = _sondar(lambda r: httpx.Response(200, text="<html>pagina de erro do tunel</html>"))
    assert not sonda.ok and sonda.tentativas == 3


def test_sonda_tenta_de_novo_e_recupera_na_segunda():
    respostas = iter([httpx.Response(502), httpx.Response(200, json={"status": "ok"})])
    sonda = _sondar(lambda r: next(respostas))
    assert sonda.ok and sonda.tentativas == 2


def test_sonda_com_5xx_ou_erro_de_rede_falha_depois_das_tentativas():
    assert _sondar(lambda r: httpx.Response(503)).ultimo_status == 503

    def cai(request):
        raise httpx.ConnectError("sem rota")

    sonda = _sondar(cai)
    assert not sonda.ok and sonda.erro == "ConnectError" and sonda.ultimo_status is None


@pytest.fixture(autouse=True)
def _zera_cooldown():
    failover.zerar_cooldown()
    yield
    failover.zerar_cooldown()


def test_cooldown_do_aviso():
    assert failover.deve_avisar(1000.0, 1800)
    assert not failover.deve_avisar(1100.0, 1800)
    assert failover.deve_avisar(1000.0 + 1800, 1800)


def _trata(monkeypatch, *, ok: bool, avisos: list, alert_url="https://alerta.exemplo.test/x"):
    async def sondar(url, **kw):
        return failover.Sonda(ok, 1 if ok else 3, 200 if ok else 503, None)

    async def avisar(url, texto, detalhes, **kw):
        avisos.append((url, texto, detalhes))
        return True

    monkeypatch.setattr(failover, "sondar", sondar)
    monkeypatch.setattr(failover, "avisar", avisar)
    return asyncio.run(
        failover.tratar_callback(
            probe_url=SONDA, alert_url=alert_url, cooldown_s=1800, callback={"status": 503, "retried": 2}
        )
    )


def test_alarme_falso_nao_avisa(monkeypatch):
    avisos: list = []
    resultado = _trata(monkeypatch, ok=True, avisos=avisos)
    assert resultado["status"] == "false_alarm" and resultado["mode"] == "alert" and not avisos


def test_queda_confirmada_avisa_uma_vez_e_respeita_o_cooldown(monkeypatch):
    avisos: list = []
    primeiro = _trata(monkeypatch, ok=False, avisos=avisos)
    assert primeiro["status"] == "alert" and primeiro["avisou"] and len(avisos) == 1
    assert avisos[0][2]["qstash_retried"] == 2
    segundo = _trata(monkeypatch, ok=False, avisos=avisos)
    assert segundo["status"] == "alert" and not segundo["avisou"] and len(avisos) == 1


def test_queda_sem_webhook_configurado_so_registra(monkeypatch):
    avisos: list = []
    resultado = _trata(monkeypatch, ok=False, avisos=avisos, alert_url=None)
    assert resultado["status"] == "alert" and not resultado["avisou"] and not avisos


def test_avisar_posta_json_e_falha_de_rede_vira_false():
    vistos: list = []

    def handler(request):
        vistos.append(request.read())
        return httpx.Response(200)

    async def corre(h):
        async with _cliente(h) as c:
            return await failover.avisar("https://alerta.exemplo.test/x", "texto", {"a": 1}, cliente=c)

    assert asyncio.run(corre(handler)) and b'"text"' in vistos[0] and b'"details"' in vistos[0]

    def cai(request):
        raise httpx.ConnectError("sem rota")

    assert asyncio.run(corre(cai)) is False


# --- rota -------------------------------------------------------------------------------------

def _assinar(corpo: bytes, chave: str = ATUAL, *, url: str = URL_FAILOVER) -> str:
    agora = int(time.time())
    hash_corpo = base64.urlsafe_b64encode(hashlib.sha256(corpo).digest()).decode().rstrip("=")
    return jwt.encode(
        {"iss": "Upstash", "sub": url, "exp": agora + 300, "nbf": agora - 5, "iat": agora, "jti": "t", "body": hash_corpo},
        chave,
        algorithm="HS256",
    )


@pytest.fixture
def cliente(monkeypatch):
    from app import main as main_mod
    from app.settings import get_settings

    monkeypatch.setenv("QSTASH_CURRENT_SIGNING_KEY", ATUAL)
    monkeypatch.setenv("QSTASH_NEXT_SIGNING_KEY", PROXIMA)
    monkeypatch.setenv("FAILOVER_PROBE_URL", SONDA)
    monkeypatch.setenv("FAILOVER_ALERT_URL", "https://alerta.exemplo.test/x")
    monkeypatch.delenv("QSTASH_FAILOVER_URL", raising=False)
    get_settings.cache_clear()
    yield TestClient(main_mod.app)  # sem `with`: nao roda o lifespan
    get_settings.cache_clear()


def test_rota_confirma_a_queda_e_avisa(cliente, monkeypatch):
    avisos: list = []

    async def sondar(url, **kw):
        assert url == SONDA
        return failover.Sonda(False, 3, 502, None)

    async def avisar(url, texto, detalhes, **kw):
        avisos.append(url)
        return True

    monkeypatch.setattr(failover, "sondar", sondar)
    monkeypatch.setattr(failover, "avisar", avisar)
    corpo = b'{"status":502,"retried":2,"scheduleId":"vps002-watchdog"}'
    resposta = cliente.post("/v1/control/qstash-failover", content=corpo, headers={"Upstash-Signature": _assinar(corpo)})
    assert resposta.status_code == 200
    assert resposta.json()["status"] == "alert" and resposta.json()["mode"] == "alert" and avisos == ["https://alerta.exemplo.test/x"]


def test_rota_alarme_falso_devolve_200_sem_avisar(cliente, monkeypatch):
    async def sondar(url, **kw):
        return failover.Sonda(True, 1, 200, None)

    monkeypatch.setattr(failover, "sondar", sondar)
    corpo = b"{}"
    resposta = cliente.post("/v1/control/qstash-failover", content=corpo, headers={"Upstash-Signature": _assinar(corpo)})
    assert resposta.status_code == 200 and resposta.json()["status"] == "false_alarm"


def test_rota_corpo_que_nao_e_json_ainda_funciona(cliente, monkeypatch):
    async def sondar(url, **kw):
        return failover.Sonda(True, 1, 200, None)

    monkeypatch.setattr(failover, "sondar", sondar)
    corpo = b"nao e json"
    resposta = cliente.post("/v1/control/qstash-failover", content=corpo, headers={"Upstash-Signature": _assinar(corpo)})
    assert resposta.status_code == 200


def test_rota_sem_assinatura_ou_com_assinatura_errada_da_401(cliente):
    assert cliente.post("/v1/control/qstash-failover", content=b"{}").status_code == 401
    errada = {"Upstash-Signature": _assinar(b"{}", "sig_de_outro_projeto_00000000000")}
    assert cliente.post("/v1/control/qstash-failover", content=b"{}", headers=errada).status_code == 401


def test_rota_confere_o_destino_quando_configurado(cliente, monkeypatch):
    from app.settings import get_settings

    async def sondar(url, **kw):
        return failover.Sonda(True, 1, 200, None)

    monkeypatch.setattr(failover, "sondar", sondar)
    monkeypatch.setenv("QSTASH_FAILOVER_URL", URL_FAILOVER)
    get_settings.cache_clear()
    ok = {"Upstash-Signature": _assinar(b"{}", url=URL_FAILOVER)}
    assert cliente.post("/v1/control/qstash-failover", content=b"{}", headers=ok).status_code == 200
    outro = {"Upstash-Signature": _assinar(b"{}", url="https://outro.exemplo.test/x")}
    assert cliente.post("/v1/control/qstash-failover", content=b"{}", headers=outro).status_code == 401


def test_rota_sem_chave_ou_sem_url_da_sonda_falha_fechado(monkeypatch):
    from app import main as main_mod
    from app.settings import get_settings

    for nome in ("QSTASH_CURRENT_SIGNING_KEY", "FAILOVER_PROBE_URL"):
        monkeypatch.setenv("QSTASH_CURRENT_SIGNING_KEY", ATUAL)
        monkeypatch.setenv("FAILOVER_PROBE_URL", SONDA)
        monkeypatch.delenv(nome)
        get_settings.cache_clear()
        try:
            resposta = TestClient(main_mod.app).post(
                "/v1/control/qstash-failover", content=b"{}", headers={"Upstash-Signature": _assinar(b"{}")}
            )
            assert resposta.status_code == 503, nome
        finally:
            get_settings.cache_clear()


# --- script -----------------------------------------------------------------------------------

def _script():
    caminho = Path(__file__).resolve().parent.parent / "scripts" / "qstash_schedule.py"
    spec = importlib.util.spec_from_file_location("qstash_schedule", caminho)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


def test_script_watchdog_simulado_mostra_o_callback_e_nao_grava(monkeypatch, capsys):
    script = _script()
    monkeypatch.setattr(script, "_cliente", lambda: pytest.fail("nao devia falar com o QStash"))
    codigo = script.main(["apply", "--schedule", "watchdog", "--url", SONDA, "--callback", URL_FAILOVER])
    saida = capsys.readouterr().out
    assert codigo == 0 and "vps002-watchdog" in saida and "failure_callback=" + URL_FAILOVER in saida
    assert "method=GET" in saida and "retries=2" in saida and "simulacao" in saida


def test_script_exige_https_schedule_e_callback(monkeypatch):
    script = _script()
    for nome in ("QSTASH_FAILOVER_URL", "FAILOVER_PROBE_URL", "QSTASH_WAKE_URL"):
        monkeypatch.delenv(nome, raising=False)
    assert script.main(["apply", "--schedule", "watchdog", "--url", "http://x", "--callback", URL_FAILOVER]) == 2
    assert script.main(["apply", "--schedule", "watchdog", "--url", SONDA]) == 2  # sem callback
    assert script.main(["apply", "--schedule", "wake"]) == 2  # sem destino
    assert script.main(["apply"]) == 2  # sem --schedule


# --- webhook de saida do Better Stack ----------------------------------------------------------

SEGREDO_BS = "segredo-do-webhook-de-teste-0123456789"
INCIDENTE = (
    b'{"data":{"id":"1","type":"incident","attributes":{"name":"Next \xc2\xb7 /health","cause":"Status 503"},'
    b'"relationships":{"monitor":{"data":{"id":"5222117","type":"monitor"}}}}}'
)


def test_monitor_do_incidente_le_id_e_nome_e_tolera_lixo():
    assert failover.monitor_do_incidente({"data": {"attributes": {"name": "x"}, "relationships": {"monitor": {"data": {"id": 7}}}}}) == ("7", "x")
    assert failover.monitor_do_incidente({}) == (None, None)
    assert failover.monitor_do_incidente("texto") == (None, None)
    assert failover.monitor_do_incidente({"data": []}) == (None, None)


@pytest.fixture
def cliente_bs(monkeypatch):
    from app import main as main_mod
    from app.settings import get_settings

    monkeypatch.setenv("BETTERSTACK_WEBHOOK_SECRET", SEGREDO_BS)
    monkeypatch.setenv("BETTERSTACK_WATCHED_MONITORS", "5222117, 5222118")
    monkeypatch.setenv("FAILOVER_PROBE_URL", SONDA)
    monkeypatch.setenv("FAILOVER_ALERT_URL", "https://alerta.exemplo.test/x")
    get_settings.cache_clear()
    yield TestClient(main_mod.app)
    get_settings.cache_clear()


def test_betterstack_incidente_de_monitor_vigiado_confirma_e_avisa(cliente_bs, monkeypatch):
    avisos: list = []

    async def sondar(url, **kw):
        return failover.Sonda(False, 3, 503, None)

    async def avisar(url, texto, detalhes, **kw):
        avisos.append(detalhes["schedule"])
        return True

    monkeypatch.setattr(failover, "sondar", sondar)
    monkeypatch.setattr(failover, "avisar", avisar)
    resposta = cliente_bs.post("/v1/control/betterstack-incident", content=INCIDENTE, headers={"X-Webhook-Secret": SEGREDO_BS})
    assert resposta.status_code == 200 and resposta.json()["status"] == "alert" and resposta.json()["mode"] == "alert"
    assert avisos == ["betterstack:5222117"]


def test_betterstack_incidente_de_outro_monitor_e_ignorado_sem_sondar(cliente_bs, monkeypatch):
    async def sondar(url, **kw):
        pytest.fail("nao devia sondar")

    monkeypatch.setattr(failover, "sondar", sondar)
    outro = INCIDENTE.replace(b"5222117", b"999")
    resposta = cliente_bs.post("/v1/control/betterstack-incident", content=outro, headers={"X-Webhook-Secret": SEGREDO_BS})
    assert resposta.status_code == 200 and resposta.json()["status"] == "ignored"


def test_betterstack_segredo_errado_ou_ausente_da_401(cliente_bs):
    caminho = "/v1/control/betterstack-incident"
    assert cliente_bs.post(caminho, content=INCIDENTE).status_code == 401
    assert cliente_bs.post(caminho, content=INCIDENTE, headers={"X-Webhook-Secret": "errado"}).status_code == 401


def test_betterstack_sem_segredo_ou_sem_url_da_sonda_falha_fechado(monkeypatch):
    from app import main as main_mod
    from app.settings import get_settings

    for nome in ("BETTERSTACK_WEBHOOK_SECRET", "FAILOVER_PROBE_URL"):
        monkeypatch.setenv("BETTERSTACK_WEBHOOK_SECRET", SEGREDO_BS)
        monkeypatch.setenv("FAILOVER_PROBE_URL", SONDA)
        monkeypatch.delenv(nome)
        get_settings.cache_clear()
        try:
            resposta = TestClient(main_mod.app).post(
                "/v1/control/betterstack-incident", content=INCIDENTE, headers={"X-Webhook-Secret": SEGREDO_BS}
            )
            assert resposta.status_code == 503, nome
        finally:
            get_settings.cache_clear()
