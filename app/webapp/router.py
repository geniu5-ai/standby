"""Webapp read-only endpoints.

Auth: same timestamp-prefixed HMAC as /v1/control/conversations.
Headers required:
  X-Control-Timestamp: unix timestamp (int string)
  X-Control-Signature-256: sha256=HMAC(secret, timestamp + "." + body)

For GET requests body is b"" (empty). For POST /send the signed body is the raw
request body, exactly as sent.
"""

from __future__ import annotations

import asyncio
import logging
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.durable.webapp_send import WebappSendRequest, perform_send
from app.hmac_auth import verify_control_signature
from app.settings import get_settings
from app.webapp.db import execute_write, fetch_all

router = APIRouter(prefix="/v1/webapp")
logger = logging.getLogger(__name__)

SEND_MAX_BODY_BYTES = 32_768
CATALOG_MAX_ITEMS = 200
CATALOG_CONTENT_MAX_CHARS = 20_000
# Ordem fixa das categorias do catalogo; as demais vem depois, em ordem alfabetica.
CATALOG_CATEGORY_ORDER = ("empresa", "especialista", "exame", "vacina", "promo", "politica")
HIDE_MAX_EVENTS = 100


def _verify(request: Request, body: bytes = b"") -> None:
    settings = get_settings()
    secret_setting = settings.CONTROL_HMAC_SECRET
    if secret_setting is None:
        raise HTTPException(status_code=503, detail="webapp api not configured")
    ok = verify_control_signature(
        body,
        request.headers.get("X-Control-Timestamp"),
        request.headers.get("X-Control-Signature-256"),
        secret_setting.get_secret_value(),
        max_age_seconds=settings.CONTROL_HMAC_MAX_AGE_SECONDS,
    )
    if not ok:
        raise HTTPException(status_code=401, detail="unauthorized")


async def _read_send_body(request: Request) -> bytes:
    """Read the raw body (the signature covers its exact bytes), bounded."""
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > SEND_MAX_BODY_BYTES:
                raise HTTPException(status_code=413, detail="request body too large")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid request") from exc
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > SEND_MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="request body too large")
    return bytes(body)


@router.post("/send")
async def send_message(request: Request) -> JSONResponse:
    """Operator reply to a Zernio contact (the Genius App "Assumir" chat).

    200 {"status":"sent","message_id"}; 404 no_zernio_route; 403
    destination_not_allowlisted; 409 no_conversation / delivery_not_live /
    send_id_reused / send_in_progress / <terminal Zernio reason>; 422
    invalid_request; 502 provider_unavailable; 503 on infrastructure failure.
    """
    settings = get_settings()
    if settings.CONTROL_HMAC_SECRET is None:
        raise HTTPException(status_code=503, detail="webapp api not configured")
    body = await _read_send_body(request)
    _verify(request, body)
    try:
        req = WebappSendRequest.model_validate_json(body)
    except ValidationError:
        return JSONResponse({"error": "invalid_request"}, status_code=422)
    try:
        result = await asyncio.to_thread(perform_send, req, settings=settings)
    except Exception:
        logger.error(
            "webapp send failed before dispatch: tenant=%s send_id=%s actor=%s",
            req.tenant_id, req.send_id, req.actor_id, exc_info=True,
        )
        raise HTTPException(status_code=503, detail="service unavailable") from None
    return JSONResponse(result.body, status_code=result.status_code)


@router.get("/conversations")
async def list_conversations(request: Request, tenant_id: str) -> list[dict]:
    _verify(request)
    rows = fetch_all(
        """
        SELECT phone, name, push_name, status, automation_state,
               last_seen, last_intent, tags
        FROM genius_next.clients
        WHERE tenant_id = :tenant_id
        ORDER BY last_seen DESC NULLS LAST
        LIMIT 100
        """,
        {"tenant_id": tenant_id},
    )
    for r in rows:
        if r.get("last_seen"):
            r["last_seen"] = r["last_seen"].isoformat()
        r["tags"] = list(r.get("tags") or [])
    return rows


