from __future__ import annotations
from registry_fixture import registry_document
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import sqlite3
import unittest

from freqtrade.binana.execution_manager import BinanaExecutionManager, AdmissionRejected
from freqtrade.binana.state_store import StateStore


class Api:
    def __init__(self):
        self.oco_calls=[]
        self.cancel_response={'orderReports':[]}
    def publicGetExchangeInfo(self, params):
        return {'symbols':[{'symbol':'ADAUSDT','status':'TRADING','ocoAllowed':True,'otoAllowed':True,
            'allowTrailingStop':True,'filters':[
                {'filterType':'PRICE_FILTER','minPrice':'0.0001','maxPrice':'1000','tickSize':'0.0001'},
                {'filterType':'LOT_SIZE','minQty':'0.01','maxQty':'1000000','stepSize':'0.01'},
                {'filterType':'NOTIONAL','minNotional':'0.1','maxNotional':'1000000000'},
                {'filterType':'PERCENT_PRICE_BY_SIDE','bidMultiplierDown':'0.2','bidMultiplierUp':'5',
                 'askMultiplierDown':'0.2','askMultiplierUp':'5'},
                {'filterType':'TRAILING_DELTA','minTrailingBelowDelta':10,'maxTrailingBelowDelta':2000},
                {'filterType':'MAX_NUM_ORDERS','maxNumOrders':100},
                {'filterType':'MAX_NUM_ORDER_LISTS','maxNumOrderLists':100},
                {'filterType':'MAX_NUM_ALGO_ORDERS','maxNumAlgoOrders':100},
            ]}]}
    def publicGetAvgPrice(self, params): return {'price':'1'}
    def privateGetOpenOrders(self, params): return []
    def privateGetOpenOrderList(self, params=None): return []
    def fetch_my_trades(self, pair, params=None): return []
    def privateDeleteOrderList(self, params): return self.cancel_response
    def privatePostOrderListOco(self, params):
        self.oco_calls.append(params)
        return {'orderListId':22,'orders':[{'orderId':23,'clientOrderId':params['aboveClientOrderId']},{'orderId':24,'clientOrderId':params['belowClientOrderId']}]}

class Exchange:
    def __init__(self):
        self._api=Api()
        self.markets={'ADA/USDT':{'active':True,'spot':True,'precision':{'price':'0.0001'},'info':{
            'ocoAllowed':True,'otoAllowed':True,'allowTrailingStop':True,
            'filters':[{'filterType':'PRICE_FILTER','tickSize':'0.0001'},{'filterType':'TRAILING_DELTA','minTrailingBelowDelta':10,'maxTrailingBelowDelta':2000}]}}}
    def amount_to_precision(self,pair,amount): return str(amount)
    def price_to_precision(self,pair,price): return str(price)
    def fetch_order(self,order_id,pair):
        oid=str(order_id)
        if oid in {'23','24'} and self._api.oco_calls:
            params=self._api.oco_calls[-1]
            client=params['aboveClientOrderId'] if oid=='23' else params['belowClientOrderId']
            qty=float(params['quantity'])
            return {'id':oid,'clientOrderId':client,'symbol':pair,'side':'sell','status':'open',
                    'amount':qty,'filled':0.0,'remaining':qty,
                    'price':float(params.get('abovePrice') or params.get('belowPrice') or 1),
                    'cost':0.0,'info':{'clientOrderId':client}}
        report=next((r for r in self._api.cancel_response.get('orderReports',[])
                     if str(r.get('orderId'))==oid),None)
        status=str((report or {}).get('status') or 'CANCELED').lower()
        if status in {'cancelled','partially_filled'}: status='canceled'
        filled=float((report or {}).get('executedQty') or 0)
        client=(report or {}).get('clientOrderId') or ('TP' if oid=='11' else 'SL')
        return {'id':oid,'clientOrderId':client,'symbol':pair,'side':'sell','status':status,
                'amount':9.98,'filled':filled,'remaining':max(0.0,9.98-filled),
                'price':1.02 if oid=='11' else 0.99,'average':1.02 if filled else None,
                'cost':filled*1.02,'info':report or {'clientOrderId':client}}

class GoodMarket:
    def refresh(self,pair,quote_budget=None):
        return SimpleNamespace(state='GOOD',flow_status='fresh',taker_buy_ratio_60s='0.70',cvd_quote_60s='1000',best_bid='1.01',to_dict=lambda:{'state':'GOOD'})

def cfg(td, registry):
    return {'binana':{'enabled':True,'environment':'testnet','spot_only':True,'allow_live':False,'single_halal_decision':True,
        'allocation_usdt':1000,'testnet_epoch_id':'epoch-A','halal_registry_path':str(registry),'state_db_path':str(Path(td)/'ext.sqlite'),
        'market_context_path':str(Path(td)/'context.json'),'liquidity':{},'flow_policy':{'promote_buy_ratio':0.58,'promotion_min_profit_fraction':0.004},
        'pending_base_fee_reserve_fraction':'0.002','defensive_stop_fraction':'0.006','normal_stop_fraction':'0.01',
        'fixed_target_fraction':'0.015','stop_limit_buffer_fraction':'0.0015','estimated_exit_fee_fraction':'0.001','trailing_delta_bips':75},
        'dry_run':False,'trading_mode':'spot','margin_mode':None,'stake_currency':'USDT','stake_amount':250,'max_open_trades':4,'timeframe':'5m',
        'force_entry_enable':False,'position_adjustment_enable':False,'exchange':{'name':'binance','key':'key-A'}}

