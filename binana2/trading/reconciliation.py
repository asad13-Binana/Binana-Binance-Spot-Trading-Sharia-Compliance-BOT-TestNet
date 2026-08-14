from __future__ import annotations

from dataclasses import dataclass

from binana2.exchange.base import ExchangePort
from binana2.state.repositories import StateRepository

from .execution_state_machine import ExecutionState
from .order_manager import OrderManager, state_from_exchange


@dataclass(frozen=True)
class StartupReconciliation:
    examined: int
    unresolved: int


class Reconciler:
    def __init__(self, exchange: ExchangePort, state: StateRepository, orders: OrderManager) -> None:
        self.exchange = exchange
        self.state = state
        self.orders = orders

    async def startup(self) -> StartupReconciliation:
        candidates = self.state.nonterminal_orders()
        unresolved = 0
        if candidates:
            self.state.set_entry_pause(True, "startup order reconciliation in progress")
        for stored in candidates:
            try:
                observed = await self.exchange.query_order(stored.symbol, client_order_id=stored.client_order_id)
            except Exception as exc:
                unresolved += 1; self._mark_unresolved(stored.client_order_id, stored.symbol, stored.state, f"query failed: {exc}"); continue
            if observed is None:
                unresolved += 1; self._mark_unresolved(stored.client_order_id, stored.symbol, stored.state, "order absent from REST query"); continue
            target = state_from_exchange(observed.status)
            try:
                self.orders._apply_exchange_order(observed, source="STARTUP_RECONCILE")
            except Exception as exc:
                unresolved += 1; self._mark_unresolved(stored.client_order_id, stored.symbol, stored.state, f"state conflict: {exc}"); continue
            if target is ExecutionState.RECONCILIATION_REQUIRED:
                unresolved += 1
        if unresolved:
            self.state.set_entry_pause(True, f"startup reconciliation unresolved orders={unresolved}")
        return StartupReconciliation(len(candidates), unresolved)

    def _mark_unresolved(self, client_order_id: str, symbol: str, previous_state: str, reason: str) -> None:
        current = self.state.get_order(client_order_id)
        if current is not None and current.state != ExecutionState.RECONCILIATION_REQUIRED.value:
            try:
                self.orders._transition(client_order_id, ExecutionState.RECONCILIATION_REQUIRED)
            except Exception:
                pass
        self.state.record_reconciliation(client_order_id=client_order_id, symbol=symbol, source="STARTUP_RECONCILE", previous_state=previous_state, observed_status=None, outcome="UNRESOLVED", details={"reason": reason})
