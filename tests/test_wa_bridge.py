"""wa-bridge (connectors/wa-bridge): assinatura, normalizacao, fila, entrega e ponta a ponta local."""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from app.contracts import EvolutionWebhook
from app.hmac_auth import verify_control_signature
from app.ingress.connector import ConnectorEnvelope


def _carrega():
    caminho = Path(__file__).resolve().parent.parent / "connectors" / "wa-bridge" / "wa_bridge.py"
    spec = importlib.util.spec_from_file_location("wa_bridge", caminho)
    modulo = importlib.util.module_from_spec(spec)
    sys.modules["wa_bridge"] = modulo  # os dataclass procuram o modulo em sys.modules
    spec.loader.exec_module(modulo)
    return modulo


wb = _carrega()

SEG_GATEWAY = "segredo-do-webhook-do-gateway-0123"
SEG_SAIDA = "segredo-do-conector-0123456789abcdef"
SESSAO = "00000000-0000-4000-8000-000000000001"


def _evento(**dados) -> dict:
    data = {
        "id": "TESTMSG00000000000000000000AA01",
        "from": "12345678901234@lid",
        "to": "5511900003333@c.us",
        "chatId": "12345678901234@lid",
        "body": "Ola, com quem eu falo?",
        "type": "text",
        "timestamp": 1794414116,
        "fromMe": False,
        "isGroup": False,
        "kind": "individual",
        "isStatusBroadcast": False,
        "isLidSender": True,
        "contact": {"pushName": "Fulano"},
    }
    data.update(dados)
    return {
        "event": "message.received",
        "timestamp": "2026-11-11T16:38:36.239Z",
        "sessionId": SESSAO,
        "idempotencyKey": "msg_sessao_TESTMSG_webhook",
        "deliveryId": "dlv_teste",
        "data": data,
    }


def _resolve(sessao: str, lid: str) -> str | None:
    return "5511900001111" if lid.endswith("@lid") else None


# --- assinatura de entrada --------------------------------------------------------------------


def test_assinatura_do_openwa():
    corpo = b'{"a":1}'
    ok = "sha256=" + hmac.new(SEG_GATEWAY.encode(), corpo, hashlib.sha256).hexdigest()
    assert wb.verify_openwa_signature(corpo, ok, SEG_GATEWAY)
    assert not wb.verify_openwa_signature(corpo, ok, "outro")
    assert not wb.verify_openwa_signature(b'{"a":2}', ok, SEG_GATEWAY)
    assert not wb.verify_openwa_signature(corpo, None, SEG_GATEWAY)
    assert not wb.verify_openwa_signature(corpo, "", SEG_GATEWAY)
    assert not wb.verify_openwa_signature(corpo, ok, "")


# --- normalizacao -----------------------------------------------------------------------------


def test_lid_vira_telefone_e_o_envelope_passa_nos_contratos_do_app():
    event_id, envelope = wb.normalize_openwa(_evento(), instance="instancia-de-teste", resolve_phone=_resolve)
    assert event_id == "msg_sessao_TESTMSG_webhook"
    parsed = ConnectorEnvelope.model_validate(envelope)
    webhook = EvolutionWebhook.model_validate(parsed.evolution)
    assert webhook.event == "messages.upsert" and webhook.instance == "instancia-de-teste"
    assert webhook.data.key.remote_jid == "5511900001111@s.whatsapp.net" and webhook.data.key.from_me is False
    assert webhook.data.key.id == "TESTMSG00000000000000000000AA01"
    assert webhook.text_content == "Ola, com quem eu falo?" and webhook.data.push_name == "Fulano"


def test_remetente_com_numero_nao_precisa_resolver():
    def nao_chamar(sessao, lid):
        raise AssertionError("nao devia resolver")

    ev = _evento(**{"from": "5511977776666@c.us", "chatId": "5511977776666@c.us", "isLidSender": False})
    _, envelope = wb.normalize_openwa(ev, instance="x", resolve_phone=nao_chamar)
    assert envelope["evolution"]["data"]["key"]["remoteJid"] == "5511977776666@s.whatsapp.net"