class FinalContracts(unittest.TestCase):
    def test_scoped_order_ids_do_not_collide_across_symbols(self):
        with TemporaryDirectory() as td:
            s=StateStore(Path(td)/'s.sqlite')
            for iid,pair in [('a','AAA/USDT'),('b','BBB/USDT')]:
                s.put_intent(intent_id=iid,pair=pair,signal_id='x',halal_allowed=True,registry_sha256='h',nominal_usdt='250')
                s.bind_order_identity(scope_id='scope',intent_id=iid,generation=1,pair=pair,role='WORKING',client_id='c'+iid,order_id='77')
            with s._connect() as c:
                self.assertEqual(c.execute('select count(*) from order_identity where order_id=?',('77',)).fetchone()[0],2)

    def test_open_incident_blocks_new_exposure(self):
        with TemporaryDirectory() as td:
            r=Path(td)/'halal.json'; r.write_text(json.dumps(registry_document(['ADAUSDT'])))
            m=BinanaExecutionManager(cfg(td,r),Exchange())
            m.store.incident(incident_id='x',code='TEST',detail='open')
            with self.assertRaisesRegex(AdmissionRejected,'RECONCILIATION_BLOCKER_OPEN'):
                m.prepare_candidate(pair='ADA/USDT',enter_tag='61',candle_date='2026-09-10T00:00:00Z')
            self.assertEqual(m.registry.lookup_count,0)

    def test_allowed_halal_decision_is_durable_before_later_failure(self):
        with TemporaryDirectory() as td:
            r=Path(td)/'halal.json'; r.write_text(json.dumps(registry_document(['ADAUSDT'])))
            ex=Exchange()
            original_info=ex._api.publicGetExchangeInfo
            def unsupported(params):
                info=original_info(params);info['symbols'][0]['ocoAllowed']=False;return info
            ex._api.publicGetExchangeInfo=unsupported
            c=cfg(td,r); m=BinanaExecutionManager(c,ex)
            with self.assertRaisesRegex(AdmissionRejected,'OCO_UNSUPPORTED'):
                m.prepare_candidate(pair='ADA/USDT',enter_tag='61',candle_date='2026-09-10T00:00:00Z')
            self.assertEqual(m.registry.lookup_count,1)
            r.write_text(json.dumps(registry_document([])))
            m2=BinanaExecutionManager(c,ex)
            with self.assertRaises(AdmissionRejected):
                m2.prepare_candidate(pair='ADA/USDT',enter_tag='61',candle_date='2026-09-10T00:00:00Z')
            self.assertEqual(m2.registry.lookup_count,0)

    def _promotion_setup(self,td):
        r=Path(td)/'halal.json'; r.write_text(json.dumps(registry_document(['ADAUSDT'])))
        ex=Exchange(); m=BinanaExecutionManager(cfg(td,r),ex); m.market_data=GoodMarket()
        iid='intent'; m.store.put_intent(intent_id=iid,pair='ADA/USDT',signal_id='61',halal_allowed=True,registry_sha256='h',nominal_usdt='250',state='OPEN')
        ids={'order_list_id':'10','list_client_id':'L','tp_order_id':'11','tp_client_id':'TP','sl_order_id':'12','sl_client_id':'SL'}
        m.store.put_generation(intent_id=iid,generation=1,pair='ADA/USDT',mode='FIXED_OCO',status='FIXED_ACTIVE',ids=ids,expected_qty='9.98')
        trade=SimpleNamespace(id=1,pair='ADA/USDT',amount=10,open_rate=1)
        return m,ex,iid,trade,m.store.latest_generation(iid)

    def test_promotion_reconciles_old_child_execution_before_replacement(self):
        with TemporaryDirectory() as td:
            m,ex,iid,trade,gen=self._promotion_setup(td)
            ex._api.cancel_response={'orderReports':[{'clientOrderId':'TP','orderId':'11','status':'PARTIALLY_FILLED','executedQty':'1'}]}
            ex._api.fetch_my_trades=lambda pair,params=None:[{'id':'101','order':'11','symbol':pair,'side':'sell','amount':1,'cost':1.02,'price':1.02,'timestamp':1000,'fee':{'currency':'USDT','cost':0}}]
            self.assertIsNone(m.maybe_promote(trade,iid,gen))
            self.assertEqual(len(ex._api.oco_calls),1)
            self.assertEqual(ex._api.oco_calls[0]['quantity'],'8.98')
            self.assertFalse(any(x['code']=='PROMOTION_OLD_CHILD_EXECUTED' for x in m.store.unresolved()))

    def test_promotion_reuses_protected_quantity_not_gross_trade_amount(self):
        with TemporaryDirectory() as td:
            m,ex,iid,trade,gen=self._promotion_setup(td)
            self.assertIsNone(m.maybe_promote(trade,iid,gen))
            self.assertEqual(ex._api.oco_calls[0]['quantity'],'9.98')

    def test_fill_ledger_is_idempotent(self):
        with TemporaryDirectory() as td:
            s=StateStore(Path(td)/'s.sqlite'); s.put_intent(intent_id='i',pair='ADA/USDT',signal_id='x',halal_allowed=True,registry_sha256='h',nominal_usdt='250')
            kw=dict(fill_key='f',scope_id='s',intent_id='i',pair='ADA/USDT',order_id='1',side='BUY',base_qty='1',quote_qty='2',average_price='2')
            s.record_fill(**kw); s.record_fill(**kw)
            self.assertEqual(len(s.fill_rows('i')),1)

if __name__=='__main__': unittest.main()
