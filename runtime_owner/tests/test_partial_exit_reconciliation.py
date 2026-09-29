from __future__ import annotations
from registry_fixture import registry_document
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from freqtrade.binana.execution_manager import BinanaExecutionManager
from binana_tests.test_partial_promotion_recovery import Exchange, cfg


class PartialExitReconciliation(unittest.TestCase):
    def test_canceled_partially_filled_exit_is_returned_to_freqtrade(self):
        with TemporaryDirectory() as td:
            registry = Path(td) / 'halal.json'
            registry.write_text(json.dumps(registry_document(['ADAUSDT'])))
            ex = Exchange()
            manager = BinanaExecutionManager(cfg(td, registry), ex)
            iid = 'intent'
            manager.store.put_intent(intent_id=iid, pair='ADA/USDT', signal_id='x', halal_allowed=True,
                registry_sha256='h', nominal_usdt='250', state='UNKNOWN')
            manager.store.set_trade_id(iid, 1)
            manager.store.put_generation(intent_id=iid, generation=1, pair='ADA/USDT', mode='FIXED_OCO',
                status='PARTIAL_EXIT', ids={'order_list_id': '10', 'list_client_id': 'L',
                'tp_order_id': '11', 'tp_client_id': 'TP', 'sl_order_id': '12', 'sl_client_id': 'SL'},
                expected_qty='9.98')
            partial = {'id': '11', 'clientOrderId':'TP', 'symbol':'ADA/USDT',
                'status': 'canceled', 'side': 'sell', 'type': 'limit',
                'price': 1.02, 'average': 1.02, 'amount': 9.98, 'filled': 1.0,
                'remaining': 8.98, 'cost': 1.02, 'timestamp': 1000,
                'fee': {'currency': 'USDT', 'cost': 0.001}, 'info':{'clientOrderId':'TP'}}
            base_fetch=ex.fetch_order
            def fetch_order(order_id, pair):
                return partial if str(order_id) == '11' else base_fetch(order_id,pair)
            ex.fetch_order = fetch_order

            result = manager.reconcile_trade(SimpleNamespace(id=1, pair='ADA/USDT'))

            self.assertIsNotNone(result)
            self.assertEqual(result['exit_order']['status'], 'canceled')
            self.assertEqual(result['exit_order']['filled'], 1.0)
            self.assertEqual(result['exit_reason'], 'binana_tp')
            rows = manager.store.fill_rows(iid)
            self.assertEqual(sum(float(r['base_qty']) for r in rows if r['side'] == 'SELL'), 1.0)


if __name__ == '__main__':
    unittest.main()
