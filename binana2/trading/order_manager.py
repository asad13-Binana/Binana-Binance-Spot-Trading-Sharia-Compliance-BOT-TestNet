from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Iterable

from binana2.exchange.base import ExchangeOrder, ExchangePort, ExchangeRejected, OrderIntent, UnknownExecutionStatus
from binana2.exchange.user_stream import ExecutionReport
from binana2.state.repositories import StateRepository
from .execution_state_machine import ExecutionState, assert_transition


@dataclass(frozen=True)
class SubmissionResult:
    client_order_id: str
    state: ExecutionState
    exchange_status: str | None
    reason: str


def state_from_exchange(status: str) -> ExecutionState:
    normalized = status.upper()
    if normalized == "NEW": return ExecutionState.ENTRY_OPEN
    if normalized == "PARTIALLY_FILLED": return ExecutionState.PARTIALLY_FILLED
    if normalized == "FILLED": return ExecutionState.FILLED
    if normalized in {"CANCELED", "REJECTED", "EXPIRED", "EXPIRED_IN_MATCH"}: return ExecutionState.ENTRY_REJECTED
    return ExecutionState.RECONCILIATION_REQUIRED


class OrderManager:
    def __init__(self, exchange: ExchangePort, state: StateRepository) -> None:
        self.exchange = exchange; self.state = state

    async def submit_entry(self, intent: OrderIntent) -> SubmissionResult:
        if self.state.get_order(intent.client_order_id) is not None:
            raise ValueError(f"client order id already exists: {intent.client_order_id}")
        self.state.create_order_intent(client_order_id=intent.client_order_id, symbol=intent.symbol, side=intent.side.value, order_type=intent.order_type.value, quantity=intent.quantity, price=intent.price, state=ExecutionState.ENTRY_SUBMITTING.value)
        try:
            observed = await self.exchange.place_order(intent)
        except UnknownExecutionStatus as exc:
            self._transition(intent.client_order_id, ExecutionState.ENTRY_UNKNOWN)
            self.state.record_recovery_intent(intent.client_order_id, intent.symbol, "UNKNOWN_ENTRY_SUBMISSION", {"error": str(exc)})
            return SubmissionResult(intent.client_order_id, ExecutionState.ENTRY_UNKNOWN, None, str(exc))
        except ExchangeRejected as exc:
            self._transition(intent.client_order_id, ExecutionState.ENTRY_REJECTED)
            return SubmissionResult(intent.client_order_id, ExecutionState.ENTRY_REJECTED, "REJECTED", str(exc))
        return self._apply_exchange_order(observed, source="REST_SUBMIT")

    async def reconcile_unknown(self, client_order_id: str, *, attempts: int = 3, delays: Iterable[float] = (0.25,1.0,2.0)) -> SubmissionResult:
        stored = self.state.get_order(client_order_id)
        if stored is None: raise KeyError(client_order_id)
        if stored.state not in {ExecutionState.ENTRY_UNKNOWN.value, ExecutionState.RECONCILIATION_REQUIRED.value}:
            return SubmissionResult(client_order_id, ExecutionState(stored.state), stored.exchange_status, "no reconciliation required")
        delay_list = list(delays)
        for index in range(attempts):
            observed = await self.exchange.query_order(stored.symbol, client_order_id=client_order_id)
            if observed is not None:
                result = self._apply_exchange_order(observed, source="REST_RECONCILE"); self.state.close_recovery_intents(client_order_id, result.state.value); return result
            if index < attempts - 1: await asyncio.sleep(delay_list[min(index, len(delay_list)-1)] if delay_list else 0)
        current = self.state.get_order(client_order_id); assert current is not None
        self._transition(client_order_id, ExecutionState.RECONCILIATION_REQUIRED)
        self.state.record_reconciliation(client_order_id=client_order_id, symbol=current.symbol, source="REST_RECONCILE", previous_state=current.state, observed_status=None, outcome="ORDER_NOT_FOUND_AFTER_BOUNDED_RECONCILIATION")
        return SubmissionResult(client_order_id, ExecutionState.RECONCILIATION_REQUIRED, None, "manual/stream reconciliation required")

    def on_execution_report(self, report: ExecutionReport) -> SubmissionResult | None:
        stored = self.state.get_order(report.client_order_id)
        if stored is None:
            self.state.record_reconciliation(client_order_id=report.client_order_id, symbol=report.symbol, source="USER_DATA_STREAM", previous_state=None, observed_status=report.order_status, outcome="UNRECOGNISED_CLIENT_ORDER", details=report.raw); return None
        target = state_from_exchange(report.order_status)
        self._transition(report.client_order_id, target, exchange_order_id=report.order_id, exchange_status=report.order_status, executed_qty=report.cumulative_qty, cumulative_quote_qty=report.cumulative_quote_qty, raw=report.raw)
        if target not in {ExecutionState.ENTRY_UNKNOWN, ExecutionState.RECONCILIATION_REQUIRED}: self.state.close_recovery_intents(report.client_order_id, target.value)
        return SubmissionResult(report.client_order_id, target, report.order_status, "user data stream")

    def _apply_exchange_order(self, order: ExchangeOrder, *, source: str) -> SubmissionResult:
        target = state_from_exchange(order.status); stored = self.state.get_order(order.client_order_id)
        if stored is None: raise KeyError(order.client_order_id)
        self._transition(order.client_order_id, target, exchange_order_id=order.order_id, exchange_status=order.status, executed_qty=order.executed_qty, cumulative_quote_qty=order.cumulative_quote_qty, raw=order.raw)
        self.state.record_reconciliation(client_order_id=order.client_order_id, symbol=order.symbol, source=source, previous_state=stored.state, observed_status=order.status, outcome=target.value)
        return SubmissionResult(order.client_order_id, target, order.status, source)

    def _transition(self, client_order_id: str, target: ExecutionState, **exchange_fields: object) -> None:
        stored = self.state.get_order(client_order_id)
        if stored is None: raise KeyError(client_order_id)
        current = ExecutionState(stored.state); assert_transition(current, target)
        self.state.transition_order(client_order_id, new_state=target.value, **exchange_fields)
