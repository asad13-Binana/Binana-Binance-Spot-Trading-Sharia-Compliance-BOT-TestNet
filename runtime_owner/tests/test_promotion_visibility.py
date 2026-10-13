"""Observed Testnet cancel acknowledgement can precede query visibility."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from registry_fixture import registry_document
from freqtrade.binana.execution_manager import BinanaExecutionManager
from binana_tests.test_partial_promotion_recovery import Exchange, GoodMarket, cfg
from freqtrade.binana.protection_plan import provisional_plan


class PromotionVisibility(unittest.TestCase):
    def setup_manager(self, td):
        registry = Path(td) / 'halal.json'
        registry.write_text(json.dumps(registry_document(['ADAUSDT'])))
        ex = Exchange()
        manager = BinanaExecutionManager(cfg(td, registry), ex)
        manager.market_data = GoodMarket()
        manager.store.put_intent(intent_id='i', pair='ADA/USDT', signal_id='s',
            halal_allowed=True, registry_sha256='h', nominal_usdt='250', state='OPEN')
        manager.store.put_generation(intent_id='i', generation=1, pair='ADA/USDT',
            mode='FIXED_OCO', status='FIXED_ACTIVE', expected_qty='9.98',
            ids={'order_list_id':'10', 'list_client_id':'L', 'tp_order_id':'11',
                 'tp_client_id':'TP', 'sl_order_id':'12', 'sl_client_id':'SL'})
        trade = SimpleNamespace(id=1, pair='ADA/USDT', amount=10., open_rate=1.)
        return manager, ex, trade

    def test_reconciliation_verifies_new_protection_in_same_cycle(self):
        for foreign in (False, True):
            with self.subTest(foreign=foreign), TemporaryDirectory() as td:
                manager, ex, trade = self.setup_manager(td)
                manager.store.set_trade_id('i', 1)
                trade.orders = []
                plan = provisional_plan(intent_id='i', pair='ADA/USDT', quantity='9.98',
                    entry_limit='1', tick_size='0.0001', target_fraction='0.015',
                    stop_fraction='0.01', stop_limit_buffer_fraction='0.0015',
                    estimated_exit_fee_fraction='0.001')
                manager.store.put_generation(intent_id='i', generation=1, pair='ADA/USDT',
                    mode='FIXED_OCO', status='FIXED_ACTIVE', expected_qty='9.98',
                    ids={'working_order_id':'10', 'working_client_id':'W'},
                    payload={'plan':json.loads(json.dumps(plan.__dict__, default=str))})
                original = ex.fetch_order
                cancelled = []
                ex._api.privateDeleteOrderList = lambda p: cancelled.append(p) or {}
                def fetch(oid, pair):
                    row = original(oid, pair)
                    if str(oid) in {'11','12'} and not cancelled:
                        row['status'] = 'open'
                        row['info'].update(orderListId=10, type='LIMIT_MAKER' if str(oid)=='11' else 'STOP_LOSS_LIMIT',
                            price=str(plan.tp_price if str(oid)=='11' else plan.stop_limit),
                            stopPrice=str(plan.stop_trigger), timeInForce='GTC')
                    if foreign and str(oid)=='23': row['clientOrderId']='foreign'
                    return row
                ex.fetch_order = fetch
                ex._api.fetch_open_orders = lambda pair: [fetch(oid,pair) for oid in
                    (('23','24') if ex._api.oco_calls else (() if cancelled else ('11','12')))]
                manager.reconcile_trade(trade)
                self.assertEqual(len(ex._api.oco_calls), 1)
                if foreign:
                    self.assertNotEqual(manager.store.latest_generation('i')['status'], 'TRAILING_ACTIVE')
                    self.assertTrue(manager.store.has_blockers())
                else:
                    self.assertEqual(manager.store.latest_generation('i')['status'], 'TRAILING_ACTIVE')
                    self.assertFalse(manager.store.has_blockers())

    def test_delayed_cancel_visibility_rechecks_both_children_before_one_replacement(self):
        with TemporaryDirectory() as td, patch('time.sleep') as sleep:
            manager, ex, trade = self.setup_manager(td)
            original = ex.fetch_order
            reads = []
            def fetch(oid, pair):
                reads.append(str(oid))
                row = original(oid, pair)
                if len(reads) <= 2:
                    row['status'] = 'open'
                return row
            ex.fetch_order = fetch
            manager.maybe_promote(trade, 'i', manager.store.latest_generation('i'))
            self.assertEqual(reads, ['11', '12', '11', '12'])
            self.assertEqual(len(ex._api.oco_calls), 1)
            self.assertEqual(ex._api.oco_calls[0]['quantity'], '9.98')
            self.assertEqual(sleep.call_count, 1)

    def test_persistently_live_child_exhausts_bound_without_replacement(self):
        with TemporaryDirectory() as td, patch('time.sleep') as sleep:
            manager, ex, trade = self.setup_manager(td)
            original = ex.fetch_order
            def fetch(oid, pair):
                row = original(oid, pair)
                if str(oid) == '12': row['status'] = 'open'
                return row
            ex.fetch_order = fetch
            manager.maybe_promote(trade, 'i', manager.store.latest_generation('i'))
            self.assertEqual(ex._api.oco_calls, [])
            self.assertEqual(manager.store.latest_generation('i')['status'], 'CANCEL_UNKNOWN')
            self.assertEqual(sleep.call_count, 3)

    def test_identity_mismatch_never_retries_or_submits(self):
        with TemporaryDirectory() as td, patch('time.sleep') as sleep:
            manager, ex, trade = self.setup_manager(td)
            original = ex.fetch_order
            def fetch(oid, pair):
                row = original(oid, pair)
                row['clientOrderId'] = 'foreign'
                return row
            ex.fetch_order = fetch
            manager.maybe_promote(trade, 'i', manager.store.latest_generation('i'))
            self.assertEqual(ex._api.oco_calls, [])
            self.assertFalse(sleep.called)

