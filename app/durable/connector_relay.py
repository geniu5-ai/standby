"""Entrega das respostas gravadas ao conector externo (OpenWA, waxum...), sem sair do ``record_only``.

O Standby continua gravando toda resposta no ``outbox`` com ``delivery_mode='record_only'`` e
``delivery_status='recorded'``; nada aqui muda esse estado. Para uma instancia que um conector
representa, o conector BUSCA as linhas gravadas (rota assinada) e as entrega pelo proprio gateway. A
entrega e anotada so em ``provider_result.relay`` (JSONB), de modo que a contabilidade do app
(``ja_respondeu``, metricas, reaper) segue igual.

Travas, todas fail-closed:
  * so instancias de ``CONNECTOR_RELAY_INSTANCES`` (vazia por padrao = nada sai);
  * so ``kind='customer'``, ``action='send_text'`` e ``record_only``;
  * so linhas criadas ha no maximo ``CONNECTOR_RELAY_MAX_AGE_SECONDS`` (nunca o passivo antigo);
  * cada linha e entregue uma unica vez (``provider_result.relay`` ausente na busca, confirmado no ack).

O conector ainda tem a sua propria lista de destinos permitidos (``SEND_ALLOWLIST``).
SQL em estilo ``%(nome)s`` (psycopg, ``exec_driver_sql``), como ``app/wake.py``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

PENDING_SQL = """
SELECT id, instance, destination_jid, payload ->> 'text' AS text, idempotency_key, created_at
FROM genius_next.outbox
WHERE delivery_status = 'recorded'
  AND delivery_mode = 'record_only'
  AND kind = 'customer'
  AND action = 'send_text'
  AND instance = ANY(%(instances)s::text[])
  AND created_at > now() - make_interval(secs => %(max_age_s)s)
  AND provider_result -> 'relay' IS NULL
  AND coalesce(payload ->> 'text', '') <> ''
ORDER BY id
LIMIT %(limit)s
"""

ACK_SQL = """
UPDATE genius_next.outbox
SET provider_result = provider_result || %(relay)s::jsonb
WHERE id = %(id)s
  AND delivery_status = 'recorded'
  AND delivery_mode = 'record_only'
  AND instance = ANY(%(instances)s::text[])
  AND provider_result -> 'relay' IS NULL
RETURNING id
"""

MAX_ITEMS = 20


def parse_instances(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def pending_params(*, instances: list[str], max_age_s: int, limit: int = MAX_ITEMS) -> dict[str, Any]:
    return {"instances": instances, "max_age_s": max_age_s, "limit": max(1, min(limit, MAX_ITEMS))}


def relay_annotation(*, status: str, provider_message_id: str | None, error: str | None, now: datetime | None = None) -> str:
    """JSON de ``provider_result.relay``: so o resultado da entrega, nunca texto de mensagem."""
    stamp = (now or datetime.now(UTC)).isoformat(timespec="seconds")
    relay: dict[str, Any] = {"status": status, "at": stamp}
    if provider_message_id:
        relay["provider_message_id"] = provider_message_id[:256]
    if error:
        relay["error"] = error[:200]
    return json.dumps({"relay": relay}, separators=(",", ":"))


def list_pending(database_url: str, *, instances: list[str], max_age_s: int) -> list[dict[str, Any]]:
    """Respostas gravadas e ainda nao entregues das instancias do conector. Vazio sem instancias."""
    if not instances:
        return []
    from sqlalchemy.orm import Session

    from app.durable.tenant_registry import get_registry_engine

    with Session(get_registry_engine(database_url)) as session:
        rows = session.connection().exec_driver_sql(PENDING_SQL, pending_params(instances=instances, max_age_s=max_age_s)).fetchall()
    return [
        {
            "id": int(row[0]),
            "instance": row[1],
            "destination_jid": row[2],
            "text": row[3],
            "idempotency_key": row[4],
            "created_at": row[5].isoformat() if hasattr(row[5], "isoformat") else str(row[5]),
        }
        for row in rows
    ]


def acknowledge(
    database_url: str,
    *,
    outbox_id: int,
    instances: list[str],
    status: str,
    provider_message_id: str | None,
    error: str | None,
) -> bool:
    """Anota a entrega em ``provider_result.relay``. False se a linha nao e elegivel ou ja foi anotada."""
    if not instances:
        return False
    from sqlalchemy.orm import Session

    from app.durable.tenant_registry import get_registry_engine

    params = {
        "id": outbox_id,
        "instances": instances,
        "relay": relay_annotation(status=status, provider_message_id=provider_message_id, error=error),
    }
    with Session(get_registry_engine(database_url)) as session:
        row = session.connection().exec_driver_sql(ACK_SQL, params).fetchone()
        session.commit()
    return row is not None
