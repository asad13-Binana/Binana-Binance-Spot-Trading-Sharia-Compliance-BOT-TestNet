"""Small deterministic reservation ledger for BINANA's 1000/4x250 contract."""
from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal
from threading import RLock


class AllocationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Reservation:
    intent_id: str
    nominal_usdt: Decimal
    state: str


class AllocationLedger:
    OCCUPYING = {"RESERVED", "ENTRY_PENDING", "ENTRY_PARTIAL", "OPEN", "EXIT_PENDING", "UNKNOWN"}

    def __init__(self, allocation_usdt: Decimal = Decimal("1000"), slot_usdt: Decimal = Decimal("250"), max_slots: int = 4):
        self.allocation_usdt = Decimal(allocation_usdt)
        self.slot_usdt = Decimal(slot_usdt)
        self.max_slots = int(max_slots)
        self._items: dict[str, Reservation] = {}
        self._lock = RLock()

    def occupying(self) -> list[Reservation]:
        with self._lock:
            return [r for r in self._items.values() if r.state in self.OCCUPYING]

    def reserve(self, intent_id: str) -> Reservation:
        with self._lock:
            if intent_id in self._items:
                return self._items[intent_id]
            active = self.occupying()
            if len(active) >= self.max_slots:
                raise AllocationError("MAX_SLOTS_REACHED")
            used = sum((r.nominal_usdt for r in active), Decimal("0"))
            if used + self.slot_usdt > self.allocation_usdt:
                raise AllocationError("ALLOCATION_EXHAUSTED")
            item = Reservation(intent_id, self.slot_usdt, "RESERVED")
            self._items[intent_id] = item
            return item

    def transition(self, intent_id: str, state: str) -> Reservation:
        with self._lock:
            old = self._items.get(intent_id)
            if old is None:
                raise AllocationError("UNKNOWN_RESERVATION")
            new = Reservation(old.intent_id, old.nominal_usdt, state)
            self._items[intent_id] = new
            return new

    def release(self, intent_id: str) -> None:
        with self._lock:
            old = self._items.get(intent_id)
            if old is not None:
                self._items[intent_id] = Reservation(old.intent_id, old.nominal_usdt, "CLOSED")
