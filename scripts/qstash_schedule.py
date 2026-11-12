#!/usr/bin/env python3
"""Schedules do QStash do Standby: o wake do worker e o vigia da VPS 002.

Idempotente: o ``schedule_id`` fixo faz o ``apply`` sobrescrever em vez de duplicar. Sem
``--aplicar`` so mostra o que faria (nenhuma escrita no QStash).

    python scripts/qstash_schedule.py show
    python scripts/qstash_schedule.py apply --schedule watchdog              # simulacao
    python scripts/qstash_schedule.py apply --schedule watchdog --aplicar    # cria/atualiza
    python scripts/qstash_schedule.py pause|resume|delete --schedule watchdog

Schedules:
  wake      ``standby-wake``     POST no Standby (``/v1/control/qstash-wake``), a cada 20 min.
                                 Acorda o Render sempre: NAO aplicar enquanto o Standby deve
                                 dormir com a VPS 002 saudavel (ver ``docs/WAKE.md``).
  watchdog  ``vps002-watchdog``  GET no ``/health`` da VPS 002, a cada 5 min. Se falhar em todas as
                                 retentativas, o QStash chama ``/v1/control/qstash-failover`` no
                                 Standby (modo alerta). O Standby so acorda quando a 002 cai.

Ambiente: ``QSTASH_TOKEN`` e, fora da regiao padrao, ``QSTASH_URL`` (ex.:
``https://qstash-us-east-1.upstash.io``). Nunca imprime o token. Destinos: ``--url`` /
``QSTASH_WAKE_URL`` (wake); ``--url`` / ``FAILOVER_PROBE_URL`` e ``--callback`` /
``QSTASH_FAILOVER_URL`` (watchdog).

Custo: cada disparo e uma mensagem; o plano free do QStash tem 1000 por dia (retentativas e
callbacks contam). ``wake`` a cada 20 min gasta 72 por dia; ``watchdog`` a cada 5 min gasta 288.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

SCHEDULES = {
    "wake": {
        "id": "standby-wake",
        "cron": "*/20 * * * *",
        "method": "POST",
        "retries": 3,
        "timeout": 120,  # cobre o cold start (~1 min) do Render free
        "url_env": "QSTASH_WAKE_URL",
    },
    "watchdog": {
        "id": "vps002-watchdog",
        "cron": "*/5 * * * *",
        "method": "GET",
        "retries": 2,
        "retry_delay": "30000",  # ms: 2 retentativas a cada 30 s, falha confirmada em ~1 min
        "timeout": 20,
        "url_env": "FAILOVER_PROBE_URL",
        "callback_env": "QSTASH_FAILOVER_URL",
    },
}


def _limpa(valor: str) -> str:
    return valor.strip().strip("\"'")


def _cliente():
    from qstash import QStash

    token = _limpa(os.environ.get("QSTASH_TOKEN", ""))
    if not token:
        raise SystemExit("QSTASH_TOKEN ausente no ambiente")
    base = _limpa(os.environ.get("QSTASH_URL", "")) or None
    return QStash(token, base_url=base)


def _mostrar(cliente) -> int:
    itens = cliente.schedule.list()
    if not itens:
        print("nenhum schedule")
        return 0
    for s in itens:
        pausado = getattr(s, "paused", False)
        print(f"{s.schedule_id}  cron={s.cron}  destino={s.destination}  pausado={pausado}")
    return 0


def _parametros(nome: str, args: argparse.Namespace) -> tuple[dict[str, Any], str | None]:
    """Monta os argumentos do ``schedule.create``; devolve (parametros, erro)."""
    cfg = SCHEDULES[nome]
    destino = _limpa(args.url or os.environ.get(cfg["url_env"], ""))
    if not destino.startswith("https://"):
        return {}, f"destino precisa ser https:// (--url ou {cfg['url_env']})"
    params: dict[str, Any] = {
        "destination": destino,
        "cron": args.cron or cfg["cron"],
        "method": cfg["method"],
        "schedule_id": cfg["id"],
        "retries": cfg["retries"],
        "timeout": cfg["timeout"],
        "label": cfg["id"],
    }
    if "retry_delay" in cfg:
        params["retry_delay"] = cfg["retry_delay"]
    if "callback_env" in cfg:
        callback = _limpa(args.callback or os.environ.get(cfg["callback_env"], ""))
        if not callback.startswith("https://"):
            return {}, f"callback precisa ser https:// (--callback ou {cfg['callback_env']})"
        params["failure_callback"] = callback
    return params, None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("acao", choices=["show", "apply", "pause", "resume", "delete"])
    ap.add_argument("--schedule", choices=sorted(SCHEDULES), help="qual schedule (obrigatorio fora de show)")
    ap.add_argument("--url", default="", help="destino (wake) ou URL sondada (watchdog)")
    ap.add_argument("--callback", default="", help="URL do failure_callback (watchdog)")
    ap.add_argument("--cron", default="")
    ap.add_argument("--aplicar", action="store_true", help="grava no QStash (sem isso, simula)")
    args = ap.parse_args(argv)

    if args.acao != "show" and not args.schedule:
        print("--schedule e obrigatorio para esta acao", file=sys.stderr)
        return 2

    if args.acao == "apply":
        params, erro = _parametros(args.schedule, args)
        if erro:
            print(erro, file=sys.stderr)
            return 2
        resumo = {k: v for k, v in params.items() if k != "label"}
        print(f"schedule {params['schedule_id']}: " + " ".join(f"{k}={v}" for k, v in resumo.items()))
        if not args.aplicar:
            print("simulacao: nada foi gravado (use --aplicar)")
            return 0
        cliente = _cliente()
        cliente.schedule.create(**params)
        print("gravado")
        return _mostrar(cliente)

    cliente = _cliente()
    if args.acao == "show":
        return _mostrar(cliente)
    schedule_id = SCHEDULES[args.schedule]["id"]
    {"pause": cliente.schedule.pause, "resume": cliente.schedule.resume, "delete": cliente.schedule.delete}[args.acao](
        schedule_id
    )
    print(f"{args.acao} {schedule_id}: ok")
    return _mostrar(cliente)


if __name__ == "__main__":
    raise SystemExit(main())
