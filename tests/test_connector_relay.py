"""Saida pelo conector sem sair do record_only: rotas assinadas /v1/connector/outbox e travas."""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

from app.durable import connector_relay
from app.ingress.connector import SIGNATURE_HEADER, TIMESTAMP_HEADER

SEGREDO = "segredo-do-conector-de-teste-0123456789"
HMAC_EVOLUTION = "segredo-da-evolution-de-teste"
LISTA = "/v1/connector/outbox"
ACK = "/v1/connector/outbox/41/ack"


def _assinar(corpo: bytes = b"", segredo: str = SEGREDO, ts: str | None = None) -> dict[str, str]:
    ts = ts or str(int(time.time()))
    assinatura = "sha256=" + hmac.new(segredo.encode(), ts.encode() + b"." + corpo, hashlib.sha256).hexdigest()
    return {TIMESTAMP_HEADER: ts, SIGNATURE_HEADER: assinatura, "content-type": "application/json"}


# --- SQL e anotacao (sem banco) ---------------------------------------------------------------


def test_todos_os_marcadores_do_sql_tem_parametro():
    pend = connector_relay.pending_params(instances=["a"], max_age_s=900)
    for chave in pend:
        assert f"%({chave})s" in connector_relay.PENDING_SQL
    for chave in ("id", "instances", "relay"):
        assert f"%({chave})s" in connector_relay.ACK_SQL


def test_sql_so_pega_gravado_record_only_texto_de_cliente_e_nao_entregue():
    sql = " ".join(connector_relay.PENDING_SQL.split())
    for trecho in (
        "delivery_status = 'recorded'",
        "delivery_mode = 'record_only'",
        "kind = 'customer'",
        "action = 'send_text'",
        "instance = ANY(%(instances)s::text[])",
        "created_at > now() - make_interval(secs => %(max_age_s)s)",
        "provider_result -> 'relay' IS NULL",
    ):
        assert trecho in sql
    # o ack nao muda o estado da linha: so anota provider_result
    ack = " ".join(connector_relay.ACK_SQL.split())
    assert "SET provider_result = provider_result ||" in ack and "delivery_status =" in ack.split("WHERE")[1]
    assert "delivery_status = %" not in ack.split("WHERE")[0]


def test_limite_de_itens_e_instancias():
    assert connector_relay.pending_params(instances=["a"], max_age_s=900, limit=500)["limit"] == connector_relay.MAX_ITEMS
    assert connector_relay.pending_params(instances=["a"], max_age_s=900, limit=0)["limit"] == 1
    assert connector_relay.parse_instances(" a, b ,,c ") == ["a", "b", "c"] and connector_relay.parse_instances("") == []


def test_anotacao_guarda_so_o_resultado_da_entrega():
    bruto = connector_relay.relay_annotation(status="sent", provider_message_id="MSG-1", error=None)
    assert json.loads(bruto)["relay"]["status"] == "sent" and json.loads(bruto)["relay"]["provider_message_id"] == "MSG-1"
    longo = json.loads(connector_relay.relay_annotation(status="failed", provider_message_id=None, error="x" * 500))
    assert len(longo["relay"]["error"]) == 200 and "provider_message_id" not in longo["relay"]


def test_sem_instancias_nada_sai_e_nem_toca_no_banco():
    assert connector_relay.list_pending("postgresql://invalido/x", instances=[], max_age_s=900) == []
    assert connector_relay.acknowledge("postgresql://invalido/x", outbox_id=1, instances=[], status="sent", provider_message_id=None, error=None) is False


# --- rotas ------------------------------------------------------------------------------------


