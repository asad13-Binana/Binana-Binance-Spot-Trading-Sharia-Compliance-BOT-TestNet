from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from .base import ExchangeOrder, OrderIntent
from .binance_rest import BinanceRestClient
from .binance_ws import BinanceMarketStream
from .ws_api import BinanceUserDataStream


class BinanceSpotAdapter:
    """Single Binance-specific implementation behind the ExchangePort seam."""

    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        rest_base: str,
        market_ws_base: str,
        ws_api_base: str,
        recv_window_ms: int = 5000,
    ) -> None:
        expected = (
            "https://testnet.binance.vision",
            "wss://stream.testnet.binance.vision:9443",
            "wss://ws-api.testnet.binance.vision/ws-api/v3",
        )
        if (rest_base.rstrip("/"), market_ws_base.rstrip("/"), ws_api_base.rstrip("/")) != expected:
            raise ValueError("Binana 2.0 foundation adapter is Binance Spot Testnet-only")
        self.rest = BinanceRestClient(api_key, api_secret, rest_base, recv_window_ms=recv_window_ms)
        self.market = BinanceMarketStream(market_ws_base)
        self.user_stream = BinanceUserDataStream(
            url=ws_api_base,
            api_key=api_key,
            api_secret=api_secret,
            recv_window_ms=recv_window_ms,
        )

    async def __aenter__(self) -> "BinanceSpotAdapter":
        await self.rest.__aenter__()
        offset = await self.rest.sync_clock()
        self.user_stream.clock_offset_ms = offset
        return self

    async def __aexit__(self, *args: object) -> None:
        self.market.close()
        self.user_stream.close()
        await self.rest.__aexit__(*args)

    async def place_order(self, intent: OrderIntent) -> ExchangeOrder:
        return await self.rest.place_order(intent)

    async def query_order(self, symbol: str, *, client_order_id: str) -> ExchangeOrder | None:
        return await self.rest.query_order(symbol, client_order_id=client_order_id)

    async def cancel_order(self, symbol: str, *, client_order_id: str) -> ExchangeOrder:
        return await self.rest.cancel_order(symbol, client_order_id=client_order_id)

    async def account(self) -> dict[str, Any]:
        return await self.rest.account()

    async def exchange_info(self, symbol: str | None = None) -> dict[str, Any]:
        return await self.rest.exchange_info(symbol)

    async def klines(self, symbol: str, interval: str, limit: int = 500) -> list[list[Any]]:
        return await self.rest.klines(symbol, interval, limit)

    def user_events(self) -> AsyncIterator[dict[str, Any]]:
        return self.user_stream.events()
