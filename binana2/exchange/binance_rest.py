from __future__ import annotations

import asyncio
from decimal import Decimal
import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import urlencode

import aiohttp

from .base import ExchangeOrder, ExchangeRejected, OrderIntent, UnknownExecutionStatus


class BinanceRestClient:
    """Native Binance Spot REST adapter.

    Matching-engine timeouts and 5xx responses are never treated as definite
    failures. Callers receive UnknownExecutionStatus and must reconcile using
    the User Data Stream and/or GET /api/v3/order.
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        base_url: str,
        *,
        recv_window_ms: int = 5000,
        timeout_seconds: float = 12.0,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        normalized_base = base_url.rstrip("/")
        if normalized_base != "https://testnet.binance.vision":
            raise ValueError("Binana 2.0 foundation REST client is Testnet-only")
        self.api_key = api_key
        self._secret = api_secret.encode()
        self.base_url = normalized_base
        self.recv_window_ms = recv_window_ms
        self.timeout_seconds = timeout_seconds
        self._session = session
        self._owns_session = session is None
        self._clock_offset_ms = 0

    async def __aenter__(self) -> "BinanceRestClient":
        if self._session is None:
            timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError("BinanceRestClient must be entered before use")
        return self._session

    def _signed_query(self, params: dict[str, Any]) -> str:
        clean = {k: v for k, v in params.items() if v is not None}
        clean.setdefault("recvWindow", self.recv_window_ms)
        clean.setdefault("timestamp", int(time.time() * 1000) + self._clock_offset_ms)
        payload = urlencode(clean, doseq=True)
        signature = hmac.new(self._secret, payload.encode(), hashlib.sha256).hexdigest()
        return f"{payload}&signature={signature}"

    async def sync_clock(self) -> int:
        local_before = int(time.time() * 1000)
        data = await self._request("GET", "/api/v3/time")
        local_after = int(time.time() * 1000)
        midpoint = (local_before + local_after) // 2
        self._clock_offset_ms = int(data["serverTime"]) - midpoint
        return self._clock_offset_ms

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        signed: bool = False,
        execution_sensitive: bool = False,
        client_order_id: str | None = None,
    ) -> Any:
        params = dict(params or {})
        headers: dict[str, str] = {}
        query = self._signed_query(params) if signed else urlencode({k: v for k, v in params.items() if v is not None})
        if signed or self.api_key:
            headers["X-MBX-APIKEY"] = self.api_key
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{query}"

        try:
            async with self.session.request(method, url, headers=headers) as response:
                text = await response.text()
                try:
                    data = json.loads(text) if text else {}
                except json.JSONDecodeError:
                    data = {"msg": text}

                if response.status in {418, 429}:
                    retry_after = response.headers.get("Retry-After")
                    raise ExchangeRejected(f"Binance rate limit {response.status}; retry-after={retry_after}")

                code = data.get("code") if isinstance(data, dict) else None
                if response.status >= 500 or code == -1007:
                    if execution_sensitive:
                        raise UnknownExecutionStatus(
                            f"Binance execution status unknown: HTTP {response.status}, code={code}",
                            client_order_id=client_order_id,
                        )
                    raise RuntimeError(f"Binance server error: HTTP {response.status}, code={code}")

                if response.status >= 400 or (isinstance(code, int) and code < 0):
                    raise ExchangeRejected(f"Binance rejected request: HTTP {response.status}, payload={data}")
                return data
        except (asyncio.TimeoutError, aiohttp.ClientError) as exc:
            if execution_sensitive:
                raise UnknownExecutionStatus(
                    f"Binance transport error after order submission attempt: {type(exc).__name__}",
                    client_order_id=client_order_id,
                ) from exc
            raise

    @staticmethod
    def _order(data: dict[str, Any]) -> ExchangeOrder:
        return ExchangeOrder(
            symbol=str(data["symbol"]),
            order_id=int(data["orderId"]),
            client_order_id=str(data["clientOrderId"]),
            status=str(data["status"]),
            side=str(data["side"]),
            order_type=str(data["type"]),
            orig_qty=Decimal(str(data["origQty"])),
            executed_qty=Decimal(str(data["executedQty"])),
            cumulative_quote_qty=Decimal(str(data.get("cummulativeQuoteQty", "0"))),
            raw=data,
        )

    async def place_order(self, intent: OrderIntent) -> ExchangeOrder:
        params: dict[str, Any] = {
            "symbol": intent.symbol,
            "side": intent.side.value,
            "type": intent.order_type.value,
            "quantity": format(intent.quantity, "f"),
            "newClientOrderId": intent.client_order_id,
            "newOrderRespType": "FULL",
            "price": format(intent.price, "f") if intent.price is not None else None,
            "stopPrice": format(intent.stop_price, "f") if intent.stop_price is not None else None,
            "timeInForce": intent.time_in_force,
        }
        data = await self._request(
            "POST",
            "/api/v3/order",
            params=params,
            signed=True,
            execution_sensitive=True,
            client_order_id=intent.client_order_id,
        )
        return self._order(data)

    async def query_order(self, symbol: str, *, client_order_id: str) -> ExchangeOrder | None:
        try:
            data = await self._request(
                "GET",
                "/api/v3/order",
                params={"symbol": symbol, "origClientOrderId": client_order_id},
                signed=True,
            )
        except ExchangeRejected as exc:
            if "-2013" in str(exc):
                return None
            raise
        return self._order(data)

    async def cancel_order(self, symbol: str, *, client_order_id: str) -> ExchangeOrder:
        data = await self._request(
            "DELETE",
            "/api/v3/order",
            params={"symbol": symbol, "origClientOrderId": client_order_id},
            signed=True,
            execution_sensitive=True,
            client_order_id=client_order_id,
        )
        return self._order(data)

    async def account(self) -> dict[str, Any]:
        return await self._request("GET", "/api/v3/account", signed=True)

    async def exchange_info(self, symbol: str | None = None) -> dict[str, Any]:
        params = {"symbol": symbol} if symbol else {}
        return await self._request("GET", "/api/v3/exchangeInfo", params=params)

    async def klines(self, symbol: str, interval: str, limit: int = 500) -> list[list[Any]]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be 1..1000")
        return await self._request(
            "GET",
            "/api/v3/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
        )
