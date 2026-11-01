"""Offline validation of an immutable Oracle bundle and private startup inputs.

This validates files only. It never contacts an exchange, starts containers,
renews a certificate, asserts acceptance, or enables trading.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re

SECRET_FIELDS = {
    "key", "secret", "password", "jwt_secret_key", "ws_token", "token",
    "apikey", "api_key", "api_secret",
}


def verify_bundle(root):
    root = Path(root)
    manifest = json.loads((root / "BUNDLE_MANIFEST.json").read_text())
    if manifest.get("schema") != "binana-oracle-owner-bundle-v1":
        raise ValueError("BUNDLE_SCHEMA")
    environment = manifest.get("execution_environment")
    if environment not in {"testnet", "live"}:
        raise ValueError("BUNDLE_ENVIRONMENT_REQUIRED")
    expected = manifest["files"]
    actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
    if actual != set(expected) | {"BUNDLE_MANIFEST.json"}:
        raise ValueError("BUNDLE_FILE_SET_MISMATCH")
    for relative, record in expected.items():
        path = PurePosixPath(relative)
        if path.is_absolute() or ".." in path.parts or "\\" in relative:
            raise ValueError("BUNDLE_UNSAFE_PATH")
        target = root / relative
        if any(p.is_symlink() for p in (target, *target.parents) if p != root.parent):
            raise ValueError("BUNDLE_SYMLINK")
        raw = target.read_bytes()
        if len(raw) != record["bytes"] or hashlib.sha256(raw).hexdigest() != record["sha256"]:
            raise ValueError("BUNDLE_CONTENT_MISMATCH:" + relative)
    profile = ast.parse((root / "owner/owner/freqtrade/binana/package_profile.py").read_text())
    values = [ast.literal_eval(n.value) for n in profile.body
              if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "PACKAGE_ENVIRONMENT" for t in n.targets)]
    if values != [environment]:
        raise ValueError("BUNDLE_ENVIRONMENT_MISMATCH")
    return manifest


def validate_config(config, environment="testnet"):
    if environment not in {"testnet", "live"}:
        raise ValueError("INVALID_PACKAGE_ENVIRONMENT")
    policy, exchange = config.get("binana", {}), config.get("exchange", {})
    expected = {
        "strategy": "BinanaNfiSpot", "strategy_path": "/freqtrade/binana-strategies",
        "initial_state": "stopped", "trading_mode": "spot", "margin_mode": "",
        "timeframe": "5m", "stake_currency": "USDT", "stake_amount": 250,
        "max_open_trades": 4, "dry_run": False, "force_entry_enable": False,
        "position_adjustment_enable": False,
        "db_url": "sqlite:////freqtrade/shared/freqtrade/binana-owner.sqlite",
    }
    if any(config.get(key) != value for key, value in expected.items()):
        raise ValueError("STOPPED_OWNER_CONFIG_REQUIRED")
    if any(policy.get(key) != value for key, value in {
        "environment": environment, "enabled": True, "spot_only": True,
        "allow_live": False, "allocation_usdt": 1000, "single_halal_decision": True,
    }.items()):
        raise ValueError("PACKAGE_POLICY_REQUIRED")
    if exchange.get("name") != "binance" or exchange.get("binana_testnet") is not (environment == "testnet"):
        raise ValueError("PACKAGE_EXCHANGE_REQUIRED")
    if environment == "live":
        if policy.get("live_activation") != "" or "testnet_epoch_id" in policy:
            raise ValueError("LIVE_PREPARATION_MUST_REMAIN_DISABLED")
        account = policy.get("account_scope", "")
        if (not isinstance(account, str) or "REPLACE" in account
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}", account)):
            raise ValueError("LIVE_ACCOUNT_SCOPE_REQUIRED")
    epoch = policy.get("testnet_epoch_id" if environment == "testnet" else "exchange_epoch_id", "")
    if not isinstance(epoch, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}", epoch):
        raise ValueError("RECONCILED_EXCHANGE_EPOCH_REQUIRED")
    if "REPLACE" in epoch or epoch == "binana-20260910-owner-cutover-1":
        raise ValueError("RECONCILED_EXCHANGE_EPOCH_REQUIRED")
    # URL/sandbox overrides may silently replace the patched Testnet contract.
    for key in ("ccxt_config", "ccxt_async_config"):
        if exchange.get(key, {}):
            raise ValueError("CUSTOM_CCXT_OVERRIDES_REQUIRE_REVIEW")
    if config.get("telegram", {}).get("enabled") is not False:
        raise ValueError("DUPLICATE_TELEGRAM_POLLER")
    api = config.get("api_server", {})
    if api.get("enabled") is not True or api.get("listen_port") != 8080:
        raise ValueError("OWNER_INTERNAL_API_REQUIRED")

    def secrets(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if str(key).lower() in SECRET_FIELDS and child:
                    raise ValueError("CONFIG_CONTAINS_SECRET_USE_SCOPED_ENV")
                secrets(child)
        elif isinstance(value, list):
            for child in value:
                secrets(child)
    secrets(config)


def parse_environment(path):
    result = {}
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError("INVALID_ENVIRONMENT_LINE")
        key, value = line.split("=", 1)
        if key in result or not re.fullmatch("[A-Z][A-Z0-9_]*", key):
            raise ValueError("INVALID_OR_DUPLICATE_ENVIRONMENT_KEY")
        # No shell interpretation, interpolation, or executable .env loading.
        if ("$" in value or "`" in value or "\x00" in value
                or any(c.isspace() for c in value) or value[:1] in {"'", '"'}):
            raise ValueError("ENVIRONMENT_INTERPOLATION_FORBIDDEN")
        result[key] = value
    return result


def validate_private_inputs(env_path, environment="testnet"):
    env_path = Path(env_path)
    if env_path.is_symlink() or not env_path.is_file():
        raise ValueError("PRIVATE_ENVIRONMENT_REQUIRED")
    if os.name == "posix" and env_path.stat().st_mode & 0o077:
        raise ValueError("PRIVATE_ENVIRONMENT_REQUIRES_MODE_600")
    env = parse_environment(env_path)
    if environment not in {"testnet", "live"}:
        raise ValueError("INVALID_PACKAGE_ENVIRONMENT")
    key_prefix = "BINANCE_" + environment.upper() + "_API_"
    for name in (
        key_prefix + "KEY", key_prefix + "SECRET", "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_OWNER_CHAT_ID", "FREQTRADE_API_USERNAME", "FREQTRADE_API_PASSWORD",
        "FREQTRADE_API_JWT_SECRET", "FREQTRADE_API_WS_TOKEN", "SIGNAL_HMAC_KEY",
        "COMMAND_HMAC_KEY", "SHARIA_HMAC_KEY", "SHARIA_APPROVAL_HMAC_KEY",
        "SHARIA_RESULT_HMAC_KEY", "SHARIA_RESULT_SIGNING_PRIVATE_KEY_B64",
        "SHARIA_RESULT_VERIFY_PUBLIC_KEY_B64", "BOT_INSTANCE_ID",
    ):
        if not env.get(name, "").strip() or "REPLACE" in env[name]:
            raise ValueError("PRIVATE_VALUE_REQUIRED:" + name)
        if name.endswith("HMAC_KEY") and len(env[name]) < 32:
            raise ValueError("PRIVATE_BUS_KEY_TOO_SHORT:" + name)
    paths = {}
    for name in ("BINANA_CONFIG_DIR", "BINANA_STATE_DIR", "BINANA_RELEASE_DIR"):
        path = Path(env.get(name, ""))
        if not path.is_absolute() or not path.is_dir() or path.is_symlink():
            raise ValueError("PRIVATE_DIRECTORY_REQUIRED:" + name)
        if any(parent.is_symlink() for parent in path.parents):
            raise ValueError("PRIVATE_DIRECTORY_SYMLINK:" + name)
        paths[name] = path
    for name, path in paths.items():
        if any(path == other or path in other.parents or other in path.parents
               for other_name, other in paths.items() if other_name != name):
            raise ValueError("PRIVATE_DIRECTORIES_MUST_BE_DISJOINT")
    config_dir = paths["BINANA_CONFIG_DIR"]
    if (config_dir / "config.json").is_symlink():
        raise ValueError("PRIVATE_CONFIG_SYMLINK")
    validate_config(json.loads((config_dir / "config.json").read_text()), environment)
    verifier = config_dir / "release-authority.ed25519.pub"
    if verifier.is_symlink() or not verifier.is_file() or len(verifier.read_bytes()) != 32:
        raise ValueError("RELEASE_PUBLIC_VERIFIER_REQUIRED")
    # Never mount a private release issuer or an unrelated host secret directory.
    if {p.name for p in config_dir.iterdir()} != {"config.json", "release-authority.ed25519.pub"}:
        raise ValueError("PRIVATE_CONFIG_DIRECTORY_NOT_MINIMAL")
    state = paths["BINANA_STATE_DIR"]
    for name in ("freqtrade", "freqtrade/logs", "sharia", "universe", "runtime"):
        if not (state / name).is_dir() or (state / name).is_symlink():
            raise ValueError("PERSISTENT_DIRECTORY_REQUIRED:" + name)
    registry = state / "sharia/halal_coins.json"
    if not registry.is_file():
        raise ValueError("OWNER_MAINTAINED_HALAL_REGISTRY_REQUIRED")
    if env.get("BINANA_RESEARCH_ENABLED", "false") == "true":
        for name in ("sharia_research", "runtime/sharia_research", "audit"):
            if not (state / name).is_dir() or (state / name).is_symlink():
                raise ValueError("RESEARCH_DIRECTORY_REQUIRED:" + name)
        source_registry = state / "sharia_research/source_registry.json"
        if not source_registry.is_file() or source_registry.is_symlink():
            raise ValueError("RESEARCH_SOURCE_REGISTRY_REQUIRED")
    # Presence alone is not authorization. The image verifies exact binding,
    # Ed25519 signature, evidence flags and expiry before any new entry.
    release = paths["BINANA_RELEASE_DIR"]
    permitted = {"deployment.json", "certificate.json"}
    if {p.name for p in release.iterdir()} - permitted:
        raise ValueError("RELEASE_MOUNT_MUST_NOT_CONTAIN_ISSUER")
    return {
        "configuration_valid": True, "initial_state": "stopped",
        "certificate_present": (release / "certificate.json").is_file(),
        "host_accepted": False, "trading_authorized": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args()
    receipt = verify_bundle(args.bundle)
    result = {"bundle_verified": True, "bundle_id": receipt["bundle_id"],
              "host_accepted": False, "trading_authorized": False}
    if args.env_file:
        result.update(validate_private_inputs(args.env_file, receipt["execution_environment"]))
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
