import hashlib
import hmac
import json
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parents[1]
RELEASE_MODE = (ROOT / "RELEASE_MODE").read_text(encoding="utf-8").strip()
INSTANCE_SLUG = f"binana-{RELEASE_MODE}"
sys.path.insert(0, str(ROOT))

from scripts import api_readiness  # noqa: E402


PROTECTED = {
    "freqtrade/user_data/strategies/IctSmcStrategy.py":
        "9f6bafc78c8cd0d9b9cbde615ddce89e304ab09738584b88d05bfdf92ff4e830",
    "legacy_core/binance_bot_V4.9.16_ALL_IN_ONE.py":
        "70b1d67cc0092b5b8db4a68b343cf893641bde1aae580e9ef51e2adec1062459",
    "services/common/sharia_v19.py":
        "5eb9fd5338d80fcaf0d39bb3f4935a75b57dd91136c72a83a7551b659b04d865",
    "shared/sharia/HALAL_CRYPTO_SPOT_SCREENING_V19_1_PRODUCTION.json":
        "07106bb8bfc1924d8d0c6f61ced4e0c51c2ac2054988423f42c1fd67f3b2ba78",
}


def _config(**overrides):
    value = {
        "BINANCE_API_KEY": "binance-key",
        "BINANCE_API_SECRET": "binance-secret",
        "TELEGRAM_BOT_TOKEN": "1234567:" + "T" * 40,
        "TELEGRAM_OWNER_CHAT_ID": "123456",
        "COINGECKO_API_KEY": "coingecko-key",
        "COINMARKETCAP_API_KEY": "cmc-key",
        "ENABLE_COINGECKO_SIGNALS": "false",
        "ENABLE_CMC_TRENDING": "false",
        "SHARIA_AUTO_SOURCE_DISCOVERY_ENABLED": "true",
    }
    value.update(overrides)
    return value


class Transport:
    def __init__(self):
        self.calls = []

    def __call__(self, url, headers, timeout):
        self.calls.append((url, dict(headers), timeout))
        parsed = urlsplit(url)
        if parsed.path == "/api/v3/time":
            return {"serverTime": 1_700_000_000_000}
        if parsed.path == "/api/v3/account":
            query = parse_qs(parsed.query)
            signature = query.pop("signature")[0]
            canonical = "omitZeroBalances=true&recvWindow=5000&timestamp=1700000000000"
            assert signature == hmac.new(b"binance-secret", canonical.encode(), hashlib.sha256).hexdigest()
            assert headers == {"X-MBX-APIKEY": "binance-key"}
            return {"canTrade": True, "accountType": "SPOT", "balances": [{"asset": "SECRET"}]}
        if parsed.path.endswith("/getMe"):
            return {"ok": True, "result": {"id": 99, "username": "private"}}
        if parsed.path.endswith("/getChat"):
            assert parse_qs(parsed.query) == {"chat_id": ["123456"]}
            return {"ok": True, "result": {"title": "private"}}
        if parsed.netloc == "api.coingecko.com":
            assert headers == {"x-cg-demo-api-key": "coingecko-key"}
            return {"gecko_says": "ok"}
        if parsed.netloc == "pro-api.coinmarketcap.com":
            assert headers == {"X-CMC_PRO_API_KEY": "cmc-key"}
            return {"status": {"error_code": 0}, "data": {"plan": {"plan_name": "Basic"}}}
        raise AssertionError(url)


def test_all_provider_checks_are_get_only_sanitised_and_pass():
    transport = Transport()
    result = api_readiness.run(_config(), "testnet", transport)
    assert result["ok"] is True
    assert result["network_operations"] == "GET_ONLY_NO_ORDERS"
    assert {item["status"] for item in result["providers"].values()} == {"PASS"}
    assert all(url.startswith("https://") for url, _, _ in transport.calls)
    serialised = json.dumps(result)
    for secret in ("binance-key", "binance-secret", "coingecko-key", "cmc-key", "T" * 20, "SECRET", "private"):
        assert secret not in serialised


def test_package_mode_hard_binds_binance_endpoint():
    testnet = Transport()
    api_readiness.run(_config(), "testnet", testnet)
    live = Transport()
    api_readiness.run(_config(), "live", live)
    assert testnet.calls[0][0].startswith("https://testnet.binance.vision/")
    assert live.calls[0][0].startswith("https://api.binance.com/")


def test_missing_sharia_provider_key_fails_required_preflight():
    result = api_readiness.run(_config(COINGECKO_API_KEY=""), "testnet", Transport())
    assert result["ok"] is False
    assert result["providers"]["coingecko"] == {
        "status": "FAIL", "required": True, "details": {"reason": "key_not_configured"}
    }


