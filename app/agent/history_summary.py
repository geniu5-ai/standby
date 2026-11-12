"""First-contact Evolution history deep-fetch + best-effort summarization.

Genius Next only began ingesting a tenant's real messages when its webhook was
wired up, so any customer who already conversed (via WhatsApp/Evolution or the
legacy n8n) has months of history in the Evolution API but zero context in
genius_next.clients.  On the first turn for such a customer (``is_new_client``
in the persist result) this module paginates the full pre-existing history and
reduces it to a short factual summary that is written into
``clients.metadata.summary`` and injected as mandatory context into every
future reply.

Two hard constraints inherited from the rest of the pipeline:
  * Same HTTP/endpoint/api-key pattern as ``fetch_history`` (no new provider),
    and the SAME OmniRoute endpoint/model (``OMNIROUTE_*``) used by the rest of the pipeline.
  * Best-effort only: a fetch/summarize failure returns ``None`` and must never
    derail the enclosing conversation turn.
"""

from __future__ import annotations

import json

import logging

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.settings import Settings, get_settings

logger = logging.getLogger(__name__)

_EXTERNAL = ConfigDict(extra="ignore", populate_by_name=True, str_strip_whitespace=True)


class _HistoryKey(BaseModel):
    model_config = _EXTERNAL

    id: str = Field(min_length=1, max_length=512)
    remote_jid: str = Field(alias="remoteJid", min_length=1, max_length=512)
    from_me: bool = Field(alias="fromMe")


class _HistoryText(BaseModel):
    model_config = _EXTERNAL

    text: str | None = None


class _HistoryCaption(BaseModel):
    model_config = _EXTERNAL

    caption: str | None = None


class _HistoryBody(BaseModel):
    model_config = _EXTERNAL

    conversation: str | None = None
    extended_text: _HistoryText | None = Field(
        default=None, alias="extendedTextMessage"
    )
    image: _HistoryCaption | None = Field(default=None, alias="imageMessage")
    video: _HistoryCaption | None = Field(default=None, alias="videoMessage")

    @property
    def text_content(self) -> str | None:
        candidates = (
            self.conversation,
            self.extended_text.text if self.extended_text else None,
            self.image.caption if self.image else None,
            self.video.caption if self.video else None,
        )
        return next((candidate for candidate in candidates if candidate), None)


class _HistoryRecord(BaseModel):
    model_config = _EXTERNAL

    key: _HistoryKey
    message: _HistoryBody


class _HistoryRecords(BaseModel):
    model_config = _EXTERNAL

    records: list[_HistoryRecord]


class _HistoryResponse(BaseModel):
    model_config = _EXTERNAL

    messages: _HistoryRecords


class _ProviderMessage(BaseModel):
    model_config = _EXTERNAL

    content: str = Field(min_length=1)


class _ProviderChoice(BaseModel):
    model_config = _EXTERNAL

    finish_reason: str
    message: _ProviderMessage


class _ProviderResponse(BaseModel):
    model_config = _EXTERNAL

    choices: list[_ProviderChoice] = Field(min_length=1)


def _secret_value(secret: SecretStr | None) -> str:
    if secret is None:
        raise ValueError("Evolution API key is not configured")
    return secret.get_secret_value()


def _secret_value(secret: SecretStr | None) -> str:
    if secret is None:
        raise ValueError("OmniRoute API key is not configured")
    return secret.get_secret_value()


_SUMMARIZER_SYSTEM = json.dumps(
    {
        "role": "Conversation history summarizer",
        "task": (
            "Read this WhatsApp conversation history and write a short factual "
            "summary for a sales agent picking up this conversation for the "
            "first time."
        ),
        "rules": [
            "3-6 sentences, plain text, no markdown",
            (
                "Include: what the customer was interested in, any commitments "
                "made (dates, prices discussed), objections raised, "
                "unresolved questions"
            ),
            (
                "If nothing substantive happened (only greetings/spam), say so "
                "briefly instead of inventing content"
            ),
        ],
    },
    ensure_ascii=False,
    separators=(",", ":"),
)


