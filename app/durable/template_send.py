"""Envio de MODELO (template) da Meta: a única mensagem fora da janela de 24 h.

O WhatsApp Cloud API só aceita **texto livre** dentro de 24 h da última mensagem
do paciente; fora disso devolve o erro ``131047`` ("It's been over 24h...") e a
única saída é um **modelo aprovado pela Meta**. Este módulo dá ao outbox o
``action='send_template'``, com as travas todas no caminho de entrega:

* o modelo tem que estar no catálogo **APROVADO** do tenant (verificado via
  ``GET /v1/whatsapp/templates`` — :func:`fetch_tenant_templates`);
* a categoria declarada tem que bater com a aprovada: um modelo MARKETING nunca
  é apresentado como UTILITY (a Meta já sabe a categoria real pelo nome; a
  checagem só nos impede de mentir para nós mesmos);
* o número de variáveis do corpo tem que bater com os ``{{n}}`` do modelo;
* a janela de 24 h tem que estar **FECHADA** (dentro dela usa-se texto livre);
* a conversa tem que ser da automação (open/open, sem handoff, sem win/loss,
  sem adiamento/encerramento na última fala do paciente);
* no máximo ``TEMPLATE_MAX_PER_PHONE_24H`` modelo por contato em 24 h e
  ``TEMPLATE_MAX_PER_PHONE_7D`` em 7 dias;
* uma flag própria (padrão desligada) **e** o tenant numa lista liberam tudo.

A decisão de envio é uma função pura (:func:`template_block_reason`) sobre fatos
coletados à parte, para poder ser testada sem rede e sem banco. O corpo bruto do
provedor nunca é guardado.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app.durable.persistence import ClaimedOutbox
from app.durable.tenant_registry import get_registry_engine
from app.identity_primitives import brazilian_mobile_jid_variants, normalize_phone
from app.settings import Settings, get_settings
from app.status.rules import is_deferral_or_closing

logger = logging.getLogger(__name__)

# Janela de atendimento da Cloud API: texto livre só até 24 h da última fala do
# paciente. Fechada, apenas modelo aprovado.
SERVICE_WINDOW = timedelta(hours=24)

# ``{{1}}``, ``{{ 1 }}`` — as variaveis de corpo posicionais da Meta.
_PLACEHOLDER = re.compile(r"\{\{\s*(\d+)\s*\}\}")

# Tenant da clínica médica (cliente real em produção) e a conta Zernio dele. Os modelos abaixo foram
# confirmados APPROVED via GET /v1/whatsapp/templates?accountId=... em 20/out/2026.
CLINIC_TENANT_ID = "6f4d3510-b5bc-4c19-9d44-2679f9dbfdb9"
CLINIC_ZERNIO_ACCOUNT = "6ac69dc249c222c48d0c0d53"

_RETOMADA_BODY = (
    "Oi, {{1}}! Aqui é a Júlia, da recepção do Kyrios. Ainda posso te ajudar "
    "com a informação que você pediu? Se preferir, me responde aqui."
)
_AGENDAMENTO_BODY = (
    "Olá {{1}},\n"
    "Sua consulta está agendada para {{2}}.\n\n"
    "Serviço: {{3}}\n"
    "Número de confirmação: {{4}}\n\n"
    "Estamos ansiosos pela sua visita."
)


class ApprovedTemplate(BaseModel):
    """Um modelo que a Meta aprovou, como o outbox o enxerga."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    category: Literal["UTILITY", "MARKETING"]
    language: str
    body: str
    # Quando e de onde o status APPROVED foi confirmado (prova auditavel).
    verified_at: str
    source: str = "GET /v1/whatsapp/templates"

    @property
    def variable_count(self) -> int:
        """Quantas variaveis de corpo posicionais o texto exige."""
        return placeholder_count(self.body)


def placeholder_count(body: str) -> int:
    """Maior indice ``{{n}}`` do corpo (0 quando nao ha placeholder)."""
    indices = [int(match) for match in _PLACEHOLDER.findall(body)]
    return max(indices, default=0)


def render_body(body: str, variables: list[str]) -> str:
    """Substitui ``{{n}}`` pelos valores posicionais (1-indexado)."""
    def replace(match: re.Match[str]) -> str:
        position = int(match.group(1))
        if 1 <= position <= len(variables):
            return variables[position - 1]
        return match.group(0)

    return _PLACEHOLDER.sub(replace, body)