def test_disabled_optional_providers_can_be_skipped():
    result = api_readiness.run(
        _config(
            COINGECKO_API_KEY="",
            COINMARKETCAP_API_KEY="",
            SHARIA_AUTO_SOURCE_DISCOVERY_ENABLED="false",
        ),
        "testnet",
        Transport(),
    )
    assert result["ok"] is True
    assert result["providers"]["coingecko"]["status"] == "SKIPPED"
    assert result["providers"]["coinmarketcap"]["status"] == "SKIPPED"


def test_provider_failure_is_sanitised_and_fail_closed():
    def failed(url, headers, timeout):
        raise api_readiness.ReadinessError("provider rejected authentication or permission")

    result = api_readiness.run(_config(), "live", failed)
    assert result["ok"] is False
    assert all(value["status"] == "FAIL" for value in result["providers"].values())


def test_coingecko_demo_auth_failure_is_actionable_and_secret_free():
    def rejected(url, headers, timeout):
        assert headers == {"x-cg-demo-api-key": "coingecko-key"}
        raise api_readiness.ReadinessError(
            "provider rejected authentication or permission")

    result = api_readiness.check_coingecko(_config(), rejected).as_dict()
    assert result == {
        "status": "FAIL",
        "required": True,
        "details": {"reason": "free_demo_key_rejected_or_not_enabled"},
    }
    assert "coingecko-key" not in json.dumps(result)


def test_cli_reads_configuration_from_stdin_and_not_arguments():
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts/api_readiness.py"), "--package-mode", "testnet"],
        input="not-json",
        text=True,
        capture_output=True,
        check=False,
    )
    payload = json.loads(completed.stdout)
    assert completed.returncode == 1
    assert payload["ok"] is False
    assert "configuration input" in payload["error"]


def test_shell_wrapper_has_fixed_paths_atomic_status_and_no_order_methods():
    wrapper = (ROOT / "deploy/api_preflight.sh").read_text(encoding="utf-8")
    assert "secure_env_read" in wrapper
    assert 'source "$SCRIPT_DIR/instance_identity.sh"' in wrapper
    assert "readonly ENV_FILE=$PRIVATE_ROOT/.env" in wrapper
    assert "readonly STATUS=$PERSIST/runtime/api_readiness_status.json" in wrapper
    identity = (ROOT / "deploy/instance_identity.sh").read_text(encoding="utf-8")
    assert f"readonly PRIVATE_ROOT=/etc/{INSTANCE_SLUG}" in identity
    assert f"readonly PERSIST=/var/lib/{INSTANCE_SLUG}/shared" in identity
    assert "mktemp" in wrapper and "mv -fT" in wrapper
    assert "chmod 0640" in wrapper
    assert "network_operations=GET_ONLY_NO_ORDERS" in wrapper
    assert "sys.stdin.buffer.read()" in wrapper
    assert "printf '%s\\0'" in wrapper
    assert '"${VALUES[BINANCE_API_SECRET]:-}"' in wrapper
    assert '"${VALUES[TELEGRAM_BOT_TOKEN]:-}"' in wrapper
    assert "| python3 -c" in wrapper
    assert "sys.argv[1:]" not in wrapper
    assert "checker_failed_before_valid_status" in wrapper
    for forbidden in ("/api/v3/order", "sendMessage", "cancel", "DELETE", "POST"):
        assert forbidden not in wrapper


def test_systemd_schedules_get_only_preflight_with_hardening():
    service = (ROOT / "monitoring/systemd/binana-api-readiness.service").read_text(encoding="utf-8")
    timer = (ROOT / "monitoring/systemd/binana-api-readiness.timer").read_text(encoding="utf-8")
    installer = (ROOT / "deploy/install_monitoring.sh").read_text(encoding="utf-8")
    assert "ExecStart=/opt/binana-freqtrade-v101/current/deploy/api_preflight.sh" in service
    assert "ProtectSystem=strict" in service and "NoNewPrivileges=true" in service
    assert "ReadOnlyPaths=/opt/binana-freqtrade-v101/current /etc/binana-freqtrade-v101/.env" in service
    assert "ReadWritePaths=/var/lib/binana-freqtrade-v101/shared/runtime" in service
    assert "OnUnitActiveSec=6h" in timer and "Persistent=true" in timer
    assert "binana-api-readiness.service binana-api-readiness.timer" in installer
    assert 'render_unit "$UNIT_DIR/$unit"' in installer
    assert 'systemctl enable --now "${SYSTEMD_PREFIX}-disk-guard.timer"' in installer
    assert "binana-api-readiness.timer" in installer


def test_protected_core_hashes_remain_exact():
    for relative, expected in PROTECTED.items():
        assert hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() == expected
