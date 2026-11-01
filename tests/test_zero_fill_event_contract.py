"""Contradictory execution events must keep or re-reserve a durable slot.

These tests use the real SQLite store and terminal transition. The preceding
authenticated REST proof is a fixture; no exchange client or network is used.
"""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from services.execution_sidecar.state_store import StateStore


class ZeroFillEventContract(unittest.TestCase):
    def store(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = StateStore(Path(directory.name) / 'state.json')
        store.upsert_trade('entry', 'ETH/USDT', entry_order_id=1,
                           entry_client_order_id='fixture-owned-entry',
                           lifecycle_state='ENTRY_SUBMITTED', filled_quantity='0')
        return store

    def finalize(self, store):
        with store._connect() as connection:
            saved = dict(connection.execute('SELECT * FROM trade_records').fetchone())
            cursor = connection.execute('SELECT COALESCE(MAX(id),0) FROM exchange_events').fetchone()[0]
        store.finalize_zero_fill_entry(saved, 'ETHUSDT', cursor, {'orders': {'1': {
            'symbol': 'ETHUSDT', 'orderId': 1, 'clientOrderId': 'fixture-owned-entry',
            'status': 'CANCELED', 'executedQty': '0', 'cummulativeQuoteQty': '0'}}})

    @staticmethod
    def event(**fields):
        return {'e': 'executionReport', 's': 'ETHUSDT', 'i': 1, 'I': 990,
                'E': 1000, 'S': 'BUY', 'o': 'LIMIT', 'X': 'CANCELED',
                'z': '0', 'Z': '0', 'l': '0', 'Y': '0', **fields}

    def assert_reopened(self, store, event):
        self.assertTrue(store.record_exchange_event(event))
        self.assertEqual(store._active_trade_id('ETH/USDT'), 'entry')
        with store._connect() as connection:
            row = connection.execute('SELECT * FROM trade_records').fetchone()
            proof = json.loads(connection.execute(
                "SELECT payload_json FROM recovery_intents WHERE safety_key='entry-terminal:ETHUSDT:1'"
            ).fetchone()[0])
        self.assertEqual(row['lifecycle_state'], 'RECONCILIATION_REQUIRED')
        self.assertEqual(row['reconciliation_status'], 'TERMINAL_ENTRY_CONTRADICTION_RECONCILE_REQUIRED')
        self.assertTrue(proof['invalidated'])

    def test_partial_status_with_zero_cumulative_fill_reopens_terminal_slot(self):
        store = self.store()
        self.finalize(store)
        self.assert_reopened(store, self.event(X='PARTIALLY_FILLED'))

    def test_positive_or_malformed_last_fill_reopens_terminal_slot(self):
        for fields in ({'l': '.1'}, {'Y': '10'}, {'l': None}, {'Y': 'NaN'},
                       {'l': '-1'}, {'Y': 'Infinity'}):
            with self.subTest(fields=fields):
                store = self.store()
                self.finalize(store)
                self.assert_reopened(store, self.event(**fields))

    def test_contradictory_prior_event_prevents_terminal_release(self):
        for fields in ({'X': 'PARTIALLY_FILLED'}, {'l': '.1'}, {'Y': '10'}):
            with self.subTest(fields=fields):
                store = self.store()
                store.record_exchange_event(self.event(**fields))
                with self.assertRaisesRegex(RuntimeError, 'durable execution evidence contradicts'):
                    self.finalize(store)
                self.assertEqual(store._active_trade_id('ETH/USDT'), 'entry')

    def test_trade_execution_and_expiry_reason_reopen_terminal_slot(self):
        for fields in ({'x': 'TRADE'}, {'eR': 'INSUFFICIENT_LIQUIDITY'},
                       {'expiryReason': 'INSUFFICIENT_LIQUIDITY'}):
            with self.subTest(fields=fields):
                store = self.store()
                self.finalize(store)
                self.assert_reopened(store, self.event(**fields))

    def test_valid_zero_fill_event_keeps_verified_terminal_slot_released(self):
        store = self.store()
        store.record_exchange_event(self.event())
        self.finalize(store)
        self.assertIsNone(store._active_trade_id('ETH/USDT'))
        self.assertTrue(store.record_exchange_event(self.event(I=991)))
        restarted = StateStore(store.path, store.db_path)
        self.assertIsNone(restarted._active_trade_id('ETH/USDT'))


if __name__ == '__main__':
    unittest.main()