# Catalogo por tenant. Modelo novo so entra aqui com o status confirmado por
# GET (ou ordem do chairman para criar na Meta) — nunca no escuro.
APPROVED_TEMPLATES: dict[str, dict[str, ApprovedTemplate]] = {
    CLINIC_TENANT_ID: {
        "retomada_kyrios_utilidade": ApprovedTemplate(
            name="retomada_kyrios_utilidade",
            category="UTILITY",
            language="pt_BR",
            body=_RETOMADA_BODY,
            verified_at="2026-10-20T00:16Z",
        ),
        "retomada_kyrios_marketing": ApprovedTemplate(
            name="retomada_kyrios_marketing",
            category="MARKETING",
            language="pt_BR",
            body=_RETOMADA_BODY,
            verified_at="2026-10-20T00:16Z",
        ),
        "agendamento": ApprovedTemplate(
            name="agendamento",
            category="UTILITY",
            language="pt_BR",
            body=_AGENDAMENTO_BODY,
            verified_at="2026-10-20T00:16Z",
        ),
    }
}


def approved_template(tenant_id: str, name: str) -> ApprovedTemplate | None:
    """O modelo aprovado do tenant, ou None (nao aprovado / nao catalogado)."""
    return APPROVED_TEMPLATES.get(tenant_id, {}).get(name)


def preferred_template(
    tenant_id: str, candidates: tuple[str, ...]
) -> ApprovedTemplate | None:
    """Escolhe entre candidatos preferindo UTILITY (menos restricao da Meta).

    MARKETING so quando nao houver UTILITY entre os candidatos: a categoria tem
    que refletir o conteudo, nunca ser escolhida para burlar a politica.
    """
    approved = [
        template
        for name in candidates
        if (template := approved_template(tenant_id, name)) is not None
    ]
    for template in approved:
        if template.category == "UTILITY":
            return template
    return approved[0] if approved else None


class TemplateFacts(BaseModel):
    """Fatos necessarios para decidir um envio de modelo, sem I/O."""

    model_config = ConfigDict(extra="forbid")

    feature_enabled: bool
    template: ApprovedTemplate | None
    declared_category: str
    variable_count_ok: bool
    window_closed: bool
    state_allowed: bool
    sends_24h: int
    sends_7d: int
    max_24h: int
    max_7d: int


# Motivos terminais. `window_open_use_free_text` e proposital: dentro da janela
# o caminho e texto livre, nao modelo.
def template_block_reason(facts: TemplateFacts) -> str | None:
    """None = pode enviar; string = motivo do bloqueio (fail-closed)."""
    if not facts.feature_enabled:
        return "template_send_disabled"
    if facts.template is None:
        return "template_not_approved"
    if facts.template.category != facts.declared_category:
        return "template_category_mismatch"
    if not facts.variable_count_ok:
        return "template_variable_mismatch"
    if not facts.window_closed:
        return "window_open_use_free_text"
    if not facts.state_allowed:
        return "conversation_not_eligible"
    if facts.sends_24h >= facts.max_24h:
        return "template_quota_24h"
    if facts.sends_7d >= facts.max_7d:
        return "template_quota_7d"
    return None


def window_is_closed(
    last_inbound_at: datetime | None, now: datetime | None = None
) -> bool:
    """Janela da Meta fechada? Sem fala do paciente, fechada (so modelo sai)."""
    if last_inbound_at is None:
        return True
    moment = now or datetime.now(UTC)
    if last_inbound_at.tzinfo is None:
        last_inbound_at = last_inbound_at.replace(tzinfo=UTC)
    return moment - last_inbound_at >= SERVICE_WINDOW


# --- leitura dos fatos (somente leitura; uma consulta por chamada) -----------

_LAST_INBOUND_SQL = text(
    """
    /* template.last_inbound */
    SELECT max(received_at)
    FROM genius_next.events
    WHERE tenant_id = :tenant_id AND phone = :phone AND direction = 'inbound'
    """
)

_LAST_INBOUND_TEXTS_SQL = text(
    """
    /* template.last_inbound_texts */
    SELECT coalesce(text_content, '') FROM genius_next.events
    WHERE tenant_id = :tenant_id AND phone = :phone AND direction = 'inbound'
    ORDER BY received_at DESC, id DESC LIMIT 2
    """
)

_CLIENT_STATE_SQL = text(
    """
    /* template.client_state */
    SELECT status, automation_state, last_intent FROM genius_next.clients
    WHERE tenant_id = :tenant_id AND phone = :phone
    """
)

# Disparos de modelo confirmados ao contato. Conta por variantes do nono digito
# para nao escapar do teto por telefone.
_TEMPLATE_SENDS_SQL = text(
    """
    /* template.sends */
    SELECT
        count(*) FILTER (
            WHERE dispatched_at > now() - interval '24 hours'
        ) AS sends_24h,
        count(*) AS sends_7d
    FROM genius_next.outbox
    WHERE tenant_id = :tenant_id
      AND destination_jid IN :jids
      AND action = 'send_template'
      AND delivery_status IN ('sent', 'uncertain')
      AND dispatched_at > now() - interval '7 days'
    """
).bindparams(bindparam("jids", expanding=True))


