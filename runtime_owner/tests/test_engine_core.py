from __future__ import annotations
from registry_fixture import registry_document
import json
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from freqtrade.binana.allocation import AllocationError, AllocationLedger
from freqtrade.binana.binance_order_lists import BinanceOrderLists, OrderListUnknown
from freqtrade.binana.environment import BinanaConfigError, validate_runtime_contract
from freqtrade.binana.protection_plan import provisional_plan, trailing_plan, ProtectionPlanError
from freqtrade.binana.registry import OwnerRegistry
from freqtrade.binana.state_store import StateStore


def config():
    return {
        'binana':{'enabled':True,'environment':'testnet','spot_only':True,'allow_live':False,
                  'allocation_usdt':1000,'single_halal_decision':True,'testnet_epoch_id':'test-epoch'},
        'dry_run':False,'trading_mode':'spot','margin_mode':None,'stake_currency':'USDT',
        'stake_amount':250,'max_open_trades':4,'timeframe':'5m','force_entry_enable':False,
        'position_adjustment_enable':False,'exchange':{'name':'binance'}
    }


class FakeApi:
    def __init__(self, fail=False): self.fail=fail; self.last=None
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
    def privatePostOrderListOtoco(self, params):
        self.last=params
        if self.fail: raise TimeoutError('unknown')
        return {'orderListId':7,'orders':[{'orderId':11,'clientOrderId':params['workingClientOrderId']},
                 {'orderId':12,'clientOrderId':params['pendingAboveClientOrderId']},
                 {'orderId':13,'clientOrderId':params['pendingBelowClientOrderId']}],
                'orderReports':[{'orderId':11,'clientOrderId':params['workingClientOrderId'],'status':'NEW','origQty':params['workingQuantity'],'executedQty':'0','price':params['workingPrice']} ]}


class CoreTests(unittest.TestCase):
    def test_runtime_contract_rejects_non_spot_and_live(self):
        validate_runtime_contract(config())
        c=config(); c['trading_mode']='futures'
        with self.assertRaises(BinanaConfigError): validate_runtime_contract(c)
        c=config(); c['binana']['allow_live']=True
        with self.assertRaises(BinanaConfigError): validate_runtime_contract(c)

    def test_registry_is_exactly_once_per_intent(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'halal.json'; p.write_text(json.dumps(registry_document(['ADAUSDT','DOTUSDT'])))
            r=OwnerRegistry(p)
            a=r.decide(intent_id='i1',pair='ADA/USDT')
            p.write_text(json.dumps(registry_document([])))
            b=r.decide(intent_id='i1',pair='ADA/USDT')
            self.assertTrue(a.allowed and b.allowed); self.assertEqual(r.lookup_count,1)

    def test_four_slot_ledger(self):
        l=AllocationLedger()
        for n in range(4): l.reserve(str(n))
        with self.assertRaises(AllocationError): l.reserve('5')
        l.release('0'); l.reserve('5')

    def test_state_store_persists_admission_and_generation(self):
        with tempfile.TemporaryDirectory() as td:
            s=StateStore(Path(td)/'s.sqlite')
            s.put_intent(intent_id='i',pair='ADA/USDT',signal_id='x',halal_allowed=True,
                         registry_sha256='abc',nominal_usdt='250',admission={'state':'GOOD'})
            self.assertEqual(s.get_intent('i')['state'],'RESERVED')
            s.put_generation(intent_id='i',generation=1,pair='ADA/USDT',mode='FIXED_OCO',status='PENDING',
                             ids={'list_client_id':'L1'},expected_qty='10',payload={'x':1})
            self.assertEqual(s.latest_generation('i')['list_client_id'],'L1')

    def test_fixed_plan_and_promotion_cannot_loosen_boundary(self):
        p=provisional_plan(intent_id='i',pair='ADA/USDT',quantity='100',entry_limit='1',tick_size='0.0001',
                           target_fraction='0.015',stop_fraction='0.008',stop_limit_buffer_fraction='0.001')
        self.assertEqual(p.mode,'FIXED_OCO')
        with self.assertRaises(ProtectionPlanError):
            trailing_plan(p,market_price='0.995',trailing_delta_bips=60,min_bips=10,max_bips=2000)
        q=trailing_plan(p,market_price='1.01',trailing_delta_bips=60,min_bips=10,max_bips=2000)
        self.assertEqual(q.mode,'TRAILING_OCO')

    def test_otoco_transport_uses_fixed_children_and_unknown_is_not_retry(self):
        p=provisional_plan(intent_id='i',pair='ADA/USDT',quantity='100',entry_limit='1',tick_size='0.0001',
                           target_fraction='0.015',stop_fraction='0.008',stop_limit_buffer_fraction='0.001')
        api=FakeApi(); response,ids=BinanceOrderLists(api).submit_fixed_otoco(p,pending_quantity=Decimal('99.8'))
        self.assertEqual(api.last['workingType'],'LIMIT'); self.assertEqual(api.last['workingSide'],'BUY'); self.assertEqual(api.last['workingTimeInForce'],'FOK')
        self.assertEqual(api.last['pendingAboveType'],'LIMIT_MAKER'); self.assertEqual(api.last['pendingBelowType'],'STOP_LOSS_LIMIT')
        self.assertEqual(api.last['pendingQuantity'],'99.8'); self.assertEqual(ids.working_order_id,'11')
        with self.assertRaises(OrderListUnknown): BinanceOrderLists(FakeApi(True)).submit_fixed_otoco(p)

if __name__=='__main__': unittest.main()
