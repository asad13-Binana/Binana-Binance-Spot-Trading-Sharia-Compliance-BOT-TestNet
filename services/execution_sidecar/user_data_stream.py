# Adopted (Step 2 selective adoption) from the ChatGPT-built V10.1/V19.1 package,
# verified against the official Binance Spot changelog: the listen-key REST
# /api/v3/userDataStream endpoints were retired 2026-02-20 07:00 UTC, so the
# preserved legacy listen-key transport no longer works against current Binance.
# This WebSocket API (userDataStream.subscribe.signature) transport replaces it
# at runtime without editing the byte-preserved legacy core. Constructor signature
# matches legacy UserDataStream exactly. NOT integration-validated here (requires
# Binance Spot Testnet WebSocket); unit-tested for signing/dispatch/reconnect.
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path

from services.common.atomic import atomic_write_json


log = logging.getLogger("modern-user-data-stream")


class ModernUserDataStream:
    """Binance Spot WebSocket API user stream (post-listenKey retirement).

    Uses `userDataStream.subscribe.signature`, supported by HMAC API keys, and
    unwraps the WebSocket API event envelope before forwarding legacy callbacks.
    No retired REST `/api/v3/userDataStream` endpoint is called.
    """

    def __init__(self, broker, on_order_update=None, on_list_update=None, on_resync=None, testnet=True):
        self.broker = broker
        self.on_order_update = on_order_update
        self.on_list_update = on_list_update
        self.on_resync = on_resync
        self.testnet = bool(testnet)
        self._ws = None
        self._running = False
        self._ready = threading.Event()
        self._startup_error: Exception | None = None
        self._request_id = ""
        self._backoff = 1.0
        self._reconnect_lock = threading.Lock()
        self._connected = False
        self._subscribed = False
        self._reconnect_count = 0
        self._last_message_at = None
        self._last_event_at = None
        self._last_error = None
        shared = Path(os.getenv('SHARED_ROOT', '/app/shared'))
        self._health_path = shared / 'runtime/user_stream_health.json'

    def _write_health(self):
        """Publish credential-free stream state for the read-only monitor."""
        try:
            atomic_write_json(self._health_path, {
                'ok': bool(self._connected and self._subscribed),
                'ts': time.time(),
                'mode': 'testnet' if self.testnet else 'live',
                'connected': self._connected,
                'subscribed': self._subscribed,
                'reconnect_count': self._reconnect_count,
                'last_message_at': self._last_message_at,
                'last_event_at': self._last_event_at,
                'last_error': self._last_error,
            })
        except Exception as exc:
            # Observability must never take down the user-data stream.
            log.warning("could not write user-stream health: %s", type(exc).__name__)

    @property
    def endpoint(self) -> str:
        return (
            "wss://ws-api.testnet.binance.vision:443/ws-api/v3"
            if self.testnet
            else "wss://ws-api.binance.com:443/ws-api/v3"
        )

    @staticmethod
    def _credentials() -> tuple[str, str]:
        api_key = os.getenv("BINANCE_API_KEY", "")
        secret = os.getenv("BINANCE_API_SECRET", "")
        if not api_key or not secret:
            raise RuntimeError("BINANCE_API_KEY and BINANCE_API_SECRET are required for user-data streaming")
        return api_key, secret

    def _subscription_request(self) -> dict:
        api_key, secret = self._credentials()
        params = {
            "apiKey": api_key,
            "recvWindow": 5000,
            "timestamp": int(time.time() * 1000),
        }
        query = "&".join(f"{key}={params[key]}" for key in sorted(params))
        params["signature"] = hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        self._request_id = uuid.uuid4().hex
        return {
            "id": self._request_id,
            "method": "userDataStream.subscribe.signature",
            "params": params,
        }

    def _on_open(self, ws):
        self._connected = True
        self._subscribed = False
        self._last_error = None
        self._write_health()
        try:
            ws.send(json.dumps(self._subscription_request(), separators=(",", ":")))
        except Exception as exc:
            self._startup_error = exc
            self._last_error = type(exc).__name__
            self._write_health()
            self._ready.set()
            ws.close()

    def _dispatch_event(self, event: dict):
        event_type = event.get("e") or event.get("eventType")
        if event_type == "executionReport" and self.on_order_update:
            self.on_order_update(event)
        elif event_type == "listStatus" and self.on_list_update:
            self.on_list_update(event)
        # UDS-EVENT-001: outboundAccountPosition was the ONLY account event that
        # triggered reconciliation. Binance also emits balanceUpdate (deposit,
        # withdrawal, transfer) and externalLockUpdate (balance locked/unlocked
        # outside the order flow). Ignoring them left local balances stale until
        # some later refresh happened to catch up.
        elif event_type in ("outboundAccountPosition", "balanceUpdate",
                            "externalLockUpdate") and self.on_resync:
            log.info("account event %s -> reconciliation", event_type)
            self.on_resync()
        elif event_type == "eventStreamTerminated":
            log.warning("Binance terminated the user-data subscription")
            self._close_socket("eventStreamTerminated")
        # H-001: Binance's documented serverShutdown notice requires the client
        # to disconnect and establish a NEW connection. Previously it was
        # ignored, so the socket could linger while no events arrived.
        elif event_type == "serverShutdown":
            log.warning("Binance announced serverShutdown; reconnecting")
            self._last_error = "server_shutdown"
            self._close_socket("serverShutdown")

    def _close_socket(self, reason: str) -> None:
        """Close the socket so the supervising reconnect loop takes over."""
        self._subscribed = False
        self._write_health()
        ws = self._ws
        if ws is None:
            return
        try:
            ws.close()
        except Exception as exc:  # never raise out of a callback
            log.warning("closing user-data socket after %s failed: %s", reason, exc)

    def _on_message(self, _ws, raw):
        try:
            self._last_message_at = time.time()
            message = json.loads(raw)
            if message.get("id") == self._request_id:
                if int(message.get("status", 0)) != 200:
                    # H-001 / UDS-STARTUP-001: a rejected subscription used to
                    # leave a CONNECTED but UNSUBSCRIBED socket alive — no
                    # events arriving, no reconnect, and (before the health fix
                    # below) a container still reporting healthy. Treat a
                    # non-200 acknowledgement as fatal for this connection and
                    # let the reconnect loop establish a fresh one.
                    self._startup_error = RuntimeError("Binance rejected user-data subscription")
                    self._subscribed = False
                    self._last_error = "subscription_rejected"
                    self._write_health()
                    self._ready.set()
                    self._close_socket("subscription_rejected")
                    return
                else:
                    self._backoff = 1.0
                    self._subscribed = True
                    self._last_error = None
                self._write_health()
                self._ready.set()
                return
            event = message.get("event", message)
            if isinstance(event, dict):
                self._last_event_at = time.time()
                self._dispatch_event(event)
            self._write_health()
        except Exception as exc:
            log.error("invalid Binance user-data frame: %s", exc)
            self._last_error = type(exc).__name__
            self._write_health()

    def _on_pong(self, _ws, _payload):
        self._last_message_at = time.time()
        self._write_health()

    def _on_error(self, _ws, error):
        if not self._ready.is_set():
            self._startup_error = RuntimeError(f"user-data WebSocket error: {error}")
            self._ready.set()
        log.warning("user-data WebSocket error: %s", error)
        self._last_error = type(error).__name__
        self._write_health()

    def _on_close(self, _ws, *_args):
        self._connected = False
        self._subscribed = False
        self._write_health()
        if not self._ready.is_set():
            self._startup_error = RuntimeError("user-data WebSocket closed before subscription acknowledgement")
            self._ready.set()
        if self._running:
            self._schedule_reconnect()

    def _schedule_reconnect(self):
        if not self._reconnect_lock.acquire(blocking=False):
            return
        try:
            self._reconnect_count += 1
            self._write_health()
            if self.on_resync:
                try:
                    self.on_resync()
                except Exception as exc:
                    log.error("reconciliation before stream reconnect failed: %s", exc)
            delay = min(self._backoff, 60.0)
            self._backoff = min(self._backoff * 2, 60.0)

            def reconnect():
                try:
                    if self._running:
                        self._connect(wait_for_ready=False)
                finally:
                    self._reconnect_lock.release()

            threading.Timer(delay, reconnect).start()
        except Exception:
            self._reconnect_lock.release()
            raise

    def _connect(self, *, wait_for_ready: bool):
        import websocket

        self._ready.clear()
        self._startup_error = None
        self._ws = websocket.WebSocketApp(
            self.endpoint,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
            on_pong=self._on_pong,
        )
        thread = threading.Thread(
            target=lambda: self._ws.run_forever(ping_interval=20, ping_timeout=10),
            daemon=True,
            name="binance-ws-api-user-data",
        )
        thread.start()
        if wait_for_ready:
            timeout = float(os.getenv("BINANCE_USER_STREAM_START_TIMEOUT_SECONDS", "15"))
            if not self._ready.wait(timeout):
                self._ws.close()
                raise TimeoutError("Binance user-data subscription acknowledgement timed out")
            if self._startup_error:
                raise self._startup_error

    def start(self):
        self._credentials()
        self._running = True
        self._last_error = None
        self._write_health()
        self._connect(wait_for_ready=True)
        log.info("Binance Spot WebSocket API user-data stream subscribed (%s)", "testnet" if self.testnet else "live")

    def stop(self):
        self._running = False
        self._connected = False
        self._subscribed = False
        self._write_health()
        if self._ws:
            self._ws.close()
