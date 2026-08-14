from __future__ import annotations

from binana2.state.repositories import StateRepository


class ProtectionManager:
    """Durable instrumentation for any non-atomic protection replacement gap."""

    def __init__(self, state: StateRepository) -> None:
        self.state = state

    def begin_gap(self, symbol: str, client_order_id: str, replacement_client_order_id: str | None = None) -> None:
        self.state.protection_gap_start(symbol, client_order_id, replacement_client_order_id)

    def acknowledge(self, symbol: str, client_order_id: str) -> None:
        self.state.protection_ack(symbol, client_order_id)

    def fail(self, symbol: str, client_order_id: str, reason: str) -> None:
        self.state.protection_failed(symbol, client_order_id, reason)
