#!/usr/bin/env python3
"""Read-only API credential preflight.

This module deliberately implements GET requests only.  It authenticates the
configured providers without placing, testing, cancelling, or modifying an
exchange order and without printing credentials or account balances.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import hmac
import json
import sys
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


BINANCE_BASES = {
    "testnet": "https://testnet.binance.vision",
    "live": "https://api.binance.com",
}
COINGECKO_BASE = "https://api.coingecko.com/api/v3"
CMC_BASE = "https://pro-api.coinmarketcap.com"
TELEGRAM_BASE = "https://api.telegram.org"
RECV_WINDOW_MS = 5_000
TIMEOUT_SECONDS = 15
MAX_RESPONSE_BYTES = 1_048_576


class ReadinessError(RuntimeError):
    """A sanitised provider-readiness failure safe for status output."""


@dataclass(frozen=True)
class Result:
    status: str
    required: bool
    details: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "required": self.required,
            "details": dict(self.details),
        }


Transport = Callable[[str, Mapping[str, str], int], Any]


def _bool(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "on"}:
        return True
    if text in {"false", "0", "no", "off", ""}:
        return False
    raise ReadinessError("invalid boolean configuration")


def _configured(value: Any, placeholders: tuple[str, ...] = ()) -> bool:
    text = str(value or "").strip()
    lowered = text.lower()
    return bool(text) and lowered not in {
        "changeme", "placeholder", "replace_me", *[item.lower() for item in placeholders]
    }


def _http_json(url: str, headers: Mapping[str, str], timeout: int) -> Any:
    request = Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "binana-api-readiness/1", **headers},
        method="GET",
    )
    try:
        with urlopen(request, timeout=timeout) as response:  # nosec B310
            length = response.headers.get("Content-Length")
            if length and int(length) > MAX_RESPONSE_BYTES:
                raise ReadinessError("provider response exceeded size limit")
            payload = response.read(MAX_RESPONSE_BYTES + 1)
            if len(payload) > MAX_RESPONSE_BYTES:
                raise ReadinessError("provider response exceeded size limit")
    except HTTPError as exc:
        if exc.code == 429:
            raise ReadinessError("provider rate limited the preflight") from exc
        if exc.code in {401, 403}:
            raise ReadinessError("provider rejected authentication or permission") from exc
        raise ReadinessError(f"provider returned HTTP {exc.code}") from exc
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        raise ReadinessError(f"provider request failed: {type(exc).__name__}") from exc
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReadinessError("provider returned malformed JSON") from exc


def _call(transport: Transport, url: str, headers: Mapping[str, str] | None = None) -> Any:
    return transport(url, headers or {}, TIMEOUT_SECONDS)


def _pass(required: bool, **details: Any) -> Result:
    return Result("PASS", required, details)


def _skip(reason: str) -> Result:
    return Result("SKIPPED", False, {"reason": reason})


def _fail(required: bool, reason: str) -> Result:
    return Result("FAIL", required, {"reason": reason})


def check_binance(config: Mapping[str, Any], package_mode: str, transport: Transport) -> Result:
    required = True
    api_key = str(config.get("BINANCE_API_KEY") or "").strip()
    secret = str(config.get("BINANCE_API_SECRET") or "").strip()
    if not _configured(api_key) or not _configured(secret):
        return _fail(required, "credentials_not_configured")
    base = BINANCE_BASES[package_mode]
    try:
        server = _call(transport, base + "/api/v3/time")
        server_time = int(server["serverTime"])
        if server_time <= 0:
            raise (ValueError("non-positive server time"))
        query = urlencode({
            "omitZeroBalances": "true",
            "recvWindow": str(RECV_WINDOW_MS),
            "timestamp": str(server_time),
        })
        signature = hmac.new(secret.encode("utf-8"), query.encode("ascii"), hashlib.sha256).hexdigest()
        account = _call(
            transport,
            f"{base}/api/v3/account?{query}&signature={signature}",
            {"X-MBX-APIKEY": api_key},
        )
        if not isinstance(account, dict) or not isinstance(account.get("canTrade"), bool):
            raise ReadinessError("provider returned an invalid account response")
        return _pass(
            required,
            endpoint_mode=package_mode,
            authenticated=True,
            can_trade=account["canTrade"],
            account_type=str(account.get("accountType") or "UNKNOWN")[:32],
            order_methods_used=False,
        )
    except (ReadinessError, KeyError, TypeError, ValueError) as exc:
        reason = str(exc) if isinstance(exc, ReadinessError) else "provider returned an invalid account response"
        return _fail(required, reason)


def check_telegram(config: Mapping[str, Any], transport: Transport) -> Result:
    required = True
    token = str(config.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat = str(config.get("TELEGRAM_OWNER_CHAT_ID") or "").strip()
    if not _configured(token, ("REPLACE_WITH_REAL_TELEGRAM_TOKEN",)):
        return _fail(required, "token_not_configured")
    if not chat or not chat.lstrip("-").isdigit():
        return _fail(required, "owner_chat_id_not_configured")
    try:
        me = _call(transport, f"{TELEGRAM_BASE}/bot{token}/getMe")
        if not isinstance(me, dict) or me.get("ok") is not True:
            raise ReadinessError("provider rejected bot authentication")
        chat_result = _call(
            transport,
            f"{TELEGRAM_BASE}/bot{token}/getChat?{urlencode({'chat_id': chat})}",
        )
        if not isinstance(chat_result, dict) or chat_result.get("ok") is not True:
            raise ReadinessError("provider rejected owner chat access")
        bot = me.get("result") if isinstance(me.get("result"), dict) else {}
        return _pass(required, authenticated=True, owner_chat_accessible=True, bot_id=bot.get("id"))
    except ReadinessError as exc:
        return _fail(required, str(exc))


def check_coingecko(config: Mapping[str, Any], transport: Transport) -> Result:
    key = str(config.get("COINGECKO_API_KEY") or "").strip()
    required = _bool(config.get("ENABLE_COINGECKO_SIGNALS")) or _bool(
        config.get("SHARIA_AUTO_SOURCE_DISCOVERY_ENABLED"), default=True
    )
    if not _configured(key):
        return _fail(required, "key_not_configured") if required else _skip("key_not_configured")
    try:
        payload = _call(transport, COINGECKO_BASE + "/ping", {"x-cg-demo-api-key": key})
        if not isinstance(payload, dict) or "gecko_says" not in payload:
            raise ReadinessError("provider returned an invalid ping response")
        return _pass(required, authenticated=True, plan="demo_or_compatible")
    except ReadinessError as exc:
        return _fail(required, str(exc))


def check_coinmarketcap(config: Mapping[str, Any], transport: Transport) -> Result:
    key = str(config.get("COINMARKETCAP_API_KEY") or config.get("CMC_API_KEY") or "").strip()
    required = _bool(config.get("ENABLE_CMC_TRENDING")) or _bool(
        config.get("SHARIA_AUTO_SOURCE_DISCOVERY_ENABLED"), default=True
    )
    if not _configured(key):
        return _fail(required, "key_not_configured") if required else _skip("key_not_configured")
    try:
        payload = _call(transport, CMC_BASE + "/v1/key/info", {"X-CMC_PRO_API_KEY": key})
        if not isinstance(payload, dict):
            raise ReadinessError("provider returned an invalid key response")
        status = payload.get("status")
        if not isinstance(status, dict) or int(status.get("error_code", -1)) != 0:
            raise ReadinessError("provider rejected key authentication")
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        plan = data.get("plan") if isinstance(data.get("plan"), dict) else {}
        return _pass(required, authenticated=True, plan_name=str(plan.get("plan_name") or "configured")[:64])
    except (ReadinessError, TypeError, ValueError) as exc:
        reason = str(exc) if isinstance(exc, ReadinessError) else "provider returned an invalid key response"
        return _fail(required, reason)


def run(config: Mapping[str, Any], package_mode: str, transport: Transport = _http_json) -> dict[str, Any]:
    if package_mode not in BINANCE_BASES:
        raise ReadinessError("package mode must be testnet or live")
    providers = {
        "binance": check_binance(config, package_mode, transport),
        "telegram": check_telegram(config, transport),
        "coingecko": check_coingecko(config, transport),
        "coinmarketcap": check_coinmarketcap(config, transport),
    }
    required_failed = any(item.required and item.status != "PASS" for item in providers.values())
    return {
        "schema_version": 1,
        "ok": not required_failed,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "package_mode": package_mode,
        "network_operations": "GET_ONLY_NO_ORDERS",
        "providers": {name: result.as_dict() for name, result in providers.items()},
    }


def _config_from_stdin() -> Mapping[str, Any]:
    try:
        value = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        raise ReadinessError("configuration input is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ReadinessError("configuration input must be an object")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only API credential preflight")
    parser.add_argument("--package-mode", required=True, choices=sorted(BINANCE_BASES))
    args = parser.parse_args(argv)
    try:
        payload = run(_config_from_stdin(), args.package_mode)
    except ReadinessError as exc:
        payload = {
            "schema_version": 1,
            "ok": False,
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
            "package_mode": args.package_mode,
            "network_operations": "GET_ONLY_NO_ORDERS",
            "providers": {},
            "error": str(exc),
        }
    json.dump(payload, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    return 0 if payload.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