def _state_allowed(session: Session, tenant_id: str, phone: str) -> bool:
    """Conversa da automacao: open/open, sem handoff, sem adiamento/encerrada."""
    state = session.execute(
        _CLIENT_STATE_SQL, {"tenant_id": tenant_id, "phone": phone}
    ).mappings().one_or_none()
    if state is None:
        return False
    if state["status"] != "open" or state["automation_state"] != "open":
        return False
    if state["last_intent"] == "handoff":
        return False
    textos = session.execute(
        _LAST_INBOUND_TEXTS_SQL, {"tenant_id": tenant_id, "phone": phone}
    ).scalars().all()
    return not any(t and is_deferral_or_closing(t) for t in textos)


def _sends(session: Session, tenant_id: str, destination_jid: str) -> tuple[int, int]:
    row = session.execute(
        _TEMPLATE_SENDS_SQL,
        {
            "tenant_id": tenant_id,
            "jids": list(brazilian_mobile_jid_variants(destination_jid)),
        },
    ).mappings().one()
    return int(row["sends_24h"] or 0), int(row["sends_7d"] or 0)


def collect_template_facts(
    session: Session,
    claimed: ClaimedOutbox,
    settings: Settings,
) -> TemplateFacts:
    """Le so os fatos do envio de modelo (nunca escreve)."""
    try:
        phone = normalize_phone(claimed.destination_jid)
    except ValueError:
        phone = claimed.destination_jid
    command = claimed.template
    template = (
        approved_template(claimed.tenant_id, command.name)
        if command is not None
        else None
    )
    last_inbound = session.execute(
        _LAST_INBOUND_SQL, {"tenant_id": claimed.tenant_id, "phone": phone}
    ).scalar_one_or_none()
    sends_24h, sends_7d = _sends(session, claimed.tenant_id, claimed.destination_jid)
    return TemplateFacts(
        feature_enabled=settings.template_send_enabled(claimed.tenant_id),
        template=template,
        declared_category=command.category if command is not None else "",
        variable_count_ok=(
            template is not None
            and command is not None
            and len(command.variables) == template.variable_count
        ),
        window_closed=window_is_closed(last_inbound),
        state_allowed=_state_allowed(session, claimed.tenant_id, phone),
        sends_24h=sends_24h,
        sends_7d=sends_7d,
        max_24h=settings.TEMPLATE_MAX_PER_PHONE_24H,
        max_7d=settings.TEMPLATE_MAX_PER_PHONE_7D,
    )


def template_block_reason_step(claimed: ClaimedOutbox) -> str | None:
    """Checkpoint único da trava de modelo, imediatamente antes do I/O."""
    settings = get_settings()
    if not settings.template_send_enabled(claimed.tenant_id):
        return "template_send_disabled"
    engine = get_registry_engine(settings.DATABASE_URL)
    with Session(engine) as session:
        facts = collect_template_facts(session, claimed, settings)
    return template_block_reason(facts)


# --- verificação do status real na Meta/Zernio (GET, nunca escreve) ----------


class RemoteTemplate(BaseModel):
    """Um modelo como o Zernio o reporta (para verificação/auditoria)."""

    model_config = ConfigDict(extra="ignore")

    name: str
    status: str
    category: str
    language: str
    body: str = ""

    @property
    def approved(self) -> bool:
        return self.status.upper() == "APPROVED"

    @property
    def variable_count(self) -> int:
        return placeholder_count(self.body)


def _remote_template(raw: dict[str, Any]) -> RemoteTemplate:
    body = ""
    for component in raw.get("components") or []:
        if isinstance(component, dict) and component.get("type") == "BODY":
            body = str(component.get("text") or "")
            break
    return RemoteTemplate(
        name=str(raw.get("name") or ""),
        status=str(raw.get("status") or ""),
        category=str(raw.get("category") or ""),
        language=str(raw.get("language") or ""),
        body=body,
    )


def fetch_tenant_templates(
    account_id: str,
    *,
    settings: Settings,
    client: Any = None,
) -> list[RemoteTemplate]:
    """GET /v1/whatsapp/templates — status real de todos os modelos da conta."""
    import httpx

    if settings.ZERNIO_API_KEY is None:
        raise ValueError("ZERNIO_API_KEY is not configured")
    owned_client = client is None
    http = client or httpx.Client(timeout=settings.DELIVERY_TIMEOUT_SECONDS)
    try:
        response = http.get(
            f"{settings.ZERNIO_API_URL}/v1/whatsapp/templates",
            params={"accountId": account_id},
            headers={
                "Authorization": f"Bearer {settings.ZERNIO_API_KEY.get_secret_value()}"
            },
        )
        response.raise_for_status()
        payload = response.json()
    finally:
        if owned_client:
            http.close()
    templates = payload.get("templates") if isinstance(payload, dict) else None
    if not isinstance(templates, list):
        raise ValueError("unexpected templates response shape")
    return [_remote_template(raw) for raw in templates if isinstance(raw, dict)]
