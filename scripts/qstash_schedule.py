#!/usr/bin/env python3
"""Schedule do QStash que acorda o Standby (``/v1/control/qstash-wake``).

Idempotente: o ``schedule_id`` fixo faz o ``apply`` sobrescrever o schedule em vez de duplicar.
Sem ``--aplicar`` so mostra o que faria (nenhuma escrita no QStash).

    python scripts/qstash_schedule.py show
    python scripts/qstash_schedule.py apply                 # simulacao
    python scripts/qstash_schedule.py apply --aplicar       # cria/atualiza
    python scripts/qstash_schedule.py pause | resume | delete

Ambiente: ``QSTASH_TOKEN`` e, fora da regiao padrao, ``QSTASH_URL`` (ex.:
``https://qstash-us-east-1.upstash.io``). Nunca imprime o token. O destino vem de
``--url`` ou de ``QSTASH_WAKE_URL``.

Custo: cada disparo e uma mensagem; o plano free do QStash tem 1000 mensagens por dia
(retentativas contam). ``*/20 * * * *`` gasta 72 por dia; ``*/10`` gasta 144 e deixa o servico
quase sempre acordado (ver ``docs/WAKE.md``).
"""

from __future__ import annotations

import argparse
import os
import sys

SCHEDULE_ID = "standby-wake"
CRON_PADRAO = "*/20 * * * *"
# 3 retentativas padrao do QStash; o timeout cobre o cold start (~1 min) do Render free.
RETRIES = 3
TIMEOUT_S = 120


def _cliente():
    from qstash import QStash

    token = os.environ.get("QSTASH_TOKEN", "").strip().strip("\"'")
    if not token:
        raise SystemExit("QSTASH_TOKEN ausente no ambiente")
    base = os.environ.get("QSTASH_URL", "").strip().strip("\"'") or None
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("acao", choices=["show", "apply", "pause", "resume", "delete"])
    ap.add_argument("--url", default=os.environ.get("QSTASH_WAKE_URL", ""), help="destino do wake")
    ap.add_argument("--cron", default=CRON_PADRAO)
    ap.add_argument("--aplicar", action="store_true", help="grava no QStash (sem isso, simula)")
    args = ap.parse_args(argv)

    if args.acao == "apply":
        destino = args.url.strip().strip("\"'")
        if not destino.startswith("https://"):
            print("destino precisa ser https:// (--url ou QSTASH_WAKE_URL)", file=sys.stderr)
            return 2
        print(f"schedule {SCHEDULE_ID}: cron={args.cron} destino={destino} retries={RETRIES} timeout={TIMEOUT_S}s")
        if not args.aplicar:
            print("simulacao: nada foi gravado (use --aplicar)")
            return 0
        cliente = _cliente()
        cliente.schedule.create(
            destination=destino,
            cron=args.cron,
            schedule_id=SCHEDULE_ID,
            retries=RETRIES,
            timeout=TIMEOUT_S,
            label="standby-wake",
        )
        print("gravado")
        return _mostrar(cliente)

    cliente = _cliente()
    if args.acao == "show":
        return _mostrar(cliente)
    {"pause": cliente.schedule.pause, "resume": cliente.schedule.resume, "delete": cliente.schedule.delete}[args.acao](
        SCHEDULE_ID
    )
    print(f"{args.acao}: ok")
    return _mostrar(cliente)


if __name__ == "__main__":
    raise SystemExit(main())
