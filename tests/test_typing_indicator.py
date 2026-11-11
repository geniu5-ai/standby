"""Indicador 'digitando...' do wa-bridge: começa depois do pré-atraso, renova, para quando a resposta sai."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path



def _carrega():
    caminho = Path(__file__).resolve().parent.parent / "connectors" / "wa-bridge" / "wa_bridge.py"
    spec = importlib.util.spec_from_file_location("wa_bridge", caminho)
    modulo = importlib.util.module_from_spec(spec)
    sys.modules["wa_bridge"] = modulo
    spec.loader.exec_module(modulo)
    return modulo


wb = _carrega()
SESSAO = "00000000-0000-4000-8000-000000000001"
DESTINO = "5511900000000"
CHAT = f"{DESTINO}@c.us"


class Registro:
    def __init__(self) -> None:
        self.chamadas: list[tuple[str, str, str]] = []

    def __call__(self, sid: str, chat: str, state: str) -> bool:
        self.chamadas.append((sid, chat, state))
        return True


def _cfg(**extra):
    base = dict(sessions={SESSAO: "mariaeduarda-ficticia"}, send_allowlist=[DESTINO],
                typing_pre_delay_s=5.0, typing_refresh_s=4.0, typing_max_s=60.0)
    base.update(extra)
    return wb.Config(**base)


def test_nao_mostra_antes_do_pre_atraso():
    reg = Registro()
    t = wb.Typist(_cfg(), None, sender=reg)
    t.arm(SESSAO, CHAT, now=100.0)
    assert t.step(now=104.9) == 0 and reg.chamadas == []


def test_comeca_no_pre_atraso_e_renova():
    reg = Registro()
    t = wb.Typist(_cfg(), None, sender=reg)
    t.arm(SESSAO, CHAT, now=100.0)
    t.step(now=105.0)
    t.step(now=107.0)  # ainda dentro da renovacao de 4 s
    t.step(now=109.1)
    assert [c[2] for c in reg.chamadas] == ["typing", "typing"]
    assert reg.chamadas[0][:2] == (SESSAO, CHAT)


def test_para_com_paused_quando_a_resposta_sai():
    reg = Registro()
    t = wb.Typist(_cfg(), None, sender=reg)
    t.arm(SESSAO, CHAT, now=100.0)
    t.step(now=106.0)
    t.stop(SESSAO, CHAT)
    assert reg.chamadas[-1] == (SESSAO, CHAT, "paused")
    t.step(now=120.0)
    assert reg.chamadas[-1][2] == "paused" and len(reg.chamadas) == 2  # nada mais depois de parar


def test_parar_antes_de_comecar_nao_manda_paused():
    reg = Registro()
    t = wb.Typist(_cfg(), None, sender=reg)
    t.arm(SESSAO, CHAT, now=100.0)
    t.stop(SESSAO, CHAT)
    assert reg.chamadas == []


def test_prazo_maximo_apaga_o_indicador():
    reg = Registro()
    t = wb.Typist(_cfg(typing_max_s=20.0), None, sender=reg)
    t.arm(SESSAO, CHAT, now=100.0)
    t.step(now=106.0)
    t.step(now=121.0)
    assert reg.chamadas[-1][2] == "paused"


def test_destino_fora_da_lista_nunca_mostra_digitando():
    reg = Registro()
    t = wb.Typist(_cfg(), None, sender=reg)
    t.arm(SESSAO, "5511911111111@c.us", now=100.0)
    t.step(now=110.0)
    assert reg.chamadas == []


def test_desligado_por_env_nao_faz_nada(monkeypatch):
    reg = Registro()
    t = wb.Typist(_cfg(typing_enabled=False), None, sender=reg)
    t.arm(SESSAO, CHAT, now=100.0)
    t.step(now=110.0)
    assert reg.chamadas == []


def test_env_liga_e_desliga(monkeypatch):
    base = {"OPENWA_SESSION_MAP": f"{SESSAO}:inst", "SEND_ALLOWLIST": DESTINO}
    assert wb.Config.from_env({**base}).typing_enabled is True
    assert wb.Config.from_env({**base, "TYPING_ENABLED": "0"}).typing_enabled is False
    cfg = wb.Config.from_env({**base, "TYPING_PRE_DELAY_S": "3", "TYPING_MAX_S": "30"})
    assert (cfg.typing_pre_delay_s, cfg.typing_max_s) == (3.0, 30.0)


def test_falha_de_rede_nao_derruba_nada():
    cliente = wb.OpenWAClient("http://127.0.0.1:1", "chave")
    assert cliente.send_state(SESSAO, CHAT, "typing") is False


def test_entrega_da_resposta_apaga_o_digitando(tmp_path):
    reg = Registro()
    cfg = _cfg(out_url="https://standby.example", out_secret="segredo-de-saida-0123456789", relay_window_s=300.0)
    spool = wb.Spool(str(tmp_path / "spool.sqlite"))

    class Cliente:
        def send_text(self, sid, chat, text):
            return ("sent", "MSG-1", None)

    typist = wb.Typist(cfg, None, sender=reg)
    typist.arm(SESSAO, CHAT, now=100.0)
    typist.step(now=106.0)  # ja esta digitando

    def pedido(metodo, url, corpo, cabecalhos, timeout):
        return 200, b"{}"

    relay = wb.Relay(cfg, spool, Cliente(), request=pedido, typist=typist)
    relay._deliver({"id": 41, "instance": "mariaeduarda-ficticia", "destination_jid": f"{DESTINO}@s.whatsapp.net",
                    "text": "Oi!"}, now=107.0)
    assert reg.chamadas[-1] == (SESSAO, CHAT, "paused")
