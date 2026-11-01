"""Credential-free support image import and current-panel compatibility gate."""
from __future__ import annotations

import ast
import importlib
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile


def denied(*args, **kwargs):
    raise RuntimeError("OFFLINE_IMAGE_CHECK_DENIES_EXTERNAL_EFFECTS")


def required_panel_attributes(source):
    """Find broker inputs; assigning a replacement does not require an old value."""
    tree = ast.parse(source)
    required = {
        node.attr for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
        and node.value.id == "base" and isinstance(node.ctx, (ast.Load, ast.Del))
    }
    for node in ast.walk(tree):
        if (isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Attribute)
                and isinstance(node.target.value, ast.Name) and node.target.value.id == "base"):
            required.add(node.target.attr)
    return required


def main():
    for name in ("BINANCE_API_KEY", "BINANCE_API_SECRET", "BINANCE_TESTNET_API_KEY",
                 "BINANCE_LIVE_API_KEY", "BINANCE_LIVE_API_SECRET",
                 "BINANCE_TESTNET_API_SECRET", "TELEGRAM_BOT_TOKEN",
                 "FREQTRADE__EXCHANGE__KEY", "FREQTRADE__EXCHANGE__SECRET"):
        if os.environ.get(name):
            raise RuntimeError("NO_CREDENTIALS_PERMITTED_IN_IMAGE_CHECK")
    socket.socket.connect = denied
    socket.socket.connect_ex = denied
    socket.create_connection = denied
    socket.getaddrinfo = denied
    # ctypes may invoke the read-only native linker lookup during this pinned
    # dependency import. Bootstrap it after network denial, before forbidding
    # subprocesses for all service entrypoint imports. The container itself is
    # read-only, unprivileged and has no network or host runtime access.
    importlib.import_module("Crypto.PublicKey.ECC")
    subprocess.Popen = denied
    with tempfile.TemporaryDirectory(prefix="binana-support-offline-") as temporary:
        os.environ["SHARED_ROOT"] = temporary
        os.environ["UNIVERSE_LIMIT"] = "0"
        modules = (
            "services.market_context.universe_main",
            "services.sharia_screener.manual_registry_service",
            "services.sharia_screener.service", "services.sharia_egress_proxy.server",
            "runtime_stable_panel", "oracle_panel", "owner_monitor",
        )
        for name in modules:
            importlib.import_module(name)
        panel = importlib.import_module("runtime_stable_panel")
        attributes = required_panel_attributes(Path(panel.__file__).read_text())
        missing = sorted(name for name in attributes if not hasattr(panel.base, name))
        if missing:
            raise RuntimeError("PANEL_BROKER_INTERFACE_MISSING:" + ",".join(missing))
        if importlib.util.find_spec("services.execution_sidecar") is not None:
            raise RuntimeError("OLD_EXECUTION_ENGINE_PRESENT")
        if Path("/app/legacy_core").exists():
            raise RuntimeError("OLD_EXECUTION_CORE_PRESENT")
        scanner = importlib.import_module("services.universe_service.scanner")
        scanner.validate_runtime_settings()
        if scanner.LIMIT != 0:
            raise RuntimeError("ALL_ELIGIBLE_UNIVERSE_DEFAULT_LOST")
        if scanner.EXCLUDED != {"BTC", "ETH", "BNB", "SOL", "XRP"}:
            raise RuntimeError("OWNER_ASSET_EXCLUSIONS_CHANGED")
        from services.common.sharia_v19 import load_controller
        load_controller("/app/controllers/HALAL_CRYPTO_SPOT_SCREENING_V19_3_PRODUCTION.json")
        print(json.dumps({
            "imports_verified": list(modules),
            "panel_base_attributes_verified": len(attributes),
            "network_and_subprocess_denied": True,
            "native_crypto_loaded_before_subprocess_guard": True,
            "old_order_engine_absent": True,
            "host_accepted": False, "trading_authorized": False,
        }, sort_keys=True))


if __name__ == "__main__":
    main()
