"""Alerta de erro de flow no WhatsApp do chairman (ordem de 15/out/2026).

``send_ops_alert`` manda um ``sendText`` para ``Settings.OPS_ALERT_TO`` pela
instancia ``Settings.OPS_ALERT_EVOLUTION_INSTANCE``. Instancia e destino
faltando = alerta vira so log, nunca erro. O destino NAO passa pela
``DELIVERY_ALLOWLIST`` (o alerta e operacional, nao mensagem de cliente) e
nenhum outro destino e aceito.

Freio anti-barulho, atomico em PG (``genius_next.ops_alerts``, migration 078):
no maximo 1 alerta por chave a cada 30 min e 20 por hora no total. **Nunca
levanta**: um alerta que nao sai nao pode derrubar o turno que ele esta
denunciando.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import text as _sql_text
from sqlalchemy.orm import Session

from app.durable.tenant_registry import get_registry_engine
from app.settings import get_settings

logger = logging.getLogger(__name__)

# Freios: nenhum erro repetido deve virar chuva de mensagem no celular.
KEY_COOLDOWN_SECONDS = 30 * 60
MAX_PER_HOUR = 20
# A chamada e fora do caminho do turno: curta de proposito.
TIMEOUT_SECONDS = 10.0
# Erro em 1 linha.
ERROR_MAX_CHARS = 160

_TENANT_LABELS = {
    "6f4d3510-b5bc-4c19-9d44-2679f9dbfdb9": "Clinic",
    "d0b00bad-814d-4be4-835c-5762c9ac374d": "teste",
}

# Um unico statement decide tudo: INSERT cego (chave nova) e UPDATE da chave
# conhecida. Timestamps entram como parametro (nao `now()`) para a consulta
# rodar identica em qualquer banco e para chavear a janela no teste.
_RESERVE_SQL = _sql_text(
    """
    /* ops_alerts.reserve */
    INSERT INTO genius_next.ops_alerts (key, last_sent_at, count)
    SELECT :key, :now, 1
     WHERE (SELECT count(*) FROM genius_next.ops_alerts
             WHERE last_sent_at > :since_hour) < :max_per_hour
    ON CONFLICT (key) DO UPDATE
       SET last_sent_at = :now,
           count = genius_next.ops_alerts.count + 1
     WHERE genius_next.ops_alerts.last_sent_at < :since_cooldown
       AND (SELECT count(*) FROM genius_next.ops_alerts
             WHERE last_sent_at > :since_hour) < :max_per_hour
    RETURNING key
    """
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def tenant_label(tenant_id: str) -> str:
    """Rotulo curto e reconhecivel do tenant."""
    return _TENANT_LABELS.get(tenant_id, tenant_id[:8])


def _mask_digits(digits: str) -> str:
    return f"{digits[:4]}…{digits[-2:]}"


def mask_contact(contact: str) -> str:
    """Contato mascarado (``5588…13``): nenhum telefone completo no alerta."""
    digits = "".join(character for character in contact if character.isdigit())
    if len(digits) < 6:
        return "contato"
    return _mask_digits(digits)


# 6+ digitos seguidos, com os separadores usuais de telefone/CPF entre eles.
# O texto da excecao tambem carrega dado sensivel (o provider costuma cuspir o
# numero na mensagem de erro), entao ele passa pelo mesmo formato do contato.
_MASK_RUNS = re.compile(r"\d(?:[.\-/]?\d){5,}")


def mask_digits(text: str) -> str:
    """Mascara toda sequencia de 6+ digitos do texto (``5588…13``).

    Menos de 6 digitos nao identifica ninguem e fica como esta (valores,
    codigos de status), para o alerta continuar legivel.
    """
    return _MASK_RUNS.sub(
        lambda match: _mask_digits(
            "".join(character for character in match.group() if character.isdigit())
        ),
        text,
    )


def ops_alert_text(
    what_failed: str,
    *,
    tenant_id: str,
    contact: str,
    error: str,
) -> str:
    """Uma linha, PT-BR, sem conteudo de mensagem e sem telefone completo."""
    # Mascara ANTES do corte: truncar primeiro poderia partir uma sequencia
    # longa em pedacos curtos e deixar digitos escaparem da mascara.
    detail = mask_digits(" ".join(error.split()))[:ERROR_MAX_CHARS]
    return (
        f"⚠️ Genius Next: {what_failed} "
        f"(tenant {tenant_label(tenant_id)}, contato {mask_contact(contact)}). {detail}"
    )


def reserve_ops_alert_slot(session: Session, key: str, *, now: datetime | None = None) -> bool:
    """Reserva 1 vaga na janela. False = freio (30 min por chave ou teto/hora)."""
    moment = now or _utcnow()
    row = session.execute(
        _RESERVE_SQL,
        {
            "key": key,
            "now": moment,
            "since_cooldown": moment - timedelta(seconds=KEY_COOLDOWN_SECONDS),
            "since_hour": moment - timedelta(seconds=3600),
            "max_per_hour": MAX_PER_HOUR,
        },
    ).scalar_one_or_none()
    session.commit()
    return row is not None


def _http_client() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT_SECONDS)


def _alert_session() -> Session:
    return Session(get_registry_engine(get_settings().DATABASE_URL))


def send_ops_alert(key: str, text: str) -> bool:
    """Manda o alerta ao chairman. Nunca levanta; False = nao enviou."""
    try:
        settings = get_settings()
        destination = settings.OPS_ALERT_TO
        instance = settings.OPS_ALERT_EVOLUTION_INSTANCE
        if not destination or not instance:
            logger.warning(
                "ops alert desativado (sem OPS_ALERT_TO/OPS_ALERT_EVOLUTION_INSTANCE): %s",
                text,
            )
            return False
        if settings.EVOLUTION_URL is None or settings.EVOLUTION_API_KEY is None:
            logger.warning("ops alert sem EVOLUTION_URL/EVOLUTION_API_KEY: %s", key)
            return False

        with _alert_session() as session:
            if not reserve_ops_alert_slot(session, key):
                logger.info("ops alert suprimido pelo freio: %s", key)
                return False

        with _http_client() as client:
            response = client.post(
                f"{settings.EVOLUTION_URL}/message/sendText/{instance}",
                headers={"apikey": settings.EVOLUTION_API_KEY.get_secret_value()},
                json={"number": destination, "text": text},
            )
        sent = response.status_code in {200, 201}
        if not sent:
            logger.warning(
                "ops alert recusado pela Evolution (%s): %s", response.status_code, key
            )
        return sent
    except Exception:
        logger.warning("ops alert nao enviado: %s", key, exc_info=True)
        return False


def ops_alert_step(key: str, text: str) -> bool:
    """Alerta no caminho do turno.

    O freio por ``key`` evita reenvio quando o job repete; onde ja existe um
    envio em curso (ex.: ``send_zernio_step``), chame ``send_ops_alert`` direto.
    """
    return send_ops_alert(key, text)
