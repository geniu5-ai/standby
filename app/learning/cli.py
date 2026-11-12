"""CLI do auto-aprendizado. Feita para o humano e para o Prompter (saída em JSON, uma resposta por comando).

    python -m app.learning.cli status
    python -m app.learning.cli insights [--tenant T] [--status proposed|approved|rejected|promoted]
    python -m app.learning.cli approve ID --by NOME      # so humano aprova; aprovado vira licao na proxima hora
    python -m app.learning.cli reject  ID --by NOME
    python -m app.learning.cli goldens --tenant T [--limit 30] [--horas 168] [--out arquivo.json]
    python -m app.learning.cli reflect --tenant T [--gravar]   # rodada manual; sem --gravar e dry-run
    python -m app.learning.cli enfileira --tenant T            # pede a rodada pela fila (precisa LEARNING_ENABLED)

Regra do Prompter: ele PROPÕE e LÊ. ``approve``/``reject`` exigem ``--by`` com o nome de um humano e
nao existem para o agente decidir por conta propria (ver docs/runbooks/RUNBOOK-LEARNING.md).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from app.learning import repo
from app.settings import get_settings


def _saida(dados: Any) -> int:
    print(json.dumps(dados, ensure_ascii=False, default=str, indent=2))
    return 0


def cmd_status(_: argparse.Namespace) -> int:
    s = get_settings()
    return _saida(
        {
            "config": {
                "enabled": s.LEARNING_ENABLED, "dry_run": s.LEARNING_DRY_RUN, "tenants": s.LEARNING_TENANTS or "(nenhum)",
                "janela_horas": s.LEARNING_WINDOW_HOURS, "min_outcomes": s.LEARNING_MIN_OUTCOMES,
                "juiz_ligado": s.TURN_JUDGE_ENABLED, "juiz_amostra": s.TURN_JUDGE_SAMPLE_RATE,
            },
            "banco": repo.resumo(),
        }
    )


def cmd_insights(a: argparse.Namespace) -> int:
    return _saida(repo.listar_insights(a.tenant, a.status, a.limit))


def _decidir(a: argparse.Namespace, status: str) -> int:
    if not a.by.strip():
        print("--by exige o nome de quem decide", file=sys.stderr)
        return 2
    ok = repo.decidir_insight(a.id, status, a.by.strip())
    return _saida({"id": a.id, "status": status if ok else "inalterado (nao estava proposed)"})


def cmd_goldens(a: argparse.Namespace) -> int:
    """Turnos de nota baixa ou handoff viram candidatos a golden do DeepEval. O esperado fica vazio de proposito."""
    outcomes = repo.coletar_outcomes(a.tenant, a.horas, a.limit * 3)
    ruins = [o for o in outcomes if (o.nota_juiz is not None and o.nota_juiz < 0.7) or o.desfecho == "handoff"][: a.limit]
    goldens = [
        {
            "input": o.texto_cliente,
            "resposta_atual": o.resposta,
            "desfecho": o.desfecho,
            "nota_juiz": o.nota_juiz,
            "turno_id": o.turno_id,
            "expected_behavior": "",  # humano ou Prompter preenche; golden sem isto nao entra na regressao
        }
        for o in ruins
    ]
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            json.dump(goldens, fh, ensure_ascii=False, indent=2)
    return _saida({"candidatos": len(goldens), "arquivo": a.out, "amostra": goldens[:3] if not a.out else None})


def cmd_reflect(a: argparse.Namespace) -> int:
    from app.learning.jobs import ArmazemPostgres, _llm
    from app.learning.runner import run_reflection

    s = get_settings()
    outcomes = repo.coletar_outcomes(a.tenant, s.LEARNING_WINDOW_HOURS, s.LEARNING_MAX_OUTCOMES)
    if len(outcomes) < s.LEARNING_MIN_OUTCOMES:
        return _saida({"status": "skipped", "outcomes": len(outcomes), "minimo": s.LEARNING_MIN_OUTCOMES})
    run_id = repo.iniciar_run(a.tenant, None, s.LEARNING_WINDOW_HOURS, not a.gravar, s.LEARNING_MODEL)

    async def _rodar():
        return await run_reflection(a.tenant, outcomes, _llm(s), ArmazemPostgres(run_id), dry_run=not a.gravar)

    try:
        rel = asyncio.run(_rodar())
    except Exception as exc:  # noqa: BLE001
        repo.finalizar_run(run_id, "failed", len(outcomes), 0, 0, type(exc).__name__)
        raise
    repo.finalizar_run(run_id, "ok", len(outcomes), len(rel.insights), rel.gravados, None)
    return _saida({"run_id": run_id, "dry_run": not a.gravar, "outcomes": len(outcomes), "gravados": rel.gravados,
                   "insights": [i.model_dump() for i in rel.insights]})


def cmd_enfileira(a: argparse.Namespace) -> int:
    from app.jobs import defer_once
    from app.learning.jobs import learning_reflect
    from app.learning.retry import LEARN_PRIORITY

    if not get_settings().LEARNING_ENABLED:
        print("LEARNING_ENABLED=false: a rodada nao faria nada", file=sys.stderr)
        return 2
    criado = defer_once(learning_reflect, lock=f"learning:{a.tenant}", queueing_lock=f"learning:{a.tenant}",
                        priority=LEARN_PRIORITY, tenant_id=a.tenant)
    return _saida({"enfileirado": criado})


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m app.learning.cli", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status").set_defaults(fn=cmd_status)

    ins = sub.add_parser("insights")
    ins.set_defaults(fn=cmd_insights)
    ins.add_argument("--tenant")
    ins.add_argument("--status", choices=["proposed", "approved", "rejected", "promoted"])
    ins.add_argument("--limit", type=int, default=50)

    for nome, status in (("approve", "approved"), ("reject", "rejected")):
        dec = sub.add_parser(nome)
        dec.set_defaults(fn=lambda a, st=status: _decidir(a, st))
        dec.add_argument("id", type=int)
        dec.add_argument("--by", required=True)

    gol = sub.add_parser("goldens")
    gol.set_defaults(fn=cmd_goldens)
    gol.add_argument("--tenant", required=True)
    gol.add_argument("--limit", type=int, default=30)
    gol.add_argument("--horas", type=int, default=168)
    gol.add_argument("--out")

    ref = sub.add_parser("reflect")
    ref.set_defaults(fn=cmd_reflect)
    ref.add_argument("--tenant", required=True)
    ref.add_argument("--gravar", action="store_true")

    enf = sub.add_parser("enfileira")
    enf.set_defaults(fn=cmd_enfileira)
    enf.add_argument("--tenant", required=True)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
