from __future__ import annotations

import contextlib
import sqlite3
import threading
from pathlib import Path
from typing import Iterator


SCHEMA_VERSION = 1


class DatabaseIntegrityError(RuntimeError):
    pass


class Database:
    """Authoritative SQLite safety-state store.

    SQLite is intentionally treated as part of the safety boundary. The process
    refuses to continue if quick_check fails, foreign keys are enabled for every
    connection, and state-changing repository methods use explicit transactions.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.path,
            timeout=10.0,
            isolation_level=None,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._configure()
        self._integrity_check()
        self._migrate()

    def _configure(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.execute("PRAGMA busy_timeout=10000")
            self._conn.execute("PRAGMA wal_autocheckpoint=1000")

    def _integrity_check(self) -> None:
        row = self._conn.execute("PRAGMA quick_check").fetchone()
        if row is None or row[0] != "ok":
            raise DatabaseIntegrityError(f"SQLite quick_check failed: {row[0] if row else 'no result'}")

    @contextlib.contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
                yield self._conn
                self._conn.execute("COMMIT")
            except Exception:
                with contextlib.suppress(sqlite3.Error):
                    self._conn.execute("ROLLBACK")
                raise

    def execute(self, sql: str, params: tuple[object, ...] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def _migrate(self) -> None:
        current = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
        if current > SCHEMA_VERSION:
            raise RuntimeError(f"Database schema {current} is newer than supported {SCHEMA_VERSION}")
        if current == 0:
            self._migration_1()
            current = 1
        if current != SCHEMA_VERSION:
            raise RuntimeError(f"Database migration incomplete: {current} != {SCHEMA_VERSION}")

    def _migration_1(self) -> None:
        ddl = """
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY,
            client_order_id TEXT NOT NULL UNIQUE,
            exchange_order_id INTEGER,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            order_type TEXT NOT NULL,
            quantity TEXT NOT NULL,
            price TEXT,
            state TEXT NOT NULL,
            exchange_status TEXT,
            executed_qty TEXT NOT NULL DEFAULT '0',
            cumulative_quote_qty TEXT NOT NULL DEFAULT '0',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            raw_json TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_orders_symbol_state ON orders(symbol, state);

        CREATE TABLE IF NOT EXISTS fills (
            id INTEGER PRIMARY KEY,
            client_order_id TEXT NOT NULL REFERENCES orders(client_order_id) ON DELETE RESTRICT,
            exchange_trade_id INTEGER,
            quantity TEXT NOT NULL,
            price TEXT NOT NULL,
            quote_quantity TEXT NOT NULL,
            commission TEXT,
            commission_asset TEXT,
            event_time TEXT NOT NULL,
            UNIQUE(client_order_id, exchange_trade_id)
        );

        CREATE TABLE IF NOT EXISTS positions (
            symbol TEXT PRIMARY KEY,
            base_asset TEXT NOT NULL,
            quantity TEXT NOT NULL,
            cost_basis_quote TEXT NOT NULL,
            state TEXT NOT NULL,
            opened_at TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS signals (
            signal_id TEXT PRIMARY KEY,
            symbol TEXT NOT NULL,
            candle_time TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            strategy TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            consumed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS sharia_decisions (
            symbol TEXT PRIMARY KEY,
            decision TEXT NOT NULL,
            status_code TEXT NOT NULL,
            source TEXT NOT NULL,
            reviewed_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            controller_version TEXT NOT NULL,
            evidence_hash TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sharia_source_versions (
            source_id TEXT PRIMARY KEY,
            version_hash TEXT NOT NULL,
            retrieved_at TEXT NOT NULL,
            metadata_json TEXT
        );

        CREATE TABLE IF NOT EXISTS daily_loss_state (
            trading_day TEXT PRIMARY KEY,
            realized_loss_quote TEXT NOT NULL DEFAULT '0',
            realized_pnl_quote TEXT NOT NULL DEFAULT '0',
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS trade_counters (
            trading_day TEXT NOT NULL,
            symbol TEXT NOT NULL,
            entries INTEGER NOT NULL DEFAULT 0,
            stopouts INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(trading_day, symbol)
        );

        CREATE TABLE IF NOT EXISTS pair_cooldowns (
            symbol TEXT PRIMARY KEY,
            until_at TEXT NOT NULL,
            reason TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS global_entry_pause (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            paused INTEGER NOT NULL CHECK(paused IN (0,1)),
            reason TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS safety_halts (
            halt_id TEXT PRIMARY KEY,
            active INTEGER NOT NULL CHECK(active IN (0,1)),
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL,
            cleared_at TEXT
        );

        CREATE TABLE IF NOT EXISTS recovery_intents (
            id INTEGER PRIMARY KEY,
            client_order_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            kind TEXT NOT NULL,
            status TEXT NOT NULL,
            details_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_recovery_open ON recovery_intents(status, client_order_id);

        CREATE TABLE IF NOT EXISTS telegram_updates (
            update_id INTEGER PRIMARY KEY,
            received_at TEXT NOT NULL,
            handled_at TEXT,
            outcome TEXT
        );

        CREATE TABLE IF NOT EXISTS telegram_command_idempotency (
            command_key TEXT PRIMARY KEY,
            update_id INTEGER,
            command TEXT NOT NULL,
            result TEXT,
            created_at TEXT NOT NULL,
            completed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS audit_events (
            id INTEGER PRIMARY KEY,
            event_type TEXT NOT NULL,
            severity TEXT NOT NULL,
            symbol TEXT,
            client_order_id TEXT,
            details_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS protection_state (
            symbol TEXT PRIMARY KEY,
            client_order_id TEXT,
            state TEXT NOT NULL,
            protection_gap_started_at TEXT,
            protection_ack_at TEXT,
            replacement_client_order_id TEXT,
            details_json TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS reconciliation_events (
            id INTEGER PRIMARY KEY,
            client_order_id TEXT,
            symbol TEXT NOT NULL,
            source TEXT NOT NULL,
            previous_state TEXT,
            observed_status TEXT,
            outcome TEXT NOT NULL,
            details_json TEXT,
            created_at TEXT NOT NULL
        );
        """
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                for statement in ddl.split(";"):
                    statement = statement.strip()
                    if statement:
                        self._conn.execute(statement)
                self._conn.execute(
                    "INSERT OR IGNORE INTO global_entry_pause(singleton, paused, reason, updated_at) "
                    "VALUES(1, 1, 'fresh database requires explicit enable', strftime('%Y-%m-%dT%H:%M:%fZ','now'))"
                )
                self._conn.execute("PRAGMA user_version=1")
                self._conn.execute("COMMIT")
            except Exception:
                with contextlib.suppress(sqlite3.Error):
                    self._conn.execute("ROLLBACK")
                raise

    def checkpoint(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def close(self) -> None:
        with self._lock:
            self._conn.close()
