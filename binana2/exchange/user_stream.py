from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class ExecutionReport:
    event_time_ms: int
    symbol: str
    client_order_id: str
    side: str
    order_type: str
    execution_type: str
    order_status: str
    order_id: int
    last_qty: Decimal
    cumulative_qty: Decimal
    last_price: Decimal
    cumulative_quote_qty: Decimal
    trade_id: int | None
    raw: dict[str, Any]


def unwrap_user_event(message: dict[str, Any]) -> dict[str, Any]:
    """Accept raw stream events and WebSocket-API subscription envelopes."""
    event = message.get("event") if isinstance(message.get("event"), dict) else message
    if isinstance(event.get("data"), dict):
        event = event["data"]
    return event


def parse_execution_report(message: dict[str, Any]) -> ExecutionReport | None:
    event = unwrap_user_event(message)
    if event.get("e") != "executionReport":
        return None
    trade_id = int(event["t"]) if event.get("t") is not None and int(event["t"]) >= 0 else None
    return ExecutionReport(
        event_time_ms=int(event["E"]),
        symbol=str(event["s"]),
        client_order_id=str(event["c"]),
        side=str(event["S"]),
        order_type=str(event["o"]),
        execution_type=str(event["x"]),
        order_status=str(event["X"]),
        order_id=int(event["i"]),
        last_qty=Decimal(str(event.get("l", "0"))),
        cumulative_qty=Decimal(str(event.get("z", "0"))),
        last_price=Decimal(str(event.get("L", "0"))),
        cumulative_quote_qty=Decimal(str(event.get("Z", "0"))),
        trade_id=trade_id,
        raw=event,
    )
