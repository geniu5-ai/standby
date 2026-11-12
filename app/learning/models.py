"""Contratos do auto-aprendizado. Tudo imutável e estrito (extra=forbid), como o resto do Next."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

_CONTRATO = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)

Camada = Literal["mundo", "experiencia", "modelo_mental"]
Desfecho = Literal["enviado", "handoff", "bloqueado", "ganho", "perdido", "trivial"]


class Outcome(BaseModel):
    """Um turno já resolvido, reduzido ao necessário para aprender.

    ``texto_cliente`` e ``resposta`` chegam já sem telefone (o coletor aplica ``mask_phone``).
    Nenhum campo guarda prompt de sistema do tenant.
    """

    model_config = _CONTRATO

    turno_id: str = Field(min_length=1, max_length=120)
    tenant_id: str = Field(min_length=1, max_length=120)
    desfecho: Desfecho
    nota_juiz: float | None = Field(default=None, ge=0.0, le=1.0)
    texto_cliente: str = Field(max_length=2_000)
    resposta: str = Field(max_length=2_048)
    motivo_handoff: str | None = Field(default=None, max_length=200)


class Insight(BaseModel):
    """Um modelo mental ou lição destilada de vários outcomes. Sai do LLM, passa pela validação."""

    model_config = _CONTRATO

    titulo: str = Field(min_length=3, max_length=80)
    corpo: str = Field(min_length=10, max_length=800)
    camada: Camada
    tags: list[str] = Field(default_factory=list, max_length=4)
    evidencias: list[str] = Field(default_factory=list, max_length=20)