@router.get("/conversations/messages")
async def get_messages(request: Request, tenant_id: str, phone: str) -> list[dict]:
    _verify(request)
    # As 200 mais RECENTES (em ordem cronologica), sem as que o operador apagou no inbox.
    rows = fetch_all(
        """
        SELECT id, direction, message_type, text_content, push_name, received_at
        FROM (
            SELECT e.id, e.direction, e.message_type, e.text_content, e.push_name, e.received_at
            FROM genius_next.events e
            WHERE e.tenant_id = :tenant_id AND e.phone = :phone
              AND NOT EXISTS (
                  SELECT 1 FROM genius_next.webapp_hidden_events h
                  WHERE h.tenant_id = e.tenant_id AND h.event_id = e.id
              )
            ORDER BY e.received_at DESC, e.id DESC
            LIMIT 200
        ) recent
        ORDER BY received_at ASC, id ASC
        """,
        {"tenant_id": tenant_id, "phone": phone},
    )
    for r in rows:
        if r.get("received_at"):
            r["received_at"] = r["received_at"].isoformat()
    return rows


class HideMessagesRequest(BaseModel):
    """Strict body of ``POST /v1/webapp/messages/hide``."""

    model_config = ConfigDict(extra="forbid", strict=True)

    tenant_id: str = Field(min_length=1, max_length=200)
    phone: str = Field(min_length=3, max_length=40)
    actor_id: str = Field(min_length=1, max_length=200)
    event_ids: list[int] = Field(min_length=1, max_length=HIDE_MAX_EVENTS)


_HIDE_SQL = """
    INSERT INTO genius_next.webapp_hidden_events (tenant_id, event_id, phone, hidden_by)
    SELECT e.tenant_id, e.id, e.phone, :actor_id
    FROM genius_next.events e
    WHERE e.tenant_id = :tenant_id AND e.phone = :phone AND e.id = ANY(:event_ids)
    ON CONFLICT (tenant_id, event_id) DO NOTHING
"""


@router.post("/messages/hide")
async def hide_messages(request: Request) -> JSONResponse:
    """Apagar mensagens no inbox do Genius App ("apagar para mim", como no WhatsApp).

    Nao apaga o evento nem some do WhatsApp do paciente (a Cloud API nao permite revogar):
    so esconde da lista do inbox. Reversivel: ``DELETE FROM genius_next.webapp_hidden_events``.
    """
    settings = get_settings()
    if settings.CONTROL_HMAC_SECRET is None:
        raise HTTPException(status_code=503, detail="webapp api not configured")
    body = await _read_send_body(request)
    _verify(request, body)
    try:
        req = HideMessagesRequest.model_validate_json(body)
    except ValidationError:
        return JSONResponse({"error": "invalid_request"}, status_code=422)
    if any(i <= 0 for i in req.event_ids):
        return JSONResponse({"error": "invalid_request"}, status_code=422)
    try:
        hidden = await asyncio.to_thread(
            execute_write,
            _HIDE_SQL,
            {
                "tenant_id": req.tenant_id,
                "phone": req.phone,
                "actor_id": req.actor_id,
                "event_ids": sorted(set(req.event_ids)),
            },
        )
    except Exception:
        logger.error("webapp hide failed: tenant=%s actor=%s", req.tenant_id, req.actor_id, exc_info=True)
        raise HTTPException(status_code=503, detail="service unavailable") from None
    logger.info(
        "webapp hide ok: tenant=%s phone_tail=%s hidden=%s actor=%s",
        req.tenant_id, req.phone[-4:], hidden, req.actor_id,
    )
    return JSONResponse({"status": "hidden", "hidden": hidden})


