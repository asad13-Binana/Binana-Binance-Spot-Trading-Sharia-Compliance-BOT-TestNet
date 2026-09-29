from decimal import Decimal
from types import SimpleNamespace
import unittest
from freqtrade.binana.execution_manager import BinanaExecutionManager, AdmissionRejected


class CommissionReserve(unittest.TestCase):
    def manager(self,standard='0',special='0',tax='0'):
        rates={'symbol':'ADAUSDT'}
        for key,value in [('standardCommission',standard),('specialCommission',special),('taxCommission',tax)]:
            rates[key]={'taker':value,'buyer':'0'}
        manager=BinanaExecutionManager.__new__(BinanaExecutionManager)
        manager.exchange=SimpleNamespace(_api=SimpleNamespace(privateGetAccountCommission=lambda p:rates))
        return manager,rates

    def test_zero_testnet_commission_does_not_manufacture_fee_residual(self):
        manager,_=self.manager()
        self.assertEqual(manager.entry_commission_reserve('ADA/USDT'),Decimal(0))

    def test_all_undiscounted_commission_components_are_reserved(self):
        manager,rates=self.manager('0.001','0.0002','0.0001')
        rates['standardCommission']['buyer']='0.00005'
        self.assertEqual(manager.entry_commission_reserve('ADA/USDT'),Decimal('0.00135'))

    def test_missing_invalid_or_wrong_symbol_rates_fail_closed(self):
        for change in ['missing','nan','negative','symbol']:
            manager,rates=self.manager()
            if change=='missing':del rates['taxCommission']
            if change=='nan':rates['taxCommission']['buyer']='NaN'
            if change=='negative':rates['standardCommission']['buyer']='-0.001'
            if change=='symbol':rates['symbol']='BTCUSDT'
            with self.subTest(change=change),self.assertRaises(AdmissionRejected):
                manager.entry_commission_reserve('ADA/USDT')
