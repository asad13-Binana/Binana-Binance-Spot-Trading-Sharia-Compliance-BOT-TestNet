from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import aiohttp


class BinanceMarketStream:
    """Native Binance Spot market-data WebSocket stream with reconnect."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self._closed = False

    async def klines(self, symbol: str, interval: str) -> AsyncIterator[dict[str, Any]]:
        stream = f"{symbol.lower()}@kline_{interval}"
        url = f"{self.base_url}/ws/{stream}"
        backoff = 1.0
        timeout = aiohttp.ClientTimeout(total=None, connect=15, sock_read=None)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            while not self._closed:
                try:
                    async with session.ws_connect(url, autoping=True, heartbeat=None) as ws:
                        backoff = 1.0
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                event = json.loads(msg.data)
                                if event.get("e") == "serverShutdown":
                                    break
                                if event.get("e") == "kline":
                                    yield event
                            elif msg.type in {aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR}:
                                break
                except asyncio.CancelledError:
                    raise
                except Exception:
                    if self._closed:
                        return
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)

    def close(self) -> None:
        self._closed = True