def fetch_full_history(
    tenant_id: str,
    instance: str,
    remote_jid: str,
    *,
    settings: Settings | None = None,
    client: httpx.Client | None = None,
) -> list[dict[str, str]]:
    """Paginado read-only deep-fetch of the pre-existing Evolution history.

    Repeats the same ``/chat/findMessages/{instance}`` request pattern as
    ``fetch_history`` (same URL, same apikey header, same where-clause) but
    walks ``page`` 1..N until a page comes back empty, a non-200 response is
    seen, or the ``HISTORY_SUMMARY_MAX_PAGES`` ceiling is reached — whichever
    comes first.  Since the API does not paginate by a stable cursor, the page
    ceiling bounds the pull even when history is unbounded.

    Each returned dict is ``{"role": "customer"|"assistant", "text": str}`` in
    chronological order (oldest first), deduplicated by provider message id and
    bounded by the same per-message / total character budgets used elsewhere.
    On any transport or parse error an empty list is returned (best-effort).
    """
    resolved = settings or get_settings()
    if resolved.EVOLUTION_URL is None:
        return []

    owned_client = client is None
    http = client or httpx.Client(timeout=resolved.HISTORY_TIMEOUT_SECONDS)
    try:
        selected: list[dict[str, str]] = []
        seen: set[str] = set()
        remaining = resolved.HISTORY_TOTAL_MAX_CHARS
        for page in range(1, resolved.HISTORY_SUMMARY_MAX_PAGES + 1):
            response = http.post(
                f"{resolved.EVOLUTION_URL}/chat/findMessages/{instance}",
                headers={"apikey": _secret_value(resolved.EVOLUTION_API_KEY)},
                json={
                    "where": {"key": {"remoteJid": remote_jid}},
                    "page": page,
                    "offset": 20,
                },
            )
            if response.status_code != 200:
                break
            if len(response.content) > resolved.HISTORY_RESPONSE_MAX_BYTES:
                break
            try:
                parsed = _HistoryResponse.model_validate(response.json())
            except Exception:
                logger.warning("history page parse failed", exc_info=True)
                break
            page_records = parsed.messages.records
            if not page_records:
                break
            for record in page_records:
                key = record.key
                if key.remote_jid != remote_jid or key.id in seen:
                    continue
                seen.add(key.id)
                content = record.message.text_content
                if content is None:
                    continue
                bounded = content.strip()[: resolved.HISTORY_ITEM_MAX_CHARS].strip()
                if not bounded:
                    continue
                bounded = bounded[: remaining].rstrip()
                if not bounded:
                    continue
                selected.append(
                    {
                        "role": "assistant" if key.from_me else "customer",
                        "text": bounded,
                    }
                )
                remaining -= len(bounded)
    finally:
        if owned_client:
            http.close()
    return selected


def summarize_history(
    tenant_id: str,
    *,
    settings: Settings,
    messages: list[dict[str, str]],
    client: httpx.Client | None = None,
) -> str | None:
    """Best-effort LLM summarization of the paginated full history.

    Uses the same OmniRoute endpoint/model/API key already used by
    ``_classify_customer_status`` (no new provider).  The prompt is dedicated
    to factual short summaries; the output is plain text, returned verbatim, so
    no extra JSON parsing is required before it is stored as a string inside
    the client context.  On any failure (non-200, invalid shape, transport
    error) returns ``None`` — curation must never derail a turn.
    """
    if not messages:
        return None
    owned_client = client is None
    http = client or httpx.Client(timeout=settings.REPLY_TIMEOUT_SECONDS)
    lines = "\n".join(
        f"[{'Cliente' if msg['role'] == 'customer' else 'Atendente'}] {msg['text']}"
        for msg in messages
    ) or "(empty)"
    try:
        response = http.post(
            f"{settings.OMNIROUTE_BASE_URL.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {_secret_value(settings.OMNIROUTE_API_KEY)}",
                "Content-Type": "application/json",
            },
            json={
                "model": settings.OMNIROUTE_MODEL,
                "thinking": {"type": "disabled"},
                "temperature": 0,
                "stream": False,
                "max_tokens": settings.OUTPUT_MAX_TOKENS,
                "messages": [
                    {"role": "system", "content": _SUMMARIZER_SYSTEM},
                    {"role": "user", "content": lines},
                ],
            },
        )
        if response.status_code != 200:
            return None
        if len(response.content) > settings.REPLY_RESPONSE_MAX_BYTES:
            return None
        provider = _ProviderResponse.model_validate(response.json())
        if len(provider.choices) != 1 or provider.choices[0].finish_reason != "stop":
            return None
        return provider.choices[0].message.content.strip() or None
    except Exception:
        logger.warning("history summary failed", exc_info=True)
        return None
    finally:
        if owned_client:
            http.close()


def summarize_history_step(
    tenant_id: str,
    phone: str,
    instance: str,
    remote_jid: str,
) -> str | None:
    """Checkpoint one first-contact history curation for a brand-new client.

    Deep-fetches the pre-existing Evolution history (paginated) for the
    conversation's tenant/instance/JID and reduces it to a short factual
    summary.  Returns ``None`` (no LLM spend) when there is no history at all
    or the fetch/summarize failed.  ``phone`` is carried for traceability; the
    summary is assigned to the (tenant_id, phone) client row downstream.
    """
    settings = get_settings()
    messages = fetch_full_history(
        tenant_id, instance, remote_jid, settings=settings
    )
    if not messages:
        return None
    return summarize_history(
        tenant_id, settings=settings, messages=messages
    )
