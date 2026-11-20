"""WAKE: "ha trabalho vencido enquanto o servico dormia?" (sem importar nada do app).

O Procrastinate roda DENTRO do processo web. Quando o Render free dorme (15 min sem
trafego de entrada) o worker e os relogios dormem juntos. Ao acordar, os relogios
recuperam so o ULTIMO tick perdido de cada um (``periodic_defaults.max_delay``) e os jobs
``todo`` com ``scheduled_at`` vencido rodam todos: falta so alguem acordar o processo.

Este modulo e a pergunta barata, de leitura, que o Actions (``scripts/wake_check.py``) e o
endpoint ``/v1/control/wake`` fazem ao banco. Ele **nao** reimplementa a regra de negocio de
quem abordar (``app/durable/pendentes.py`` decide, com todas as guardas): a contagem de
``pendentes`` aqui e grossa de proposito. Falso positivo custa um despertar; falso negativo
custaria um cliente sem resposta, entao na duvida acorda.

SQL em estilo ``%(nome)s`` (psycopg), servido a script (psycopg puro) e a app (SQLAlchemy
``exec_driver_sql``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# Fila dos relogios: um ``todo`` aqui vencido so diz que o worker esta parado, e quem o
# acorda e o proprio despertar; nao justifica acordar por si so.
CRON_QUEUE = "cron"

LOOKAHEAD_S = 300  # job que vence nos proximos 5 min ja justifica acordar
STALE_HEARTBEAT_S = 60  # o Procrastinate considera o worker morto apos 30 s sem sinal
ESPERA_MINIMA_S = 180  # mesma espera do pendentes_sweep
JANELA_META_H = 23  # a janela de 24 h do WhatsApp fecha em 24 h

WAKE_SQL = """
SELECT
  (SELECT count(*) FROM procrastinate_jobs AS j
    WHERE j.status = 'todo'
      AND j.queue_name <> %(cron_queue)s
      AND coalesce(j.scheduled_at, now()) <= now() + make_interval(secs => %(lookahead_s)s)
  ) AS jobs_vencidos,
  (SELECT count(*) FROM procrastinate_jobs AS j
    LEFT JOIN procrastinate_workers AS w ON w.id = j.worker_id
    WHERE j.status = 'doing'
      AND (w.id IS NULL
           OR w.last_heartbeat < now() - make_interval(secs => %(stale_s)s))
  ) AS jobs_travados,
  (SELECT count(*) FROM (
      SELECT DISTINCT ON (e.tenant_id, e.phone)
             e.tenant_id, e.phone, e.remote_jid, e.received_at
      FROM genius_next.events AS e
      WHERE e.direction = 'inbound'
        AND coalesce(e.text_content, '') <> ''
        AND e.received_at > now() - make_interval(hours => %(janela_h)s)
        AND (cardinality(%(tenants)s::text[]) = 0 OR e.tenant_id = ANY(%(tenants)s::text[]))
      ORDER BY e.tenant_id, e.phone, e.received_at DESC, e.id DESC
  ) AS u
  JOIN genius_next.clients AS c ON c.tenant_id = u.tenant_id AND c.phone = u.phone
  WHERE c.automation_state = 'open'
    AND c.status = 'open'
    AND u.received_at < now() - make_interval(secs => %(espera_s)s)
    AND NOT EXISTS (
        SELECT 1 FROM genius_next.events AS o
        WHERE o.tenant_id = u.tenant_id AND o.phone = u.phone
          AND o.direction = 'outbound' AND o.received_at > u.received_at)
    AND NOT EXISTS (
        SELECT 1 FROM genius_next.outbox AS ob
        WHERE ob.tenant_id = u.tenant_id AND ob.destination_jid = u.remote_jid
          AND ob.kind IN ('customer', 'team') AND ob.created_at > u.received_at)
  ) AS pendentes
"""


@dataclass(frozen=True)
class WakeState:
    """Resultado da pergunta: quantos itens de cada tipo estao esperando o app acordar."""

    jobs_vencidos: int
    jobs_travados: int
    pendentes: int

    @property
    def wake_needed(self) -> bool:
        return (self.jobs_vencidos + self.jobs_travados + self.pendentes) > 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "jobs_vencidos": self.jobs_vencidos,
            "jobs_travados": self.jobs_travados,
            "pendentes": self.pendentes,
            "wake_needed": self.wake_needed,
        }


def wake_params(
    *,
    tenants: list[str] | None = None,
    lookahead_s: int = LOOKAHEAD_S,
    stale_s: int = STALE_HEARTBEAT_S,
) -> dict[str, Any]:
    return {
        "cron_queue": CRON_QUEUE,
        "lookahead_s": lookahead_s,
        "stale_s": stale_s,
        "janela_h": JANELA_META_H,
        "espera_s": ESPERA_MINIMA_S,
        "tenants": [t for t in (tenants or []) if t],
    }


def state_from_row(row: Mapping[str, Any] | tuple[Any, ...]) -> WakeState:
    if isinstance(row, Mapping):
        values = (row["jobs_vencidos"], row["jobs_travados"], row["pendentes"])
    else:
        values = tuple(row[:3])
    return WakeState(*(int(v or 0) for v in values))


def parse_tenants(raw: str) -> list[str]:
    return [t.strip() for t in raw.split(",") if t.strip()]


def consultar(database_url: str, *, tenants: list[str]) -> WakeState:
    """Versao do servico (SQLAlchemy). O script do Actions usa psycopg puro com o mesmo SQL."""
    from sqlalchemy.orm import Session

    from app.durable.tenant_registry import get_registry_engine

    with Session(get_registry_engine(database_url)) as session:
        row = session.connection().exec_driver_sql(
            WAKE_SQL, wake_params(tenants=tenants)
        ).fetchone()
    return state_from_row(row)


TRABALHO_FUTURO_SQL = """
SELECT count(*) FROM procrastinate_jobs AS j
 WHERE j.status = 'todo' AND j.queue_name <> %(cron_queue)s
"""


def tem_trabalho_futuro(database_url: str) -> bool:
    """True se ha job fora da fila cron esperando (follow-up de 30 min, 24 h, 48 h, turno com atraso).

    Enquanto houver, o wake do QStash nao pode ser pausado: ele e quem acorda o servico para o job
    vencer. So com a fila vazia (alem dos relogios) o Standby pode dormir com a 002 saudavel.
    """
    from sqlalchemy.orm import Session

    from app.durable.tenant_registry import get_registry_engine

    with Session(get_registry_engine(database_url)) as session:
        row = session.connection().exec_driver_sql(
            TRABALHO_FUTURO_SQL, {"cron_queue": CRON_QUEUE}
        ).fetchone()
    return bool(row and row[0])
