"""Small systemd ExecCondition helper for explicit enable/disable controls."""
from __future__ import annotations

import os
import socket
import sys

from .api.configuration import CONFIG, loopback_http_url, secret_is_configured


def monitor_port_available() -> tuple[bool, str]:
    family = socket.AF_INET6 if ":" in CONFIG.bind_host else socket.AF_INET
    host = "::1" if CONFIG.bind_host == "localhost" and family == socket.AF_INET6 else CONFIG.bind_host
    if host == "localhost":
        host = "127.0.0.1"
    probe = socket.socket(family, socket.SOCK_STREAM)
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        probe.bind((host, CONFIG.port))
    except OSError:
        return False, f"monitor port already occupied: {CONFIG.bind_host}:{CONFIG.port}"
    finally:
        probe.close()
    return True, "ok"


def check(kind: str) -> tuple[bool, str]:
    if kind == "api":
        if not CONFIG.enabled:
            return False, "MONITOR_ENABLED is false"
        errors = CONFIG.runtime_errors()
        available, reason = monitor_port_available()
        if not available:
            errors.append(reason)
        return (not errors, "; ".join(errors) if errors else "ok")
    if kind == "telegram":
        if not CONFIG.telegram_reports_enabled:
            return False, "TELEGRAM_REPORTS_ENABLED is false"
        if not loopback_http_url(os.getenv("MONITOR_URL", "")):
            return False, "MONITOR_URL must be loopback HTTP"
        for name, minimum in (
            ("MONITOR_TOKEN", 32),
            ("TELEGRAM_MONITOR_BOT_TOKEN", 32),
            ("TELEGRAM_MONITOR_CHAT_ID", 1),
        ):
            if not secret_is_configured(os.getenv(name, ""), minimum=minimum):
                return False, f"{name} is missing or a placeholder"
        return True, "ok"
    return False, "kind must be api or telegram"


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    ok, reason = check(argv[0] if argv else "")
    if not ok:
        print(reason)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
