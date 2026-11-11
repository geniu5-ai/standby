"""WAKE pelo QStash: verificacao da Upstash-Signature e a rota /v1/control/qstash-wake."""

from __future__ import annotations

import base64
import hashlib
import time

import jwt
import pytest
from fastapi.testclient import TestClient

from app import wake
from app.qstash_auth import assinatura_valida

ATUAL = "sig_chave_atual_de_teste_000000000"
PROXIMA = "sig_chave_proxima_de_teste_00000000"
URL = "https://standby.exemplo.test/v1/control/qstash-wake"


def _assinar(corpo: bytes, chave: str = ATUAL, *, url: str = URL, exp: int | None = None) -> str:
    agora = int(time.time())
    hash_corpo = base64.urlsafe_b64encode(hashlib.sha256(corpo).digest()).decode().rstrip("=")
    return jwt.encode(
        {
            "iss": "Upstash",
            "sub": url,
            "exp": exp if exp is not None else agora + 300,
            "nbf": agora - 5,
            "iat": agora,
            "jti": "jwt_teste",
            "body": hash_corpo,
        },
        chave,
        algorithm="HS256",
    )


def _valida(corpo: bytes, assinatura: str | None, **extra) -> bool:
    return assinatura_valida(corpo, assinatura, current_key=ATUAL, next_key=PROXIMA, **extra)


@pytest.mark.parametrize("corpo", [b"", b'{"origem":"schedule"}', "acentuação".encode()])
def test_assinatura_da_chave_atual_passa(corpo):
    assert _valida(corpo, _assinar(corpo))


def test_assinatura_da_chave_proxima_passa_na_rotacao():
    assert _valida(b"{}", _assinar(b"{}", PROXIMA))


def test_chave_errada_corpo_adulterado_e_expirada_falham():
    assert not _valida(b"{}", _assinar(b"{}", "sig_de_outro_projeto_00000000000"))
    assert not _valida(b'{"a":2}', _assinar(b'{"a":1}'))
    assert not _valida(b"{}", _assinar(b"{}", exp=int(time.time()) - 60))


def test_sem_cabecalho_sem_chave_e_corpo_binario_falham_fechado():
    assert not _valida(b"{}", None)
    assert not _valida(b"{}", "")
    assert not _valida(b"{}", "nao-e-um-jwt")
    assert not assinatura_valida(b"{}", _assinar(b"{}"), current_key="", next_key=PROXIMA)
    assert not assinatura_valida(b"{}", _assinar(b"{}"), current_key=ATUAL, next_key="")
    assert not _valida(b"\xff\xfe", _assinar(b"\xff\xfe"))


def test_destino_so_e_conferido_quando_informado():
    assinatura = _assinar(b"{}", url=URL)
    assert _valida(b"{}", assinatura)
    assert _valida(b"{}", assinatura, url=URL)
    assert not _valida(b"{}", assinatura, url="https://outro.exemplo.test/x")


@pytest.fixture
def cliente(monkeypatch):
    from app import main as main_mod
    from app.settings import get_settings

    monkeypatch.setenv("QSTASH_CURRENT_SIGNING_KEY", ATUAL)
    monkeypatch.setenv("QSTASH_NEXT_SIGNING_KEY", PROXIMA)
    monkeypatch.delenv("QSTASH_WAKE_URL", raising=False)
    get_settings.cache_clear()
    monkeypatch.setattr(wake, "consultar", lambda *a, **k: wake.WakeState(1, 0, 2))
    yield TestClient(main_mod.app)  # sem `with`: nao roda o lifespan (sem worker/banco)
    get_settings.cache_clear()


def test_rota_autorizada_devolve_o_mesmo_estado_do_wake(cliente):
    resposta = cliente.post(
        "/v1/control/qstash-wake", content=b"", headers={"Upstash-Signature": _assinar(b"")}
    )
    assert resposta.status_code == 200
    assert resposta.json() == {
        "status": "awake", "jobs_vencidos": 1, "jobs_travados": 0, "pendentes": 2, "wake_needed": True,
    }


def test_rota_com_a_chave_proxima_tambem_passa(cliente):
    corpo = b'{"x":1}'
    resposta = cliente.post(
        "/v1/control/qstash-wake", content=corpo, headers={"Upstash-Signature": _assinar(corpo, PROXIMA)}
    )
    assert resposta.status_code == 200


def test_rota_assinatura_errada_ausente_ou_adulterada_da_401(cliente):
    caminho = "/v1/control/qstash-wake"
    assert cliente.post(caminho, content=b"{}").status_code == 401
    errada = {"Upstash-Signature": _assinar(b"{}", "sig_de_outro_projeto_00000000000")}
    assert cliente.post(caminho, content=b"{}", headers=errada).status_code == 401
    adulterado = {"Upstash-Signature": _assinar(b'{"a":1}')}
    assert cliente.post(caminho, content=b'{"a":2}', headers=adulterado).status_code == 401


def test_rota_confere_o_destino_quando_a_url_esta_configurada(cliente, monkeypatch):
    from app.settings import get_settings

    monkeypatch.setenv("QSTASH_WAKE_URL", URL)
    get_settings.cache_clear()
    caminho = "/v1/control/qstash-wake"
    ok = {"Upstash-Signature": _assinar(b"", url=URL)}
    assert cliente.post(caminho, content=b"", headers=ok).status_code == 200
    outro = {"Upstash-Signature": _assinar(b"", url="https://outro.exemplo.test/x")}
    assert cliente.post(caminho, content=b"", headers=outro).status_code == 401


def test_rota_sem_chave_configurada_falha_fechado(monkeypatch):
    from app import main as main_mod
    from app.settings import get_settings

    monkeypatch.delenv("QSTASH_CURRENT_SIGNING_KEY", raising=False)
    monkeypatch.delenv("QSTASH_NEXT_SIGNING_KEY", raising=False)
    get_settings.cache_clear()
    try:
        resposta = TestClient(main_mod.app).post(
            "/v1/control/qstash-wake", content=b"", headers={"Upstash-Signature": _assinar(b"")}
        )
        assert resposta.status_code == 503
    finally:
        get_settings.cache_clear()


def test_rota_do_wake_por_hmac_continua_igual(monkeypatch):
    """A refatoracao do miolo nao muda /v1/control/wake: sem segredo, 503; sem assinatura, 401."""
    from app import main as main_mod
    from app.settings import get_settings

    monkeypatch.setenv("CONTROL_HMAC_SECRET", "segredo-de-controle-de-teste")
    get_settings.cache_clear()
    try:
        assert TestClient(main_mod.app).post("/v1/control/wake", content=b"{}").status_code == 401
    finally:
        get_settings.cache_clear()
