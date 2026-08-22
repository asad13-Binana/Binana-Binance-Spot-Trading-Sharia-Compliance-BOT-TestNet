from __future__ import annotations

import datetime
import inspect
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[2]
RELEASE_MODE = (ROOT / "RELEASE_MODE").read_text(encoding="utf-8").strip()
INSTANCE = {
    "testnet": {"slug": "binana-testnet", "port": 8090, "bot_user": "binanatn", "monitor_user": "binanatnmon"},
    "live": {"slug": "binana-live", "port": 8092, "bot_user": "binanalive", "monitor_user": "binanalivemon"},
}[RELEASE_MODE]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TOKEN = "a" * 64
os.environ.setdefault("MONITOR_TOKEN", TOKEN)
os.environ.setdefault("MONITOR_ALLOWED_IPS", "127.0.0.1/32,::1/128")
os.environ.setdefault("MONITOR_BIND_HOST", "127.0.0.1")

from monitoring.api import metrics  # noqa: E402
from monitoring.api.app import app  # noqa: E402
from monitoring.api.authentication import _WINDOWS, audit as auth_audit  # noqa: E402
from monitoring.api.configuration import CONFIG, Config, loopback_http_url  # noqa: E402
from monitoring.api.database import query  # noqa: E402
from monitoring.api.log_redaction import redact, redact_obj  # noqa: E402
from monitoring import control, snapshot  # noqa: E402
from monitoring.mcp import monitor_mcp_server as bridge  # noqa: E402
from monitoring.telegram import telegram_reporter as reporter  # noqa: E402


AUTH = {"Authorization": f"Bearer {TOKEN}"}
client = TestClient(app, client=("127.0.0.1", 50000))


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    CONFIG.enabled = True
    CONFIG.telegram_reports_enabled = False
    CONFIG.bind_host = "127.0.0.1"
    CONFIG.token = TOKEN
    CONFIG.allowed_ips = "127.0.0.1/32,::1/128"
    CONFIG.rate_limit_per_minute = 100000
    CONFIG.bot_mode = "testnet"
    CONFIG.bot_dir = tmp_path / "release"
    CONFIG.shared_root = tmp_path / "shared"
    CONFIG.execution_db_path = tmp_path / "execution.sqlite"
    CONFIG.signal_db_path = tmp_path / "signal.sqlite"
    CONFIG.pnl_ledger_path = tmp_path / "pnl.jsonl"
    CONFIG.log_path = tmp_path / "freqtrade.log"
    CONFIG.audit_path = tmp_path / "monitor-audit.jsonl"
    CONFIG.security_audit_path = tmp_path / "security.jsonl"
    CONFIG.container_status_path = tmp_path / "container_status.json"
    CONFIG.sidecar_health_path = tmp_path / "sidecar_health.json"
    CONFIG.telegram_health_path = tmp_path / "telegram_health.json"
    CONFIG.telegram_alert_outbox_health_path = tmp_path / "telegram_alert_outbox_health.json"
    CONFIG.user_stream_health_path = tmp_path / "user_stream_health.json"
    CONFIG.market_context_health_path = tmp_path / "market_context_health.json"
    CONFIG.market_context_path = tmp_path / "market_context.json"
    CONFIG.sharia_health_path = tmp_path / "sharia_health.json"
    CONFIG.universe_health_path = tmp_path / "universe_health.json"
    CONFIG.sharia_status_path = tmp_path / "sharia_status.json"
    CONFIG.deploy_status_path = tmp_path / "deployment_status.json"
    CONFIG.validation_status_path = tmp_path / "release_validation.json"
    CONFIG.offhost_backup_status_path = tmp_path / "offhost_backup_status.json"
    CONFIG.api_readiness_status_path = tmp_path / "api_readiness_status.json"
    CONFIG.binance_base = "https://testnet.binance.vision"
    _WINDOWS.clear()
    bridge.URL = "http://127.0.0.1:8090"
    bridge.TOKEN = TOKEN
    for name in (
        "TELEGRAM_REPORTS_ENABLED", "TELEGRAM_MONITOR_BOT_TOKEN",
        "TELEGRAM_MONITOR_CHAT_ID", "MONITOR_URL",
    ):
        monkeypatch.delenv(name, raising=False)


