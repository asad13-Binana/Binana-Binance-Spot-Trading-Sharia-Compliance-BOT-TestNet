from __future__ import annotations
from registry_fixture import registry_document
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from freqtrade.binana.execution_manager import BinanaExecutionManager
from binana_tests.test_partial_promotion_recovery import Exchange, GoodMarket, cfg


def owned(order, client_id, pair='ADA/USDT'):
    row=dict(order)
    row['clientOrderId']=client_id
    row['symbol']=pair
    info=dict(row.get('info') or {})
    info['clientOrderId']=client_id
    row['info']=info
    return row


class RestartPartialRecovery(unittest.TestCase):
    def test_historical_absence_allows_new_owned_oco_but_blocks_unowned_orders(self):
        from time import time
        with TemporaryDirectory() as td:
            manager, ex = self._manager(td)
            iid = self._seed(manager)
            manager.store.update_generation_status(iid, 2, 'SUBMISSION_ABSENT',
                payload={'verified_absent_at': time(), 'list_client_id': 'L2'})

            def missing_list(**kwargs):
                self.assertEqual(kwargs['list_client_id'], 'L2')
                raise RuntimeError('{"code":-2018,"msg":"Order list does not exist."}')

            def missing_child(params):
                self.assertIn(params['origClientOrderId'], ('TP2', 'SL2'))
                raise RuntimeError('{"code":-2013,"msg":"Order does not exist."}')

            manager.order_lists.query_list = missing_list
            ex._api.privateGetOrder = missing_child
            trade = SimpleNamespace(id=1, pair='ADA/USDT', amount=10.0,
                                    open_rate=1.0, orders=[])
            # Initial absence permits exactly one replacement with no open orders.
            self.assertIsNone(manager.reconcile_trade(trade))
            self.assertEqual(len(ex._api.oco_calls), 1)
            ex._api.fetch_open_orders = lambda pair: [ex.fetch_order(oid, pair) for oid in ('23', '24')]
            for restarted in (False, True):
                if restarted:
                    manager = BinanaExecutionManager(manager.config, ex)
                    manager.market_data = GoodMarket()
                    manager.order_lists.query_list = missing_list
                self.assertIsNone(manager.reconcile_trade(trade))
                self.assertEqual(manager.store.latest_generation(iid)['status'], 'TRAILING_ACTIVE')
                self.assertEqual(manager.store.unresolved(), [])
                self.assertEqual(len(ex._api.oco_calls), 1)
            ex._api.fetch_open_orders = lambda pair: [{'id': 'unowned'}]
            self.assertIsNone(manager.reconcile_trade(trade))
            self.assertTrue(manager.store.has_blockers())
            self.assertEqual(len(ex._api.oco_calls), 1)

    def _manager(self, td):
        registry = Path(td) / 'halal.json'
        registry.write_text(json.dumps(registry_document(['ADAUSDT'])))
        ex = Exchange()
        ex.get_min_pair_stake_amount = lambda *a, **k: 5.0
        manager = BinanaExecutionManager(cfg(td, registry), ex)
        manager.market_data = GoodMarket()
        return manager, ex

    def _seed(self, manager):
        iid='intent'
        manager.store.put_intent(intent_id=iid,pair='ADA/USDT',signal_id='x',halal_allowed=True,
            registry_sha256='h',nominal_usdt='250',state='UNKNOWN')
        manager.store.set_trade_id(iid,1)
        manager.store.put_generation(intent_id=iid,generation=1,pair='ADA/USDT',mode='FIXED_OCO',
            status='PARTIAL_EXIT',ids={'order_list_id':'10','list_client_id':'L',
            'working_order_id':'10','working_client_id':'W','tp_order_id':'11',
            'tp_client_id':'TP','sl_order_id':'12','sl_client_id':'SL'},expected_qty='9.98')
        manager.store.put_generation(intent_id=iid,generation=2,pair='ADA/USDT',mode='TRAILING_OCO',
            status='ABORTED_OLD_FILL',ids={'list_client_id':'L2','tp_client_id':'TP2','sl_client_id':'SL2'},
            expected_qty='9.98')
        manager.store.incident(incident_id='promotion-old-fill-'+iid,intent_id=iid,pair='ADA/USDT',
            code='PROMOTION_OLD_CHILD_EXECUTED',detail='partial')
        return iid
    def test_restart_imports_partial_fill_then_reprotects_once(self):
        with TemporaryDirectory() as td:
            manager, ex = self._manager(td)
            iid = self._seed(manager)
            partial={'id':'11','status':'canceled','side':'sell','type':'limit','price':1.02,
                'average':1.02,'amount':9.98,'filled':1.0,'remaining':8.98,'cost':1.02,
                'timestamp':1000,'fee':{'currency':'USDT','cost':0.001}}
            partial=owned(partial,'TP')
            base_fetch=ex.fetch_order
            ex.fetch_order=lambda oid,pair: partial if str(oid)=='11' else base_fetch(oid,pair)
            trade=SimpleNamespace(id=1,pair='ADA/USDT',amount=9.98,open_rate=1.0)

            result=manager.reconcile_trade(trade)
            self.assertIsNotNone(result)
            self.assertEqual(result['exit_order']['filled'],1.0)
            self.assertEqual(sum(float(r['base_qty']) for r in manager.store.fill_rows(iid) if r['side']=='SELL'),1.0)
            self.assertEqual(len(ex._api.oco_calls),0)

            # Simulate Freqtrade canonical trade amount after it consumes the partial exit.
            trade.amount=9.0
            trade.orders=[SimpleNamespace(order_id="11",filled=1.0,status="canceled",ft_is_open=False)]
            # The exchange keeps returning the same terminal partial child after restart.
            # Recovery must recognize that Freqtrade already consumed this fill.
            self.assertIsNone(manager.reconcile_trade(trade))
            self.assertEqual(len(ex._api.oco_calls),1)
            self.assertEqual(ex._api.oco_calls[0]['quantity'],'8.98')
            self.assertEqual(manager.store.latest_generation(iid)['status'],'TRAILING_ACTIVE')
            self.assertEqual(manager.store.unresolved(),[])

            # Third pass must not duplicate fills or replacement protection.
            self.assertIsNone(manager.reconcile_trade(trade))
            self.assertEqual(len(ex._api.oco_calls),1)
            self.assertEqual(len([r for r in manager.store.fill_rows(iid) if r['side']=='SELL']),1)

    def test_fee_reserve_does_not_block_residual_reprotection(self):
        with TemporaryDirectory() as td:
            manager, ex = self._manager(td)
            iid = self._seed(manager)
            partial={"id":"11","status":"canceled","side":"sell","type":"limit","price":1.02,
                "average":1.02,"amount":9.98,"filled":1.0,"remaining":8.98,"cost":1.02,
                "timestamp":1000,"fee":{"currency":"USDT","cost":0.001}}
            partial=owned(partial,'TP')
            base_fetch=ex.fetch_order
            ex.fetch_order=lambda oid,pair: partial if str(oid)=="11" else base_fetch(oid,pair)
            trade=SimpleNamespace(id=1,pair="ADA/USDT",amount=10.0,open_rate=1.0,orders=[])

            self.assertIsNotNone(manager.reconcile_trade(trade))
            trade.amount=9.0
            trade.orders=[SimpleNamespace(order_id="11",filled=1.0,status="canceled",ft_is_open=False)]

            self.assertIsNone(manager.reconcile_trade(trade))
            self.assertEqual(len(ex._api.oco_calls),1)
            self.assertEqual(ex._api.oco_calls[0]["quantity"],"8.98")
            self.assertEqual(manager.store.latest_generation(iid)["status"],"TRAILING_ACTIVE")
            self.assertEqual(manager.store.unresolved(),[])

    def test_restart_with_working_entry_keeps_generation_ownership_correct(self):
        with TemporaryDirectory() as td:
            manager, ex = self._manager(td)
            iid = self._seed(manager)
            manager.store.put_generation(intent_id=iid,generation=1,pair="ADA/USDT",mode="FIXED_OCO",
                status="PARTIAL_EXIT",ids={"order_list_id":"10","list_client_id":"L","working_order_id":"10",
                "working_client_id":"W","tp_order_id":"11","tp_client_id":"TP","sl_order_id":"12","sl_client_id":"SL"},
                expected_qty="9.98")
            entry={"id":"10","status":"closed","side":"buy","type":"limit","price":1.0,
                "average":1.0,"amount":10.0,"filled":10.0,"remaining":0.0,"cost":10.0,"timestamp":900}
            partial={"id":"11","status":"canceled","side":"sell","type":"limit","price":1.02,
                "average":1.02,"amount":9.98,"filled":1.0,"remaining":8.98,"cost":1.02,
                "timestamp":1000,"fee":{"currency":"USDT","cost":0.001}}
            entry=owned(entry,'W')
            partial=owned(partial,'TP')
            base_fetch=ex.fetch_order
            ex.fetch_order=lambda oid,pair: entry if str(oid)=="10" else (partial if str(oid)=="11" else base_fetch(oid,pair))
            trade=SimpleNamespace(id=1,pair="ADA/USDT",amount=10.0,open_rate=1.0,orders=[])

            self.assertIsNotNone(manager.reconcile_trade(trade))
            self.assertEqual(manager.store.generation(iid,1)["status"],"TERMINAL")
            self.assertNotEqual(manager.store.latest_generation(iid)["status"],"PARTIAL_EXIT")

            trade.amount=9.0
            trade.orders=[SimpleNamespace(order_id="11",filled=1.0,status="canceled",ft_is_open=False)]
            self.assertIsNone(manager.reconcile_trade(trade))
            self.assertEqual(len(ex._api.oco_calls),1)
            self.assertEqual(ex._api.oco_calls[0]["quantity"],"8.98")
            self.assertEqual(manager.store.latest_generation(iid)["status"],"TRAILING_ACTIVE")
            self.assertEqual(manager.store.unresolved(),[])

    def test_polluted_newer_partial_without_order_ids_stays_blocked(self):
        with TemporaryDirectory() as td:
            manager, ex = self._manager(td)
            iid = self._seed(manager)
            partial={"id":"11","status":"canceled","side":"sell","type":"limit","price":1.02,
                "average":1.02,"amount":9.98,"filled":1.0,"remaining":8.98,"cost":1.02,
                "timestamp":1000,"fee":{"currency":"USDT","cost":0.001}}
            partial=owned(partial,'TP')
            base_fetch=ex.fetch_order
            ex.fetch_order=lambda oid,pair: partial if str(oid)=="11" else base_fetch(oid,pair)
            trade=SimpleNamespace(id=1,pair="ADA/USDT",amount=9.98,open_rate=1.0,orders=[])
            self.assertIsNotNone(manager.reconcile_trade(trade))
            manager.store.update_generation_status(iid,2,"PARTIAL_EXIT")
            trade.amount=9.0
            trade.orders=[SimpleNamespace(order_id="11",filled=1.0,status="canceled",ft_is_open=False)]
            self.assertIsNone(manager.reconcile_trade(trade))
            self.assertEqual(len(ex._api.oco_calls),0)
            self.assertTrue(manager.store.has_blockers())


    def test_latest_generation_partial_exit_reprotects_after_freqtrade_applies_fill(self):
        with TemporaryDirectory() as td:
            manager, ex = self._manager(td)
            iid='intent-latest'
            manager.store.put_intent(intent_id=iid,pair='ADA/USDT',signal_id='x',halal_allowed=True,
                registry_sha256='h',nominal_usdt='250',state='OPEN')
            manager.store.set_trade_id(iid,1)
            manager.store.put_generation(intent_id=iid,generation=1,pair='ADA/USDT',mode='TRAILING_OCO',
                status='TRAILING_ACTIVE',ids={'order_list_id':'100','list_client_id':'L','working_order_id':'10',
                'working_client_id':'W','tp_order_id':'11','tp_client_id':'TP','sl_order_id':'12','sl_client_id':'SL'},expected_qty='9.98')
            partial={'id':'11','status':'canceled','side':'sell','type':'limit','price':1.02,
                'average':1.02,'amount':9.98,'filled':1.0,'remaining':8.98,'cost':1.02,
                'timestamp':1000,'fee':{'currency':'USDT','cost':0.001}}
            partial=owned(partial,'TP')
            base_fetch=ex.fetch_order
            ex.fetch_order=lambda oid,pair: partial if str(oid)=='11' else base_fetch(oid,pair)
            trade=SimpleNamespace(id=1,pair='ADA/USDT',amount=9.98,open_rate=1.0,orders=[])
            self.assertIsNotNone(manager.reconcile_trade(trade))
            trade.amount=9.0
            trade.orders=[SimpleNamespace(order_id='11',filled=1.0,status='canceled',ft_is_open=False)]
            self.assertIsNone(manager.reconcile_trade(trade))
            self.assertEqual(len(ex._api.oco_calls),1)
            self.assertEqual(ex._api.oco_calls[0]['quantity'],'8.98')
            self.assertEqual(manager.store.latest_generation(iid)['status'],'TRAILING_ACTIVE')
            self.assertEqual(manager.store.unresolved(),[])

    def test_unverifiable_partial_fill_stays_blocked(self):
        with TemporaryDirectory() as td:
            manager, ex = self._manager(td)
            iid = self._seed(manager)
            partial={'id':'11','status':'canceled','side':'sell','type':'limit','price':1.02,
                'average':1.02,'amount':9.98,'filled':1.0,'remaining':8.98,'cost':1.02,'timestamp':1000}
            partial=owned(partial,'TP')
            base_fetch=ex.fetch_order
            ex.fetch_order=lambda oid,pair: partial if str(oid)=='11' else base_fetch(oid,pair)
            ex._api.fetch_my_trades=lambda *a,**k: []
            trade=SimpleNamespace(id=1,pair='ADA/USDT',amount=9.98,open_rate=1.0)
            result=manager.reconcile_trade(trade)
            self.assertIsNone(result)
            self.assertTrue(manager.store.has_blockers())
            self.assertEqual(len(ex._api.oco_calls),0)


if __name__ == '__main__':
    unittest.main()
