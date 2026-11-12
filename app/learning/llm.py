"""Cliente LLM assíncrono do auto-aprendizado (OmniRoute, mesmo gateway do resto do Next).

Duas classes de erro, porque o retry do Procrastinate decide por elas (ver ``retry.py``):
- ``LLMTransitorio``: 429, 5xx, timeout, queda de rede. Vale tentar de novo, com espera.
- ``LLMDefinitivo``: 4xx de configuração (chave, modelo, corpo). Repetir só gasta fila.
Mensagens de erro nunca carregam o corpo da resposta (pode conter trecho de conversa).
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_STATUS_TRANSITORIOS = {408, 409, 425, 429, 500, 502, 503, 504, 520, 522, 524}


class LLMErro(Exception):
    """Base. Só o tipo e o status entram na mensagem."""


class LLMTransitorio(LLMErro):
    pass


class LLMDefinitivo(LLMErro):
    pass


class LLMOmniRoute:
    """Implementa o protocolo ``app.learning.reflect.LLM``."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 90.0,
        max_tokens: int = 4096,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "x-omniroute-compression": "off",
        }
        self._model = model
        self._max_tokens = max_tokens
        self._timeout = timeout
        self._client = client
        self.ultimo_finish_reason: str | None = None

    async def completar(self, prompt: str) -> str:
        corpo: dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "max_tokens": self._max_tokens,
            # O gateway devolve SSE (text/event-stream) para o gpt-pool se isto faltar. Medido em 05/nov/2026.
            "stream": False,
        }
        try:
            if self._client is not None:
                resposta = await self._client.post(self._url, headers=self._headers, json=corpo, timeout=self._timeout)
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resposta = await client.post(self._url, headers=self._headers, json=corpo)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise LLMTransitorio(f"rede: {type(exc).__name__}") from None
        if resposta.status_code in _STATUS_TRANSITORIOS:
            raise LLMTransitorio(f"http {resposta.status_code}")
        if resposta.status_code != 200:
            raise LLMDefinitivo(f"http {resposta.status_code}")
        try:
            if "event-stream" in resposta.headers.get("content-type", ""):
                # Defesa: o gateway ignorou stream=False. Junta os pedacos em vez de falhar a rodada.
                texto, self.ultimo_finish_reason = _texto_de_sse(resposta.text)
            else:
                escolha = resposta.json()["choices"][0]
                self.ultimo_finish_reason = escolha.get("finish_reason")
                texto = (escolha.get("message") or {}).get("content") or ""
        except (ValueError, KeyError, IndexError, TypeError):
            raise LLMTransitorio("resposta fora do formato") from None
        if self.ultimo_finish_reason == "length":
            # O array JSON sai truncado: o parse devolve [] e o relatório registra o motivo.
            logger.warning("learning llm: resposta truncada (finish_reason=length)")
        return texto


def _texto_de_sse(corpo: str) -> tuple[str, str | None]:
    """Junta ``choices[0].delta.content`` (ou ``message.content``) de um corpo ``text/event-stream``."""
    pedacos: list[str] = []
    finish: str | None = None
    for linha in corpo.splitlines():
        if not linha.startswith("data:"):
            continue
        carga = linha[5:].strip()
        if not carga or carga == "[DONE]":
            continue
        escolha = json.loads(carga)["choices"][0]
        parte = escolha.get("delta") or escolha.get("message") or {}
        pedacos.append(parte.get("content") or "")
        finish = escolha.get("finish_reason") or finish
    if not pedacos:
        raise ValueError("sse sem conteudo")
    return "".join(pedacos), finish
