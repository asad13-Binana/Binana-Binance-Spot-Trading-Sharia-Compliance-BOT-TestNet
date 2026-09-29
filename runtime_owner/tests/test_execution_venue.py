from types import SimpleNamespace
import unittest
from freqtrade.binana.binance_order_lists import BinanceOrderLists
from freqtrade.binana.execution_manager import BinanaExecutionManager, AdmissionRejected


class ExecutionVenue(unittest.TestCase):
    def test_testnet_metadata_is_used_when_history_client_is_production(self):
        testnet=SimpleNamespace(publicGetExchangeInfo=lambda p:{'symbols':[
            {'symbol':p['symbol'],'ocoAllowed':False,'filters':[{'tickSize':'0.01'}]}]})
        def forbidden(params):
            raise AssertionError('Production metadata used for Testnet execution')
        mixed=SimpleNamespace(binana_testnet_public=testnet,publicGetExchangeInfo=forbidden)
        info=BinanceOrderLists(mixed).symbol_info('ADA/USDT')
        self.assertFalse(info['ocoAllowed'])
        self.assertEqual(info['filters'][0]['tickSize'],'0.01')

    def test_missing_symbol_and_timeout_are_normal_admission_rejections(self):
        for behavior in [lambda p:{'symbols':[]},lambda p:(_ for _ in ()).throw(TimeoutError())]:
            manager=BinanaExecutionManager.__new__(BinanaExecutionManager)
            manager.exchange=SimpleNamespace(markets={'ADA/USDT':{'active':True,'spot':True}})
            manager.order_lists=BinanceOrderLists(SimpleNamespace(publicGetExchangeInfo=behavior))
            with self.assertRaisesRegex(AdmissionRejected,'TESTNET_METADATA_UNAVAILABLE'):
                manager._capability_check('ADA/USDT')
