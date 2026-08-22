from __future__ import annotations

"""One credential-free, multiplexed Binance Spot WebSocket connection."""

import json
import logging
import random
import threading
import time
from collections.abc import Callable
from urllib.parse import urlparse

from .analytics import SpotMicrostructureAnalytics

log = logging.getLogger("spot-market-context-stream")

LIVE_ENDPOINT = "wss://stream.binance.com:9443/ws"
TESTNET_ENDPOINT = "wss://stream.testnet.binance.vision/ws"
PUBLIC_MARKET_ENDPOINT = "wss://data-stream.binance.vision/ws"
ALLOWED_HOSTS = {
    "data-stream.binance.vision",
    "stream.binance.com",
    "stream.testnet.binance.vision",
}
MAX_STREAMS = 100
MAX_PARAMS_PER_COMMAND = 200


def validated_endpoint(value: str, *, testnet: bool) -> str:
    # TestNet's Freqtrade signal engine and universe observe production Spot
    # public data; only authenticated execution is sent to Spot Testnet. Use
    # Binance's market-data-only host so evidence matches the signal market.
    expected = PUBLIC_MARKET_ENDPOINT if testnet else LIVE_ENDPOINT
    candidate = str(value or expected).strip()
    parsed = urlparse(candidate)
    expected_host = urlparse(expected).hostname
    if (
        parsed.scheme != "wss"
        or parsed.hostname not in ALLOWED_HOSTS
        or parsed.hostname != expected_host
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"/ws", "/stream"}
    ):
        raise ValueError(
            "Spot market stream endpoint must match the official package environment"
        )
    return candidate.rstrip("/")


class BoundedBackoff:
    def __init__(
        self,
        *,
        initial: float = 1.0,
        maximum: float = 60.0,
        jitter_ratio: float = 0.20,
        random_value: Callable[[], float] = random.random,
    ) -> None:
        if not 0 < initial <= maximum <= 300:
            raise ValueError("invalid reconnect backoff bounds")
        if not 0 <= jitter_ratio <= 0.50:
            raise ValueError("invalid reconnect jitter")
        self.initial = float(initial)
        self.maximum = float(maximum)
        self.jitter_ratio = float(jitter_ratio)
        self.random_value = random_value
        self.attempt = 0

    def next_delay(self) -> float:
        base = min(self.maximum, self.initial * (2 ** min(self.attempt, 20)))
        self.attempt += 1
        multiplier = 1 + ((self.random_value() * 2) - 1) * self.jitter_ratio
        return max(0.0, min(self.maximum, base * multiplier))

    def reset(self) -> None:
        self.attempt = 0


def _streams(symbols: set[str]) -> tuple[str, ...]:
    return tuple(
        stream
        for symbol in sorted(symbols)
        for stream in (f"{symbol.lower()}@aggTrade", f"{symbol.lower()}@bookTicker")
    )


