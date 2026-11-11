#!/usr/bin/env python3
"""wa-bridge: conector WhatsApp (OpenWA) -> Standby. So biblioteca padrao, um arquivo.

    gateway (OpenWA) --webhook assinado--> wa-bridge --envelope assinado--> Standby /webhook/connector

O que ele faz, na ordem:
  1. valida a assinatura do gateway (``X-OpenWA-Signature``, segredo do webhook do OpenWA);
  2. ignora o que nao e mensagem de texto recebida de uma pessoa (grupo, status, enviada por nos);
  3. resolve o LID do remetente para telefone pela API do proprio gateway (o app indexa por numero);
  4. normaliza para o evento ``messages.upsert`` da Evolution, dentro de um envelope ``v1``;
  5. grava o envelope numa fila SQLite (idempotente por ``event_id``) e responde 200 ao gateway;
  6. um worker entrega a fila ao Standby, assinada com o segredo PROPRIO do conector
     (``CONNECTOR_HMAC_SECRET``, nunca o ``HMAC_SECRET`` da Evolution), com retentativas
     (o Render free leva ~60 s para acordar) e fila de mortos.

Sem ``OUT_URL`` ele roda em modo seco: grava o envelope em ``out.jsonl`` e nao envia nada.
Configuracao so por ambiente (ver README). Nunca loga corpo de mensagem nem segredo.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

log = logging.getLogger("wa_bridge")

TIMESTAMP_HEADER = "X-Connector-Timestamp"
SIGNATURE_HEADER = "X-Connector-Signature"
BACKOFF_S = (5, 30, 120, 600, 3600, 21600)  # depois da 6a tentativa, 6 h entre elas
MAX_ATTEMPTS = 10
MAX_BODY = 1_000_000


class Ignored(Exception):
    """Evento que o conector descarta de proposito (nao e erro)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# --- assinatura -------------------------------------------------------------------------------


def verify_openwa_signature(body: bytes, header: str | None, secret: str) -> bool:
    """``X-OpenWA-Signature: sha256=<hex>`` = HMAC-SHA256 do corpo cru (sem carimbo de tempo)."""
    if not header or not secret:
        return False
    esperado = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header.strip(), esperado)


def sign_out(secret: str, body: bytes, timestamp: str) -> str:
    """Mesmo esquema ``timestamp.corpo`` dos controles do app, com o segredo do conector."""
    return "sha256=" + hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


# --- normalizacao -----------------------------------------------------------------------------


