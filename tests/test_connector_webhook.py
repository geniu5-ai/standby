"""/webhook/connector: envelope assinado com segredo PROPRIO; o HMAC da Evolution nao se mistura."""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

from app.ingress.connector import SIGNATURE_HEADER, TIMESTAMP_HEADER

SEGREDO_CONECTOR = "segredo-do-conector-de-teste-0123456789"
HMAC_EVOLUTION = "segredo-da-evolution-de-teste"
CAMINHO = "/webhook/connector"


def _assinar(corpo: bytes, segredo: str = SEGREDO_CONECTOR, ts: str | None = None) -> dict[str, str]:
    ts = ts or str(int(time.time()))
    assinatura = "sha256=" + hmac.new(segredo.encode(), ts.encode() + b"." + corpo, hashlib.sha256).hexdigest()
    return {TIMESTAMP_HEADER: ts, SIGNATURE_HEADER: assinatura, "content-type": "application/json"}


def _envelope(**extra) -> bytes:
    evolution = {
        "event": "messages.upsert",
        "instance": "instancia-de-teste",
        "data": {
            "key": {"id": "MSG-1", "remoteJid": "5511999999999@s.whatsapp.net", "fromMe": False},
            "message": {"conversation": "Ola, com quem eu falo?"},
            "messageType": "conversation",
            "pushName": "Fulano",
        },
    }
    corpo = {"v": 1, "source": "openwa", "event_id": "evt-1", "evolution": evolution}
    corpo.update(extra)
    return json.dumps(corpo).encode()


@pytest.fixture
def cliente(monkeypatch):
    from app import main as main_mod
    from app.settings import get_settings

    monkeypatch.setenv("CONNECTOR_HMAC_SECRET", SEGREDO_CONECTOR)
    monkeypatch.setenv("HMAC_SECRET", HMAC_EVOLUTION)
    get_settings.cache_clear()
    chamadas: list[dict] = []

    async def aceita(webhook, *, provider, runtime_settings, **kw):
        chamadas.append({"webhook": webhook, "provider": provider})
        return {"status": "accepted", "workflow_id": "wf-1", "conversation_id": "conv-1"}

    monkeypatch.setattr(main_mod, "_accept_ingress", aceita)
    yield TestClient(main_mod.app), chamadas  # sem `with`: nao roda o lifespan
    get_settings.cache_clear()


def test_envelope_valido_entra_no_fluxo_comum_como_evolution(cliente):
    http, chamadas = cliente
    corpo = _envelope()
    resposta = http.post(CAMINHO, content=corpo, headers=_assinar(corpo))
    assert resposta.status_code == 202 and resposta.json()["status"] == "accepted"
    assert len(chamadas) == 1 and chamadas[0]["provider"] == "evolution"
    webhook = chamadas[0]["webhook"]
    assert webhook.instance == "instancia-de-teste"
    assert webhook.data.key.id == "MSG-1" and webhook.text_content == "Ola, com quem eu falo?"


def test_assinatura_ausente_errada_ou_vencida_da_401_e_nada_entra(cliente):
    http, chamadas = cliente
    corpo = _envelope()
    assert http.post(CAMINHO, content=corpo).status_code == 401
    assert http.post(CAMINHO, content=corpo, headers=_assinar(corpo, "outro-segredo")).status_code == 401
    velho = str(int(time.time()) - 3600)
    assert http.post(CAMINHO, content=corpo, headers=_assinar(corpo, ts=velho)).status_code == 401
    adulterado = _assinar(corpo)
    assert http.post(CAMINHO, content=corpo.replace(b"MSG-1", b"MSG-2"), headers=adulterado).status_code == 401
    assert chamadas == []


def test_hmac_da_evolution_nao_autentica_no_conector(cliente):
    http, chamadas = cliente
    corpo = _envelope()
    # assinado com o HMAC_SECRET da Evolution, nos dois formatos que a rota da Evolution aceita
    assert http.post(CAMINHO, content=corpo, headers=_assinar(corpo, HMAC_EVOLUTION)).status_code == 401
    cabecalhos = {"X-Hub-Signature-256": "sha256=" + hmac.new(HMAC_EVOLUTION.encode(), corpo, hashlib.sha256).hexdigest()}
    assert http.post(CAMINHO, content=corpo, headers=cabecalhos).status_code == 401
    assert http.post(CAMINHO, content=corpo, headers={"X-Evolution-Token": HMAC_EVOLUTION}).status_code == 401
    assert chamadas == []


def test_segredo_do_conector_nao_autentica_na_rota_da_evolution(cliente):
    http, chamadas = cliente
    corpo = _envelope()
    cabecalhos = {"X-Hub-Signature-256": "sha256=" + hmac.new(SEGREDO_CONECTOR.encode(), corpo, hashlib.sha256).hexdigest()}
    assert http.post("/webhook/evolution", content=corpo, headers=cabecalhos).status_code == 401
    assert http.post("/webhook/evolution", content=corpo, headers={"X-Evolution-Token": SEGREDO_CONECTOR}).status_code == 401
    assert chamadas == []


def test_sem_segredo_do_conector_falha_fechado_e_nao_cai_no_hmac_da_evolution(monkeypatch):
    from app import main as main_mod
    from app.settings import get_settings

    monkeypatch.delenv("CONNECTOR_HMAC_SECRET", raising=False)
    monkeypatch.setenv("HMAC_SECRET", HMAC_EVOLUTION)
    get_settings.cache_clear()
    try:
        corpo = _envelope()
        # mesmo assinado com o HMAC da Evolution: 503, nunca 202
        resposta = TestClient(main_mod.app).post(CAMINHO, content=corpo, headers=_assinar(corpo, HMAC_EVOLUTION))
        assert resposta.status_code == 503
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize(
    "corpo",
    [
        _envelope(v=2),  # versao desconhecida
        _envelope(source="outro"),  # origem fora da lista
        _envelope(extra_campo="x"),  # campo desconhecido (extra=forbid)
        json.dumps({"v": 1, "source": "openwa", "event_id": "e", "evolution": {"event": "messages.upsert"}}).encode(),
        json.dumps({"v": 1, "source": "openwa", "event_id": "", "evolution": {}}).encode(),
    ],
)
def test_envelope_invalido_da_422(cliente, corpo):
    http, chamadas = cliente
    assert http.post(CAMINHO, content=corpo, headers=_assinar(corpo)).status_code == 422
    assert chamadas == []


def test_corpo_que_nao_e_json_assinado_da_400(cliente):
    http, chamadas = cliente
    corpo = b"isto nao e json"
    assert http.post(CAMINHO, content=corpo, headers=_assinar(corpo)).status_code == 400
    assert chamadas == []


def test_media_do_zernio_vinda_de_fora_e_descartada(cliente):
    http, chamadas = cliente
    envelope = json.loads(_envelope())
    envelope["evolution"]["data"]["geniusMedia"] = {"mediaId": "abc123", "kind": "image"}
    corpo = json.dumps(envelope).encode()
    resposta = http.post(CAMINHO, content=corpo, headers=_assinar(corpo))
    assert resposta.status_code in (202, 422)  # 422 se o formato da referencia nao validar
    if resposta.status_code == 202:
        assert chamadas[0]["webhook"].data.genius_media is None