def _execution_db(path: Path):
    connection = sqlite3.connect(path)
    connection.executescript("""
      CREATE TABLE trade_records (
        trade_id TEXT PRIMARY KEY,pair TEXT,lifecycle_state TEXT,
        filled_quantity TEXT,protected_quantity TEXT,average_entry_price TEXT,
        protection_mode TEXT,last_event_time TEXT,reconciliation_status TEXT,
        updated_at TEXT
      );
      CREATE TABLE exchange_events (
        id INTEGER PRIMARY KEY,event_type TEXT,event_time TEXT,payload_json TEXT
      );
      CREATE TABLE processed_signals (
        signal_id TEXT PRIMARY KEY,result TEXT,processed_at TEXT
      );
    """)
    return connection


def _signal_db(path: Path):
    connection = sqlite3.connect(path)
    connection.execute("""CREATE TABLE trades (
      pair TEXT,close_profit_abs REAL,close_profit REAL,open_date TEXT,
      close_date TEXT,is_open INTEGER,open_rate REAL,amount REAL,stake_amount REAL
    )""")
    return connection


def _write(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


# Authentication, authorization, rate limiting, and audit durability.
def test_auth_missing_is_401():
    assert client.get("/api/v1/health").status_code == 401


def test_auth_wrong_is_401():
    assert client.get("/api/v1/health", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_auth_valid_is_200():
    assert client.get("/api/v1/health", headers=AUTH).status_code == 200


def test_auth_uses_constant_time_compare():
    from monitoring.api import authentication
    assert "compare_digest" in inspect.getsource(authentication.require_bearer)


def test_monitor_enabled_flag_is_enforced():
    CONFIG.enabled = False
    assert client.get("/api/v1/health", headers=AUTH).status_code == 503


def test_placeholder_monitor_token_is_rejected():
    CONFIG.token = "replace_on_oracle_only"
    assert client.get("/api/v1/health", headers={"Authorization": "Bearer replace_on_oracle_only"}).status_code == 503


def test_source_allowlist_is_enforced():
    outsider = TestClient(app, client=("10.0.0.1", 50000))
    assert outsider.get("/api/v1/health", headers=AUTH).status_code == 403


def test_invalid_auth_does_not_exhaust_valid_quota():
    CONFIG.rate_limit_per_minute = 1
    for _ in range(3):
        assert client.get("/api/v1/health", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/api/v1/health", headers=AUTH).status_code == 200
    assert client.get("/api/v1/health", headers=AUTH).status_code == 429


def test_authorized_request_is_audited():
    client.get("/api/v1/health", headers=AUTH)
    record = json.loads(CONFIG.audit_path.read_text(encoding="utf-8").splitlines()[-1])
    assert record["event"] == "authorized" and record["request_id"]


def test_audit_failure_fails_request_visibly(tmp_path):
    CONFIG.audit_path = tmp_path  # opening a directory as a file must fail
    assert client.get("/api/v1/health", headers=AUTH).status_code == 503


# Redaction regressions from the independent re-audit.
@pytest.mark.parametrize("value,secret", [
    ("Authorization: Bearer abc123", "abc123"),
    ("Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
    ("MONITOR_TOKEN=short123", "short123"),
    ("MCP_AUTH_TOKEN=abcdefghijklmnop", "abcdefghijklmnop"),
    ('password="secret with spaces"', "secret with spaces"),
    ("1234567:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef", "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef"),
])
def test_secret_redaction(value, secret):
    output = redact(value)
    assert secret not in output and "[REDACTED]" in output


def test_recursive_redaction_covers_sensitive_fields():
    value = redact_obj({"nested": [{"api_secret": "tiny"}], "text": "token=abcdef"})
    assert value["nested"][0]["api_secret"] == "[REDACTED]"
    assert "abcdef" not in value["text"]


# SQLite, topology, and the known midnight/date-format regression.
def test_database_missing_is_structured():
    assert metrics.execution_state()["error"] == "database_missing"


def test_offhost_backup_status_is_missing_safe_fresh_and_failed():
    assert metrics.offhost_backup_status()["status"] == "not_configured"
    now = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    _write(CONFIG.offhost_backup_status_path, {
        "ok": True, "completed_at": now, "source_backup": "20260814T010000Z",
        "object_name": "binana-testnet/20260814T010000Z.tar.age",
        "encrypted_sha256": "a" * 64, "authentication": "instance_principal",
    })
    healthy = metrics.offhost_backup_status()
    assert healthy["status"] == "healthy" and healthy["fresh"] is True
    _write(CONFIG.offhost_backup_status_path, {
        "ok": False, "failed_at": now, "exit_code": 1,
        "authentication": "instance_principal",
    })
    failed = metrics.offhost_backup_status()
    assert failed["status"] == "degraded" and failed["fresh"] is True
    payload = client.get("/api/v1/health", headers=AUTH).json()
    assert payload["offhost_backup"]["status"] == "degraded"


def test_api_readiness_status_is_sanitised_and_exposed():
    assert metrics.api_readiness_status()["status"] == "not_run"
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    _write(CONFIG.api_readiness_status_path, {
        "schema_version": 1,
        "ok": True,
        "generated_at": now,
        "package_mode": "testnet",
        "network_operations": "GET_ONLY_NO_ORDERS",
        "providers": {
            "binance": {
                "status": "PASS", "required": True,
                "details": {"authenticated": True, "balance": "SECRET"},
            },
        },
    })
    value = metrics.api_readiness_status()
    assert value["status"] == "healthy"
    assert value["network_operations"] == "GET_ONLY_NO_ORDERS"
    assert "SECRET" not in json.dumps(value)
    payload = client.get("/api/v1/status", headers=AUTH).json()
    assert payload["api_readiness"]["providers"]["binance"]["status"] == "PASS"


def test_spot_market_context_is_bearer_protected_sanitised_and_advisory_only():
    now = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    _write(CONFIG.market_context_health_path, {
        "schema_version": 1, "ok": True, "ts": time.time(),
        "advisory_only": True, "spot_only": True, "can_trade": False,
    })
    _write(CONFIG.market_context_path, {
        "schema_version": 1,
        "generated_at": now,
        "advisory_only": True,
        "spot_only": True,
        "can_trade": False,
        "package_mode": "testnet",
        "universe_snapshot_hash": "a" * 64,
        "symbol_count": 1,
        "fresh_symbol_count": 1,
        "statistics": {"accepted_agg_trades": 4},
        "symbols": {
            "ETHUSDT": {
                "symbol": "ETHUSDT", "status": "fresh", "advisory_only": True,
                "spot_aggressive_flow": {"cvd_quote_60s": "12.5"},
                "top_of_book_liquidity": {"spread_bps": "1.2"},
            }
        },
    })
    assert client.get("/api/v1/market-context").status_code == 401
    response = client.get(
        "/api/v1/market-context?symbol=ETHUSDT", headers=AUTH
    )
    assert response.status_code == 200
    value = response.json()["spot_market_context"]
    assert value["fresh"] is True
    assert value["advisory_only"] is True
    assert value["used_for_trade_decision"] is False
    assert value["evidence"]["spot_aggressive_flow"]["cvd_quote_60s"] == "12.5"
    assert "secret" not in json.dumps(value).lower()


def test_telegram_report_includes_sanitised_api_readiness():
    text = reporter._format({
        "banner": "TEST",
        "api_readiness": {"status": "healthy"},
    })
    assert "API readiness: healthy | GET-only/no orders" in text


def test_database_malformed_is_structured():
    CONFIG.execution_db_path.write_text("not sqlite", encoding="utf-8")
    assert metrics.execution_state()["error"].startswith("database_error")


def test_database_helper_rejects_writes(tmp_path):
    database = tmp_path / "x.sqlite"
    sqlite3.connect(database).execute("CREATE TABLE x(v INTEGER)").connection.close()
    assert query(database, "DELETE FROM x")[1] == "query_not_read_only"


def test_signal_date_filter_crosses_midnight_with_space_timestamp(monkeypatch):
    fixed = datetime.datetime(2026, 7, 19, 6, 0, tzinfo=datetime.timezone.utc)

    class FixedDateTime(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed if tz else fixed.replace(tzinfo=None)

    monkeypatch.setattr(metrics, "dt", SimpleNamespace(
        datetime=FixedDateTime, timezone=datetime.timezone, timedelta=datetime.timedelta
    ))
    connection = _signal_db(CONFIG.signal_db_path)
    connection.execute(
        "INSERT INTO trades VALUES(?,?,?,?,?,?,?,?,?)",
        ("ETH/USDT", 2.0, 0.02, "2026-07-18 17:00:00", "2026-07-18 18:00:00", 0, 1, 1, 1),
    )
    connection.commit(); connection.close()
    result = metrics.signal_performance(1)
    assert result["closed_signals"] == 1


def test_execution_state_is_authoritative_and_separate():
    connection = _execution_db(CONFIG.execution_db_path)
    connection.execute(
        "INSERT INTO trade_records VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("t1", "ETH/USDT", "PROTECTION_ACTIVE", "1", "1", "100", "FIXED_OCO", None, "OK", "2026-07-19T00:00:00+00:00"),
    )
    connection.commit(); connection.close()
    result = metrics.execution_state()
    assert result["open_count"] == 1
    assert result["source"] == "authoritative_execution_state.sqlite"


def test_execution_pnl_ledger_drives_real_performance():
    now = time.time()
    CONFIG.pnl_ledger_path.write_text("\n".join([
        json.dumps({"ts": now - 10, "utc": "x", "symbol": "ETHUSDT", "pnl_pct": 2.0}),
        json.dumps({"ts": now - 5, "utc": "y", "symbol": "SOLUSDT", "pnl_pct": -1.0}),
    ]) + "\n", encoding="utf-8")
    result = metrics.performance(1)
    assert result["closed_trades"] == 2
    assert result["net_pnl_pct"] == 1.0
    assert result["profit_factor"] == 2.0


def test_recent_trades_are_bounded():
    now = time.time()
    CONFIG.pnl_ledger_path.write_text("\n".join(
        json.dumps({"ts": now + i, "symbol": f"S{i}", "pnl_pct": i}) for i in range(5)
    ), encoding="utf-8")
    assert len(metrics.recent_trades(2)["trades"]) == 2


def test_order_quality_uses_structured_execution_events():
    connection = _execution_db(CONFIG.execution_db_path)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    connection.execute("INSERT INTO exchange_events(event_type,event_time,payload_json) VALUES(?,?,?)", ("executionReport", now, json.dumps({"X": "REJECTED"})))
    connection.execute("INSERT INTO processed_signals VALUES(?,?,?)", ("s1", "REJECTED_BY_RISK", now))
    connection.commit(); connection.close()
    result = metrics.order_quality(1)
    assert result["rejected_orders"] == 1 and result["rejected_signals"] == 1


# Bounded logs, actual time windows, health files, Sharia, and deployment.
def test_log_tail_reads_bounded_suffix():
    CONFIG.MAX_LOG_SCAN_BYTES = 128
    CONFIG.log_path.write_text("old\n" * 1000 + "FINAL_ERROR\n", encoding="utf-8")
    lines, truncated, error = metrics.tail_log(10)
    assert error is None and truncated and lines[-1] == "FINAL_ERROR"


def test_crash_window_is_hours_not_fake_line_count():
    recent = datetime.datetime.now(datetime.timezone.utc).isoformat()
    old = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=10)).isoformat()
    CONFIG.log_path.write_text(
        f"{old} Traceback (most recent call last):\n ValueError: old\n"
        f"{recent} Traceback (most recent call last):\n RuntimeError: new\n",
        encoding="utf-8",
    )
    assert metrics.crash_blocks(24)["crash_count"] == 1


def test_container_snapshot_replaces_docker_socket_access():
    _write(CONFIG.container_status_path, {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "containers": [
            {"service": service, "status": "running", "health": "healthy", "restart_count": 0}
            for service in ("universe", "sharia-screener", "freqtrade", "execution-sidecar", "telegram-broker")
        ],
    })
    assert metrics.container_state()["status"] == "healthy"


def test_runtime_health_uses_bot_generated_files():
    now = time.time()
    for path in (CONFIG.sidecar_health_path, CONFIG.telegram_health_path, CONFIG.sharia_health_path):
        _write(path, {"ok": True, "ts": now})
    _write(CONFIG.universe_health_path, {"generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat()})
    assert metrics.runtime_health()["status"] == "healthy"


def test_websocket_state_and_reconnects_are_exposed():
    _write(CONFIG.user_stream_health_path, {
        "ts": time.time(), "connected": True, "subscribed": True,
        "reconnect_count": 3, "mode": "testnet",
    })
    result = metrics.websocket_status()
    assert result["connected"] and result["subscribed"] and result["reconnect_count"] == 3


def test_telegram_alert_backlog_and_dead_letters_are_exposed():
    _write(CONFIG.telegram_alert_outbox_health_path, {
        "ok": False,
        "ts": time.time(),
        "pending_alert_count": 4,
        "oldest_pending_alert_age_seconds": 91.5,
        "dead_letter_count": 2,
        "blocked_reason": "dedupe state invalid",
    })
    result = metrics.telegram_alert_outbox_status()
    assert result["fresh"]
    assert result["ok"] is False
    assert result["pending_alert_count"] == 4
    assert result["oldest_pending_alert_age_seconds"] == 91.5
    assert result["dead_letter_count"] == 2


def test_sharia_service_queue_and_latest_scan_are_exposed():
    _write(CONFIG.sharia_status_path, {"controller_sha256": "abc", "records": [{"final_code": "GREEN", "reviewed_at": "2026-07-19"}]})
    _write(CONFIG.sharia_health_path, {"ok": True, "ts": time.time(), "queue": {"PENDING": 2}, "last_done": {"base": "ETH"}})
    result = metrics.sharia_status()
    assert result["status_counts"]["GREEN"] == 1
    assert result["queue"]["PENDING"] == 2


def test_deployment_schema_matches_installer_output():
    CONFIG.bot_dir.mkdir()
    (CONFIG.bot_dir / "RELEASE_MODE").write_text("testnet", encoding="utf-8")
    (CONFIG.bot_dir / "RELEASE_SHA256.txt").write_text("b" * 64 + "  RELEASE_MANIFEST.json\n", encoding="utf-8")
    _write(CONFIG.deploy_status_path, {"ok": True, "status": "DEPLOYED", "at": "2026-07-19T01:02:03+00:00"})
    _write(CONFIG.validation_status_path, {"secret_scan": "passed", "manifest_verification": "passed"})
    result = metrics.deployment_info()
    assert result["last_deploy"] == "2026-07-19T01:02:03+00:00"
    assert result["validation"]["secret_scan"] == "passed"


def test_recent_security_warnings_are_redacted():
    CONFIG.security_audit_path.write_text(json.dumps({"severity": "CRITICAL", "details": "token=abcdef"}) + "\n", encoding="utf-8")
    value = metrics.recent_security_warnings()["warnings"][0]
    assert "abcdef" not in json.dumps(value)


def test_monitor_audit_is_durable_and_rotated(monkeypatch):
    monkeypatch.setenv("MONITOR_AUDIT_MAX_BYTES", "200")
    monkeypatch.setenv("MONITOR_AUDIT_BACKUPS", "2")
    for index in range(8):
        auth_audit("monitor_request", f"request-{index}", "x" * 80)
    assert CONFIG.audit_path.exists()
    assert CONFIG.audit_path.with_name(CONFIG.audit_path.name + ".1").exists()
    assert CONFIG.audit_path.with_name(CONFIG.audit_path.name + ".lock").exists()


# MCP and Telegram are loopback/read-only and fail without leaking secrets.
def test_mcp_rejects_non_loopback_url():
    bridge.URL = "https://evil.example"
    assert bridge._get("/health")["ok"] is False


def test_mcp_clamps_arguments_and_sends_token_only_in_header(monkeypatch):
    captured = {}

    class Response:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"ok": True}

    def fake_get(url, **kwargs):
        captured.update({"url": url, **kwargs})
        return Response()

    monkeypatch.setattr(bridge.httpx, "get", fake_get)
    assert bridge._get("/report", days=90)["ok"]
    assert captured["headers"] == {"Authorization": f"Bearer {TOKEN}"}
    assert TOKEN not in captured["url"]


def test_telegram_disabled_flag_is_enforced(capsys):
    assert reporter.main([]) == 1
    assert "reports_disabled" in capsys.readouterr().out


def test_telegram_failure_never_prints_its_token(monkeypatch, capsys):
    telegram = "1234567:" + "S" * 40
    monkeypatch.setenv("TELEGRAM_REPORTS_ENABLED", "true")
    monkeypatch.setenv("MONITOR_URL", "http://127.0.0.1:8090")
    monkeypatch.setenv("MONITOR_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_MONITOR_BOT_TOKEN", telegram)
    monkeypatch.setenv("TELEGRAM_MONITOR_CHAT_ID", "123")
    monkeypatch.setattr(reporter.httpx, "get", lambda *a, **k: (_ for _ in ()).throw(reporter.httpx.ConnectError("down")))
    assert reporter.main([]) == 1
    assert telegram not in capsys.readouterr().out


def test_telegram_checks_json_ok(monkeypatch, capsys):
    class Response:
        def raise_for_status(self): pass
        def json(self): return {"ok": False}
    monkeypatch.setattr(reporter.httpx, "post", lambda *a, **k: Response())
    with pytest.raises(reporter.httpx.HTTPError):
        reporter._send("x" * 40, "1", "hello")


# Static integration and release controls.
def test_loopback_url_validation():
    assert loopback_http_url("http://127.0.0.1:8090")
    assert not loopback_http_url("https://127.0.0.1:8090")
    assert not loopback_http_url("http://example.com:8090")


def test_monitor_config_source_does_not_read_trading_credentials():
    source = inspect.getsource(Config)
    for forbidden in ("BINANCE_API_KEY", "BINANCE_API_SECRET", "SHARIA_OPENAI_API_KEY", "SIGNAL_HMAC_KEY"):
        assert forbidden not in source


def test_mode_templates_have_correct_topology_ports_and_isolation():
    testnet = (ROOT / "monitoring/.env.monitor.testnet.example").read_text(encoding="utf-8")
    live = (ROOT / "monitoring/.env.monitor.live.example").read_text(encoding="utf-8")
    active = testnet if RELEASE_MODE == "testnet" else live
    assert f"/opt/{INSTANCE['slug']}/current" in active
    assert f"/var/lib/{INSTANCE['slug']}/shared" in active
    assert "BOT_PRODUCT=BINANA" in testnet and "BOT_PRODUCT=BINANA" in live
    assert "BOT_ENVIRONMENT=TESTNET" in testnet and "BOT_ENVIRONMENT=LIVE" in live
    assert f"MONITOR_URL=http://127.0.0.1:{INSTANCE['port']}" in active
    assert "testnet-audit.jsonl" in testnet and "live-audit.jsonl" in live
    assert "MONITOR_ENABLED=false" in live


def test_gitignore_keeps_monitor_examples():
    text = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "!**/.env.*.example" in text


def test_systemd_pairs_and_hardening():
    units = ROOT / "monitoring/systemd"
    services = {path.stem for path in units.glob("*.service")}
    for timer in units.glob("*.timer"):
        text = timer.read_text(encoding="utf-8")
        target = next((line.split("=", 1)[1] for line in text.splitlines() if line.startswith("Unit=")), timer.with_suffix(".service").name)
        assert Path(target).stem in services
    api = (units / "binana-monitor-testnet.service").read_text(encoding="utf-8")
    assert "User=botmon" in api and "ProtectSystem=strict" in api and "PrivateDevices=true" in api
    assert "docker.sock" not in api


def test_monitor_installer_preserves_runtime_ownership_and_refreshes_release():
    installer = (ROOT / "deploy/install_monitoring.sh").read_text(encoding="utf-8")
    setup = (ROOT / "deploy/oracle_setup.sh").read_text(encoding="utf-8")
    assert 'usermod -a -G "$BOT_USER" "$MONITOR_USER"' in setup
    assert 'usermod -a -G "$BOT_USER" "$MONITOR_USER"' in installer
    assert 'install -d -m 0750 -o "$BOT_USER" -g "$BOT_USER" "$PERSIST/runtime"' in installer
    assert INSTANCE["bot_user"] in (ROOT / "deploy/instance_identity.sh").read_text(encoding="utf-8")
    assert INSTANCE["monitor_user"] in (ROOT / "deploy/instance_identity.sh").read_text(encoding="utf-8")
    assert 'install -d -m 0755 -o root -g root "$PERSIST/runtime"' not in installer
    assert 'systemctl restart "$MONITOR_SERVICE"' in installer


@pytest.mark.skipif(os.name != "posix", reason="POSIX ownership and modes only")
def test_root_snapshot_publishes_only_fixed_sanitised_sources_read_only(tmp_path):
    root = tmp_path / "shared"
    root.mkdir()
    source = root / "runtime/sidecar_health.json"
    source.parent.mkdir()
    source.write_text("{}", encoding="utf-8")
    private = root / "runtime/sidecar_state.json"
    private.write_text("private", encoding="utf-8")
    (root / "runtime/telegram_health.json").symlink_to(private)
    os.link(private, root / "runtime/user_stream_health.json")
    published = snapshot.publish_monitor_permissions(root)
    assert published == ["runtime/sidecar_health.json"]
    assert source.stat().st_mode & 0o777 == 0o640
    assert private.stat().st_mode & 0o777 != 0o640
    allowlist = set(snapshot.READONLY_MONITOR_SOURCES)
    assert "runtime/sidecar_state.json" not in allowlist
    assert not any("command" in item or "inbox" in item or "evidence" in item for item in allowlist)


def test_monitor_status_files_are_root_owned_group_readable_not_public():
    installer = (ROOT / "deploy/install_artifact.sh").read_text(encoding="utf-8")
    snapshot = (ROOT / "monitoring/snapshot.py").read_text(encoding="utf-8")
    assert installer.count("os.fchmod(fd, 0o640)") >= 2
    assert installer.count("os.fchown(fd, 0, p.parent.stat().st_gid)") >= 2
    assert "os.fchmod(descriptor, 0o640)" in snapshot
    assert "os.fchown(descriptor, 0, path.parent.stat().st_gid)" in snapshot
    assert "0o644" not in snapshot
    for name in (
        "binana-monitor-live.service", "binana-monitor-testnet.service",
        "binana-monitor-report-live.service", "binana-monitor-report-testnet.service",
    ):
        unit = (ROOT / "monitoring/systemd" / name).read_text(encoding="utf-8")
        assert "User=botmon" in unit
        assert "SupplementaryGroups=binanabot" in unit


def test_snapshot_helper_has_no_mutating_docker_commands():
    source = inspect.getsource(snapshot.collect)
    for forbidden in (" stop ", " restart ", " exec ", " rm ", " kill "):
        assert forbidden not in source.lower()


def test_ci_and_release_gate_include_monitoring():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    verifier = (ROOT / "deploy/verify_release.sh").read_text(encoding="utf-8")
    assert "requirements-monitoring.lock" in workflow
    assert "pytest -q monitoring/tests" in verifier
    assert "systemd-analyze verify" in verifier
    assert "scripts/build_manifest.py" not in verifier


def test_monitoring_tests_install_exact_hash_locked_runtime_separately():
    dev = (ROOT / "monitoring/requirements-monitoring-dev.txt").read_text(
        encoding="utf-8"
    )
    includes = [
        line.strip()
        for line in dev.splitlines()
        if line.strip().startswith(("-r ", "--requirement "))
    ]
    assert includes == []
    assert (ROOT / "monitoring/requirements-monitoring.lock").is_file()
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "--require-hashes -r monitoring/requirements-monitoring.lock" in workflow
    installer = (ROOT / "deploy/install_monitoring.sh").read_text(encoding="utf-8")
    assert "--require-hashes" in installer


def test_oracle_installer_installs_monitoring():
    installer = (ROOT / "deploy/install_artifact.sh").read_text(encoding="utf-8")
    setup = (ROOT / "deploy/oracle_setup.sh").read_text(encoding="utf-8")
    assert "install_monitoring.sh" in installer
    assert "useradd --system" in setup and "python3-venv" in setup


def test_every_api_route_is_get_only_and_has_no_order_route():
    for route in app.routes:
        if getattr(route, "path", "").startswith("/api/"):
            assert route.methods == {"GET"}
            assert not any(word in route.path.lower() for word in ("order", "cancel", "buy", "sell"))


def test_docs_are_disabled_by_default():
    assert not any(getattr(route, "path", "") == "/docs" for route in app.routes)


def test_control_requires_explicit_telegram_enable(monkeypatch):
    monkeypatch.setenv("TELEGRAM_REPORTS_ENABLED", "false")
    CONFIG.telegram_reports_enabled = False
    assert control.check("telegram")[0] is False