def digits(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


def normalize_openwa(
    payload: dict[str, Any],
    *,
    instance: str,
    resolve_phone: Callable[[str, str], str | None],
) -> tuple[str, dict[str, Any]]:
    """Evento do OpenWA -> (event_id, envelope v1). Levanta ``Ignored`` para o que nao entra."""
    if payload.get("event") != "message.received":
        raise Ignored("unsupported_event")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise Ignored("no_data")
    if data.get("fromMe"):
        raise Ignored("from_me")
    if data.get("isGroup") or data.get("isStatusBroadcast"):
        raise Ignored("group_or_status")
    if data.get("type") != "text":
        raise Ignored("unsupported_type")
    text = (data.get("body") or "").strip()
    if not text:
        raise Ignored("no_text")
    message_id = data.get("id")
    chat = data.get("chatId") or data.get("from")
    if not message_id or not isinstance(chat, str):
        raise Ignored("no_identity")

    if chat.endswith("@lid") or data.get("isLidSender"):
        phone = resolve_phone(str(payload.get("sessionId") or ""), chat)
        if not phone:
            raise Ignored("lid_unresolved")
    else:
        phone = digits(chat.split("@", 1)[0])
    if not phone:
        raise Ignored("no_phone")

    event_id = str(payload.get("idempotencyKey") or payload.get("deliveryId") or message_id)
    evolution_data: dict[str, Any] = {
        "key": {"id": str(message_id), "remoteJid": f"{phone}@s.whatsapp.net", "fromMe": False},
        "message": {"conversation": text},
        "messageType": "conversation",
    }
    push_name = (data.get("contact") or {}).get("pushName")
    if isinstance(push_name, str) and push_name.strip():
        evolution_data["pushName"] = push_name.strip()[:256]
    envelope = {
        "v": 1,
        "source": "openwa",
        "event_id": event_id[:256],
        "evolution": {"event": "messages.upsert", "instance": instance, "data": evolution_data},
    }
    return event_id[:256], envelope


# --- cliente do gateway (resolucao de LID) ----------------------------------------------------


class OpenWAClient:
    """Resolve LID -> telefone pela API do OpenWA (``GET /contacts/{lid}/phone``), com cache."""

    def __init__(self, base_url: str, api_key: str, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self._cache: dict[tuple[str, str], str] = {}
        self._lock = threading.Lock()

    def phone_of(self, session_id: str, lid: str) -> str | None:
        chave = (session_id, lid)
        with self._lock:
            if chave in self._cache:
                return self._cache[chave]
        url = f"{self.base_url}/api/sessions/{urllib.parse.quote(session_id)}/contacts/{urllib.parse.quote(lid, safe='@')}/phone"
        req = urllib.request.Request(url, headers={"X-API-Key": self.api_key})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                phone = digits(str(json.loads(resp.read().decode()).get("phone") or ""))
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
            log.warning("resolucao de LID falhou (%s)", type(exc).__name__)
            return None
        if phone:
            with self._lock:
                self._cache[chave] = phone
        return phone or None


# --- fila (SQLite) ----------------------------------------------------------------------------


class Spool:
    """Fila de saida durável. ``event_id`` unico: reentrega do gateway nao duplica."""

    def __init__(self, path: str) -> None:
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._lock = threading.Lock()
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS outbox ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL, body TEXT NOT NULL, "
            "state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL, "
            "last_status INTEGER, last_error TEXT, created_at REAL NOT NULL, done_at REAL)"
        )

    def put(self, event_id: str, body: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self._lock:
            try:
                self._db.execute(
                    "INSERT INTO outbox (event_id, body, next_at, created_at) VALUES (?, ?, ?, ?)",
                    (event_id, body, now, now),
                )
            except sqlite3.IntegrityError:
                return False
        return True

    def due(self, now: float, limit: int = 20) -> list[tuple[int, str, str, int]]:
        with self._lock:
            return list(
                self._db.execute(
                    "SELECT id, event_id, body, attempts FROM outbox WHERE state='pending' AND next_at<=? "
                    "ORDER BY id LIMIT ?",
                    (now, limit),
                )
            )

    def finish(self, row_id: int, state: str, status: int | None, error: str | None, now: float) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE outbox SET state=?, attempts=attempts+1, last_status=?, last_error=?, done_at=? WHERE id=?",
                (state, status, error, now, row_id),
            )

    def retry(self, row_id: int, status: int | None, error: str | None, next_at: float) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE outbox SET attempts=attempts+1, last_status=?, last_error=?, next_at=? WHERE id=?",
                (status, error, next_at, row_id),
            )

    def stats(self) -> dict[str, int]:
        with self._lock:
            rows = self._db.execute("SELECT state, count(*) FROM outbox GROUP BY state").fetchall()
        return {state: n for state, n in rows}


# --- entrega ----------------------------------------------------------------------------------


def classify(status: int | None) -> str:
    """'done' (2xx), 'retry' (rede, 5xx, 408/425/429, 401/403: pode ser segredo ainda errado) ou 'dead'."""
    if status is None:
        return "retry"
    if 200 <= status < 300:
        return "done"
    if status >= 500 or status in (401, 403, 408, 425, 429):
        return "retry"
    return "dead"


def http_post(url: str, body: bytes, headers: dict[str, str], timeout: float) -> int:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read(2048)
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


@dataclass
class Config:
    listen_host: str = "0.0.0.0"
    listen_port: int = 8800
    openwa_webhook_secret: str = ""
    openwa_api_url: str = ""
    openwa_api_key: str = ""
    sessions: dict[str, str] = field(default_factory=dict)  # sessionId -> instancia do Standby
    out_url: str = ""
    out_secret: str = ""
    out_timeout: float = 90.0
    data_dir: str = "/data"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Config:
        env = dict(os.environ) if env is None else env
        sessions = {}
        for par in filter(None, (p.strip() for p in env.get("OPENWA_SESSION_MAP", "").split(","))):
            session_id, _, instance = par.partition(":")
            if session_id and instance:
                sessions[session_id.strip()] = instance.strip()
        host, _, port = env.get("BRIDGE_LISTEN", "0.0.0.0:8800").rpartition(":")
        return cls(
            listen_host=host or "0.0.0.0",
            listen_port=int(port or 8800),
            openwa_webhook_secret=env.get("OPENWA_WEBHOOK_SECRET", ""),
            openwa_api_url=env.get("OPENWA_API_URL", ""),
            openwa_api_key=env.get("OPENWA_API_KEY", ""),
            sessions=sessions,
            out_url=env.get("OUT_URL", "").strip(),
            out_secret=env.get("OUT_SECRET", ""),
            out_timeout=float(env.get("OUT_TIMEOUT", "90")),
            data_dir=env.get("DATA_DIR", "/data"),
        )


