"""Observabilidade (Pydantic Logfire) do Genius Next.

Regras duras:
- NUNCA derruba o app: qualquer falha do Logfire vira um aviso no log e o fluxo segue.
- NUNCA leva texto de mensagem, prompt, resposta do modelo nem telefone completo.
  So ids, tenant, resultado, status e tempo. Telefone so mascarado (``***1234``).
- Sem token (``LOGFIRE_TOKEN``) nada sai da maquina (``send_to_logfire="if-token-present"``).
"""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import Iterator
from typing import Any

logger = logging.getLogger(__name__)

try:  # o app precisa subir mesmo sem a biblioteca
    import logfire
except Exception:  # pragma: no cover - ambiente sem logfire
    logfire = None  # type: ignore[assignment]

# Chaves que nunca viram atributo de span, mesmo se alguem passar por engano.
_BLOCKED_KEYS = frozenset(
    {"text", "body", "message", "content", "prompt", "response", "phone", "number", "jid", "destination_jid"}
)


def mask_phone(value: Any) -> str:
    """``5511987654321`` -> ``***4321``. Qualquer coisa sem digitos suficientes vira ``***``."""
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return f"***{digits[-4:]}" if len(digits) >= 4 else "***"


def _clean(attrs: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in attrs.items():
        if value is None:
            continue
        if key == "contact":  # unico caminho de telefone: sempre mascarado
            out["contact"] = mask_phone(value)
        elif key.lower() in _BLOCKED_KEYS:
            continue
        elif isinstance(value, (str, int, float, bool)):
            out[key] = value
        else:
            out[key] = str(value)[:120]
    return out


# Segunda barreira (alem de nao enviar): o scrubbing do Logfire apaga qualquer atributo cujo nome ou
# valor case com estes padroes. O padrao de telefone e ancorado para nao apagar ids em hexadecimal.
_SCRUB_PATTERNS = (
    r"telefone",
    r"phone",
    r"jid",
    r"whatsapp",
    r"texto",
    r"prompt",
    # Telefone: casa so o PREFIXO (1 a 3 digitos) de uma sequencia de 10 a 15 digitos. O Logfire guarda o trecho
    # casado em ``logfire.scrubbed``; casar o numero inteiro o gravaria ali. O valor inteiro e apagado igual.
    r"(?<![0-9A-Za-z])\+?\d{1,3}(?=\d{9,12}(?![0-9A-Za-z]))",
)


# pydantic-ai manda o prompt de sistema do tenant em model_request_parameters.instruction_parts[].content mesmo
# com include_content=False, e o scrubbing do Logfire so olha o nome do atributo de topo e nao casa objetos
# aninhados. Entao o atributo inteiro e removido do span antes da exportacao (por dentro, como o proprio
# Logfire faz com o scrubbing). Nada do que se perde e necessario: o uso de tokens fica em gen_ai.usage.*.
_DROP_ATTRS = frozenset({"model_request_parameters"})


def _remove_attrs_processor() -> Any:
    from opentelemetry.sdk.trace import SpanProcessor

    class _RemoveAttrs(SpanProcessor):
        def on_end(self, span: Any) -> None:  # noqa: D401
            attrs = getattr(span, "_attributes", None)
            if attrs and any(key in attrs for key in _DROP_ATTRS):
                span._attributes = {k: v for k, v in attrs.items() if k not in _DROP_ATTRS}

    return _RemoveAttrs()


def extra_span_processors() -> list[Any]:
    return [_remove_attrs_processor()]


def scrubbing_options() -> Any:
    return logfire.ScrubbingOptions(extra_patterns=list(_SCRUB_PATTERNS))


def configure() -> None:
    """Chamar no inicio de ``app/main.py``. Sem token, nao envia nada."""
    if logfire is None:
        return
    try:
        logfire.configure(
            service_name=os.environ.get("LOGFIRE_SERVICE_NAME") or "genius-next",
            environment=os.environ.get("LOGFIRE_ENVIRONMENT") or None,
            send_to_logfire="if-token-present",
            scrubbing=scrubbing_options(),
            additional_span_processors=extra_span_processors(),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("logfire: configure falhou (%s)", type(exc).__name__)


def _try(name: str, fn: Any) -> None:
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 - pacote de instrumentacao ausente nao pode derrubar o app
        logger.warning("logfire: instrumentacao %s indisponivel (%s)", name, type(exc).__name__)


def instrument(app: Any) -> None:
    """FastAPI, httpx, driver do Postgres e o agente (sem conteudo). Cada uma e opcional."""
    if logfire is None:
        return
    _try(
        "fastapi",
        lambda: logfire.instrument_fastapi(
            app,
            # /v1/webapp leva telefone na query (phone=): nao instrumentar.
            excluded_urls="/health,/metrics,/v1/webapp",
            # Os argumentos da rota (corpo, query) podem trazer texto de paciente: nao registrar.
            request_attributes_mapper=lambda request, attributes: {},
        ),
    )
    _try("httpx", lambda: logfire.instrument_httpx())
    # O Next usa psycopg (nao asyncpg): o equivalente do pedido e o instrumento do psycopg.
    _try("psycopg", lambda: logfire.instrument_psycopg())
    _try("pydantic_ai", lambda: logfire.instrument_pydantic_ai(include_content=False))
    _try("system_metrics", logfire.instrument_system_metrics)


class _NoopSpan:
    def set_attribute(self, *_: Any, **__: Any) -> None:
        return None

    def set_level(self, *_: Any, **__: Any) -> None:
        return None


@contextlib.contextmanager
def message_span(name: str, **attrs: Any) -> Iterator[Any]:
    """Span de uma etapa da mensagem. Se o Logfire falhar, entrega um span vazio."""
    cm: Any = None
    if logfire is not None:
        try:
            cm = logfire.span(name, **_clean(attrs))
        except Exception as exc:  # noqa: BLE001
            logger.warning("logfire: span %s falhou (%s)", name, type(exc).__name__)
    if cm is None:
        yield _NoopSpan()
        return
    with cm as span:
        yield span


def annotate(**attrs: Any) -> None:
    """Acrescenta atributos ao span corrente (sem efeito fora de um span)."""
    try:
        from opentelemetry import trace

        span = trace.get_current_span()
        for key, value in _clean(attrs).items():
            span.set_attribute(key, value)
    except Exception:  # noqa: BLE001
        return


def finish(span: Any, *, resultado: str, warn: bool = False, **attrs: Any) -> None:
    """Fecha o resultado de uma etapa; ``warn`` marca o span como aviso (falha de envio, 5xx)."""
    try:
        span.set_attribute("resultado", resultado)
        for key, value in _clean(attrs).items():
            span.set_attribute(key, value)
        if warn:
            span.set_attribute("falha", True)
            span.set_level("warn")
    except Exception:  # noqa: BLE001
        return


def record_delivery(span: Any, outcome: Any) -> None:
    """Resultado do envio (Evolution ou Zernio): status, motivo e codigo HTTP, nunca o corpo."""
    if outcome is None:
        finish(span, resultado="nao_reivindicado")
        return
    result = getattr(outcome, "provider_result", None) or {}
    status = getattr(outcome, "status", "desconhecido")
    finish(
        span,
        resultado=status,
        warn=status in {"failed", "uncertain"},
        motivo=result.get("reason"),
        status_code=result.get("status_code"),
        provider_message_id=getattr(outcome, "provider_message_id", None),
    )
