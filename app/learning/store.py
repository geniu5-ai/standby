"""Gravação dos insights. Só existe a versão em memória; a gravação em Postgres fica para depois.

Nada aqui escreve no banco de produção. O protocolo permite trocar a implementação quando houver
tabela aprovada (migração própria, revisada).
"""

from __future__ import annotations

from typing import Protocol

from app.learning.models import Insight


class Armazem(Protocol):
    async def gravar(self, tenant_id: str, insights: list[Insight]) -> int: ...


class ArmazemEmMemoria:
    """Guarda por tenant, para teste e para dry-run com relatório."""

    def __init__(self) -> None:
        self.por_tenant: dict[str, list[Insight]] = {}

    async def gravar(self, tenant_id: str, insights: list[Insight]) -> int:
        existentes = {i.titulo for i in self.por_tenant.get(tenant_id, [])}
        novos = [i for i in insights if i.titulo not in existentes]
        self.por_tenant.setdefault(tenant_id, []).extend(novos)
        return len(novos)