def deliver_due(
    spool: Spool,
    cfg: Config,
    *,
    now: float | None = None,
    post: Callable[[str, bytes, dict[str, str], float], int] = http_post,
) -> int:
    """Entrega o que esta vencido. Devolve quantos itens processou. Modo seco sem ``out_url``."""
    now = time.time() if now is None else now
    feitos = 0
    for row_id, event_id, body, attempts in spool.due(now):
        feitos += 1
        if not cfg.out_url:
            with open(os.path.join(cfg.data_dir, "out.jsonl"), "a") as f:
                f.write(body + "\n")
            spool.finish(row_id, "done", None, "dry_run", now)
            continue
        raw = body.encode()
        ts = str(int(now))
        headers = {
            "Content-Type": "application/json",
            TIMESTAMP_HEADER: ts,
            SIGNATURE_HEADER: sign_out(cfg.out_secret, raw, ts),
        }
        try:
            status: int | None = post(cfg.out_url, raw, headers, cfg.out_timeout)
            erro = None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            status, erro = None, type(exc).__name__
        decisao = classify(status)
        if decisao == "done":
            spool.finish(row_id, "done", status, None, now)
        elif decisao == "dead" or attempts + 1 >= MAX_ATTEMPTS:
            spool.finish(row_id, "dead", status, erro, now)
            log.error("evento %s descartado (status=%s erro=%s)", event_id[:12], status, erro)
        else:
            espera = BACKOFF_S[min(attempts, len(BACKOFF_S) - 1)]
            spool.retry(row_id, status, erro, now + espera)
            log.warning("evento %s: nova tentativa em %ss (status=%s erro=%s)", event_id[:12], espera, status, erro)
    return feitos


# --- servidor ---------------------------------------------------------------------------------


def make_handler(cfg: Config, spool: Spool, client: OpenWAClient | None) -> type[BaseHTTPRequestHandler]:
    resolve = (lambda sid, lid: client.phone_of(sid, lid)) if client else (lambda sid, lid: None)

    class Handler(BaseHTTPRequestHandler):
        server_version = "wa-bridge/1"

        def log_message(self, *args: Any) -> None:  # sem log de acesso: nada de corpo nem caminho com dado
            pass

        def _reply(self, code: int, body: dict[str, Any]) -> None:
            raw = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            if self.path == "/health":
                self._reply(200, {"status": "ok", "fila": spool.stats(), "modo": "enviando" if cfg.out_url else "seco"})
            else:
                self._reply(404, {"error": "not_found"})

        def do_POST(self) -> None:
            if self.path != "/in/openwa":
                self._reply(404, {"error": "not_found"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                self._reply(413, {"error": "too_large"})
                return
            body = self.rfile.read(length)
            if not verify_openwa_signature(body, self.headers.get("X-OpenWA-Signature"), cfg.openwa_webhook_secret):
                self._reply(401, {"error": "unauthorized"})
                return
            try:
                payload = json.loads(body)
                instance = cfg.sessions.get(str(payload.get("sessionId")))
                if instance is None:
                    raise Ignored("session_not_mapped")
                event_id, envelope = normalize_openwa(payload, instance=instance, resolve_phone=resolve)
            except Ignored as ignored:
                self._reply(200, {"status": "ignored", "reason": ignored.reason})
                return
            except (ValueError, AttributeError):
                self._reply(400, {"error": "invalid_payload"})
                return
            novo = spool.put(event_id, json.dumps(envelope, ensure_ascii=False, separators=(",", ":")))
            self._reply(200, {"status": "queued" if novo else "duplicate"})

    return Handler


def worker_loop(spool: Spool, cfg: Config, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            if not deliver_due(spool, cfg):
                stop.wait(1.0)
        except Exception:  # o worker nunca morre por um item ruim
            log.exception("falha inesperada no worker")
            stop.wait(5.0)


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    cfg = Config.from_env()
    if not cfg.openwa_webhook_secret:
        raise SystemExit("OPENWA_WEBHOOK_SECRET ausente")
    if cfg.out_url and not cfg.out_secret:
        raise SystemExit("OUT_URL sem OUT_SECRET")
    os.makedirs(cfg.data_dir, exist_ok=True)
    spool = Spool(os.path.join(cfg.data_dir, "spool.sqlite"))
    client = OpenWAClient(cfg.openwa_api_url, cfg.openwa_api_key) if cfg.openwa_api_url else None
    stop = threading.Event()
    threading.Thread(target=worker_loop, args=(spool, cfg, stop), daemon=True).start()
    server = ThreadingHTTPServer((cfg.listen_host, cfg.listen_port), make_handler(cfg, spool, client))
    log.info("wa-bridge no ar em %s:%s (%s, %d sessao(oes) mapeada(s))", cfg.listen_host, cfg.listen_port,
             "enviando" if cfg.out_url else "modo seco", len(cfg.sessions))
    try:
        server.serve_forever()
    finally:
        stop.set()


if __name__ == "__main__":
    main()
