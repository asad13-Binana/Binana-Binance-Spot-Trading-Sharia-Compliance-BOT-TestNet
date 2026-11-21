"""Two isolated support containers exercise signed projection, scanner health, and restart."""
import argparse
import base64
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import errno
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import subprocess
import tempfile
import time


SHARED = Path("/app/shared")
SCRIPT = "/ci_support_integration.py"
MARKET_HEALTH = "runtime/market_context/health.json"
SHARIA_HEALTH = "runtime/sharia_screener/health.json"
SYMBOL = "ADAUSDT"


def read(path):
    return json.loads(path.read_bytes())


def verify_image(mode):
    import services

    assert Path(services.__file__).resolve() == Path("/app/services/__init__.py")
    marker = Path("/app/RELEASE_MODE")
    assert mode in {"testnet", "live"} and marker.read_text().strip() == mode
    assert stat.S_IMODE(marker.stat().st_mode) == 0o444
    assert os.getuid() != 0


def run_projector():
    from Crypto.PublicKey import ECC
    from services.sharia_screener.manual_registry_service import main

    # Generate an ephemeral signing key inside the producer. Share the public bytes only.
    key = ECC.generate(curve="Ed25519")
    public_der = key.public_key().export_key(format="DER")
    private_der = key.export_key(format="DER", use_pkcs8=True)
    os.environ["SHARIA_RESULT_SIGNING_PRIVATE_KEY_B64"] = base64.b64encode(private_der).decode()
    os.environ["SHARIA_RESULT_VERIFY_PUBLIC_KEY_B64"] = base64.b64encode(public_der).decode()
    os.environ["SHARIA_APPROVAL_HMAC_KEY"] = secrets.token_hex(32)
    os.environ["SHARIA_RESULT_HMAC_KEY"] = secrets.token_hex(32)
    (SHARED / "sharia/ci-verifier.der").write_bytes(public_der)
    main()


