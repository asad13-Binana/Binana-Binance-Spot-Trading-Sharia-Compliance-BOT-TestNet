"""Terminal status alone cannot release an unfilled entry's durable slot."""
from decimal import Decimal
from time import time
import unittest

import test_entry_reprice_late_fills as fixtures
from services.execution_sidecar.state_store import StateStore


class ZeroFillTerminalReconciliation(unittest.TestCase):
    fixture = fixtures.RepriceLateFills.fixture
    read_trade = fixtures.RepriceLateFills.read_trade

    def zero_entry(self, *, bracket=False):
        adapter, engine, position, exchange = self.fixture()
        position.filled_qty = position.entry_price = Decimal('0')
        position.entry_client_id = 'FORTRESS_owned_entry'
        position.bracket = bracket
        position.state = __import__('services.execution_sidecar.core_adapter', fromlist=['legacy']).legacy.PosState.PENDING_ENTRY
        if not bracket:
            position.order_list_id = position.tp_order_id = position.sl_order_id = None
        entry = {**exchange.order_facts[1], 'status': 'REJECTED',
                 'executedQty': '0', 'cummulativeQuoteQty': '0',
                 'orderListId': 7 if bracket else -1}
        exchange.order_facts = {1: entry}
        if bracket:
            for oid in (3, 4):
                exchange.order_facts[oid] = {**entry, 'orderId': oid,
                    'side': 'SELL', 'clientOrderId': f'FORTRESS_owned_{oid}'}
            exchange.list_fact = {'symbol': 'ETHUSDT', 'orderListId': 7,
                'listStatusType': 'ALL_DONE', 'listOrderStatus': 'ALL_DONE',
                'orders': [{key: row[key] for key in ('symbol', 'orderId', 'clientOrderId')}
                           for row in exchange.order_facts.values()]}
        exchange.fills = []
        adapter.state_store.mirror_legacy_position(position.symbol, position)
        return adapter, engine, position, exchange

    def test_terminal_list_requires_all_known_members_and_no_open_obligations(self):
        for incomplete in (True, False):
            with self.subTest(incomplete=incomplete):
                adapter, engine, position, exchange = self.zero_entry(bracket=True)
                if incomplete:
                    exchange.list_fact['orders'] = []
                result = adapter._complete_zero_fill_entry(engine.b, engine.pf, position)
                self.assertEqual(result, not incomplete)
                self.assertEqual(adapter.state_store._active_trade_id('ETH/USDT') is None, not incomplete)
                self.assertEqual(exchange.sent, [])
                self.assertFalse(any(event.startswith('cancel') for event in engine.b.events))

    def test_durable_identity_change_during_rest_reads_prevents_release(self):
        for change in ({'entry_client_order_id': 'different-entry'}, {'order_list_id': 999},
                       {'stop_order_id': 777}, {'take_profit_order_id': 778}):
            with self.subTest(change=change):
                adapter, engine, position, exchange = self.zero_entry()
                original = exchange.get_open_orders

                def concurrent_write(**params):
                    adapter.state_store.upsert_trade('1', 'ETH/USDT', **change)
                    return original(**params)

                exchange.get_open_orders = concurrent_write
                self.assertFalse(adapter._complete_zero_fill_entry(engine.b, engine.pf, position))
                self.assertIsNotNone(adapter.state_store._active_trade_id('ETH/USDT'))
                self.assertIn('ETHUSDT', engine.pf.positions)
                self.assertEqual(exchange.sent, [])

    def test_polling_terminal_zero_fill_bracket_does_not_cancel_again(self):
        for age in (0, 86400):
            with self.subTest(age=age):
                adapter, engine, position, exchange = self.zero_entry(bracket=True)
                engine._pending = {'ETHUSDT': {'p': position, 't0': time()-age, 'repriced': 0}}
                adapter._await_fill_interposed(engine, 'ETHUSDT')
                self.assertIsNone(adapter.state_store._active_trade_id('ETH/USDT'))
                self.assertNotIn('ETHUSDT', engine._pending)
                self.assertFalse(any(event.startswith('cancel') for event in engine.b.events))
                self.assertEqual(exchange.sent, [])

    def test_terminal_evidence_survives_stale_state_save_and_restart(self):
        adapter, engine, position, exchange = self.zero_entry()
        store = adapter.state_store
        stale = StateStore(store.path, store.db_path)
        self.assertTrue(adapter._complete_zero_fill_entry(engine.b, engine.pf, position))
        stale._write_safety_state({}, {})
        restarted = StateStore(store.path, store.db_path)
        with restarted._connect() as con:
            row = con.execute("SELECT payload_json FROM recovery_intents WHERE safety_key='entry-terminal:ETHUSDT:1'").fetchone()
        self.assertIsNotNone(row)
        self.assertIsNone(restarted._active_trade_id('ETH/USDT'))

    def test_late_contradictory_fill_reopens_terminal_slot_for_reconciliation(self):
        adapter, engine, position, exchange = self.zero_entry()
        self.assertTrue(adapter._complete_zero_fill_entry(engine.b, engine.pf, position))
        adapter.state_store.record_exchange_event({'e': 'executionReport', 's': 'ETHUSDT',
            'i': 1, 'I': 901, 'E': int(time()*1000), 'S': 'BUY', 'o': 'LIMIT',
            'X': 'PARTIALLY_FILLED', 'z': '.1', 'Z': '10'})
        self.assertEqual(self.read_trade(adapter)['lifecycle_state'], 'RECONCILIATION_REQUIRED')
        self.assertIsNotNone(adapter.state_store._active_trade_id('ETH/USDT'))

    def test_missing_or_contradictory_stream_quantity_cannot_be_treated_as_zero(self):
        for fields in ({}, {'z': 'NaN'}, {'z': '0', 'Z': '1'}, {'z': '0', 'X': 'FILLED'}):
            with self.subTest(fields=fields):
                adapter, engine, position, exchange = self.zero_entry()
                adapter.state_store.record_exchange_event({'e': 'executionReport', 's': 'ETHUSDT',
                    'i': 1, 'I': 902, 'E': int(time()*1000), 'S': 'BUY', 'o': 'LIMIT',
                    'X': 'REJECTED', 'Z': '0', **fields})
                self.assertFalse(adapter._complete_zero_fill_entry(engine.b, engine.pf, position))
                self.assertIsNotNone(adapter.state_store._active_trade_id('ETH/USDT'))


if __name__ == '__main__':
    unittest.main()
