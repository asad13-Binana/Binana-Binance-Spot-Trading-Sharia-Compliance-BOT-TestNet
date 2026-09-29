"""Durable BINANA extension state linked to canonical Freqtrade trades.

This is not a second portfolio. Freqtrade owns trades; this store persists
admission, order-list identities, protection generations and unresolved intents.
"""
from __future__ import annotations
from contextlib import contextmanager
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
from threading import RLock
from time import time
from typing import Any, Iterator


class _ClosingConnection(sqlite3.Connection):
    """sqlite3 context manager that also closes the descriptor on exit."""
    def __exit__(self, exc_type, exc, tb):
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self.close()


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS intents (
 intent_id TEXT PRIMARY KEY,
 pair TEXT NOT NULL,
 signal_id TEXT,
 freqtrade_trade_id INTEGER,
 halal_allowed INTEGER NOT NULL,
 registry_sha256 TEXT NOT NULL,
 nominal_usdt TEXT NOT NULL,
 admission_json TEXT NOT NULL DEFAULT '{}',
 state TEXT NOT NULL,
 created_ts REAL NOT NULL,
 updated_ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS protection (
 intent_id TEXT NOT NULL,
 generation INTEGER NOT NULL,
 pair TEXT NOT NULL,
 mode TEXT NOT NULL,
 status TEXT NOT NULL,
 order_list_id TEXT,
 list_client_id TEXT,
 working_order_id TEXT,
 working_client_id TEXT,
 tp_order_id TEXT,
 tp_client_id TEXT,
 sl_order_id TEXT,
 sl_client_id TEXT,
 expected_qty TEXT,
 filled_qty TEXT,
 avg_fill TEXT,
 fees_json TEXT NOT NULL DEFAULT '[]',
 payload_json TEXT NOT NULL DEFAULT '{}',
 updated_ts REAL NOT NULL,
 PRIMARY KEY(intent_id, generation),
 FOREIGN KEY(intent_id) REFERENCES intents(intent_id)
);
CREATE TABLE IF NOT EXISTS incidents (
 incident_id TEXT PRIMARY KEY,
 intent_id TEXT,
 pair TEXT,
 code TEXT NOT NULL,
 detail TEXT NOT NULL,
 status TEXT NOT NULL,
 created_ts REAL NOT NULL,
 updated_ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_protection_pair ON protection(pair, status);
CREATE INDEX IF NOT EXISTS idx_incidents_status ON incidents(status, pair);
CREATE TABLE IF NOT EXISTS order_identity (
 scope_id TEXT NOT NULL,
 intent_id TEXT NOT NULL,
 generation INTEGER NOT NULL,
 pair TEXT NOT NULL,
 role TEXT NOT NULL,
 order_id TEXT,
 client_id TEXT NOT NULL,
 created_ts REAL NOT NULL,
 PRIMARY KEY(scope_id, pair, client_id),
 UNIQUE(scope_id, pair, order_id),
 FOREIGN KEY(intent_id) REFERENCES intents(intent_id)
);
CREATE INDEX IF NOT EXISTS idx_order_identity_intent ON order_identity(intent_id, generation);
CREATE TABLE IF NOT EXISTS fill_ledger (
 fill_key TEXT PRIMARY KEY,
 scope_id TEXT NOT NULL,
 intent_id TEXT NOT NULL,
 pair TEXT NOT NULL,
 order_id TEXT NOT NULL,
 side TEXT NOT NULL,
 base_qty TEXT NOT NULL,
 quote_qty TEXT NOT NULL,
 average_price TEXT,
 fee_asset TEXT,
 fee_amount TEXT,
 fee_quote_value TEXT,
 exchange_trade_id TEXT,
 source_kind TEXT NOT NULL DEFAULT 'exchange_trade',
 occurred_ts REAL,
 raw_json TEXT NOT NULL DEFAULT '{}',
 created_ts REAL NOT NULL,
 FOREIGN KEY(intent_id) REFERENCES intents(intent_id)
);
CREATE INDEX IF NOT EXISTS idx_fill_ledger_intent ON fill_ledger(intent_id, occurred_ts);
"""


class StateStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate_fill_ledger(conn)

    @staticmethod
    def _migrate_fill_ledger(conn: sqlite3.Connection) -> None:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(fill_ledger)")}
        if "exchange_trade_id" not in columns:
            conn.execute("ALTER TABLE fill_ledger ADD COLUMN exchange_trade_id TEXT")
        if "source_kind" not in columns:
            conn.execute(
                "ALTER TABLE fill_ledger ADD COLUMN source_kind TEXT NOT NULL "
                "DEFAULT 'legacy_order_snapshot'"
            )
        rows = conn.execute(
            "SELECT fill_key, raw_json, exchange_trade_id, source_kind FROM fill_ledger"
        ).fetchall()
        for row in rows:
            exchange_trade_id = row["exchange_trade_id"]
            source_kind = row["source_kind"]
            try:
                raw = json.loads(row["raw_json"] or "{}")
            except Exception:
                raw = {}
            raw_trade_id = raw.get("id")
            raw_order_id = raw.get("order") or raw.get("orderId")
            if raw_trade_id is not None and raw_order_id is not None:
                exchange_trade_id = str(raw_trade_id)
                source_kind = "exchange_trade"
            elif not source_kind or source_kind == "exchange_trade":
                source_kind = "legacy_order_snapshot"
            conn.execute(
                "UPDATE fill_ledger SET exchange_trade_id=?, source_kind=? WHERE fill_key=?",
                (exchange_trade_id, source_kind, row["fill_key"]),
            )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_fill_exchange_trade "
            "ON fill_ledger(scope_id, pair, exchange_trade_id) "
            "WHERE exchange_trade_id IS NOT NULL"
        )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=20, isolation_level=None, factory=_ClosingConnection)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()

    def put_intent(self, *, intent_id: str, pair: str, signal_id: str | None,
                   halal_allowed: bool, registry_sha256: str, nominal_usdt: str,
                   state: str = "RESERVED", admission: dict[str, Any] | None = None) -> None:
        now = time()
        with self.tx() as c:
            row = c.execute("SELECT pair, halal_allowed, registry_sha256 FROM intents WHERE intent_id=?", (intent_id,)).fetchone()
            if row:
                if row["pair"] != pair or bool(row["halal_allowed"]) != bool(halal_allowed) or row["registry_sha256"] != registry_sha256:
                    raise RuntimeError("intent replay contract mismatch")
                return
            c.execute("""INSERT INTO intents(intent_id,pair,signal_id,halal_allowed,registry_sha256,nominal_usdt,admission_json,state,created_ts,updated_ts)
                         VALUES(?,?,?,?,?,?,?,?,?,?)""",
                      (intent_id,pair,signal_id,int(halal_allowed),registry_sha256,nominal_usdt,json.dumps(admission or {},sort_keys=True,separators=(",",":")),state,now,now))

    def get_intent(self, intent_id: str) -> dict[str, Any] | None:
        with self._connect() as c:
            row=c.execute("SELECT * FROM intents WHERE intent_id=?",(intent_id,)).fetchone()
            return dict(row) if row else None

    OCCUPYING_STATES=("HALAL_DECIDED","RESERVED","ENTRY_PENDING","ENTRY_PARTIAL","OPEN","EXIT_PENDING","UNKNOWN")

    def occupied_count(self) -> int:
        marks=",".join("?" for _ in self.OCCUPYING_STATES)
        with self._connect() as c:
            return int(c.execute(
                f"SELECT COUNT(*) FROM intents WHERE state IN ({marks})",
                self.OCCUPYING_STATES,
            ).fetchone()[0])

    def occupied_nominal_usdt(self) -> Decimal:
        """Return reserved/open nominal exposure and fail closed on malformed rows."""
        marks=",".join("?" for _ in self.OCCUPYING_STATES)
        with self._connect() as c:
            rows=c.execute(
                f"SELECT nominal_usdt FROM intents WHERE state IN ({marks})",
                self.OCCUPYING_STATES,
            ).fetchall()
        total=Decimal("0")
        for row in rows:
            value=Decimal(str(row["nominal_usdt"]))
            if not value.is_finite() or value < 0:
                raise RuntimeError("invalid nominal_usdt in canonical state")
            total += value
        return total

    def has_generation(self, intent_id: str) -> bool:
        with self._connect() as c:
            return bool(c.execute(
                "SELECT 1 FROM protection WHERE intent_id=? LIMIT 1",
                (intent_id,),
            ).fetchone())

    def set_trade_id(self, intent_id: str, trade_id: int) -> None:
        with self.tx() as c:
            c.execute("UPDATE intents SET freqtrade_trade_id=?, updated_ts=? WHERE intent_id=?", (trade_id,time(),intent_id))

    def set_intent_state(self, intent_id: str, state: str, *, admission: dict[str, Any] | None = None) -> None:
        with self.tx() as c:
            if admission is None:
                c.execute("UPDATE intents SET state=?, updated_ts=? WHERE intent_id=?", (state,time(),intent_id))
            else:
                c.execute(
                    "UPDATE intents SET state=?, admission_json=?, updated_ts=? WHERE intent_id=?",
                    (state,json.dumps(admission,sort_keys=True,separators=(",",":")),time(),intent_id),
                )

    def put_generation(self, *, intent_id: str, generation: int, pair: str, mode: str,
                       status: str, ids: dict[str, Any] | None = None,
                       expected_qty: str | None = None, payload: dict[str, Any] | None = None) -> None:
        ids = ids or {}; payload = payload or {}; now = time()
        fields = (
            intent_id,generation,pair,mode,status,
            ids.get("order_list_id"),ids.get("list_client_id"),ids.get("working_order_id"),ids.get("working_client_id"),
            ids.get("tp_order_id"),ids.get("tp_client_id"),ids.get("sl_order_id"),ids.get("sl_client_id"),
            expected_qty,None,None,"[]",json.dumps(payload,sort_keys=True,separators=(",",":")),now,
        )
        with self.tx() as c:
            c.execute("""INSERT INTO protection(intent_id,generation,pair,mode,status,order_list_id,list_client_id,
              working_order_id,working_client_id,tp_order_id,tp_client_id,sl_order_id,sl_client_id,expected_qty,
              filled_qty,avg_fill,fees_json,payload_json,updated_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
              ON CONFLICT(intent_id,generation) DO UPDATE SET mode=excluded.mode,status=excluded.status,
              order_list_id=COALESCE(excluded.order_list_id,protection.order_list_id),
              list_client_id=COALESCE(excluded.list_client_id,protection.list_client_id),
              working_order_id=COALESCE(excluded.working_order_id,protection.working_order_id),
              working_client_id=COALESCE(excluded.working_client_id,protection.working_client_id),
              tp_order_id=COALESCE(excluded.tp_order_id,protection.tp_order_id),
              tp_client_id=COALESCE(excluded.tp_client_id,protection.tp_client_id),
              sl_order_id=COALESCE(excluded.sl_order_id,protection.sl_order_id),
              sl_client_id=COALESCE(excluded.sl_client_id,protection.sl_client_id),
              expected_qty=COALESCE(excluded.expected_qty,protection.expected_qty),payload_json=excluded.payload_json,
              updated_ts=excluded.updated_ts""", fields)

    def update_generation_status(self, intent_id: str, generation: int, status: str, *, payload: dict[str, Any] | None = None) -> None:
        with self.tx() as c:
            if payload is None:
                c.execute("UPDATE protection SET status=?,updated_ts=? WHERE intent_id=? AND generation=?",
                          (status,time(),intent_id,generation))
            else:
                c.execute("UPDATE protection SET status=?,payload_json=?,updated_ts=? WHERE intent_id=? AND generation=?",
                          (status,json.dumps(payload,sort_keys=True,separators=(",",":"),default=str),time(),intent_id,generation))

    def close_incident(self, incident_id: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE incidents SET status='CLOSED',updated_ts=? WHERE incident_id=?",(time(),incident_id))

    def update_fill(self, intent_id: str, generation: int, *, filled_qty: str, avg_fill: str | None, fees: list[dict[str, Any]]) -> None:
        with self.tx() as c:
            c.execute("UPDATE protection SET filled_qty=?,avg_fill=?,fees_json=?,updated_ts=? WHERE intent_id=? AND generation=?",
                      (filled_qty,avg_fill,json.dumps(fees,sort_keys=True,separators=(",",":")),time(),intent_id,generation))

    def incident(self, *, incident_id: str, code: str, detail: str, intent_id: str | None = None, pair: str | None = None, status: str = "OPEN") -> None:
        now=time()
        with self.tx() as c:
            c.execute("""INSERT INTO incidents(incident_id,intent_id,pair,code,detail,status,created_ts,updated_ts)
              VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(incident_id) DO UPDATE SET detail=excluded.detail,status=excluded.status,updated_ts=excluded.updated_ts""",
              (incident_id,intent_id,pair,code,detail,status,now,now))

    def unresolved(self) -> list[dict[str, Any]]:
        with self._connect() as c:
            rows=c.execute("SELECT * FROM incidents WHERE status='OPEN' ORDER BY created_ts").fetchall()
            return [dict(r) for r in rows]


    def has_blockers(self) -> bool:
        """New exposure is forbidden while any unresolved incident/unknown intent exists."""
        with self._connect() as c:
            incident = c.execute("SELECT 1 FROM incidents WHERE status='OPEN' LIMIT 1").fetchone()
            unknown = c.execute("SELECT 1 FROM intents WHERE state IN ('UNKNOWN','ENTRY_PARTIAL') LIMIT 1").fetchone()
            return bool(incident or unknown)

    def bind_order_identity(self, *, scope_id: str, intent_id: str, generation: int, pair: str,
                            role: str, client_id: str, order_id: str | None = None) -> None:
        if not scope_id or not pair or not client_id or not role:
            raise ValueError("scoped order identity requires scope/pair/role/client_id")
        with self.tx() as c:
            existing=c.execute('SELECT intent_id,generation,role FROM order_identity WHERE scope_id=? AND pair=? AND client_id=?',(scope_id,pair,client_id)).fetchone()
            if existing and (existing['intent_id']!=intent_id or int(existing['generation'])!=int(generation) or existing['role']!=role):
                raise ValueError('scoped order identity replay mismatch')
            c.execute("""INSERT INTO order_identity(scope_id,intent_id,generation,pair,role,order_id,client_id,created_ts)
                         VALUES(?,?,?,?,?,?,?,?)
                         ON CONFLICT(scope_id,pair,client_id) DO UPDATE SET
                         order_id=COALESCE(excluded.order_id,order_identity.order_id),
                         role=excluded.role,generation=excluded.generation,intent_id=excluded.intent_id""",
                      (scope_id,intent_id,int(generation),pair,role,str(order_id) if order_id is not None else None,client_id,time()))

    def record_fill(self, *, fill_key: str, scope_id: str, intent_id: str, pair: str, order_id: str,
                    side: str, base_qty: str, quote_qty: str, average_price: str | None,
                    legacy_fill_keys: list[str] | None = None,
                    fee_asset: str | None = None, fee_amount: str | None = None,
                    fee_quote_value: str | None = None, occurred_ts: float | None = None,
                    raw: dict[str, Any] | None = None,
                    exchange_trade_id: str | None = None,
                    source_kind: str = "exchange_trade") -> None:
        with self.tx() as c:
            keys=[fill_key] + list(legacy_fill_keys or [])
            marks=','.join('?' for _ in keys)
            if c.execute(f"SELECT 1 FROM fill_ledger WHERE fill_key IN ({marks}) LIMIT 1", keys).fetchone():
                return
            if exchange_trade_id is not None and c.execute(
                "SELECT 1 FROM fill_ledger WHERE scope_id=? AND pair=? AND exchange_trade_id=? LIMIT 1",
                (scope_id, pair, str(exchange_trade_id)),
            ).fetchone():
                return
            c.execute("""INSERT OR IGNORE INTO fill_ledger(fill_key,scope_id,intent_id,pair,order_id,side,base_qty,quote_qty,
                         average_price,fee_asset,fee_amount,fee_quote_value,exchange_trade_id,source_kind,occurred_ts,raw_json,created_ts)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (fill_key,scope_id,intent_id,pair,str(order_id),side,base_qty,quote_qty,average_price,fee_asset,fee_amount,
                       fee_quote_value,str(exchange_trade_id) if exchange_trade_id is not None else None,source_kind,
                       occurred_ts,json.dumps(raw or {},sort_keys=True,separators=(",",":"),default=str),time()))

    def fill_rows(self, intent_id: str, *, canonical_only: bool = True) -> list[dict[str, Any]]:
        with self._connect() as c:
            if canonical_only:
                rows = c.execute(
                    "SELECT * FROM fill_ledger WHERE intent_id=? AND source_kind='exchange_trade' "
                    "ORDER BY occurred_ts, created_ts",
                    (intent_id,),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM fill_ledger WHERE intent_id=? ORDER BY occurred_ts, created_ts",
                    (intent_id,),
                ).fetchall()
            return [dict(r) for r in rows]


    def generation(self, intent_id: str, generation: int) -> dict[str, Any] | None:
        with self._connect() as c:
            row=c.execute("SELECT * FROM protection WHERE intent_id=? AND generation=?",(intent_id,int(generation))).fetchone()
            return dict(row) if row else None

    def latest_generation_with_orders(self, intent_id: str) -> dict[str, Any] | None:
        with self._connect() as c:
            row=c.execute("""SELECT * FROM protection WHERE intent_id=?
                AND (tp_order_id IS NOT NULL OR sl_order_id IS NOT NULL OR working_order_id IS NOT NULL)
                ORDER BY generation DESC LIMIT 1""",(intent_id,)).fetchone()
            return dict(row) if row else None

    def latest_generation(self, intent_id: str) -> dict[str, Any] | None:
        with self._connect() as c:
            row=c.execute("SELECT * FROM protection WHERE intent_id=? ORDER BY generation DESC LIMIT 1",(intent_id,)).fetchone()
            return dict(row) if row else None
