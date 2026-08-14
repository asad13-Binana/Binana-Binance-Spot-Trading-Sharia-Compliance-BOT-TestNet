from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Any


ZERO = Decimal("0")


def _dec(value: Any) -> Decimal:
    return Decimal(str(value))


def floor_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= ZERO:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def ceil_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= ZERO:
        return value
    return (value / step).to_integral_value(rounding=ROUND_UP) * step


@dataclass(frozen=True)
class SymbolFilters:
    symbol: str
    tick_size: Decimal
    min_price: Decimal
    max_price: Decimal
    step_size: Decimal
    min_qty: Decimal
    max_qty: Decimal
    min_notional: Decimal

    @classmethod
    def from_exchange_info(cls, symbol_info: dict[str, Any]) -> "SymbolFilters":
        filters = {f["filterType"]: f for f in symbol_info.get("filters", [])}
        pf = filters.get("PRICE_FILTER", {})
        lf = filters.get("LOT_SIZE", {})
        nf = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL") or {}
        return cls(
            symbol=str(symbol_info["symbol"]),
            tick_size=_dec(pf.get("tickSize", "0")),
            min_price=_dec(pf.get("minPrice", "0")),
            max_price=_dec(pf.get("maxPrice", "0")),
            step_size=_dec(lf.get("stepSize", "0")),
            min_qty=_dec(lf.get("minQty", "0")),
            max_qty=_dec(lf.get("maxQty", "0")),
            min_notional=_dec(nf.get("minNotional", "0")),
        )

    def normalize_quantity(self, quantity: Decimal) -> Decimal:
        qty = floor_step(quantity, self.step_size)
        if qty < self.min_qty:
            raise ValueError(f"quantity {qty} below minQty {self.min_qty}")
        if self.max_qty > ZERO and qty > self.max_qty:
            raise ValueError(f"quantity {qty} above maxQty {self.max_qty}")
        return qty

    def normalize_price(self, price: Decimal) -> Decimal:
        normalized = floor_step(price, self.tick_size)
        if self.min_price > ZERO and normalized < self.min_price:
            raise ValueError(f"price {normalized} below minPrice {self.min_price}")
        if self.max_price > ZERO and normalized > self.max_price:
            raise ValueError(f"price {normalized} above maxPrice {self.max_price}")
        return normalized

    def validate_notional(self, quantity: Decimal, price: Decimal) -> None:
        if self.min_notional > ZERO and quantity * price < self.min_notional:
            raise ValueError(f"notional {quantity * price} below minimum {self.min_notional}")
