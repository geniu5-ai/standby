"""Synchronous trusted provider-instance tenant lookup."""

from __future__ import annotations

from functools import lru_cache

from sqlalchemy import Engine, create_engine, make_url, text
from sqlalchemy.orm import Session

from app.contracts import (
    TenantRegistrySetEnabledRequest,
    TenantRegistrySetEnabledResult,
    TenantRegistryUpsertRequest,
    TenantRegistryUpsertResult,
)
from app.db import sql_session, transaction
from app.settings import get_settings

_RESOLVE_TENANT_SQL = text(
    """
    /* registry.resolve */
    SELECT tenant_id
    FROM genius_next.tenant_registry
    WHERE provider = :provider
      AND instance = :instance
      AND enabled = :enabled
    """
)

_MANAGER_JID_SQL = text(
    """
    /* registry.manager_jid */
    SELECT manager_jid
    FROM genius_next.tenant_registry
    WHERE tenant_id = :tenant_id
      AND enabled = true
    LIMIT 1
    """
)


_KNOWLEDGE_STRATEGY_SQL = text(
    """
    /* registry.knowledge_strategy */
    SELECT knowledge_strategy
    FROM genius_next.tenant_registry
    WHERE tenant_id = :tenant_id
    LIMIT 1
    """
)


@lru_cache(maxsize=4)
def get_registry_engine(database_url: str) -> Engine:
    """Build the shared engine lazily; creating it does not open a connection."""
    url = make_url(database_url)
    if url.drivername in {"postgres", "postgresql"}:
        url = url.set(drivername="postgresql+psycopg")
    return create_engine(url, pool_pre_ping=True)


def resolve_tenant_with_session(
    session: Session,
    *,
    provider: str,
    instance: str,
) -> str | None:
    """Return only enabled trusted mappings; let infrastructure errors propagate."""
    return session.execute(
        _RESOLVE_TENANT_SQL,
        {"provider": provider, "instance": instance, "enabled": True},
    ).scalar_one_or_none()


def resolve_tenant(
    *,
    provider: str,
    instance: str,
    database_url: str | None = None,
) -> str | None:
    """Resolve a tenant synchronously on the webhook request path."""
    resolved_url = database_url or get_settings().DATABASE_URL
    with Session(get_registry_engine(resolved_url)) as session:
        return resolve_tenant_with_session(
            session,
            provider=provider,
            instance=instance,
        )


_TENANT_DISABLED_SQL = text(
    """
    /* registry.resolve_disabled */
    SELECT tenant_id
    FROM genius_next.tenant_registry
    WHERE provider = :provider
      AND instance = :instance
      AND enabled = false
    """
)


def tenant_disabled_with_session(
    session: Session,
    *,
    provider: str,
    instance: str,
) -> bool:
    """Mapeamento existe mas esta com ``enabled=false``? Somente leitura."""
    return (
        session.execute(
            _TENANT_DISABLED_SQL,
            {"provider": provider, "instance": instance},
        ).scalar_one_or_none()
        is not None
    )


def is_tenant_disabled(
    *,
    provider: str,
    instance: str,
    database_url: str | None = None,
) -> bool:
    """Distingue "mapeamento desabilitado" de "mapeamento inexistente".

    Consulta separada do ``resolve_tenant`` (que so devolve linha enabled) para
    que o ingresso responda 202/ignored ao tenant desabilitado sem virar oraculo
    de membership para quem nao autenticou -- a autenticacao do webhook roda
    antes, em ``_process_webhook``/``_process_zernio_webhook``.
    """
    resolved_url = database_url or get_settings().DATABASE_URL
    with Session(get_registry_engine(resolved_url)) as session:
        return tenant_disabled_with_session(
            session,
            provider=provider,
            instance=instance,
        )


def manager_jid_with_session(
    session: Session,
    *,
    tenant_id: str,
) -> str | None:
    """Return the team-route JID (company manager) for an enabled tenant."""
    return session.execute(
        _MANAGER_JID_SQL,
        {"tenant_id": tenant_id},
    ).scalar_one_or_none()


@transaction(isolation_level="SERIALIZABLE")
def manager_jid_txn(tenant_id: str) -> str | None:
    """Team-route destination for one turn."""
    return manager_jid_with_session(sql_session(), tenant_id=tenant_id)


# Self-serve registration (22/set/2026): the webapp already knows
# (provider, instance, tenant_id) synchronously the moment it creates a
# provider instance for an organization -- no need to wait for a connection
# webhook. This closes the gap where a self-serve WhatsApp connect never
# created the tenant_registry row, so the number "connected" in the webapp
# UI but the webhook 401'd forever (no linking mapping existed).
#
# ``ON CONFLICT ... WHERE tenant_id = EXCLUDED.tenant_id`` is the whole
# safety property: an existing row for the same (provider, instance) owned
# by a DIFFERENT tenant is never touched, enabled or tenant_id included --
# same isolation guarantee the PRIMARY KEY already gives at rest, now also
# enforced against a hostile/buggy caller trying to reassign it.
_UPSERT_TENANT_REGISTRY_SQL = text(
    """
    /* registry.upsert_self_serve */
    INSERT INTO genius_next.tenant_registry
        (provider, instance, tenant_id, enabled, manager_jid)
    VALUES
        (:provider, :instance, :tenant_id, false, :manager_jid)
    ON CONFLICT (provider, instance) DO UPDATE
    SET manager_jid = COALESCE(
            EXCLUDED.manager_jid, genius_next.tenant_registry.manager_jid
        ),
        updated_at = now()
    WHERE genius_next.tenant_registry.tenant_id = EXCLUDED.tenant_id
    RETURNING enabled, (xmax = 0) AS was_insert
    """
)