@pytest.mark.parametrize(
    ("alteracao", "motivo"),
    [
        ({"fromMe": True}, "from_me"),
        ({"isGroup": True}, "group_or_status"),
        ({"isStatusBroadcast": True}, "group_or_status"),
        ({"type": "image"}, "unsupported_type"),
        ({"body": "   "}, "no_text"),
        ({"id": ""}, "no_identity"),
    ],
)
def test_o_que_nao_e_texto_recebido_de_pessoa_e_ignorado(alteracao, motivo):
    with pytest.raises(wb.Ignored) as exc:
        wb.normalize_openwa(_evento(**alteracao), instance="x", resolve_phone=_resolve)
    assert exc.value.reason == motivo


def test_outros_eventos_e_lid_sem_resolucao_sao_ignorados():
    outro = _evento()
    outro["event"] = "session.status"
    with pytest.raises(wb.Ignored) as exc:
        wb.normalize_openwa(outro, instance="x", resolve_phone=_resolve)
    assert exc.value.reason == "unsupported_event"
    with pytest.raises(wb.Ignored) as exc:
        wb.normalize_openwa(_evento(), instance="x", resolve_phone=lambda sessao, lid: None)
    assert exc.value.reason == "lid_unresolved"


def test_assinatura_de_saida_passa_no_verificador_do_standby():
    corpo = b'{"v":1}'
    ts = str(int(time.time()))
    assert verify_control_signature(corpo, ts, wb.sign_out(SEG_SAIDA, corpo, ts), SEG_SAIDA)
    assert not verify_control_signature(corpo, ts, wb.sign_out(SEG_SAIDA, corpo, ts), "outro-segredo")


# --- fila e entrega ---------------------------------------------------------------------------


@pytest.fixture
def fila(tmp_path):
    return wb.Spool(str(tmp_path / "spool.sqlite")), wb.Config(out_url="https://standby.exemplo.test/webhook/connector", out_secret=SEG_SAIDA, data_dir=str(tmp_path))


def test_fila_nao_duplica_por_event_id(fila):
    spool, _ = fila
    assert spool.put("e1", "{}") is True
    assert spool.put("e1", "{}") is False
    assert spool.stats() == {"pending": 1}


def test_classificacao_das_respostas():
    assert [wb.classify(s) for s in (200, 202, None, 500, 503, 429, 408, 401, 403, 400, 404, 422)] == [
        "done", "done", "retry", "retry", "retry", "retry", "retry", "retry", "retry", "dead", "dead", "dead",
    ]


def test_entrega_com_sucesso_assina_e_marca_feito(fila):
    spool, cfg = fila
    spool.put("e1", json.dumps({"v": 1}))
    capturado = {}

    def post(url, body, headers, timeout):
        capturado.update(url=url, body=body, headers=headers, timeout=timeout)
        return 202

    assert wb.deliver_due(spool, cfg, post=post) == 1
    assert spool.stats() == {"done": 1}
    assert capturado["url"] == cfg.out_url and capturado["timeout"] == 90.0
    h = capturado["headers"]
    assert verify_control_signature(capturado["body"], h[wb.TIMESTAMP_HEADER], h[wb.SIGNATURE_HEADER], SEG_SAIDA)


def test_falha_temporaria_reagenda_com_backoff_e_depois_entrega(fila):
    spool, cfg = fila
    spool.put("e1", "{}", now=1000.0)
    assert wb.deliver_due(spool, cfg, now=1000.0, post=lambda *a: 503) == 1
    assert spool.stats() == {"pending": 1}
    assert wb.deliver_due(spool, cfg, now=1000.0 + wb.BACKOFF_S[0] - 1, post=lambda *a: 200) == 0  # ainda nao venceu
    assert wb.deliver_due(spool, cfg, now=1000.0 + wb.BACKOFF_S[0], post=lambda *a: 200) == 1
    assert spool.stats() == {"done": 1}


def test_erro_de_rede_tambem_tenta_de_novo(fila):
    spool, cfg = fila

    def cai(*a):
        raise OSError("sem rota")

    spool.put("e1", "{}", now=1000.0)
    wb.deliver_due(spool, cfg, now=1000.0, post=cai)
    assert spool.stats() == {"pending": 1}


