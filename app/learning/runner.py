"""Orquestra REFLECT: coleta -> reflexão -> gravação. Padrão: dry-run (não grava, só relata).

Não está agendado em ``app/jobs.py``. Para ligar, é preciso: (1) tabela aprovada, (2) armazém em
Postgres, (3) decisão humana sobre a cadência. Ver docs/specs/JUIZ-E-SELF-LEARNING.md.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.learning.models import Insight, Outcome
from app.learning.reflect import LLM, reflect
from app.learning.store import Armazem, ArmazemEmMemoria

logger = logging.getLogger(__name__)


@dataclass
class Relatorio:
    tenant_id: str
    outcomes: int
    insights: list[Insight] = field(default_factory=list)
    gravados: int = 0
    dry_run: bool = True


async def run_reflection(
    tenant_id: str,
    outcomes: Sequence[Outcome],
    llm: LLM,
    armazem: Armazem | None = None,
    *,
    dry_run: bool = True,
) -> Relatorio:
    """Roda um lote de um tenant. Só grava se ``dry_run=False`` **e** houver armazém."""
    if any(o.tenant_id != tenant_id for o in outcomes):
        raise ValueError("outcomes de mais de um tenant no mesmo lote")
    insights = await reflect(outcomes, llm)
    rel = Relatorio(tenant_id=tenant_id, outcomes=len(outcomes), insights=insights, dry_run=dry_run)
    if dry_run or armazem is None:
        logger.info("reflect dry-run tenant=%s outcomes=%d insights=%d", tenant_id, len(outcomes), len(insights))
        return rel
    rel.gravados = await armazem.gravar(tenant_id, insights)
    return rel


__all__ = ["Relatorio", "ArmazemEmMemoria", "run_reflection"]