def upsert_tenant_registry_with_session(
    session: Session,
    request: TenantRegistryUpsertRequest,
) -> TenantRegistryUpsertResult:
    row = session.execute(
        _UPSERT_TENANT_REGISTRY_SQL,
        {
            "provider": request.provider,
            "instance": request.instance,
            "tenant_id": request.tenant_id,
            "manager_jid": request.manager_jid,
        },
    ).mappings().one_or_none()
    if row is None:
        # Conflict existed but WHERE excluded it: (provider, instance) is
        # already claimed by a different tenant_id. Never overwritten.
        return TenantRegistryUpsertResult(
            outcome="instance_owned_by_other_tenant", enabled=False
        )
    outcome = "created" if row["was_insert"] else "unchanged"
    return TenantRegistryUpsertResult(outcome=outcome, enabled=row["enabled"])


@transaction(isolation_level="SERIALIZABLE")
def upsert_tenant_registry_txn(
    request: TenantRegistryUpsertRequest,
) -> TenantRegistryUpsertResult:
    """Self-serve (provider, instance) -> tenant link upsert."""
    return upsert_tenant_registry_with_session(sql_session(), request)


_LOCK_REGISTRY_ROW_SQL = text(
    """
    /* registry.lock_row */
    SELECT tenant_id, enabled
    FROM genius_next.tenant_registry
    WHERE provider = :provider
      AND instance = :instance
    FOR UPDATE
    """
)

_SET_ENABLED_SQL = text(
    """
    /* registry.set_enabled */
    UPDATE genius_next.tenant_registry
    SET enabled = :enabled, updated_at = now()
    WHERE provider = :provider
      AND instance = :instance
      AND tenant_id = :tenant_id
    """
)


def set_tenant_enabled_with_session(
    session: Session,
    request: TenantRegistrySetEnabledRequest,
    *,
    enabled: bool,
) -> TenantRegistrySetEnabledResult:
    """Flip ``enabled`` of ONE row, only for the tenant that owns it.

    Never creates a row and never reassigns ``tenant_id``: a missing row is
    ``not_found`` and a row owned by another tenant is left untouched.
    """
    row = session.execute(
        _LOCK_REGISTRY_ROW_SQL,
        {"provider": request.provider, "instance": request.instance},
    ).mappings().one_or_none()
    if row is None:
        return TenantRegistrySetEnabledResult(outcome="not_found", enabled=False)
    if row["tenant_id"] != request.tenant_id:
        return TenantRegistrySetEnabledResult(
            outcome="instance_owned_by_other_tenant", enabled=False
        )
    if row["enabled"] == enabled:
        return TenantRegistrySetEnabledResult(outcome="unchanged", enabled=enabled)
    session.execute(
        _SET_ENABLED_SQL,
        {
            "provider": request.provider,
            "instance": request.instance,
            "tenant_id": request.tenant_id,
            "enabled": enabled,
        },
    )
    return TenantRegistrySetEnabledResult(
        outcome="enabled" if enabled else "disabled", enabled=enabled
    )


@transaction(isolation_level="SERIALIZABLE")
def set_tenant_enabled_txn(
    request: TenantRegistrySetEnabledRequest, enabled: bool
) -> TenantRegistrySetEnabledResult:
    """Enable/disable a (provider, instance) mapping for its owning tenant."""
    return set_tenant_enabled_with_session(sql_session(), request, enabled=enabled)


def knowledge_strategy_with_session(
    session: Session,
    *,
    tenant_id: str,
) -> str:
    """Como o conhecimento chega ao modelo neste tenant.

    ``inline`` cola o catalogo no prompt (uma chamada ao modelo);
    ``ferramenta`` mantem ``abrir()`` (uma chamada a mais por consulta);
    ``selecao`` fica para catalogo grande.  Tenant sem linha, ou banco fora do
    ar, cai em ``ferramenta``: e o comportamento antigo, que sempre funcionou.
    """
    try:
        valor = session.execute(
            _KNOWLEDGE_STRATEGY_SQL, {"tenant_id": tenant_id}
        ).scalar_one_or_none()
    except Exception:
        return "ferramenta"
    return str(valor) if valor else "ferramenta"


_WEB_SEARCH_SQL = text(
    """
    /* registry.web_search */
    SELECT web_search
    FROM genius_next.tenant_registry
    WHERE tenant_id = :tenant_id
    LIMIT 1
    """
)


def web_search_with_session(
    session: Session,
    *,
    tenant_id: str,
) -> bool:
    """True quando o tenant tem a tool ``pesquisar_web`` (Exa) liberada.

    Sinal de opt-in por tenant: cada busca Exa custa ~3k tokens de entrada +
    ida ao provedor, então só rende para perguntas que exigem dado externo.
    Falha no acesso ao registry cai em ``False`` (mesmo do
    ``knowledge_strategy_with_session``), nunca derruba o turno.
    """
    try:
        valor = session.execute(
            _WEB_SEARCH_SQL, {"tenant_id": tenant_id}
        ).scalar_one_or_none()
    except Exception:
        return False
    return bool(valor)
