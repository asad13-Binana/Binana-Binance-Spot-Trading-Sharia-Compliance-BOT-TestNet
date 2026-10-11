"""Regressions for the October 12 audit, using the deployable owner code."""
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from decimal import Decimal
from pathlib import Path
import tempfile
import json
from freqtrade.binana.binance_order_lists import BinanceOrderLists, OrderListError
from freqtrade.binana.execution_manager import BinanaExecutionManager, AdmissionRejected
from freqtrade.binana.state_store import StateStore
from binana_tests.test_engine_core import FakeApi
from freqtrade.binana.protection_plan import provisional_plan
from freqtrade.binana.recovery_contract import live_protection, RecoveryBlocked
from freqtrade.binana.persistence_readiness import persistence_readiness


def plan():
    return provisional_plan(intent_id='i', pair='ADA/USDT', quantity='10', entry_limit='1',
        tick_size='0.0001', target_fraction='0.015', stop_fraction='0.01',
        stop_limit_buffer_fraction='0.0015', estimated_exit_fee_fraction='0.001')


class ReferenceContracts(unittest.TestCase):
    def api(self):
        api = FakeApi()
        api.publicGetReferencePrice = lambda p: {'symbol':'ADAUSDT','referencePrice':'1'}
        api.publicGetAvgPrice = lambda p: {'price':'1','mins':5}
        return api

    def test_active_reference_rejects_avg_permitted_price(self):
        api=self.api(); api.publicGetReferencePrice=lambda p:{'symbol':'ADAUSDT','referencePrice':'10'}
        with self.assertRaises(OrderListError): BinanceOrderLists(api).preflight(plan())

    def test_active_reference_does_not_require_average(self):
        api=self.api()
        api.publicGetAvgPrice=lambda p: self.fail('active reference must not request average')
        BinanceOrderLists(api).preflight(plan())

    def test_reference_timeout_never_falls_back(self):
        api=self.api()
        def unavailable(p): raise TimeoutError()
        api.publicGetReferencePrice=unavailable
        with self.assertRaises(OrderListError): BinanceOrderLists(api).preflight(plan())

    def test_wrong_symbol_or_invalid_reference_is_not_absence(self):
        for response in [{}, {'symbol':'OTHER','referencePrice':'1'},
                         {'symbol':'ADAUSDT','referencePrice':'NaN'},
                         {'symbol':'ADAUSDT','referencePrice':'0'}]:
            api=self.api(); api.publicGetReferencePrice=lambda p:response
            with self.subTest(response=response),self.assertRaises(OrderListError):
                BinanceOrderLists(api).preflight(plan())

    def test_null_reference_with_matching_average_window(self):
        api=self.api(); api.publicGetReferencePrice=lambda p:{'symbol':'ADAUSDT','referencePrice':None}
        BinanceOrderLists(api).preflight(plan())

    def test_fallback_wrong_average_window_is_blocked(self):
        api=self.api(); api.publicGetReferencePrice=lambda p:{'symbol':'ADAUSDT','referencePrice':None}
        api.publicGetAvgPrice=lambda p:{'price':'1','mins':1}
        with self.assertRaises(OrderListError): BinanceOrderLists(api).preflight(plan())


class DurablePolicy(unittest.TestCase):
    def test_status_cannot_overwrite_protection_plan_or_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=StateStore(Path(tmp)/'state.db')
            store.put_intent(intent_id='i',pair='ADA/USDT',signal_id='s',halal_allowed=True,
                registry_sha256='a',nominal_usdt='250',admission={})
            store.put_generation(intent_id='i',generation=1,pair='ADA/USDT',mode='FIXED_OCO',
                status='SUBMISSION_PENDING',payload={'plan':json.loads(json.dumps(plan().__dict__,default=str)),'profile':'DEFENSIVE'})
            for payload in [{'plan':{}},{'profile':'NORMAL'}]:
                with self.subTest(payload=payload),self.assertRaises(ValueError):
                    store.update_generation_status('i',1,'ACTIVE',payload=payload)
            self.assertEqual(json.loads(store.generation('i',1)['payload_json'])['profile'],'DEFENSIVE')


