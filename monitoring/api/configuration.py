"""Configuration for the read-only monitor.

Only ``MONITOR_*`` and explicit read-only source paths are consumed here.  In
particular, this module never reads Binance, Freqtrade, Telegram-broker, Sharia
provider, or inter-service HMAC credentials used by the trading stack.
"""
from __future__ import annotations

import ipaddress
import os
from pathlib import Path
from urllib.parse import urlparse


def _int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(lo, min(hi, value))


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default).lower()).strip().lower() == "true"


_PLACEHOLDERS = {
    "", "changeme", "change_me", "replace_me", "replace-on-oracle",
    "replace_on_oracle_only", "placeholder", "example", "token",
}


def secret_is_configured(value: str, *, minimum: int = 32) -> bool:
    value = str(value or "").strip()
    return len(value) >= minimum and value.lower() not in _PLACEHOLDERS


def loopback_http_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        host = parsed.hostname
        return (
            parsed.scheme == "http"
            and bool(host)
            and ipaddress.ip_address(host).is_loopback
            and not parsed.username
            and not parsed.password
        )
    except (ValueError, TypeError):
        return False


class Config:
    MAX_REPORT_DAYS = 90
    MAX_CRASH_HOURS = 168
    MAX_TRADES = 200
    MAX_LOG_LINES = 500
    MAX_LATENCY_SAMPLES = 10
    MAX_LOG_SCAN_BYTES = 4 * 1024 * 1024
    MAX_LEDGER_SCAN_BYTES = 32 * 1024 * 1024

    def __init__(self) -> None:
        shared = Path(os.getenv(
            "MONITOR_SHARED_ROOT", "/var/lib/binance-freqtrade-v101/shared"
        ))
        runtime = shared / "runtime"

        self.enabled = _bool("MONITOR_ENABLED", True)
        self.telegram_reports_enabled = _bool("TELEGRAM_REPORTS_ENABLED", False)
        self.bind_host = os.getenv("MONITOR_BIND_HOST", "127.0.0.1").strip()
        self.port = _int("MONITOR_PORT", 8090, 1, 65535)
        self.token = os.getenv("MONITOR_TOKEN", "").strip()
        self.rate_limit_per_minute = _int(
            "MONITOR_RATE_LIMIT_PER_MINUTE", 30, 1, 100000
        )
        self.allowed_ips = os.getenv(
            "MONITOR_ALLOWED_IPS", "127.0.0.1/32,::1/128"
        ).strip()
        self.bot_mode = os.getenv("BOT_MODE", "testnet").strip().lower()
        self.bot_dir = Path(os.getenv(
            "BOT_DIRECTORY", "/opt/binance-freqtrade-v101/current"
        ))
        self.shared_root = shared

        # Authoritative execution state and realised-PnL sources.
        self.execution_db_path = Path(os.getenv(
            "EXECUTION_DATABASE_PATH", str(runtime / "execution_state.sqlite")
        ))
        self.pnl_ledger_path = Path(os.getenv(
            "EXECUTION_PNL_LEDGER_PATH",
            str(shared / "legacy_runtime/logs/pnl_ledger.jsonl"),
        ))

        # Freqtrade is deliberately signal-only; it is reported separately.
        self.signal_db_path = Path(os.getenv(
            "SIGNAL_DATABASE_PATH",
            str(shared / "freqtrade/tradesv3.signal-only.sqlite"),
        ))
        self.log_path = Path(os.getenv(
            "FT_LOG_PATH", str(shared / "freqtrade/logs/freqtrade.log")
        ))

        self.audit_path = Path(os.getenv(
            "MONITOR_AUDIT_LOG",
            f"/var/log/binance-freqtrade-v101/monitor/{self.bot_mode}-audit.jsonl",
        ))
        self.security_audit_path = Path(os.getenv(
            "BOT_SECURITY_AUDIT_LOG", str(shared / "audit/events.jsonl")
        ))
        self.container_status_path = Path(os.getenv(
            "CONTAINER_STATUS_PATH", str(runtime / "container_status.json")
        ))
        self.sidecar_health_path = Path(os.getenv(
            "SIDECAR_HEALTH_PATH", str(runtime / "sidecar_health.json")
        ))
        self.telegram_health_path = Path(os.getenv(
            "TELEGRAM_HEALTH_PATH", str(runtime / "telegram_health.json")
        ))
        self.user_stream_health_path = Path(os.getenv(
            "USER_STREAM_HEALTH_PATH", str(runtime / "user_stream_health.json")
        ))
        self.sharia_health_path = Path(os.getenv(
            "SHARIA_HEALTH_PATH", str(runtime / "sharia_screener/health.json")
        ))
        self.universe_health_path = Path(os.getenv(
            "UNIVERSE_HEALTH_PATH", str(shared / "universe/status.json")
        ))
        self.sharia_status_path = Path(os.getenv(
            "SHARIA_STATUS_PATH", str(shared / "sharia/sharia_status.json")
        ))
        self.deploy_status_path = Path(os.getenv(
            "DEPLOY_STATUS_PATH", str(runtime / "deployment_status.json")
        ))
        self.validation_status_path = Path(os.getenv(
            "VALIDATION_STATUS_PATH", str(runtime / "release_validation.json")
        ))

        self.enable_docs = _bool("MONITOR_ENABLE_DOCS", False)
        default_base = (
            "https://api.binance.com"
            if self.bot_mode == "live"
            else "https://testnet.binance.vision"
        )
        self.binance_base = os.getenv("BINANCE_REST_BASE", default_base).rstrip("/")

    def banner(self) -> str:
        if self.bot_mode == "live":
            return "MODE: LIVE BINANCE SPOT - REAL-MONEY ENVIRONMENT"
        if self.bot_mode == "testnet":
            return "MODE: BINANCE SPOT TESTNET - NO REAL-MONEY TRADING"
        return "MODE: SIMULATION - NO EXCHANGE ORDERS"

    def allowed_networks(self):
        networks = []
        for raw in self.allowed_ips.split(","):
            raw = raw.strip()
            if raw:
                networks.append(ipaddress.ip_network(raw, strict=False))
        return tuple(networks)

    def runtime_errors(self) -> list[str]:
        errors: list[str] = []
        if self.bot_mode not in {"simulation", "testnet", "live"}:
            errors.append("invalid BOT_MODE")
        if self.bind_host not in {"127.0.0.1", "::1", "localhost"}:
            errors.append("MONITOR_BIND_HOST must be loopback")
        if not secret_is_configured(self.token):
            errors.append("MONITOR_TOKEN must be a non-placeholder value of at least 32 characters")
        try:
            if not self.allowed_networks():
                errors.append("MONITOR_ALLOWED_IPS is empty")
        except ValueError:
            errors.append("MONITOR_ALLOWED_IPS contains an invalid network")
        if not self.binance_base.startswith("https://"):
            errors.append("BINANCE_REST_BASE must use https")
        return errors


CONFIG = Config()


def reload() -> Config:
    """Reload in place so modules holding CONFIG keep the same object."""
    new = Config()
    CONFIG.__dict__.clear()
    CONFIG.__dict__.update(new.__dict__)
    return CONFIG
