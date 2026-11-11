"""Authenticated Evolution and Zernio ingress: persist the event, then schedule the turn."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import importlib
import json
import logging
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response, status
from pydantic import ValidationError

from app.contracts import (
    ConversationControlRequest,
    EvolutionWebhook,
    TenantRegistryUpsertRequest,
    TrustedIngress,
)
from app import observability
from app.webapp.router import router as webapp_router
from app.durable.outbox import dispatch_outbox_batch_workflow
from app.durable.persistence import (
    ClientStateMissing,
    ConversationControlConflict,
    apply_conversation_control_txn,
)
from app.durable.zernio_conversations import upsert_zernio_conversation
from app.hmac_auth import (
    verify_control_signature,
    verify_signature,
    verify_zernio_signature,
)
from app.identity import build_trusted_ingress
from app.ingress.connector import SIGNATURE_HEADER, TIMESTAMP_HEADER, ConnectorEnvelope
from app.ingress.zernio import (
    SUPPORTED_EVENTS as ZERNIO_SUPPORTED_EVENTS,
    ZernioEventIgnored,
    ZernioWebhook,
    normalize_zernio_event,
)
from app.jobs import TASK_MODULES, jobs_worker
from app.metrics import WEBHOOK_LATENCY, render_metrics
from app.rate_limit import rate_limit_allow
from app.loop_guard import loop_guard_allow
from app.settings import Settings, get_settings
from app.tenant_registry import (
    is_tenant_disabled,
    resolve_tenant,
    upsert_tenant_registry_txn,
)


def _fire_ingress_presence(
    trusted: TrustedIngress, *, zernio_conversation_id: str | None = None
) -> None:
    """Mostra "digitando..." assim que a mensagem entra, antes de qualquer fila.

    Fica aqui, e não no ``ingest_event_workflow``, porque este é o primeiro
    ponto do processo que já sabe que a mensagem é do cliente: quanto antes o
    indicador aparece, mais humano fica o atendimento — a janela de debounce
    (8s) corria em silêncio total antes disso.

    Não custa tempo de resposta: ``fire_presence_indicator`` só cria uma thread
    daemon e retorna; a chamada HTTP acontece fora do caminho da requisição e
    nunca pode atrasar nem derrubar o turno.

    Os imports são tardios de propósito, mesmo motivo de ``_load_ingest_workflow``:
    ``app.durable.flow`` não pode ser importado no topo do módulo.

    Zernio (Cloud API): uma chamada so, depois de PRESENCE_PRE_DELAY_SECONDS,
    para o ``conversationId`` que o webhook acabou de gravar.  O indicador da
    Cloud API dura ate 25s e some quando a resposta chega, e a resposta nunca
    sai antes da janela de debounce (8s) mais o modelo, entao normalmente a
    chamada chega antes da resposta.  Excecao conhecida e aceita (a Evolution
    tem a mesma): uma segunda mensagem mandada nos ultimos ~5s antes do envio
    entra no mesmo turno, e o indicador dela pode aparecer depois que a
    resposta ja saiu, ate sumir sozinho.  Os demais gates (chave propria,
    mensagem trivial, allowlist com gemeo do nono digito, conversa
    automatizada) ficam em ``fire_zernio_typing_indicator``.
    """
    try:
        from app.durable.flow import _eligible_for_turn
        from app.durable.presence import (
            fire_presence_indicator,
            fire_zernio_typing_indicator,
        )

        provider = trusted.identity.provider
        if provider not in ("evolution", "zernio"):
            return
        if not _eligible_for_turn(trusted):
            return
        settings = get_settings()
        if provider == "zernio":
            fire_zernio_typing_indicator(
                tenant_id=trusted.tenant_id,
                phone=trusted.phone,
                account_id=trusted.identity.instance,
                conversation_id=zernio_conversation_id,
                remote_jid=trusted.identity.remote_jid,
                text_content=trusted.text_content,
                fire_after_seconds=settings.PRESENCE_PRE_DELAY_SECONDS,
            )
            return
        presenca = (
            "recording"
            if trusted.sanitized_payload.audio_base64 is not None
            else "composing"
        )
        # Blocos de digitacao com duracao propria cada, respiro entre eles,
        # em vez de um sopro so.  Cada bloco e uma thread daemon que dorme e
        # so entao chama a Evolution: a espera nunca acontece no caminho da
        # requisicao do webhook.
        espera = settings.PRESENCE_PRE_DELAY_SECONDS
        for duracao_seg in settings.presence_typing_seconds:
            fire_presence_indicator(
                instance=trusted.identity.instance,
                remote_jid=trusted.identity.remote_jid,
                presence=presenca,
                delay_ms=int(duracao_seg * 1000),
                fire_after_seconds=espera,
            )
            espera += duracao_seg + settings.PRESENCE_BURST_GAP_SECONDS
    except Exception:
        # O indicador e cosmetico: nenhuma falha aqui pode custar a mensagem do
        # cliente.  Antes desta guarda, um atributo faltando em settings
        # derrubava o webhook inteiro com 500 e a mensagem se perdia.
        logging.getLogger(__name__).warning(
            "presence on ingress failed", exc_info=True
        )


def _ingest(event: TrustedIngress) -> None:
    """Grava o evento e agenda o turno; so devolve depois do commit (o 202 vale como recibo)."""
    from app.durable.flow import ingest_event_workflow

    ingest_event_workflow(event)


def _webhook_is_authorized(request: Request, body: bytes, secret: str) -> bool:
    signature = request.headers.get("X-Hub-Signature-256")
    if verify_signature(body, signature, secret):
        return True

    token = request.headers.get("X-Evolution-Token")
    return token is not None and hmac.compare_digest(token, secret)


def _control_request_is_authorized(
    body: bytes,
    timestamp: str | None,
    signature: str | None,
    secret: str,
    *,
    now: int | None = None,
    max_age_seconds: int = 300,
) -> bool:
    return verify_control_signature(
        body, timestamp, signature, secret, now=now, max_age_seconds=max_age_seconds
    )


async def _read_limited_body(request: Request, *, max_bytes: int) -> bytes:
    """Bound request bytes using both declared length and streamed byte count."""
    raw_length = request.headers.get("content-length")
    declared_length: int | None = None
    if raw_length is not None:
        try:
            declared_length = int(raw_length)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid request") from exc
        if declared_length < 0:
            raise HTTPException(status_code=400, detail="Invalid request")
        if declared_length > max_bytes:
            raise HTTPException(status_code=413, detail="Request body too large")

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > max_bytes:
            raise HTTPException(status_code=413, detail="Request body too large")
        body.extend(chunk)

    if declared_length is not None and len(body) != declared_length:
        raise HTTPException(status_code=400, detail="Invalid request")
    return bytes(body)


def _configure_app_logging() -> None:
    """Send every ``app.*`` logger to stderr → journald.

    Uvicorn only configures its own loggers, and nothing here ever called
    ``basicConfig``.  The result was that every ``logger.warning`` in the
    critical path (LLM draft failure, outbox, rate limit) was written to a
    logger with no reachable handler, so production failures left no trace
    in ``journalctl`` — the reply pipeline could fall back to handoff and
    the only visible evidence was a row in the database.
    """
    app_logger = logging.getLogger("app")
    if app_logger.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(levelname)s [%(name)s] %(message)s")
    )
    app_logger.addHandler(handler)
    app_logger.setLevel(logging.INFO)
    app_logger.propagate = False


@asynccontextmanager
async def lifespan(_: FastAPI):
    _configure_app_logging()
    runtime_settings = get_settings()
    validate_integrations = getattr(
        runtime_settings, "validate_runtime_integrations", None
    )
    if validate_integrations is not None:
        validate_integrations()
    for module in TASK_MODULES:  # registra todas as tasks e relogios no Procrastinate
        importlib.import_module(module)
    from app.durable.crew_in_sweep import log_crew_release_config

    log_crew_release_config()  # janela x 1a tentativa do follow-up (08/out/2026)
    import app.reply.agent  # noqa: F401 — agente de redacao
    if runtime_settings.CONFIDENT_API_KEY:
        import deepeval
        deepeval.login(api_key=runtime_settings.CONFIDENT_API_KEY)
    import app.reply.tools.knowledge  # noqa: F401 — registra a tool abrir()
    async with jobs_worker():
        yield


observability.configure()  # Logfire: sem LOGFIRE_TOKEN nao envia nada
app = FastAPI(lifespan=lifespan)
observability.instrument(app)
app.include_router(webapp_router)


@app.post("/webhook/evolution", status_code=status.HTTP_202_ACCEPTED)
async def evolution_webhook(request: Request) -> dict[str, str]:
    started = time.perf_counter()
    try:
        return await _process_webhook(request)
    finally:
        WEBHOOK_LATENCY.observe(time.perf_counter() - started)


async def _process_webhook(request: Request) -> dict[str, str]:
    runtime_settings = get_settings()
    body = await _read_limited_body(
        request,
        max_bytes=runtime_settings.WEBHOOK_MAX_BODY_BYTES,
    )
    if not _webhook_is_authorized(request, body, runtime_settings.HMAC_SECRET):
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc

    try:
        webhook = EvolutionWebhook.model_validate(payload)
    except ValidationError as exc:
        # So a localizacao e o tipo de cada erro: nunca valores (o corpo traz
        # texto do cliente e, em midia, base64).
        logging.getLogger(__name__).warning(
            "evolution webhook rejected (422): %s",
            [
                (".".join(map(str, error["loc"])), error["type"])
                for error in exc.errors(include_input=False, include_url=False)
            ],
        )
        raise HTTPException(status_code=422, detail="Invalid webhook") from exc

    # geniusMedia e so do Zernio (montado pelo proprio Next): vindo de fora pela
    # rota da Evolution, e descartado antes de qualquer uso.
    if webhook.data.genius_media is not None:
        webhook = webhook.model_copy(
            update={"data": webhook.data.model_copy(update={"genius_media": None})}
        )

    return await _accept_ingress(
        webhook, provider="evolution", runtime_settings=runtime_settings
    )


async def _accept_ingress(
    webhook: EvolutionWebhook,
    *,
    provider: str,
    runtime_settings: Any,
    before_start: Callable[[TrustedIngress], None] | None = None,
    zernio_conversation_id: str | None = None,
) -> dict[str, str]:
    """Um span por mensagem na entrada (Evolution e Zernio): provider, tenant, event_id e resultado."""
    failure: HTTPException | None = None
    with observability.message_span("ingress.message", provider=provider) as span:
        try:
            result = await _accept_ingress_unspanned(
                webhook,
                provider=provider,
                runtime_settings=runtime_settings,
                before_start=before_start,
                zernio_conversation_id=zernio_conversation_id,
            )
        except HTTPException as exc:
            failure = exc
            observability.finish(
                span, resultado=f"http_{exc.status_code}", warn=exc.status_code >= 500
            )
        else:
            observability.finish(
                span, resultado=result.get("reason") or result.get("status") or "ok"
            )
    if failure is not None:  # fora do span: 401/429 de robo nao viram erro no Logfire
        raise failure
    return result


async def _accept_ingress_unspanned(
    webhook: EvolutionWebhook,
    *,
    provider: str,
    runtime_settings: Any,
    before_start: Callable[[TrustedIngress], None] | None = None,
    zernio_conversation_id: str | None = None,
) -> dict[str, str]:
    """Provider-agnostic tail of every authenticated, validated ingress webhook.

    Resolves the trusted tenant, builds the strict envelope, applies the rate
    and loop guards and starts the deterministic (idempotent) ingest workflow.
    ``before_start`` runs just before the workflow starts; a failure there is a
    retryable 503, so the provider redelivers instead of losing the message.
    """
    try:
        tenant_id = await asyncio.to_thread(
            resolve_tenant,
            provider=provider,
            instance=webhook.instance,
            database_url=runtime_settings.DATABASE_URL,
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Service unavailable") from exc
    if tenant_id is None:
        # 13/out/2026: mapeamento DESABILITADO nao e 401 -- o Zernio desliga o
        # webhook apos 10 falhas seguidas e o tenant ficava surdo sem log.
        # A resposta vaza so para quem ja autenticou (a assinatura Zernio/
        # Evolution roda antes, em _process_webhook/_process_zernio_webhook);
        # inexistente continua 401 indistinguivel de autenticacao ruim.
        try:
            desabilitado = await asyncio.to_thread(
                is_tenant_disabled,
                provider=provider,
                instance=webhook.instance,
                database_url=runtime_settings.DATABASE_URL,
            )
        except Exception:
            # Registry inacessivel: nao vira oraculo nem 503 -- mantem o 401
            # de sempre (o resolve_tenant ja teria derrubado com 503 se o
            # banco estivesse fora de verdade).
            logging.getLogger(__name__).warning(
                "tenant disabled check failed (provider=%s)", provider, exc_info=True
            )
            desabilitado = False
        if desabilitado:
            logging.getLogger(__name__).info(
                "webhook ignorado: tenant desabilitado (provider=%s instance=%s)",
                provider,
                webhook.instance,
            )
            return {"status": "ignored", "reason": "tenant_disabled"}
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        trusted = build_trusted_ingress(webhook, tenant_id=tenant_id, provider=provider)
    except (ValidationError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="Invalid webhook") from exc
    observability.annotate(
        tenant_id=trusted.tenant_id,
        event_id=trusted.ingest_workflow_id,
        conversation_id=trusted.conversation_id,
        direction=trusted.direction,
        contact=trusted.phone,
    )

    # Genius Shield fase 1: durable token-bucket rate limit per (tenant, phone).
    # Denied requests are dropped silently — a plain 429, no structured detail
    # (bucket state, limit, refill) is ever leaked back to the sender.
    try:
        allowed = await asyncio.to_thread(
            rate_limit_allow,
            tenant_id=trusted.tenant_id,
            phone=trusted.phone,
            settings=runtime_settings,
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Service unavailable") from exc
    if not allowed:
        raise HTTPException(status_code=429, detail="Too many requests")

    # Loop guard (28-29/set/2026): repeticao de conteudo + resposta rapida
    # = bot do outro lado, nao humano. So roda pra inbound com texto --
    # bloqueio e' sempre silencioso (200), nunca vaza detalhe de guarda.
    if trusted.direction == "inbound" and trusted.text_content:
        try:
            guard_decision = await asyncio.to_thread(
                loop_guard_allow,
                tenant_id=trusted.tenant_id,
                phone=trusted.phone,
                msg_text=trusted.text_content,
                settings=runtime_settings,
            )
        except Exception:
            guard_decision = None  # falha do guard nunca derruba o webhook
        if guard_decision is not None and not guard_decision.should_process:
            return {"status": "ok"}

    if before_start is not None:
        try:
            await asyncio.to_thread(before_start, trusted)
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "ingress pre-start hook failed (provider=%s)", provider, exc_info=True
            )
            raise HTTPException(status_code=503, detail="Service unavailable") from exc

    try:
        await asyncio.to_thread(_ingest, trusted)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Service unavailable") from exc

    # Depois do ingest aceito: um 503 aqui faz o provedor reentregar, e o
    # indicador (no Zernio, tambem o tique azul) nao pode sair por mensagem que
    # ainda nao entrou.  A espera de PRESENCE_PRE_DELAY_SECONDS cobre a latencia.
    _fire_ingress_presence(trusted, zernio_conversation_id=zernio_conversation_id)

    return {
        "status": "accepted",
        "workflow_id": trusted.ingest_workflow_id,
        "conversation_id": trusted.conversation_id,
    }


@app.post("/webhook/connector", status_code=status.HTTP_202_ACCEPTED)
async def connector_webhook(request: Request) -> dict[str, str]:
    started = time.perf_counter()
    try:
        return await _process_connector_webhook(request)
    finally:
        WEBHOOK_LATENCY.observe(time.perf_counter() - started)


async def _process_connector_webhook(request: Request) -> dict[str, str]:
    """Conector externo (OpenWA, waxum): envelope assinado com segredo PROPRIO, depois o fluxo comum.

    A assinatura (``timestamp.corpo``, janela de ``CONTROL_HMAC_MAX_AGE_SECONDS``) usa
    ``CONNECTOR_HMAC_SECRET``; o ``HMAC_SECRET`` da Evolution nao autentica aqui e este segredo nao
    autentica na rota da Evolution. O evento vai ao fluxo comum como ``evolution`` (o contrato
    confiavel e o da Evolution), e o tenant vem do registro, nunca do envelope.
    """
    runtime_settings = get_settings()
    secret_setting = runtime_settings.CONNECTOR_HMAC_SECRET
    secret = secret_setting.get_secret_value() if secret_setting is not None else ""
    if not secret:
        raise HTTPException(status_code=503, detail="Service unavailable")
    body = await _read_limited_body(
        request, max_bytes=runtime_settings.WEBHOOK_MAX_BODY_BYTES
    )
    if not verify_control_signature(
        body,
        request.headers.get(TIMESTAMP_HEADER),
        request.headers.get(SIGNATURE_HEADER),
        secret,
        max_age_seconds=runtime_settings.CONTROL_HMAC_MAX_AGE_SECONDS,
    ):
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc
    try:
        envelope = ConnectorEnvelope.model_validate(payload)
        webhook = EvolutionWebhook.model_validate(envelope.evolution)
    except ValidationError as exc:
        logging.getLogger(__name__).warning(
            "connector webhook rejected (422): %s",
            [
                (".".join(map(str, error["loc"])), error["type"])
                for error in exc.errors(include_input=False, include_url=False)
            ],
        )
        raise HTTPException(status_code=422, detail="Invalid webhook") from exc

    # geniusMedia e so do Zernio: vindo de fora, e descartado antes de qualquer uso.
    if webhook.data.genius_media is not None:
        webhook = webhook.model_copy(
            update={"data": webhook.data.model_copy(update={"genius_media": None})}
        )

    return await _accept_ingress(
        webhook, provider="evolution", runtime_settings=runtime_settings
    )


@app.post("/webhook/zernio", status_code=status.HTTP_202_ACCEPTED)
async def zernio_webhook(request: Request) -> dict[str, str]:
    started = time.perf_counter()
    try:
        return await _process_zernio_webhook(request)
    finally:
        WEBHOOK_LATENCY.observe(time.perf_counter() - started)


async def _process_zernio_webhook(request: Request) -> dict[str, str]:
    """Zernio inbox webhook: HMAC first, then filter, normalise, shared ingress."""
    runtime_settings = get_settings()
    secret_setting = runtime_settings.ZERNIO_WEBHOOK_SECRET
    secret = secret_setting.get_secret_value() if secret_setting is not None else ""
    if not secret:
        raise HTTPException(status_code=503, detail="Service unavailable")
    body = await _read_limited_body(
        request, max_bytes=runtime_settings.WEBHOOK_MAX_BODY_BYTES
    )
    if not verify_zernio_signature(
        body, request.headers.get("X-Zernio-Signature"), secret
    ):
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Invalid webhook")

    if payload.get("event") not in ZERNIO_SUPPORTED_EVENTS:
        return {"status": "ignored", "reason": "unsupported_event"}

    try:
        zernio_event = ZernioWebhook.model_validate(payload)
        ingress = normalize_zernio_event(zernio_event)
    except ZernioEventIgnored as ignored:
        logging.getLogger(__name__).info(
            "zernio event ignored: event=%s reason=%s", payload.get("event"), ignored.reason
        )
        return {"status": "ignored", "reason": ignored.reason}
    except (ValidationError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="Invalid webhook") from exc

    def remember_conversation(trusted: TrustedIngress) -> None:
        upsert_zernio_conversation(
            tenant_id=trusted.tenant_id,
            instance=trusted.identity.instance,
            remote_jid=trusted.identity.remote_jid,
            conversation_id=ingress.zernio_conversation_id,
            database_url=runtime_settings.DATABASE_URL,
        )

    return await _accept_ingress(
        ingress.webhook,
        provider="zernio",
        runtime_settings=runtime_settings,
        before_start=remember_conversation,
        zernio_conversation_id=ingress.zernio_conversation_id,
    )


@app.post("/v1/control/conversations")
async def control_conversation(request: Request) -> dict[str, str]:
    """Apply a fail-closed, replay-bounded operator conversation control."""
    runtime_settings = get_settings()
    secret_setting = runtime_settings.CONTROL_HMAC_SECRET
    if secret_setting is None:
        raise HTTPException(status_code=503, detail="Service unavailable")
    secret = secret_setting.get_secret_value()
    body = await _read_limited_body(
        request, max_bytes=runtime_settings.WEBHOOK_MAX_BODY_BYTES
    )
    if not _control_request_is_authorized(
        body,
        request.headers.get("X-Control-Timestamp"),
        request.headers.get("X-Control-Signature-256"),
        secret,
        max_age_seconds=runtime_settings.CONTROL_HMAC_MAX_AGE_SECONDS,
    ):
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        control = ConversationControlRequest.model_validate_json(body)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="Invalid control") from exc
    try:
        result, disclosure = await asyncio.to_thread(
            apply_conversation_control_txn,
            control,
            hashlib.sha256(body).hexdigest(),
        )
    except ConversationControlConflict as exc:
        raise HTTPException(status_code=409, detail="Control conflict") from exc
    except ClientStateMissing as exc:
        raise HTTPException(status_code=404, detail="Conversation not found") from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Service unavailable") from exc
    if disclosure is not None:
        try:
            await asyncio.to_thread(dispatch_outbox_batch_workflow, [disclosure])
        except Exception:
            logging.getLogger("app").warning(
                "crew_in disclosure dispatch failed for %s/%s",
                control.tenant_id,
                control.phone,
                exc_info=True,
            )
    return {"status": result.outcome, "automation_state": result.automation_state}


@app.post("/v1/control/wake")
async def control_wake(request: Request) -> dict[str, Any]:
    """Acorda o servico (a propria chamada ja o acorda) e devolve o que esta esperando.

    Chamado pelo Actions (``scripts/wake_check.py``) quando ha trabalho vencido. Ao subir,
    o Procrastinate retoma sozinho jobs vencidos, travados e o ultimo tick de cada relogio;
    aqui so se confirma que o worker esta de pe e se mostra a fila. Somente leitura.
    """
    runtime_settings = get_settings()
    secret_setting = runtime_settings.CONTROL_HMAC_SECRET
    if secret_setting is None:
        raise HTTPException(status_code=503, detail="Service unavailable")
    body = await _read_limited_body(
        request, max_bytes=runtime_settings.WEBHOOK_MAX_BODY_BYTES
    )
    if not _control_request_is_authorized(
        body,
        request.headers.get("X-Control-Timestamp"),
        request.headers.get("X-Control-Signature-256"),
        secret_setting.get_secret_value(),
        max_age_seconds=runtime_settings.CONTROL_HMAC_MAX_AGE_SECONDS,
    ):
        raise HTTPException(status_code=401, detail="Unauthorized")

    return await _estado_do_wake(runtime_settings, origem="hmac")


async def _estado_do_wake(runtime_settings: Settings, *, origem: str) -> dict[str, Any]:
    """Miolo comum das rotas de wake: le o que esta esperando (somente leitura)."""
    from app import wake

    try:
        estado = (
            await asyncio.to_thread(
                wake.consultar,
                runtime_settings.DATABASE_URL,
                tenants=wake.parse_tenants(runtime_settings.MANAGER_PENDENTES_TENANTS),
            )
        ).as_dict()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Service unavailable") from exc
    logging.getLogger("app").info("wake (%s): %s", origem, estado)
    return {"status": "awake", **estado}


@app.post("/v1/control/qstash-wake")
async def control_qstash_wake(request: Request) -> dict[str, Any]:
    """Wake agendado pelo QStash: a chamada ja acorda o servico, a rota confirma e mostra a fila.

    Autentica pela assinatura da Upstash (``Upstash-Signature``, JWT HS256 sobre o corpo), nao
    pelo HMAC de controle. Mesma resposta de ``/v1/control/wake``. Somente leitura.
    """
    from app.qstash_auth import HEADER, assinatura_valida

    runtime_settings = get_settings()
    current = runtime_settings.QSTASH_CURRENT_SIGNING_KEY
    if current is None:
        raise HTTPException(status_code=503, detail="Service unavailable")
    proxima = runtime_settings.QSTASH_NEXT_SIGNING_KEY or current
    body = await _read_limited_body(
        request, max_bytes=runtime_settings.WEBHOOK_MAX_BODY_BYTES
    )
    if not assinatura_valida(
        body,
        request.headers.get(HEADER),
        current_key=current.get_secret_value(),
        next_key=proxima.get_secret_value(),
        url=runtime_settings.QSTASH_WAKE_URL or None,
    ):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return await _estado_do_wake(runtime_settings, origem="qstash")


@app.post("/v1/control/tenant_registry")
async def control_tenant_registry(request: Request) -> dict[str, str | bool]:
    """Authenticated self-serve link of a provider instance to a tenant.

    Closes the gap where a self-serve connect (e.g. Genius-App creating a new
    Evolution instance) never created the ``tenant_registry`` row, so the
    provider showed "connected" while the webhook 401'd forever. Always lands
    ``enabled=false`` -- going live stays the separate, human-authorized step
    in ``docs/runbooks/RUNBOOK-ATIVACAO-TENANT.md`` passo 7.
    """
    runtime_settings = get_settings()
    secret_setting = runtime_settings.CONTROL_HMAC_SECRET
    if secret_setting is None:
        raise HTTPException(status_code=503, detail="Service unavailable")
    secret = secret_setting.get_secret_value()
    body = await _read_limited_body(
        request, max_bytes=runtime_settings.WEBHOOK_MAX_BODY_BYTES
    )
    if not _control_request_is_authorized(
        body,
        request.headers.get("X-Control-Timestamp"),
        request.headers.get("X-Control-Signature-256"),
        secret,
        max_age_seconds=runtime_settings.CONTROL_HMAC_MAX_AGE_SECONDS,
    ):
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        payload = TenantRegistryUpsertRequest.model_validate_json(body)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="Invalid request") from exc
    try:
        result = await asyncio.to_thread(upsert_tenant_registry_txn, payload)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Service unavailable") from exc
    return {"outcome": result.outcome, "enabled": result.enabled}


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/metrics")
def metrics() -> Response:
    """Read-only Prometheus exposition — Genius Observe phase 1.

    Scrape target for Prometheus / SigNoz (self-hosted, doc PG #951) / curl.
    DB-backed gauges refresh at scrape time; the endpoint never writes.
    """
    return Response(
        content=render_metrics(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