class StorageAdmission(unittest.TestCase):
    def test_critical_persistence_volume_blocks_even_if_release_snapshot_is_green(self):
        manager=object.__new__(BinanaExecutionManager)
        with tempfile.TemporaryDirectory() as tmp:
            manager.config={'binana':{'state_db_path':str(Path(tmp)/'state.db')}}
            manager.release_status=lambda:{'blockers':[]}
            with patch('shutil.disk_usage',return_value=SimpleNamespace(total=100*1024**3,free=2*1024**3,used=98*1024**3)):
                with self.assertRaises(AdmissionRejected): manager.assert_release_ready()

    def test_write_or_fsync_error_blocks_even_on_large_volume(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch('shutil.disk_usage',return_value=SimpleNamespace(total=100*1024**3,free=50*1024**3,used=50*1024**3)),patch('os.fsync',side_effect=OSError('I/O error')):
                result=persistence_readiness(Path(tmp)/'state.db')
                self.assertFalse(result['ok'])
                self.assertIn('PERSISTENCE_UNAVAILABLE:OSError',result['blockers'])

    def test_inode_exhaustion_blocks_even_with_bytes_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch('shutil.disk_usage',return_value=SimpleNamespace(total=100*1024**3,free=50*1024**3,used=50*1024**3)),patch('os.statvfs',return_value=SimpleNamespace(f_files=100000,f_favail=100),create=True):
                result=persistence_readiness(Path(tmp)/'state.db')
                self.assertFalse(result['ok'])
                self.assertIn('PERSISTENCE_INODES_CRITICAL',result['blockers'])


class ExchangeGeometry(unittest.TestCase):
    def children(self):
        p=json.loads(json.dumps(plan().__dict__,default=str))
        tp={'status':'open','amount':10,'filled':0,'info':{'type':'LIMIT_MAKER',
            'orderListId':7,'price':p['tp_price']}}
        sl={'status':'open','amount':10,'filled':0,'info':{'type':'STOP_LOSS_LIMIT',
            'orderListId':7,'price':p['stop_limit'],'stopPrice':p['stop_trigger'],'timeInForce':'GTC'}}
        return tp,sl,p

    def test_two_makers_do_not_prove_protection(self):
        tp,sl,p=self.children(); sl['info']={'type':'LIMIT_MAKER','orderListId':7,'price':'999'}
        with self.assertRaises(RecoveryBlocked): live_protection(tp,sl,10)

    def test_fixed_geometry_matches_approved_plan(self):
        tp,sl,p=self.children()
        self.assertTrue(live_protection(tp,sl,10,plan=p,order_list_id='7'))
        for change in [{'price':'999'},{'stopPrice':'0'},{'orderListId':8},{'timeInForce':'IOC'}]:
            tp,sl,p=self.children(); sl['info'].update(change)
            with self.subTest(change=change),self.assertRaises(RecoveryBlocked):
                live_protection(tp,sl,10,plan=p,order_list_id='7')


class FillPagination(unittest.TestCase):
    def manager(self, fetch):
        manager=object.__new__(BinanaExecutionManager)
        manager.exchange=SimpleNamespace(_api=SimpleNamespace(fetch_my_trades=fetch))
        manager.scope_id='testnet|account|epoch'; manager.legacy_scope_ids=set()
        manager.store=SimpleNamespace(record_fill=lambda **k:None)
        return manager

    def test_order_with_more_than_one_thousand_fills_is_complete(self):
        rows=[{'id':str(i),'order':'7','symbol':'ADA/USDT','side':'sell','amount':'1','cost':'2',
               'fee':{'cost':'0','currency':'USDT'},'timestamp':1000+i} for i in range(1201)]
        calls=[]
        def fetch(pair,params):
            calls.append(params)
            return [r for r in rows if int(r['id'])>=params.get('fromId',0)][:params.get('limit',1000)]
        manager=self.manager(fetch)
        self.assertEqual(manager._record_order_trades(intent_id='i',pair='ADA/USDT',order_id='7',side='SELL'),Decimal(1201))
        self.assertEqual(len(calls),2)

    def test_stalled_full_page_blocks_reconciliation(self):
        rows=[{'id':str(i),'order':'7','symbol':'ADA/USDT','side':'sell','amount':'1','cost':'2',
               'fee':{'cost':'0','currency':'USDT'},'timestamp':1000+i} for i in range(1000)]
        manager=self.manager(lambda *a,**k:rows)
        with self.assertRaises(AdmissionRejected):
            manager._record_order_trades(intent_id='i',pair='ADA/USDT',order_id='7',side='SELL')


class AccountAndMarketAdmission(unittest.TestCase):
    def api(self):
        api=FakeApi()
        api.privateGetMyFilters=lambda p:{'symbolFilters':[], 'exchangeFilters':[], 'assetFilters':[]}
        api.publicGetTickerPrice=lambda p:{'symbol':'ADAUSDT','price':'1'}
        api.publicGetTickerBookTicker=lambda p:{'symbol':'ADAUSDT','bidPrice':'1','askPrice':'1.001'}
        return api

    def test_account_asset_limit_blocks_before_submission(self):
        api=self.api()
        api.privateGetMyFilters=lambda p:{'symbolFilters':[],'exchangeFilters':[],
            'assetFilters':[{'filterType':'MAX_ASSET','asset':'ADA','limit':'9'}]}
        with self.assertRaises(OrderListError): BinanceOrderLists(api).preflight(plan())

    def test_exchange_capacity_counts_other_symbols(self):
        api=self.api()
        api.privateGetMyFilters=lambda p:{'symbolFilters':[],'assetFilters':[],
            'exchangeFilters':[{'filterType':'EXCHANGE_MAX_NUM_ORDERS','maxNumOrders':2}]}
        api.privateGetOpenOrders=lambda p:[{'symbol':'DOTUSDT','type':'LIMIT','orderId':9}]
        with self.assertRaises(OrderListError): BinanceOrderLists(api).preflight(plan())

    def test_account_symbol_limit_is_enforced(self):
        api=self.api()
        api.privateGetMyFilters=lambda p:{'symbolFilters':[{'filterType':'MAX_NUM_ORDERS','maxNumOrders':1}],
            'exchangeFilters':[], 'assetFilters':[]}
        with self.assertRaises(OrderListError): BinanceOrderLists(api).preflight(plan())

    def test_missing_account_filter_evidence_blocks(self):
        api=self.api();api.privateGetMyFilters=lambda p:{}
        with self.assertRaises(OrderListError): BinanceOrderLists(api).preflight(plan())

    def test_crossed_take_profit_or_trigger_blocks(self):
        for last in ('2','0.5'):
            api=self.api();api.publicGetTickerPrice=lambda p:{'symbol':'ADAUSDT','price':last}
            with self.subTest(last=last),self.assertRaises(OrderListError):
                BinanceOrderLists(api).preflight(plan())

    def test_maker_cross_blocks(self):
        api=self.api();api.publicGetTickerBookTicker=lambda p:{'symbol':'ADAUSDT','bidPrice':'2','askPrice':'2.01'}
        with self.assertRaises(OrderListError): BinanceOrderLists(api).preflight(plan())

    def test_read_failure_blocks_and_valid_snapshot_passes(self):
        api=self.api();BinanceOrderLists(api).preflight(plan())
        api.publicGetTickerPrice=lambda p:None
        with self.assertRaises(OrderListError): BinanceOrderLists(api).preflight(plan())
