"""Liga e desliga o schedule ``standby-wake`` do QStash conforme a saude da VPS 002.

Com a 002 saudavel o Standby nao precisa estar acordado: o wake horario fica **pausado** e o
Render dorme. Quando a queda se confirma (``app/failover.py``), o schedule e **retomado** e o
Standby passa a ser acordado e mantido de pe pelo QStash. Quando a 002 volta (o wake seguinte a
sonda e ela responde), o schedule pausa de novo. Desligado por padrao (``WAKE_AUTO``): sem a flag, o
schedule segue como esta. Nunca levanta: falha aqui so vira log, o wake e o alerta continuam.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger("app.wake_schedule")


async def _acao(settings: Any, acao: str, *, cliente: httpx.AsyncClient | None = None) -> bool:
    """POST /v2/schedules/{id}/{pause|resume}. False se desligado, sem credencial ou se falhar."""
    token = settings.QSTASH_TOKEN
    if not settings.WAKE_AUTO or token is None or not settings.QSTASH_URL:
        return False
    url = f"{settings.QSTASH_URL.rstrip('/')}/v2/schedules/{settings.WAKE_SCHEDULE_ID}/{acao}"
    proprio = cliente is None
    cliente = cliente or httpx.AsyncClient(timeout=10.0)
    try:
        resposta = await cliente.post(url, headers={"Authorization": f"Bearer {token.get_secret_value()}"})
        if not resposta.is_success:
            log.warning("wake_schedule: %s respondeu HTTP %s", acao, resposta.status_code)
        return resposta.is_success
    except httpx.HTTPError as exc:
        log.warning("wake_schedule: %s falhou (%s)", acao, type(exc).__name__)
        return False
    finally:
        if proprio:
            await cliente.aclose()


async def retomar(settings: Any, *, cliente: httpx.AsyncClient | None = None) -> bool:
    return await _acao(settings, "resume", cliente=cliente)


async def pausar(settings: Any, *, cliente: httpx.AsyncClient | None = None) -> bool:
    return await _acao(settings, "pause", cliente=cliente)