@router.get("/stats")
async def get_stats(request: Request, tenant_id: str) -> dict:
    _verify(request)
    today_start = time.strftime("%Y-%m-%d 00:00:00", time.localtime())

    clients = fetch_all(
        "SELECT status, automation_state FROM genius_next.clients WHERE tenant_id = :t",
        {"t": tenant_id},
    )
    turns = fetch_all(
        """
        SELECT status FROM genius_next.turns
        WHERE tenant_id = :t AND created_at >= :today
        """,
        {"t": tenant_id, "today": today_start},
    )
    controls = fetch_all(
        """
        SELECT control_id, actor_id, action, terminal_status, phone, created_at
        FROM genius_next.conversation_control_events
        WHERE tenant_id = :t
        ORDER BY created_at DESC
        LIMIT 20
        """,
        {"t": tenant_id},
    )

    bot_active = sum(1 for c in clients if c["automation_state"] in ("open", "automated"))
    human_owned = sum(1 for c in clients if c["automation_state"] in ("crew", "human_owned"))
    wins = sum(1 for c in clients if c["status"] == "win")
    losses = sum(1 for c in clients if c["status"] == "loss")
    closed = wins + losses
    win_rate = round((wins / closed) * 100, 1) if closed else 0.0

    activity = [
        {
            "id": r["control_id"],
            "actor": r["actor_id"],
            "action": r["action"],
            "target": r["phone"],
            "terminal_status": r["terminal_status"],
            "timestamp": r["created_at"].isoformat() if r.get("created_at") else None,
        }
        for r in controls
    ]

    return {
        "snapshot": {
            "botActive": bot_active,
            "humanOwned": human_owned,
            "winRate": win_rate,
            "turnsToday": len(turns),
            "turnsCompleted": sum(1 for t in turns if t["status"] == "completed"),
            "totalClients": len(clients),
        },
        "activity": activity,
    }


# Somente leitura. `persuasao:*` e texto de venda e nunca entra no catalogo.
_CATALOG_CATEGORIES_SQL = """
    /* webapp.catalog.categories */
    SELECT split_part(chave, ':', 1) AS id, count(*) AS total, max(updated_at) AS atualizado_em
    FROM genius_next.knowledge
    WHERE tenant_id = :tenant_id AND chave NOT LIKE 'persuasao:%'
    GROUP BY 1
"""

_CATALOG_ITEMS_SQL = """
    /* webapp.catalog.items */
    SELECT chave, split_part(chave, ':', 1) AS categoria, titulo,
           left(conteudo, :content_max) AS conteudo, updated_at
    FROM genius_next.knowledge
    WHERE tenant_id = :tenant_id AND chave NOT LIKE 'persuasao:%'
    ORDER BY array_position(CAST(:order AS text[]), split_part(chave, ':', 1)) NULLS LAST,
             split_part(chave, ':', 1), chave
    LIMIT :item_limit
"""


@router.get("/catalog")
async def get_catalog(request: Request, tenant_id: str) -> dict:
    """Catalogo da tenant (genius_next.knowledge sem persuasao:*), so leitura."""
    _verify(request)
    cats = fetch_all(_CATALOG_CATEGORIES_SQL, {"tenant_id": tenant_id})
    rank = {c: i for i, c in enumerate(CATALOG_CATEGORY_ORDER)}
    cats.sort(key=lambda c: (rank.get(c["id"], len(rank)), c["id"]))
    rows = fetch_all(
        _CATALOG_ITEMS_SQL,
        {
            "tenant_id": tenant_id,
            "content_max": CATALOG_CONTENT_MAX_CHARS,
            "item_limit": CATALOG_MAX_ITEMS,
            "order": list(CATALOG_CATEGORY_ORDER),
        },
    )
    for r in rows:
        r["updated_at"] = r["updated_at"].isoformat() if r.get("updated_at") else None
    stamps = [c["atualizado_em"] for c in cats if c.get("atualizado_em")]
    return {
        "tenant_id": tenant_id,
        "total": sum(int(c["total"]) for c in cats),
        "atualizado_em": max(stamps).isoformat() if stamps else None,
        "categorias": [{"id": c["id"], "total": int(c["total"])} for c in cats],
        "items": rows,
    }
