from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from services.execution_sidecar.risk_checks import FreshSignalGuard
from services.execution_sidecar.state_store import SCHEMA_VERSION, StateStore


ROOT = Path(__file__).resolve().parents[1]
FROZEN_HASHES = {
    "freqtrade/user_data/strategies/IctSmcStrategy.py":
        "9f6bafc78c8cd0d9b9cbde615ddce89e304ab09738584b88d05bfdf92ff4e830",
    "legacy_core/binance_bot_V4.9.16_ALL_IN_ONE.py":
        "70b1d67cc0092b5b8db4a68b343cf893641bde1aae580e9ef51e2adec1062459",
}


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


class SafetyStateSQLiteTests(unittest.TestCase):
    def test_valid_legacy_json_upgrades_an_existing_unversioned_database(self):
        with tempfile.TemporaryDirectory(prefix="safety-existing-upgrade-") as raw:
            root = Path(raw)
            sidecar = root / "state.json"
            risk = root / "risk.json"
            database = root / "state.sqlite"
            halt = {
                "reason": "legacy reconciliation", "symbol": "SOLUSDT",
                "kind": "reconciliation", "latched_at": "2026-08-13T00:00:00+00:00",
            }
            _write(sidecar, {
                "entries_enabled": False,
                "safety_halts": {"reconcile:SOLUSDT": halt},
                "recovery_intents": {
                    "reconcile:SOLUSDT": dict(halt, status="PENDING"),
                },
            })
            _write(risk, {
                "pairs": {}, "daily": {"2026-08-13": {"global_stopouts": 1}},
                "global_pause": "legacy pause",
            })

            # Represents an installation created by the pre-versioning code.
            con = sqlite3.connect(database)
            try:
                con.execute(
                    "CREATE TABLE trade_records (trade_id TEXT PRIMARY KEY, pair TEXT NOT NULL)"
                )
                self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], 0)
                con.commit()
            finally:
                con.close()

            store = StateStore(sidecar, database)
            guard = FreshSignalGuard(risk, state_store=store)
            self.assertIn("reconcile:SOLUSDT", store.safety_halts())
            self.assertEqual(guard.state["global_pause"], "legacy pause")

            con = sqlite3.connect(database)
            try:
                self.assertEqual(
                    con.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION
                )
                self.assertEqual(
                    con.execute(
                        "SELECT COUNT(*) FROM state_migrations"
                    ).fetchone()[0],
                    2,
                )
            finally:
                con.close()

    def test_valid_legacy_json_migrates_once_and_restart_uses_sqlite(self):
        with tempfile.TemporaryDirectory(prefix="safety-migration-") as raw:
            root = Path(raw)
            sidecar = root / "state.json"
            risk = root / "risk.json"
            database = root / "state.sqlite"
            halt = {
                "reason": "uncertain order", "symbol": "ETHUSDT",
                "kind": "reconciliation", "latched_at": "2026-08-13T00:00:00+00:00",
            }
            intent = dict(halt, status="PENDING", client_order_id="FORTRESS_1")
            risk_state = {
                "pairs": {
                    "ETH/USDT": {
                        "signal_id": "signal-1",
                        "candle_time": "2026-08-13T00:00:00+00:00",
                        "last_stopout_time": "2026-08-13T00:01:00+00:00",
                        "cooldown_until": "2026-08-13T00:02:00+00:00",
                    }
                },
                "daily": {
                    "2026-08-13": {
                        "global_stopouts": 2, "pairs": {"ETH/USDT": 2},
                    }
                },
                "global_pause": "daily loss budget",
            }
            _write(sidecar, {
                "entries_enabled": False,
                "safety_halts": {"reconcile:ETHUSDT": halt},
                "recovery_intents": {"reconcile:ETHUSDT": intent},
            })
            _write(risk, risk_state)

            first = StateStore(sidecar, database)
            first_guard = FreshSignalGuard(risk, state_store=first)
            self.assertEqual(first.safety_halts(), {"reconcile:ETHUSDT": halt})
            self.assertEqual(first_guard.state, risk_state)

            # Legacy files are no longer authoritative after the one-time import.
            _write(sidecar, {"safety_halts": {}, "recovery_intents": {}})
            _write(risk, {"pairs": {}, "daily": {}, "global_pause": ""})
            restarted = StateStore(sidecar, database)
            restarted_guard = FreshSignalGuard(risk, state_store=restarted)
            self.assertEqual(restarted.safety_halts(), {"reconcile:ETHUSDT": halt})
            self.assertEqual(restarted.data["recovery_intents"], {"reconcile:ETHUSDT": intent})
            self.assertEqual(restarted_guard.state, risk_state)

            con = sqlite3.connect(database)
            try:
                self.assertEqual(
                    con.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
                names = [
                    row[0] for row in con.execute(
                        "SELECT migration_name FROM state_migrations ORDER BY migration_name")
                ]
                self.assertEqual(names, [
                    "legacy-fresh-signal-guard-v1", "legacy-sidecar-safety-v1",
                ])
            finally:
                con.close()

    def test_corrupt_legacy_files_fail_closed_until_sqlite_is_authoritative(self):
        with tempfile.TemporaryDirectory(prefix="safety-corrupt-") as raw:
            root = Path(raw)
            sidecar = root / "state.json"
            sidecar.write_text("{truncated", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "migration cannot be proven"):
                StateStore(sidecar, root / "state.sqlite")

        with tempfile.TemporaryDirectory(prefix="risk-corrupt-") as raw:
            root = Path(raw)
            store = StateStore(root / "state.json", root / "state.sqlite")
            risk = root / "risk.json"
            risk.write_text("{truncated", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "recovery cannot be proven"):
                FreshSignalGuard(risk, state_store=store)

    def test_corrupt_legacy_mirrors_cannot_override_authoritative_sqlite(self):
        with tempfile.TemporaryDirectory(prefix="safety-authoritative-") as raw:
            root = Path(raw)
            sidecar, risk = root / "state.json", root / "risk.json"
            database = root / "state.sqlite"
            first = StateStore(sidecar, database)
            first_guard = FreshSignalGuard(risk, state_store=first)
            first.latch_safety("halt-1", "must reconcile", symbol="BTCUSDT")
            first_guard.set_global_pause("owner review")

            sidecar.write_text("{broken", encoding="utf-8")
            risk.write_text("{broken", encoding="utf-8")
            restarted = StateStore(sidecar, database)
            restarted_guard = FreshSignalGuard(risk, state_store=restarted)
            self.assertIn("halt-1", restarted.safety_halts())
            self.assertEqual(restarted_guard.state["global_pause"], "owner review")
            self.assertFalse(restarted.entries())

    def test_missing_legacy_is_safe_only_for_new_database_and_never_resets_sqlite(self):
        with tempfile.TemporaryDirectory(prefix="safety-missing-") as raw:
            root = Path(raw)
            sidecar, risk = root / "state.json", root / "risk.json"
            database = root / "state.sqlite"
            first = StateStore(sidecar, database)
            first_guard = FreshSignalGuard(risk, state_store=first)
            self.assertEqual(first.safety_halts(), {})
            self.assertEqual(first_guard.state["daily"], {})
            first.latch_safety("halt-1", "restart proof")
            first_guard.set_global_pause("restart proof")
            sidecar.unlink()
            risk.unlink()

            restarted = StateStore(sidecar, database)
            restarted_guard = FreshSignalGuard(risk, state_store=restarted)
            self.assertIn("halt-1", restarted.safety_halts())
            self.assertEqual(restarted_guard.state["global_pause"], "restart proof")

    def test_existing_unmigrated_database_without_legacy_state_fails_closed(self):
        with tempfile.TemporaryDirectory(prefix="safety-existing-") as raw:
            root = Path(raw)
            database = root / "state.sqlite"
            sqlite3.connect(database).close()
            with self.assertRaisesRegex(RuntimeError, "missing.*existing SQLite"):
                StateStore(root / "missing.json", database)

    def test_sqlite_commit_failure_is_not_acknowledged_and_entries_stay_closed(self):
        with tempfile.TemporaryDirectory(prefix="safety-disk-full-") as raw:
            root = Path(raw)
            store = StateStore(root / "state.json", root / "state.sqlite")
            guard = FreshSignalGuard(root / "risk.json", state_store=store)

            @contextmanager
            def fail_commit():
                con = sqlite3.connect(store.db_path)
                con.row_factory = sqlite3.Row
                try:
                    yield con
                    raise sqlite3.OperationalError("database or disk is full")
                finally:
                    con.rollback()
                    con.close()

            with mock.patch.object(store, "_connect", side_effect=fail_commit):
                with self.assertRaisesRegex(RuntimeError, "entries remain fail closed"):
                    store.latch_safety("not-durable", "disk full")
            self.assertFalse(store.entries())
            with self.assertRaisesRegex(RuntimeError, "durability failure"):
                store.set_entries(True)

            # A failed risk mutation is also not acknowledged as durable.
            store._durability_fault = ""
            with mock.patch.object(store, "_connect", side_effect=fail_commit):
                with self.assertRaisesRegex(RuntimeError, "entries remain fail closed"):
                    guard.set_global_pause("not durable")
            self.assertFalse(store.entries())

            restarted = StateStore(root / "state.json", root / "state.sqlite")
            restarted_guard = FreshSignalGuard(root / "risk.json", state_store=restarted)
            self.assertNotIn("not-durable", restarted.safety_halts())
            self.assertEqual(restarted_guard.state["global_pause"], "")

    def test_unknown_future_schema_fails_closed(self):
        with tempfile.TemporaryDirectory(prefix="safety-future-schema-") as raw:
            root = Path(raw)
            store = StateStore(root / "state.json", root / "state.sqlite")
            del store
            con = sqlite3.connect(root / "state.sqlite")
            try:
                con.execute("PRAGMA user_version=999")
                con.commit()
            finally:
                con.close()
            with self.assertRaisesRegex(RuntimeError, "unsupported future SQLite schema"):
                StateStore(root / "state.json", root / "state.sqlite")

    def test_strategy_and_legacy_core_fingerprints_are_unchanged(self):
        for relative, expected in FROZEN_HASHES.items():
            with self.subTest(relative=relative):
                actual = hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
                self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