@pytest.fixture
def cliente(monkeypatch):
    from app import main as main_mod
    from app.settings import get_settings

    monkeypatch.setenv("CONNECTOR_HMAC_SECRET", SEGREDO)
    monkeypatch.setenv("HMAC_SECRET", HMAC_EVOLUTION)
    monkeypatch.setenv("CONNECTOR_RELAY_INSTANCES", "inst-a, inst-b")
    monkeypatch.delenv("CONNECTOR_RELAY_MAX_AGE_SECONDS", raising=False)
    get_settings.cache_clear()
    chamadas: dict[str, list] = {"lista": [], "ack": []}

    def lista(url, *, instances, max_age_s):
        chamadas["lista"].append({"instances": instances, "max_age_s": max_age_s})
        return [{"id": 41, "instance": "inst-a", "destination_jid": "5511900000000@s.whatsapp.net", "text": "Oi!", "idempotency_key": "k", "created_at": "2026-11-11T18:04:05+00:00"}]

    def ack(url, *, outbox_id, instances, status, provider_message_id, error):
        chamadas["ack"].append({"id": outbox_id, "instances": instances, "status": status, "pmid": provider_message_id, "error": error})
        return outbox_id == 41

    monkeypatch.setattr(connector_relay, "list_pending", lista)
    monkeypatch.setattr(connector_relay, "acknowledge", ack)
    yield TestClient(main_mod.app), chamadas  # sem `with`: nao roda o lifespan
    get_settings.cache_clear()


def test_lista_assinada_devolve_so_o_que_a_trava_de_instancias_permite(cliente):
    http, chamadas = cliente
    resposta = http.get(LISTA, headers=_assinar())
    assert resposta.status_code == 200 and resposta.json()["items"][0]["id"] == 41
    assert chamadas["lista"] == [{"instances": ["inst-a", "inst-b"], "max_age_s": 900}]


def test_lista_e_ack_exigem_a_assinatura_do_conector(cliente):
    http, chamadas = cliente
    corpo = json.dumps({"status": "sent"}).encode()
    assert http.get(LISTA).status_code == 401
    assert http.get(LISTA, headers=_assinar(segredo="outro")).status_code == 401
    assert http.get(LISTA, headers=_assinar(ts=str(int(time.time()) - 3600))).status_code == 401
    assert http.post(ACK, content=corpo).status_code == 401
    assert http.post(ACK, content=corpo, headers=_assinar(corpo, "outro")).status_code == 401
    # o HMAC da Evolution nao autentica nas rotas do conector
    assert http.get(LISTA, headers=_assinar(segredo=HMAC_EVOLUTION)).status_code == 401
    assert chamadas == {"lista": [], "ack": []}


def test_sem_segredo_do_conector_falha_fechado(monkeypatch):
    from app import main as main_mod
    from app.settings import get_settings

    monkeypatch.delenv("CONNECTOR_HMAC_SECRET", raising=False)
    monkeypatch.setenv("HMAC_SECRET", HMAC_EVOLUTION)
    get_settings.cache_clear()
    try:
        http = TestClient(main_mod.app)
        assert http.get(LISTA, headers=_assinar(segredo=HMAC_EVOLUTION)).status_code == 503
        assert http.post(ACK, content=b"{}", headers=_assinar(b"{}", HMAC_EVOLUTION)).status_code == 503
    finally:
        get_settings.cache_clear()


def test_ack_anota_a_entrega(cliente):
    http, chamadas = cliente
    corpo = json.dumps({"status": "sent", "provider_message_id": "WA-1"}).encode()
    resposta = http.post(ACK, content=corpo, headers=_assinar(corpo))
    assert resposta.status_code == 200 and resposta.json() == {"status": "acked"}
    assert chamadas["ack"] == [{"id": 41, "instances": ["inst-a", "inst-b"], "status": "sent", "pmid": "WA-1", "error": None}]


def test_ack_de_linha_inelegivel_ou_ja_anotada_da_404(cliente):
    http, _ = cliente
    corpo = json.dumps({"status": "failed", "error": "destino fora da lista"}).encode()
    assert http.post("/v1/connector/outbox/999/ack", content=corpo, headers=_assinar(corpo)).status_code == 404


@pytest.mark.parametrize(
    "corpo",
    [b"{}", b'{"status":"talvez"}', b'{"status":"sent","extra":1}', b"nao e json", b'{"status":"failed","error":"' + b"x" * 300 + b'"}'],
)
def test_ack_invalido_da_422(cliente, corpo):
    http, chamadas = cliente
    assert http.post(ACK, content=corpo, headers=_assinar(corpo)).status_code == 422
    assert chamadas["ack"] == []
