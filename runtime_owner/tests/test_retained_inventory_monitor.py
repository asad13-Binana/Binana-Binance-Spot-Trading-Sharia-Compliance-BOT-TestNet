from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock
import unittest

from freqtrade.binana.execution_manager import BinanaExecutionManager
from freqtrade.binana.state_store import StateStore


class RetainedInventoryMonitor(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory()
        self.manager=BinanaExecutionManager.__new__(BinanaExecutionManager)
        self.manager.scope_id='testnet|account|epoch'
        self.manager.store=StateStore(Path(self.tmp.name)/'extension.sqlite')
        self.public=SimpleNamespace(
            publicGetExchangeInfo=Mock(return_value={'symbols':[{'symbol':'LSKUSDT','status':'TRADING',
                'filters':[{'filterType':'LOT_SIZE','minQty':'.1','maxQty':'10000','stepSize':'.1'},
                           {'filterType':'MIN_NOTIONAL','minNotional':'5'}]}]}),
            publicGetTickerBookTicker=Mock(return_value=[{'symbol':'LSKUSDT','bidPrice':'1'}]))
        self.manager.order_lists=SimpleNamespace(public=self.public)

    def tearDown(self):
        self.tmp.cleanup()

    def trade(self, identifier, quantity='2.9', **changes):
        evidence={'phase':'retained','scope':self.manager.scope_id,'quantity':quantity,
            'cost_basis':'3','repair_version':'authenticated-retention-replay-v1','evidence_sha256':'a'*64}
        evidence.update(changes)
        return SimpleNamespace(id=identifier,pair='LSK/USDT',amount=float(quantity),stake_amount=3,
            binana_custom_data=lambda key:evidence,binana_economics=lambda:None)

    def test_combined_dust_is_rechecked_when_price_crosses_minimum(self):
        self.manager.store.incident(incident_id='unrelated',code='OTHER',detail='preserve')
        trades=[self.trade(1),self.trade(2)]
        first=self.manager.refresh_retained_inventory(trades,[])
        self.assertTrue(first['ok']);self.assertEqual(first['executable_pairs'],1)
        self.assertEqual(first['pairs'][0]['quantity'],'5.8')
        self.assertIn('retained-executable-LSKUSDT',{row['incident_id'] for row in self.manager.store.unresolved()})
        self.public.publicGetTickerBookTicker.return_value=[{'symbol':'LSKUSDT','bidPrice':'.8'}]
        second=self.manager.refresh_retained_inventory(trades,[])
        self.assertEqual(second['executable_pairs'],0)
        self.assertEqual({row['incident_id'] for row in self.manager.store.unresolved()},{'unrelated'})
        self.assertEqual(trades[0].amount,2.9)
        self.assertEqual(self.public.publicGetExchangeInfo.call_count,2)

    def test_missing_market_data_preserves_existing_aggregate_blocker(self):
        trades=[self.trade(1),self.trade(2)]
        self.manager.refresh_retained_inventory(trades,[])
        self.public.publicGetTickerBookTicker.side_effect=TimeoutError()
        result=self.manager.refresh_retained_inventory(trades,[])
        self.assertFalse(result['ok'])
        ids={row['incident_id'] for row in self.manager.store.unresolved()}
        self.assertIn('retained-executable-LSKUSDT',ids)
        self.assertIn('retained-inventory-verification',ids)

    def test_wrong_scope_unverified_history_and_open_obligation_block(self):
        for changes in ({'scope':'foreign'}, {'evidence_sha256':''}, {'cost_basis':'4'}):
            with self.subTest(changes=changes):
                self.assertFalse(self.manager.refresh_retained_inventory([self.trade(1,**changes)],[])['ok'])
        self.public.publicGetExchangeInfo.assert_not_called()
        self.assertFalse(self.manager.refresh_retained_inventory([self.trade(1)],[{'symbol':'LSKUSDT'}])['ok'])

    def test_no_retained_inventory_needs_no_market_requests(self):
        result=self.manager.refresh_retained_inventory([],[])
        self.assertTrue(result['ok']);self.assertEqual(result['executable_pairs'],0)
        self.public.publicGetExchangeInfo.assert_not_called()

    def test_verified_new_position_on_retained_pair_is_independent(self):
        self.manager.store.put_intent(intent_id='new',pair='LSK/USDT',signal_id='s',
            halal_allowed=True,registry_sha256='registry',nominal_usdt='250',state='OPEN')
        self.manager.store.set_trade_id('new',99)
        self.manager.store.bind_order_identity(scope_id=self.manager.scope_id,intent_id='new',
            generation=1,pair='LSK/USDT',role='STOP',client_id='new-stop',order_id='101')
        active=SimpleNamespace(id=99,is_open=True,binana_custom_data=lambda key:None)
        orders=[{'symbol':'LSKUSDT','orderId':101,'clientOrderId':'new-stop'}]
        result=self.manager.refresh_retained_inventory([self.trade(1),active],orders)
        self.assertTrue(result['ok']);self.assertEqual(result['executable_pairs'],0)
        self.assertFalse(self.manager.store.has_blockers())
        active.is_open=False
        self.assertFalse(self.manager.refresh_retained_inventory([self.trade(1),active],orders)['ok'])