def market_response(_client, path, params=None):
    bases = ("ADA", "DOGE")
    if path == "/api/v3/exchangeInfo":
        filters = [{"filterType": "NOTIONAL", "minNotional": "5"},
                   {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                   {"filterType": "LOT_SIZE", "stepSize": "0.1"},
                   {"filterType": "TRAILING_DELTA", "minTrailingAboveDelta": 10,
                    "maxTrailingAboveDelta": 2000, "minTrailingBelowDelta": 10,
                    "maxTrailingBelowDelta": 2000}]
        return {"symbols": [{"symbol": base + "USDT", "baseAsset": base, "quoteAsset": "USDT",
                             "status": "TRADING", "isSpotTradingAllowed": True,
                             "ocoAllowed": True, "otoAllowed": True, "allowTrailingStop": True,
                             "filters": filters} for base in bases]}
    if path == "/api/v3/ticker/24hr":
        return [{"symbol": base + "USDT", "priceChangePercent": "1",
                 "quoteVolume": "2000000"} for base in bases]
    if path == "/api/v3/ticker/bookTicker":
        return [{"symbol": base + "USDT", "bidPrice": "100", "askPrice": "100.01"}
                for base in bases]
    if path == "/api/v3/klines" and params["symbol"] == SYMBOL:
        return [[1577836800000]]
    raise AssertionError(f"Unexpected fixture market request: {path}")


def run_universe():
    from unittest.mock import patch
    from services.market_context.service import MarketContextService
    from services.universe_service import scanner

    public_der = (SHARED / "sharia/ci-verifier.der").read_bytes()
    os.environ["SHARIA_RESULT_VERIFY_PUBLIC_KEY_B64"] = base64.b64encode(public_der).decode()
    try:
        (SHARED / "sharia/.ci-write-probe").touch(exist_ok=False)
    except OSError as error:
        if error.errno != errno.EROFS:
            raise
    else:
        raise AssertionError("Universe can write the manual registry mount")

    with patch.object(scanner.BinancePublic, "get", market_response):
        snapshot = scanner.scan_once()
    assert snapshot["pairs"] == ["ADA/USDT"]
    rejections = read(SHARED / "universe/latest_rejections.json")["rejected"]
    assert any(row["symbol"] == "DOGEUSDT" and
               any(reason.startswith("sharia_") for reason in row["reasons"])
               for row in rejections)

    context = MarketContextService()
    context._refresh_universe(time.monotonic())

    class Socket:
        def send(self, raw):
            command = json.loads(raw)
            assert command["method"] == "SUBSCRIBE"
            context.stream._on_message(self, {"result": None, "id": command["id"]})

        def close(self):
            pass

    socket = Socket()
    context.stream._on_open(socket)
    sequence = 0
    try:
        while True:
            sequence += 1
            now_ms = int(time.time() * 1000)
            context.stream._on_message(socket, {"e": "aggTrade", "s": SYMBOL, "a": sequence,
                "p": "100", "q": "2", "m": False, "T": now_ms, "E": now_ms})
            context.stream._on_message(socket, {"s": SYMBOL, "u": sequence, "b": "100",
                "B": "2", "a": "100.01", "A": "2"})
            context._publish()
            time.sleep(1)
    finally:
        context.stop()


def docker(*args):
    return subprocess.run(["docker", *args], check=True, text=True,
                          stdout=subprocess.PIPE, timeout=60).stdout.strip()


def start(stack, image, root, role, mode):
    args = ["run", "--detach", "--pull=never", "--network=none", "--read-only",
            "--cap-drop=ALL", "--security-opt=no-new-privileges", "--pids-limit=128",
            "--cpus=1", "--memory=512m", "--user", f"{os.getuid()}:{os.getgid()}",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m,mode=1777",
            "--mount", f"type=bind,src={Path(__file__).resolve()},dst={SCRIPT},readonly",
            "--mount", f"type=bind,src={root},dst={SHARED}",
            "-e", "UNIVERSE_LIMIT=0", "-e", "ENABLE_COINGECKO_SIGNALS=false",
            "-e", "ENABLE_CMC_TRENDING=false"]
    if role == "universe":
        args += ["--mount", f"type=bind,src={root / 'sharia'},dst={SHARED / 'sharia'},readonly"]
    container = docker(*args, "--entrypoint", "python", image, "-B", SCRIPT, role, mode)
    stack.callback(docker, "rm", "--force", container)
    assert docker("inspect", "--format", "{{.HostConfig.NetworkMode}}", container) == "none"
    return container


def wait_health(container, path, since, previous_snapshot=None):
    # The packaged collector requires a full 60 seconds of continuous trade flow.
    deadline = time.monotonic() + 150
    while time.monotonic() < deadline:
        if docker("inspect", "--format", "{{.State.Running}}", container) != "true":
            raise RuntimeError(f"Support fixture exited: {docker('logs', '--tail', '20', container)}")
        if path.is_file():
            health = read(path)
            if (health.get("ok") is True and since <= health["ts"] <= time.time() + 1
                    and (previous_snapshot is None or
                         health.get("universe_snapshot_hash") != previous_snapshot)):
                return health
        time.sleep(0.5)
    raise TimeoutError(f"No fresh healthy publication at {path.name}")


def run_simulation(image, mode):
    if os.getuid() == 0:
        raise RuntimeError("Run the fixture as an unprivileged host user")
    with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
        root = Path(directory).resolve()
        sharia = root / "sharia"
        sharia.mkdir()
        today = datetime.now(timezone.utc).date()
        registry = {"schema_version": 1, "version": "ci-1", "symbols": [SYMBOL],
                    "last_reviewed": today.isoformat(),
                    "next_review": (today + timedelta(days=7)).isoformat()}
        (sharia / "halal_coins.json").write_text(json.dumps(registry), encoding="utf-8")
        start_time = time.time()
        projector = start(stack, image, root, "projector", mode)
        health = wait_health(projector, root / SHARIA_HEALTH, start_time)
        assert health["manual_registry_valid"] and health["eligible_assets"] == 1
        projection = read(sharia / "sharia_status.json")
        assert projection["registry_valid"] is True
        assert projection["projection_mode"] == "manual-registry/v1"
        assert [row["symbol"] for row in projection["records"]] == ["ADA"]
        assert projection["registry_sha256"] == hashlib.sha256((sharia / "halal_coins.json").read_bytes()).hexdigest()

        universe = start(stack, image, root, "universe", mode)
        health = wait_health(universe, root / MARKET_HEALTH, start_time)
        assert health["package_mode"] == mode and health["can_trade"] is False
        assert health["fresh_symbol_count"] == 1 and health["advisory_only"] is True
        pointer = root / "universe/current_pairlist.json"
        first = read(pointer)
        assert first["pairs"] == ["ADA/USDT"] and first["snapshot_hash"] == health["universe_snapshot_hash"]
        immutable = root / "universe/snapshots" / first["snapshot_file"]
        digest = hashlib.sha256(immutable.read_bytes()).hexdigest()

        restart_time = time.time()
        docker("restart", "--time", "2", universe)
        after = wait_health(universe, root / MARKET_HEALTH, restart_time, first["snapshot_hash"])
        current = read(pointer)
        assert current["pairs"] == first["pairs"] and current["snapshot_file"] != first["snapshot_file"]
        assert after["universe_snapshot_hash"] == current["snapshot_hash"] and after["can_trade"] is False
        assert hashlib.sha256(immutable.read_bytes()).hexdigest() == digest
        print(f"{mode}: two support services healthy, new scan observed, original snapshot preserved")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=["host", "projector", "universe"])
    parser.add_argument("mode", choices=["testnet", "live"])
    parser.add_argument("--image", default="binana-integration-ci")
    args = parser.parse_args()
    if args.role == "host":
        run_simulation(args.image, args.mode)
    else:
        verify_image(args.mode)
        if args.role == "projector":
            run_projector()
        else:
            run_universe()
