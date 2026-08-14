from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import json
import sqlite3
from typing import Any

from .database import Database


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class StoredOrder:
    client_order_id: str
    symbol: str
    side: str
    order_type: str
    quantity: Decimal
    price: Decimal | None
    state: str
    exchange_order_id: int | None
    exchange_status: str | None
    executed_qty: Decimal
    cumulative_quote_qty: Decimal


class StateRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def create_order_intent(self, *, client_order_id: str, symbol: str, side: str, order_type: str, quantity: Decimal, price: Decimal | None, state: str) -> None:
        now = utcnow()
        with self.db.transaction() as conn:
            conn.execute("""INSERT INTO orders(client_order_id,symbol,side,order_type,quantity,price,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)""", (client_order_id, symbol, side, order_type, format(quantity, "f"), format(price, "f") if price is not None else None, state, now, now))
            self._audit_conn(conn, "ORDER_INTENT_PERSISTED", "INFO", symbol=symbol, client_order_id=client_order_id, details={"state": state}, created_at=now)

    def get_order(self, client_order_id: str) -> StoredOrder | None:
        row = self.db.execute("SELECT * FROM orders WHERE client_order_id=?", (client_order_id,)).fetchone()
        if row is None:
            return None
        return StoredOrder(client_order_id=row["client_order_id"], symbol=row["symbol"], side=row["side"], order_type=row["order_type"], quantity=Decimal(row["quantity"]), price=Decimal(row["price"]) if row["price"] is not None else None, state=row["state"], exchange_order_id=row["exchange_order_id"], exchange_status=row["exchange_status"], executed_qty=Decimal(row["executed_qty"]), cumulative_quote_qty=Decimal(row["cumulative_quote_qty"]))

    def transition_order(self, client_order_id: str, *, new_state: str, exchange_order_id: int | None = None, exchange_status: str | None = None, executed_qty: Decimal | None = None, cumulative_quote_qty: Decimal | None = None, raw: dict[str, Any] | None = None) -> None:
        now = utcnow()
        with self.db.transaction() as conn:
            row = conn.execute("SELECT state FROM orders WHERE client_order_id=?", (client_order_id,)).fetchone()
            if row is None:
                raise KeyError(client_order_id)
            conn.execute("""UPDATE orders SET state=?, exchange_order_id=COALESCE(?,exchange_order_id), exchange_status=COALESCE(?,exchange_status), executed_qty=COALESCE(?,executed_qty), cumulative_quote_qty=COALESCE(?,cumulative_quote_qty), raw_json=COALESCE(?,raw_json), updated_at=? WHERE client_order_id=?""", (new_state, exchange_order_id, exchange_status, format(executed_qty, "f") if executed_qty is not None else None, format(cumulative_quote_qty, "f") if cumulative_quote_qty is not None else None, json.dumps(raw, sort_keys=True) if raw is not None else None, now, client_order_id))
            self._audit_conn(conn, "ORDER_STATE_TRANSITION", "INFO", client_order_id=client_order_id, details={"from": row["state"], "to": new_state, "exchange_status": exchange_status}, created_at=now)

    def record_recovery_intent(self, client_order_id: str, symbol: str, kind: str, details: dict[str, Any]) -> int:
        now = utcnow()
        with self.db.transaction() as conn:
            cur = conn.execute("""INSERT INTO recovery_intents(client_order_id,symbol,kind,status,details_json,created_at,updated_at) VALUES(?,?,?,'OPEN',?,?,?)""", (client_order_id, symbol, kind, json.dumps(details, sort_keys=True), now, now))
            return int(cur.lastrowid)

    def close_recovery_intents(self, client_order_id: str, outcome: str) -> None:
        now = utcnow()
        with self.db.transaction() as conn:
            rows = conn.execute("SELECT id, details_json FROM recovery_intents WHERE client_order_id=? AND status='OPEN'", (client_order_id,)).fetchall()
            for row in rows:
                details = json.loads(row["details_json"] or "{}")
                details["outcome"] = outcome
                conn.execute("UPDATE recovery_intents SET status='CLOSED',details_json=?,updated_at=? WHERE id=?", (json.dumps(details, sort_keys=True), now, row["id"]))

    def record_reconciliation(self, *, client_order_id: str | None, symbol: str, source: str, previous_state: str | None, observed_status: str | None, outcome: str, details: dict[str, Any] | None = None) -> None:
        self.db.execute("""INSERT INTO reconciliation_events(client_order_id,symbol,source,previous_state,observed_status,outcome,details_json,created_at) VALUES(?,?,?,?,?,?,?,?)""", (client_order_id, symbol, source, previous_state, observed_status, outcome, json.dumps(details or {}, sort_keys=True), utcnow()))

    def is_entry_paused(self) -> tuple[bool, str]:
        row = self.db.execute("SELECT paused,reason FROM global_entry_pause WHERE singleton=1").fetchone()
        if row is None:
            return True, "pause row missing"
        return bool(row["paused"]), str(row["reason"])

    def set_entry_pause(self, paused: bool, reason: str) -> None:
        if not reason.strip():
            raise ValueError("pause reason is required")
        self.db.execute("""INSERT INTO global_entry_pause(singleton,paused,reason,updated_at) VALUES(1,?,?,?) ON CONFLICT(singleton) DO UPDATE SET paused=excluded.paused, reason=excluded.reason,updated_at=excluded.updated_at""", (1 if paused else 0, reason, utcnow()))

    def has_active_halt(self) -> tuple[bool, str]:
        row = self.db.execute("SELECT reason FROM safety_halts WHERE active=1 ORDER BY created_at DESC LIMIT 1").fetchone()
        return (row is not None, str(row["reason"]) if row else "")

    def audit(self, event_type: str, severity: str, *, symbol: str | None = None, client_order_id: str | None = None, details: dict[str, Any] | None = None) -> None:
        with self.db.transaction() as conn:
            self._audit_conn(conn, event_type, severity, symbol=symbol, client_order_id=client_order_id, details=details or {}, created_at=utcnow())

    @staticmethod
    def _audit_conn(conn: sqlite3.Connection, event_type: str, severity: str, *, symbol: str | None = None, client_order_id: str | None = None, details: dict[str, Any], created_at: str) -> None:
        conn.execute("""INSERT INTO audit_events(event_type,severity,symbol,client_order_id,details_json,created_at) VALUES(?,?,?,?,?,?)""", (event_type, severity, symbol, client_order_id, json.dumps(details, sort_keys=True), created_at))

    def protection_gap_start(self, symbol: str, client_order_id: str, replacement_client_order_id: str | None = None) -> None:
        now = utcnow()
        self.db.execute("""INSERT INTO protection_state(symbol,client_order_id,state,protection_gap_started_at,replacement_client_order_id,updated_at) VALUES(?,?,'PROTECTION_PENDING',?,?,?) ON CONFLICT(symbol) DO UPDATE SET client_order_id=excluded.client_order_id, state='PROTECTION_PENDING',protection_gap_started_at=excluded.protection_gap_started_at, protection_ack_at=NULL,replacement_client_order_id=excluded.replacement_client_order_id, updated_at=excluded.updated_at""", (symbol, client_order_id, now, replacement_client_order_id, now))
        self.audit("PROTECTION_GAP_STARTED", "WARNING", symbol=symbol, client_order_id=client_order_id, details={"replacement_client_order_id": replacement_client_order_id})

    def protection_ack(self, symbol: str, client_order_id: str) -> None:
        now = utcnow()
        self.db.execute("""UPDATE protection_state SET state='PROTECTED',protection_ack_at=?,updated_at=? WHERE symbol=?""", (now, now, symbol))
        self.audit("PROTECTION_ACKNOWLEDGED", "INFO", symbol=symbol, client_order_id=client_order_id)

    def protection_failed(self, symbol: str, client_order_id: str, reason: str) -> None:
        now = utcnow()
        self.db.execute("UPDATE protection_state SET state='PROTECTION_FAILED',details_json=?,updated_at=? WHERE symbol=?", (json.dumps({"reason": reason}, sort_keys=True), now, symbol))
        self.set_entry_pause(True, f"protection failure on {symbol}: {reason}")
        self.audit("PROTECTION_FAILED", "CRITICAL", symbol=symbol, client_order_id=client_order_id, details={"reason": reason})

    def nonterminal_orders(self) -> list[StoredOrder]:
        terminal = ("CLOSED", "ENTRY_REJECTED", "MANUAL_INTERVENTION_REQUIRED")
        rows = self.db.execute("SELECT client_order_id FROM orders WHERE state NOT IN (?,?,?) ORDER BY created_at", terminal).fetchall()
        return [order for row in rows if (order := self.get_order(str(row["client_order_id"]))) is not None]
