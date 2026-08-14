from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any, AsyncIterator, Protocol


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP_LOSS_LIMIT = "STOP_LOSS_LIMIT"


@dataclass(frozen=True)
class OrderIntent:
    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: Decimal
    client_order_id: str
    price: Decimal | None = None
    stop_price: Decimal | None = None
    time_in_force: str | None = None


@dataclass(frozen=True)
class ExchangeOrder:
    symbol: str
    order_id: int
    client_order_id: str
    status: str
    side: str
    order_type: str
    orig_qty: Decimal
    executed_qty: Decimal
    cumulative_quote_qty: Decimal
    raw: dict[str, Any]


class UnknownExecutionStatus(RuntimeError):
    """The request may have reached Binance; reconciliation is mandatory."""

    def __init__(self, message: str, *, client_order_id: str | None = None) -> None:
        super().__init__(message)
        self.client_order_id = client_order_id


class ExchangeRejected(RuntimeError):
    pass


class ExchangePort(Protocol):
    async def place_order(self, intent: OrderIntent) -> ExchangeOrder: ...
    async def query_order(self, symbol: str, *, client_order_id: str) -> ExchangeOrder | None: ...
    async def cancel_order(self, symbol: str, *, client_order_id: str) -> ExchangeOrder: ...
    async def account(self) -> dict[str, Any]: ...
    async def exchange_info(self, symbol: str | None = None) -> dict[str, Any]: ...
    async def klines(self, symbol: str, interval: str, limit: int = 500) -> list[list[Any]]: ...
    def user_events(self) -> AsyncIterator[dict[str, Any]]: ...
