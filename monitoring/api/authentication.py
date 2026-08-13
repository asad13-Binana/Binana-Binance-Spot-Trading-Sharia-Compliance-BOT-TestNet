"""Loopback allow-list, constant-time Bearer auth, per-client rate limiting,
and mandatory audit logging for the read-only API."""
from __future__ import annotations

import hmac
import ipaddress
import json
import os
import threading
import time
import uuid
from collections import defaultdict, deque

from fastapi import HTTPException, Request

from .configuration import CONFIG
from .log_redaction import redact

try:
    import fcntl

    def _lock_exclusive(handle):
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)

    def _unlock(handle):
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
except ImportError:  # Windows test/development hosts
    import msvcrt

    def _lock_exclusive(handle):
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)

    def _unlock(handle):
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


class AuditUnavailable(RuntimeError):
    pass


_WINDOWS: dict[str, deque] = defaultdict(deque)
_AUDIT_LOCK = threading.Lock()


def _rotate_audit(path, incoming_bytes: int) -> None:
    try:
        max_bytes = max(0, min(int(os.getenv(
            "MONITOR_AUDIT_MAX_BYTES", str(10 * 1024 * 1024)
        )), 1024 * 1024 * 1024))
        backups = max(0, min(int(os.getenv("MONITOR_AUDIT_BACKUPS", "5")), 50))
    except ValueError:
        max_bytes, backups = 10 * 1024 * 1024, 5
    if max_bytes <= 0 or backups <= 0:
        return
    current = path.stat().st_size if path.exists() else 0
    if current + incoming_bytes <= max_bytes:
        return
    path.with_name(f"{path.name}.{backups}").unlink(missing_ok=True)
    for index in range(backups - 1, 0, -1):
        source = path.with_name(f"{path.name}.{index}")
        if source.exists():
            source.replace(path.with_name(f"{path.name}.{index + 1}"))
    if path.exists():
        path.replace(path.with_name(f"{path.name}.1"))


def audit(event: str, request_id: str, detail: str = "") -> None:
    record = {
        "ts": round(time.time(), 3),
        "event": str(event)[:64],
        "request_id": str(request_id)[:64],
        "detail": redact(str(detail))[:300],
    }
    line = json.dumps(record, separators=(",", ":")) + "\n"
    try:
        CONFIG.audit_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = CONFIG.audit_path.with_name(CONFIG.audit_path.name + ".lock")
        with _AUDIT_LOCK, lock_path.open("a+", encoding="utf-8") as process_lock:
            _lock_exclusive(process_lock)
            try:
                _rotate_audit(CONFIG.audit_path, len(line.encode("utf-8")))
                with CONFIG.audit_path.open("a", encoding="utf-8") as handle:
                    handle.write(line)
                    handle.flush()
                    os.fsync(handle.fileno())
            finally:
                _unlock(process_lock)
    except OSError as exc:
        raise AuditUnavailable(type(exc).__name__) from exc


def _audit_or_503(event: str, rid: str, detail: str) -> None:
    try:
        audit(event, rid, detail)
    except AuditUnavailable as exc:
        raise HTTPException(
            503, {"error": "audit_unavailable", "request_id": rid}
        ) from exc


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def _allowed(client: str) -> bool:
    try:
        address = ipaddress.ip_address(client)
        return any(address in network for network in CONFIG.allowed_networks())
    except ValueError:
        return False


def require_bearer(request: Request) -> str:
    rid = uuid.uuid4().hex[:12]
    path = request.url.path
    client = _client_ip(request)

    if not CONFIG.enabled:
        raise HTTPException(503, {"error": "monitor_disabled", "request_id": rid})
    if CONFIG.runtime_errors():
        raise HTTPException(503, {"error": "monitor_misconfigured", "request_id": rid})
    if not _allowed(client):
        _audit_or_503("ip_denied", rid, f"{client} {path}")
        raise HTTPException(403, {"error": "source_ip_denied", "request_id": rid})

    auth = request.headers.get("authorization", "")
    supplied = auth[7:] if auth.lower().startswith("bearer ") else ""
    if not hmac.compare_digest(supplied, CONFIG.token):
        _audit_or_503("auth_failed", rid, f"{client} {path}")
        raise HTTPException(401, {"error": "unauthorized", "request_id": rid})

    # Only authenticated traffic consumes the authorized-client quota. Invalid
    # local requests therefore cannot exhaust the valid client's budget.
    now = time.time()
    window = _WINDOWS[client]
    while window and now - window[0] >= 60:
        window.popleft()
    if len(window) >= CONFIG.rate_limit_per_minute:
        _audit_or_503("rate_limited", rid, f"{client} {path}")
        raise HTTPException(429, {"error": "rate_limited", "request_id": rid})
    window.append(now)
    _audit_or_503("authorized", rid, f"{client} {path}")
    return rid
