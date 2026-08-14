from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import aiohttp


class BinanceUserDataStream:
    """Current Spot WebSocket-API signature subscription.

    Uses userDataStream.subscribe.signature so HMAC API keys can subscribe
    without a session.logon/Ed25519 dependency. Reconnects with bounded backoff;
    consumers must still run startup REST reconciliation because streams are not
    an event ledger.
    """

    def __init__(
        self,
        *,
        url: str,
        api_key: str,
        api_secret: str,
        recv_window_ms: int = 5000,
        clock_offset_ms: int = 0,
    ) -> None:
        self.url = url
        self.api_key = api_key
        self._secret = api_secret.encode()
        self.recv_window_ms = recv_window_ms
        self.clock_offset_ms = clock_offset_ms
        self._closed = False

    def _signed_params(self) -> dict[str, Any]:
        params: dict[str, Any] = {
            "apiKey": self.api_key,
            "recvWindow": self.recv_window_ms,
            "timestamp": int(time.time() * 1000) + self.clock_offset_ms,
        }
        payload = "&".join(f"{key}={params[key]}" for key in sorted(params))
        params["signature"] = hmac.new(self._secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()
        return params

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        backoff = 1.0
        timeout = aiohttp.ClientTimeout(total=None, connect=15, sock_connect=15, sock_read=None)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            while not self._closed:
                try:
                    async with session.ws_connect(self.url, heartbeat=None, autoping=True, max_msg_size=4 * 1024 * 1024) as ws:
                        request_id = str(uuid.uuid4())
                        await ws.send_json({
                            "id": request_id,
                            "method": "userDataStream.subscribe.signature",
                            "params": self._signed_params(),
                        })
                        subscribed = False
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                data = json.loads(msg.data)
                                if data.get("id") == request_id:
                                    if int(data.get("status", 500)) != 200:
                                        raise RuntimeError(f"User Data Stream subscription failed: {data}")
                                    subscribed = True
                                    backoff = 1.0
                                    continue
                                if isinstance(data.get("event"), dict):
                                    yield data
                                    if data["event"].get("e") == "serverShutdown":
                                        break
                            elif msg.type in {aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR}:
                                break
                        if not subscribed and not self._closed:
                            raise RuntimeError("User Data Stream closed before subscription acknowledgment")
                except asyncio.CancelledError:
                    raise
                except Exception:
                    if self._closed:
                        return
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)

    def close(self) -> None:
        self._closed = True
