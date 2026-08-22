from __future__ import annotations

"""Advisory Spot microstructure collector embedded in the universe container.

The universe container has no Binance trading credentials or order methods.
Running this bounded collector there preserves the existing four-bot Oracle
container/resource contract while keeping the implementation separate from the
protected strategy and the order-owning execution sidecar.
"""

import logging
import os
import threading
import time
from pathlib import Path

from services.common.atomic import atomic_write_json
from services.universe_service.snapshot_store import UniverseSnapshotError, load_current

from .analytics import SpotMicrostructureAnalytics
from .spot_stream import (
    LIVE_ENDPOINT,
    PUBLIC_MARKET_ENDPOINT,
    SpotMarketStream,
    validated_endpoint,
)

log = logging.getLogger("spot-market-context")
_START_LOCK = threading.Lock()
_THREAD: threading.Thread | None = None


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name, str(default).lower()).strip().lower()
    if value not in {"true", "false"}:
        raise ValueError(f"{name} must be true or false")
    return value == "true"


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be within {minimum}-{maximum}")
    return value


def package_mode() -> str:
    path = Path(__file__).resolve().parents[2] / "RELEASE_MODE"
    try:
        value = path.read_text(encoding="utf-8").strip().lower()
    except OSError as exc:
        raise RuntimeError("market context cannot read immutable RELEASE_MODE") from exc
    if value not in {"testnet", "live"}:
        raise RuntimeError("market context package mode is invalid")
    return value


class MarketContextService:
    def __init__(self) -> None:
        shared = Path(os.getenv("SHARED_ROOT", "/app/shared"))
        self.universe_path = Path(
            os.getenv("UNIVERSE_FILE", shared / "universe/current_pairlist.json")
        )
        self.snapshot_path = Path(
            os.getenv("MARKET_CONTEXT_FILE", shared / "market_context/current.json")
        )
        self.health_path = Path(
            os.getenv(
                "MARKET_CONTEXT_HEALTH_FILE",
                shared / "runtime/market_context/health.json",
            )
        )
        self.publish_seconds = _env_int("MARKET_CONTEXT_PUBLISH_SECONDS", 5, 1, 60)
        self.universe_poll_seconds = _env_int(
            "MARKET_CONTEXT_UNIVERSE_POLL_SECONDS", 15, 5, 300
        )
        self.universe_max_age = _env_int("MAX_UNIVERSE_AGE_SECONDS", 1800, 60, 86_400)
        max_age_ms = _env_int("MARKET_CONTEXT_MAX_AGE_MS", 15_000, 250, 60_000)
        rotation = _env_int("MARKET_CONTEXT_ROTATION_SECONDS", 82_200, 3_600, 86_300)
        mode = package_mode()
        endpoint_default = (
            PUBLIC_MARKET_ENDPOINT if mode == "testnet" else LIVE_ENDPOINT
        )
        endpoint = validated_endpoint(
            os.getenv("BINANCE_SPOT_MARKET_STREAM", endpoint_default),
            testnet=mode == "testnet",
        )
        self.mode = mode
        self.analytics = SpotMicrostructureAnalytics(max_age_ms=max_age_ms)
        self.stream = SpotMarketStream(
            self.analytics, endpoint=endpoint, rotation_seconds=rotation
        )
        self._stop = threading.Event()
        self._last_universe_poll = float("-inf")
        self._last_universe_error: str | None = None
        self._universe_snapshot_hash: str | None = None

    @staticmethod
    def _symbols(pairs: object) -> set[str]:
        if not isinstance(pairs, list):
            raise UniverseSnapshotError("market context universe pairs must be a list")
        symbols = set()
        for pair in pairs:
            if not isinstance(pair, str) or not pair.endswith("/USDT"):
                raise UniverseSnapshotError(
                    "market context received an invalid pair identity"
                )
            symbols.add(pair.replace("/", ""))
        return symbols

    def _refresh_universe(self, now_mono: float) -> None:
        if now_mono - self._last_universe_poll < self.universe_poll_seconds:
            return
        self._last_universe_poll = now_mono
        try:
            snapshot = load_current(
                self.universe_path,
                max_age_seconds=self.universe_max_age,
                require_nonempty=True,
            )
            self.stream.update_symbols(self._symbols(snapshot.get("pairs")))
            self._universe_snapshot_hash = (
                str(snapshot.get("snapshot_hash") or "") or None
            )
            self._last_universe_error = None
        except Exception as exc:  # noqa: BLE001 - advisory isolation boundary
            # Advisory failure policy: retain the last known active set, mark
            # health degraded, and let universe/trading behavior continue.
            self._last_universe_error = type(exc).__name__

    def _publish(self) -> None:
        snapshot = self.analytics.snapshot()
        snapshot["package_mode"] = self.mode
        snapshot["universe_snapshot_hash"] = self._universe_snapshot_hash
        atomic_write_json(self.snapshot_path, snapshot)
        stream = self.stream.status()
        ready = bool(
            stream["subscription_ready"] and snapshot["fresh_symbol_count"] > 0
        )
        atomic_write_json(
            self.health_path,
            {
                "schema_version": 1,
                "ok": ready,
                "ts": time.time(),
                "status": "fresh" if ready else "degraded",
                "advisory_only": True,
                "spot_only": True,
                "can_trade": False,
                "package_mode": self.mode,
                "symbol_count": snapshot["symbol_count"],
                "fresh_symbol_count": snapshot["fresh_symbol_count"],
                "universe_snapshot_hash": self._universe_snapshot_hash,
                "universe_error": self._last_universe_error,
                "stream": stream,
                "statistics": snapshot["statistics"],
            },
        )

    def run(self) -> None:
        self.stream.start()
        try:
            while not self._stop.is_set():
                started = time.monotonic()
                self._refresh_universe(started)
                try:
                    self._publish()
                except Exception as exc:  # noqa: BLE001 - keep scanner independent
                    # A read-only evidence write must never terminate the
                    # universe process or alter strategy behavior.
                    log.warning("market-context publish failed: %s", type(exc).__name__)
                elapsed = time.monotonic() - started
                self._stop.wait(max(0.1, self.publish_seconds - elapsed))
        finally:
            self.stream.stop()

    def stop(self) -> None:
        self._stop.set()
        self.stream.stop()


def start_background() -> threading.Thread | None:
    """Start once when enabled; return immediately to universe scanning."""
    global _THREAD
    if not _env_bool("ENABLE_SPOT_MICROSTRUCTURE", True):
        return None
    with _START_LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            return _THREAD

        def target() -> None:
            try:
                MarketContextService().run()
            except Exception as exc:
                # The outermost boundary is deliberately failure-isolated.
                log.exception(
                    "market-context collector stopped: %s", type(exc).__name__
                )

        _THREAD = threading.Thread(
            target=target,
            daemon=True,
            name="binana-market-context-service",
        )
        _THREAD.start()
        return _THREAD