class SpotMarketStream:
    """Supervise one Spot market-data socket and dynamic subscriptions."""

    def __init__(
        self,
        analytics: SpotMicrostructureAnalytics,
        *,
        endpoint: str,
        rotation_seconds: int = 82_200,
        command_interval_seconds: float = 0.25,
        websocket_factory=None,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        if not 3_600 <= int(rotation_seconds) <= 86_300:
            raise ValueError("rotation_seconds must be within 3600-86300")
        if not 0.20 <= float(command_interval_seconds) <= 5:
            raise ValueError("command interval must be within 0.20-5 seconds")
        self.analytics = analytics
        self.endpoint = endpoint
        self.rotation_seconds = int(rotation_seconds)
        self.command_interval_seconds = float(command_interval_seconds)
        self.websocket_factory = websocket_factory
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._desired: set[str] = set()
        self._subscribed: set[str] = set()
        self._pending: dict[int, tuple[str, tuple[str, ...]]] = {}
        self._request_id = 0
        self._last_command_mono = float("-inf")
        self._ws = None
        self._thread: threading.Thread | None = None
        self._rotation_timer: threading.Timer | None = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._send_lock = threading.Lock()
        self._backoff = BoundedBackoff()
        self._connected = False
        self._last_message_at: float | None = None
        self._last_market_event_at: float | None = None
        self._last_pong_at: float | None = None
        self._last_error: str | None = None
        self._reconnect_count = 0
        self._rotation_count = 0

    def status(self) -> dict:
        with self._lock:
            desired_streams = len(_streams(self._desired))
            return {
                "connected": self._connected,
                "desired_symbol_count": len(self._desired),
                "desired_stream_count": desired_streams,
                "subscribed_stream_count": len(self._subscribed),
                "subscription_ready": desired_streams > 0
                and len(self._subscribed) == desired_streams,
                "pending_command_count": len(self._pending),
                "reconnect_count": self._reconnect_count,
                "rotation_count": self._rotation_count,
                "last_message_at": self._last_message_at,
                "last_market_event_at": self._last_market_event_at,
                "last_pong_at": self._last_pong_at,
                "last_error": self._last_error,
                "endpoint_mode": (
                    "spot_testnet"
                    if "testnet" in self.endpoint
                    else "production_public_market"
                ),
            }

    def update_symbols(self, symbols: set[str] | list[str] | tuple[str, ...]) -> None:
        normalized = set(self.analytics.set_symbols(symbols))
        desired_streams = set(_streams(normalized))
        if len(desired_streams) > MAX_STREAMS:
            raise ValueError(
                f"subscription would exceed the {MAX_STREAMS}-stream safety ceiling"
            )
        with self._lock:
            previous = set(_streams(self._desired))
            self._desired = normalized
            connected = self._connected
        if not connected:
            return
        removed = tuple(sorted(previous - desired_streams))
        added = tuple(sorted(desired_streams - previous))
        if removed:
            self._send_control("UNSUBSCRIBE", removed)
        if added:
            self._send_control("SUBSCRIBE", added)

    def _send_control(self, method: str, streams: tuple[str, ...]) -> None:
        if method not in {"SUBSCRIBE", "UNSUBSCRIBE"}:
            raise ValueError("unsupported subscription command")
        for offset in range(0, len(streams), MAX_PARAMS_PER_COMMAND):
            chunk = tuple(streams[offset : offset + MAX_PARAMS_PER_COMMAND])
            with self._send_lock:
                delay = self.command_interval_seconds - (
                    self._monotonic() - self._last_command_mono
                )
                if delay > 0 and self._stop.wait(delay):
                    return
                with self._lock:
                    ws = self._ws
                    if not self._connected or ws is None:
                        return
                    self._request_id += 1
                    request_id = self._request_id
                    self._pending[request_id] = (method, chunk)
                try:
                    ws.send(
                        json.dumps(
                            {
                                "method": method,
                                "params": list(chunk),
                                "id": request_id,
                            },
                            separators=(",", ":"),
                        )
                    )
                    self._last_command_mono = self._monotonic()
                except Exception as exc:  # noqa: BLE001 - socket API boundary
                    with self._lock:
                        self._pending.pop(request_id, None)
                        self._last_error = "subscription_send_" + type(exc).__name__
                    self._close("subscription_send_failed")
                    return

    def _on_open(self, ws) -> None:
        with self._lock:
            self._ws = ws
            self._connected = True
            self._subscribed.clear()
            self._pending.clear()
            self._last_error = None
            desired = _streams(self._desired)
        self._schedule_rotation()
        if desired:
            self._send_control("SUBSCRIBE", desired)

    def _on_message(self, _ws, raw: object) -> None:
        now_wall = self._wall_clock()
        now_mono = self._monotonic()
        with self._lock:
            self._last_message_at = now_wall
        try:
            message = (
                json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else raw
            )
            if not isinstance(message, dict):
                raise TypeError("market stream message is not an object")
            request_id = message.get("id")
            if request_id is not None and ("result" in message or "code" in message):
                request_id = int(request_id)
                with self._lock:
                    pending = self._pending.pop(request_id, None)
                if pending is None:
                    return
                method, streams = pending
                if message.get("code") is not None or message.get("result") is not None:
                    with self._lock:
                        self._last_error = "subscription_rejected"
                    self._close("subscription_rejected")
                    return
                with self._lock:
                    if method == "SUBSCRIBE":
                        self._subscribed.update(streams)
                    else:
                        self._subscribed.difference_update(streams)
                return
            payload = message.get("data", message)
            if not isinstance(payload, dict):
                raise TypeError("market stream payload is not an object")
            event_type = payload.get("e")
            if event_type == "aggTrade":
                accepted = self.analytics.ingest_agg_trade(
                    payload, received_mono=now_mono, received_wall=now_wall
                )
            elif all(field in payload for field in ("u", "s", "b", "B", "a", "A")):
                accepted = self.analytics.ingest_book_ticker(
                    payload, received_mono=now_mono, received_wall=now_wall
                )
            elif event_type == "serverShutdown":
                with self._lock:
                    self._last_error = "server_shutdown"
                self._close("server_shutdown")
                return
            else:
                raise ValueError("unknown Spot market stream payload")
            if accepted:
                with self._lock:
                    self._last_market_event_at = now_wall
                    self._last_error = None
                self._backoff.reset()
        except Exception as exc:  # noqa: BLE001 - untrusted stream payload
            with self._lock:
                self._last_error = "malformed_" + type(exc).__name__
            log.warning("rejected malformed Spot market frame: %s", type(exc).__name__)

    def _on_error(self, _ws, error) -> None:
        with self._lock:
            self._last_error = "websocket_" + type(error).__name__
        log.warning("Spot market WebSocket error: %s", type(error).__name__)

    def _on_close(self, _ws, *_args) -> None:
        self._cancel_rotation()
        with self._lock:
            self._connected = False
            self._subscribed.clear()
            self._pending.clear()

    def _on_pong(self, _ws, _payload) -> None:
        with self._lock:
            self._last_pong_at = self._wall_clock()

    def _schedule_rotation(self) -> None:
        self._cancel_rotation()
        timer = threading.Timer(self.rotation_seconds, self._rotate)
        timer.daemon = True
        self._rotation_timer = timer
        timer.start()

    def _cancel_rotation(self) -> None:
        timer, self._rotation_timer = self._rotation_timer, None
        if timer is not None:
            timer.cancel()

    def _rotate(self) -> None:
        with self._lock:
            self._rotation_count += 1
            self._last_error = "proactive_24h_rotation"
        self._close("proactive_rotation")

    def _close(self, reason: str) -> None:
        with self._lock:
            ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001 - close is best effort
                log.warning("closing Spot market socket after %s failed", reason)

    def _factory(self):
        if self.websocket_factory is not None:
            return self.websocket_factory
        import websocket

        return websocket.WebSocketApp

    def _supervise(self) -> None:
        while not self._stop.is_set():
            factory = self._factory()
            ws = factory(
                self.endpoint,
                on_open=self._on_open,
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close,
                on_pong=self._on_pong,
            )
            with self._lock:
                self._ws = ws
            try:
                ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as exc:  # noqa: BLE001 - socket API boundary
                with self._lock:
                    self._last_error = "run_forever_" + type(exc).__name__
            finally:
                self._on_close(ws)
            if self._stop.is_set():
                break
            with self._lock:
                self._reconnect_count += 1
            self._stop.wait(self._backoff.next_delay())

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._supervise,
                daemon=True,
                name="binana-spot-market-context",
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._cancel_rotation()
        self._close("clean_shutdown")
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=5)
