"""Vigia da VPS 002 em MODO ALERTA: confirma a queda e avisa; nunca assume o atendimento.

O QStash sonda o ``/health`` da VPS 002 de tempos em tempos. Se a sonda falhar em todas as
retentativas, ele chama ``POST /v1/control/qstash-failover`` (``failure_callback``). Esta rota
acorda o Standby (a propria chamada ja o acorda), **confere a 002 de novo por conta propria**
(uma falha do tunel ou da borda nao e a VPS caida) e, so se a queda se confirmar, registra e
manda o aviso. Nao altera ``DELIVERY_MODE``, nao liga o ``live`` e nao escreve no banco: assumir
o atendimento e a trava 1 do ``AGENTS.md`` e exige criterios escritos pelo chairman.

Criterio de saude: o mesmo do watchdog do Next e do ``monitor.yml``: HTTP 200 e ``ok`` no corpo.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger("app.failover")

# Aviso repetido enquanto a 002 segue fora: o QStash chama de novo a cada ciclo. O controle e
# por processo (melhor esforco): se o Render reiniciar, o aviso pode repetir uma vez.
_ultimo_aviso: float | None = None


@dataclass(frozen=True)
class Sonda:
    ok: bool
    tentativas: int
    ultimo_status: int | None
    erro: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "tentativas": self.tentativas,
            "ultimo_status": self.ultimo_status,
            "erro": self.erro,
        }


async def sondar(
    url: str,
    *,
    tentativas: int = 3,
    espera_s: float = 5.0,
    timeout_s: float = 10.0,
    cliente: httpx.AsyncClient | None = None,
) -> Sonda:
    """Pergunta a ``url`` se esta saudavel: 200 e ``ok`` no corpo, em ate ``tentativas`` tentativas."""
    status: int | None = None
    erro: str | None = None
    proprio = cliente is None
    cliente = cliente or httpx.AsyncClient(timeout=timeout_s, follow_redirects=False)
    try:
        for i in range(1, tentativas + 1):
            try:
                resposta = await cliente.get(url)
                status, erro = resposta.status_code, None
                if resposta.status_code == 200 and "ok" in resposta.text:
                    return Sonda(True, i, status, None)
            except httpx.HTTPError as exc:
                status, erro = None, type(exc).__name__
            if i < tentativas:
                await asyncio.sleep(espera_s)
    finally:
        if proprio:
            await cliente.aclose()
    return Sonda(False, tentativas, status, erro)


def deve_avisar(agora: float, cooldown_s: int) -> bool:
    """True se ja passou o ``cooldown_s`` desde o ultimo aviso; marca o aviso quando for True."""
    global _ultimo_aviso
    if _ultimo_aviso is not None and agora - _ultimo_aviso < cooldown_s:
        return False
    _ultimo_aviso = agora
    return True


def zerar_cooldown() -> None:
    global _ultimo_aviso
    _ultimo_aviso = None


async def avisar(url: str, texto: str, detalhes: dict[str, Any], *, cliente: httpx.AsyncClient | None = None) -> bool:
    """POST generico ``{"text", "details"}`` para o webhook de aviso. False se nao chegou."""
    proprio = cliente is None
    cliente = cliente or httpx.AsyncClient(timeout=10.0)
    try:
        resposta = await cliente.post(url, json={"text": texto, "details": detalhes})
        return resposta.is_success
    except httpx.HTTPError as exc:
        log.warning("failover: webhook de aviso falhou (%s)", type(exc).__name__)
        return False
    finally:
        if proprio:
            await cliente.aclose()


def monitor_do_incidente(payload: Any) -> tuple[str | None, str | None]:
    """(id do monitor, nome do incidente) do payload JSON:API de incidente do Better Stack."""
    if not isinstance(payload, dict):
        return None, None
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    relacao = ((data.get("relationships") or {}).get("monitor") or {}).get("data") or {}
    nome = (data.get("attributes") or {}).get("name")
    monitor = relacao.get("id")
    return (str(monitor) if monitor is not None else None, nome if isinstance(nome, str) else None)


async def tratar_callback(
    *,
    probe_url: str,
    alert_url: str | None,
    cooldown_s: int,
    callback: dict[str, Any],
) -> dict[str, Any]:
    """Miolo da rota: confirma a queda e avisa. Devolve o que decidiu (nunca levanta por causa do aviso)."""
    sonda = await sondar(probe_url)
    if sonda.ok:
        log.info("failover: alarme falso, a VPS 002 respondeu (%s)", sonda.as_dict())
        return {"status": "false_alarm", "sonda": sonda.as_dict(), "mode": "alert"}

    detalhes = {
        "sonda": sonda.as_dict(),
        "qstash_status": callback.get("status"),
        "qstash_retried": callback.get("retried"),
        "schedule": callback.get("scheduleId"),
    }
    log.error("failover: VPS 002 fora do ar (modo alerta, nada foi assumido): %s", detalhes)
    avisou = False
    if alert_url and deve_avisar(time.time(), cooldown_s):
        avisou = await avisar(
            alert_url,
            "Genius: a VPS 002 nao responde ao /health. O Standby acordou em modo alerta (record_only).",
            detalhes,
        )
    return {"status": "alert", "mode": "alert", "sonda": sonda.as_dict(), "avisou": avisou}