def test_422_vai_direto_para_mortos_e_o_limite_de_tentativas_tambem(fila):
    spool, cfg = fila
    spool.put("e1", "{}", now=1000.0)
    wb.deliver_due(spool, cfg, now=1000.0, post=lambda *a: 422)
    assert spool.stats() == {"dead": 1}
    spool.put("e2", "{}", now=1000.0)
    agora = 1000.0
    for _ in range(wb.MAX_ATTEMPTS):
        agora += 10**6
        wb.deliver_due(spool, cfg, now=agora, post=lambda *a: 503)
    assert spool.stats() == {"dead": 2}


def test_modo_seco_grava_em_arquivo_e_nao_envia(fila, tmp_path):
    spool, cfg = fila
    cfg.out_url = ""
    spool.put("e1", '{"v":1}')

    def nao_enviar(*a):
        raise AssertionError("modo seco nao envia")

    wb.deliver_due(spool, cfg, post=nao_enviar)
    assert (tmp_path / "out.jsonl").read_text().strip() == '{"v":1}'
    assert spool.stats() == {"done": 1}


# --- ponta a ponta local: gateway -> conector -> Standby falso --------------------------------


def _servidor(handler_cls):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_ponta_a_ponta_com_lid_resolvido_pela_api_do_gateway(tmp_path):
    recebidos: list[tuple[bytes, dict]] = []

    class StandbyFalso(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            corpo = self.rfile.read(int(self.headers["Content-Length"]))
            recebidos.append((corpo, dict(self.headers)))
            self.send_response(202)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

    class GatewayFalso(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            assert self.headers["X-API-Key"] == "chave-do-gateway"
            assert self.path.endswith("/contacts/12345678901234@lid/phone")
            raw = json.dumps({"contactId": "12345678901234@lid", "phone": "5511900001111"}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    standby, gateway = _servidor(StandbyFalso), _servidor(GatewayFalso)
    cfg = wb.Config(
        openwa_webhook_secret=SEG_GATEWAY,
        openwa_api_url=f"http://127.0.0.1:{gateway.server_address[1]}",
        openwa_api_key="chave-do-gateway",
        sessions={SESSAO: "instancia-de-teste"},
        out_url=f"http://127.0.0.1:{standby.server_address[1]}/webhook/connector",
        out_secret=SEG_SAIDA,
        data_dir=str(tmp_path),
    )
    spool = wb.Spool(str(tmp_path / "spool.sqlite"))
    ponte = _servidor(wb.make_handler(cfg, spool, wb.OpenWAClient(cfg.openwa_api_url, cfg.openwa_api_key)))
    try:
        corpo = json.dumps(_evento()).encode()
        assinatura = "sha256=" + hmac.new(SEG_GATEWAY.encode(), corpo, hashlib.sha256).hexdigest()

        def envia(sig):
            req = urllib.request.Request(
                f"http://127.0.0.1:{ponte.server_address[1]}/in/openwa", data=corpo, method="POST",
                headers={"X-OpenWA-Signature": sig},
            )
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, {}

        assert envia("sha256=errada")[0] == 401
        assert envia(assinatura) == (200, {"status": "queued"})
        assert envia(assinatura) == (200, {"status": "duplicate"})  # reentrega do gateway nao duplica
        assert wb.deliver_due(spool, cfg) == 1
        assert len(recebidos) == 1
        corpo_saida, cab = recebidos[0]
        assert verify_control_signature(corpo_saida, cab[wb.TIMESTAMP_HEADER], cab[wb.SIGNATURE_HEADER], SEG_SAIDA)
        webhook = EvolutionWebhook.model_validate(ConnectorEnvelope.model_validate_json(corpo_saida).evolution)
        assert webhook.instance == "instancia-de-teste"
        assert webhook.data.key.remote_jid == "5511900001111@s.whatsapp.net"
        # sessao nao mapeada: ignorada sem erro
        outro = _evento()
        outro["sessionId"] = "sessao-desconhecida"
        raw = json.dumps(outro).encode()
        sig = "sha256=" + hmac.new(SEG_GATEWAY.encode(), raw, hashlib.sha256).hexdigest()
        req = urllib.request.Request(f"http://127.0.0.1:{ponte.server_address[1]}/in/openwa", data=raw, method="POST", headers={"X-OpenWA-Signature": sig})
        with urllib.request.urlopen(req, timeout=5) as r:
            assert json.loads(r.read()) == {"status": "ignored", "reason": "session_not_mapped"}
    finally:
        for s in (standby, gateway, ponte):
            s.shutdown()


def test_config_le_o_mapa_de_sessoes_e_o_modo_seco():
    cfg = wb.Config.from_env({"OPENWA_SESSION_MAP": "s1:inst-a, s2:inst-b", "BRIDGE_LISTEN": "127.0.0.1:9000", "OPENWA_WEBHOOK_SECRET": "x"})
    assert cfg.sessions == {"s1": "inst-a", "s2": "inst-b"} and cfg.listen_port == 9000 and cfg.out_url == ""


# --- saida: resposta de volta ao WhatsApp, so a destinos permitidos ----------------------------

PERMITIDO = "5511900001111"


class GatewayFalso:
    """Cliente do gateway com resultados roteirizados; guarda o que tentou enviar."""

    def __init__(self, *resultados):
        self.resultados = list(resultados) or [("sent", "WA-1", None)]
        self.enviados: list[tuple[str, str, str]] = []

    def send_text(self, session_id, chat_id, text):
        self.enviados.append((session_id, chat_id, text))
        return self.resultados.pop(0) if len(self.resultados) > 1 else self.resultados[0]


class StandbyFalso:
    """Registra as chamadas assinadas do conector e responde ao roteiro."""

    def __init__(self, itens, ack_code=200):
        self.itens, self.ack_code, self.chamadas = itens, ack_code, []

    def __call__(self, method, url, body, headers, timeout):
        self.chamadas.append((method, url, body, headers))
        assert verify_control_signature(body or b"", headers[wb.TIMESTAMP_HEADER], headers[wb.SIGNATURE_HEADER], SEG_SAIDA)
        if method == "GET":
            return 200, json.dumps({"items": self.itens}).encode()
        if isinstance(self.ack_code, Exception):
            raise self.ack_code
        return self.ack_code, b"{}"


def _item(id=41, instance="instancia-de-teste", phone=PERMITIDO, text="Oi! Sou a Lia."):
    return {"id": id, "instance": instance, "destination_jid": f"{phone}@s.whatsapp.net", "text": text}


def _relay(tmp_path, itens, *, gateway=None, ack_code=200, allow=(PERMITIDO,)):
    cfg = wb.Config(
        sessions={SESSAO: "instancia-de-teste"}, out_url="https://standby.exemplo.test/webhook/connector",
        out_secret=SEG_SAIDA, send_allowlist=list(allow), data_dir=str(tmp_path),
    )
    spool = wb.Spool(str(tmp_path / "spool.sqlite"))
    standby = StandbyFalso(itens, ack_code)
    gw = gateway or GatewayFalso()
    relay = wb.Relay(cfg, spool, gw, request=standby)
    relay.arm()
    return relay, spool, gw, standby


def test_relay_so_funciona_com_lista_de_destinos_e_cliente(tmp_path):
    relay, *_ = _relay(tmp_path, [], allow=())
    assert not relay.enabled and relay.step() == 0
    sem_cliente = wb.Relay(relay.cfg, relay.spool, None)
    assert not sem_cliente.enabled


def test_relay_nao_consulta_fora_da_janela(tmp_path):
    relay, _, _, standby = _relay(tmp_path, [_item()])
    relay.poll_until = 0.0
    assert relay.step() == 0 and standby.chamadas == []


def test_entrega_na_janela_envia_ao_destino_permitido_e_confirma(tmp_path):
    relay, spool, gw, standby = _relay(tmp_path, [_item()])
    assert relay.step() == 1
    assert gw.enviados == [(SESSAO, f"{PERMITIDO}@c.us", "Oi! Sou a Lia.")]
    metodos = [(m, u.rsplit("/v1/", 1)[1]) for m, u, _, _ in standby.chamadas]
    assert metodos == [("GET", "connector/outbox"), ("POST", "connector/outbox/41/ack")]
    ack = json.loads(standby.chamadas[1][2])
    assert ack == {"status": "sent", "provider_message_id": "WA-1", "error": None}
    assert spool.relayed_get(41) == ("sent", "WA-1", None)


def test_destino_fora_da_lista_nao_e_enviado_e_o_standby_fica_sabendo(tmp_path):
    relay, spool, gw, standby = _relay(tmp_path, [_item(phone="5511999990000")])
    relay.step()
    assert gw.enviados == []
    assert json.loads(standby.chamadas[1][2]) == {"status": "failed", "provider_message_id": None, "error": "destino_fora_da_lista"}


def test_instancia_nao_mapeada_e_texto_vazio_nao_sao_enviados(tmp_path):
    relay, _, gw, standby = _relay(tmp_path, [_item(id=1, instance="outra"), _item(id=2, text="   ")])
    relay.step()
    assert gw.enviados == []
    erros = [json.loads(c[2])["error"] for c in standby.chamadas if c[0] == "POST"]
    assert erros == ["instancia_nao_mapeada", "sem_texto"]


def test_falha_de_rede_no_envio_nao_registra_e_tenta_no_proximo_ciclo(tmp_path):
    gw = GatewayFalso(("retry", None, "URLError"), ("sent", "WA-9", None))
    relay, spool, _, standby = _relay(tmp_path, [_item()], gateway=gw)
    relay.step()
    assert spool.relayed_get(41) is None and [c[0] for c in standby.chamadas] == ["GET"]  # sem ack
    relay.step()
    assert spool.relayed_get(41) == ("sent", "WA-9", None) and len(gw.enviados) == 2


def test_ack_que_falha_nao_reenvia_a_mensagem(tmp_path):
    relay, spool, gw, standby = _relay(tmp_path, [_item()], ack_code=OSError("rede"))
    relay.step()
    assert len(gw.enviados) == 1 and spool.relayed_get(41) == ("sent", "WA-1", None)
    standby.ack_code = 200
    relay.step()
    assert len(gw.enviados) == 1  # so repete o ack, nunca o envio
    assert [c[0] for c in standby.chamadas].count("POST") == 2


def test_ack_404_encerra_sem_reenviar(tmp_path):
    relay, _, gw, standby = _relay(tmp_path, [_item()], ack_code=404)
    relay.step()
    relay.step()
    assert len(gw.enviados) == 1


def test_entrega_de_mensagem_arma_a_janela_de_consulta(tmp_path):
    relay, spool, _, _ = _relay(tmp_path, [])
    relay.poll_until = 0.0
    spool.put("e1", "{}")
    wb.deliver_due(spool, relay.cfg, post=lambda *a: 202, on_delivered=relay.arm)
    assert relay.poll_until > time.time() + relay.cfg.relay_window_s - 5


def _gateway_http(codigo, corpo):
    visto = {}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            visto["corpo"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            visto["chave"] = self.headers["X-API-Key"]
            raw = json.dumps(corpo).encode()
            self.send_response(codigo)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    srv = _servidor(H)
    return srv, visto


def test_cliente_do_gateway_envia_texto_e_classifica_as_falhas():
    srv, visto = _gateway_http(201, {"messageId": "WA-77"})
    try:
        c = wb.OpenWAClient(f"http://127.0.0.1:{srv.server_address[1]}", "chave-do-gateway")
        assert c.send_text(SESSAO, f"{PERMITIDO}@c.us", "Oi") == ("sent", "WA-77", None)
        assert visto["corpo"] == {"chatId": f"{PERMITIDO}@c.us", "text": "Oi"} and visto["chave"] == "chave-do-gateway"
    finally:
        srv.shutdown()
    for codigo, esperado in ((400, "failed"), (500, "retry")):
        srv, _ = _gateway_http(codigo, {"message": "x"})
        try:
            c = wb.OpenWAClient(f"http://127.0.0.1:{srv.server_address[1]}", "k")
            assert c.send_text(SESSAO, "x@c.us", "Oi")[0] == esperado
        finally:
            srv.shutdown()
    fechado = wb.OpenWAClient("http://127.0.0.1:1", "k", timeout=0.2)
    assert fechado.send_text(SESSAO, "x@c.us", "Oi")[0] == "retry"


def test_config_le_a_lista_de_destinos_e_a_base_da_saida():
    cfg = wb.Config.from_env({"SEND_ALLOWLIST": "+55 (11) 90000-1111, 5511911112222", "OUT_URL": "https://standby.exemplo.test/webhook/connector", "OPENWA_SESSION_MAP": "s1:inst"})
    assert cfg.send_allowlist == ["5511900001111", "5511911112222"]
    assert cfg.relay_base == "https://standby.exemplo.test" and cfg.instance_sessions == {"inst": "s1"}
    assert wb.Config.from_env({}).send_allowlist == [] and wb.Config.from_env({}).relay_base == ""
