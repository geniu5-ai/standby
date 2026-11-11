"""Envelope do conector externo (OpenWA, waxum...) que entrega mensagens ao Standby.

O conector (``connectors/wa-bridge/``) roda ao lado do gateway de WhatsApp, valida a assinatura
do gateway, resolve identidades (LID -> telefone) e normaliza o evento para o formato da
Evolution. Aqui so se define o envelope assinado que ele manda para ``/webhook/connector``:

    {"v": 1, "source": "openwa", "event_id": "...", "evolution": {<EvolutionWebhook>}}

A autenticacao usa um segredo PROPRIO (``CONNECTOR_HMAC_SECRET``), nunca o ``HMAC_SECRET`` da
Evolution: quem tem um nao autentica no outro. O corpo ``evolution`` e validado depois pelo
mesmo ``EvolutionWebhook`` da rota da Evolution, e o tenant sai so do registro confiavel.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# Cabecalhos do envelope (mesmo esquema ``timestamp.corpo`` dos controles, outro segredo).
TIMESTAMP_HEADER = "X-Connector-Timestamp"
SIGNATURE_HEADER = "X-Connector-Signature"


class ConnectorEnvelope(BaseModel):
    """Envelope assinado: a unica forma que o conector tem de falar com o Standby."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    v: Literal[1]
    source: Literal["openwa", "waxum"]
    event_id: str = Field(min_length=1, max_length=256)
    evolution: dict[str, Any]
