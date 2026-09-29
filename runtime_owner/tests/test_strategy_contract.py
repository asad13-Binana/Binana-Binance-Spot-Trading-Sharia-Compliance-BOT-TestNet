from __future__ import annotations
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from freqtrade.enums import RunMode
from freqtrade.binana.environment import BinanaConfigError, validate_runtime_contract
import importlib.util

STRATEGY_PATH=(Path('/freqtrade/binana-strategies/BinanaNfiSpot.py') if Path('/freqtrade/binana-strategies/BinanaNfiSpot.py').exists() else Path(__file__).resolve().parents[1]/'binana-strategies/BinanaNfiSpot.py')
spec=importlib.util.spec_from_file_location('binana_runtime_strategy_test',STRATEGY_PATH)
module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
Strategy=module.BinanaNfiSpot

class StrategyContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.journal_patch=patch.object(module, '_SIGNAL_JOURNAL', Path(self.tmp.name)/'lifecycle.jsonl')
        self.journal_patch.start()
        self.c={
            'runmode':RunMode.LIVE,'user_data_dir':Path(self.tmp.name),
            'trading_mode':'spot','margin_mode':'','stake_currency':'USDT','stake_amount':250,
            'max_open_trades':4,'timeframe':'5m','force_entry_enable':False,
            'position_adjustment_enable':False,'dry_run':False,
            'exchange':{'name':'binance','key':'unit-test-key','secret':'unit-test-secret','pair_whitelist':['ADA/USDT']},
            'binana':{
                'enabled':True,'environment':'testnet','spot_only':True,'allow_live':False,
                'single_halal_decision':True,'allocation_usdt':1000,'testnet_epoch_id':'unit-test-epoch',
                'logical_emergency_stop_fraction':0.99,'logical_roi_fraction':0.99
            }
        }
    def tearDown(self):
        self.journal_patch.stop()
        self.tmp.cleanup()
    def build(self):
        Strategy.target_profit_cache=None
        return Strategy(copy.deepcopy(self.c))
    def test_post_constructor_is_spot_long_only(self):
        x=self.build()
        self.assertEqual(x.version(),'BINANA-X8-v18.0.26-testnet-owner-5')
        self.assertFalse(x.can_short); self.assertFalse(x.is_futures_mode)
        self.assertFalse(x.position_adjustment_enable); self.assertFalse(x.hold_support_enabled)
        self.assertFalse(x.use_custom_stoploss); self.assertFalse(x.trailing_stop); self.assertFalse(x.use_exit_signal)
        self.assertEqual(x.timeframe,'5m'); self.assertEqual(x.minimal_roi,{'0':0.99})
        self.assertEqual(x.order_time_in_force['entry'],'FOK')
    def test_runtime_contract_rejects_derivatives_and_wrong_allocation(self):
        for mutate in [
            lambda c:c.__setitem__('trading_mode','futures'),
            lambda c:c.__setitem__('margin_mode','cross'),
            lambda c:c.__setitem__('stake_amount',100),
            lambda c:c.__setitem__('max_open_trades',5),
            lambda c:c['binana'].__setitem__('single_halal_decision',False),
            lambda c:c['binana'].__setitem__('allocation_usdt',1250),
            lambda c:c['binana'].__setitem__('testnet_epoch_id','')]:
            c=copy.deepcopy(self.c); mutate(c)
            with self.assertRaises(BinanaConfigError): validate_runtime_contract(c)
    def test_no_rebuy_and_fixed_stake(self):
        x=self.build(); self.assertIsNone(x.adjust_trade_position())
        self.assertEqual(x.custom_stake_amount('ADA/USDT',None,1,250,5,1000,1,'61','long'),250)
        self.assertEqual(x.custom_stake_amount('ADA/USDT',None,1,250,5,249,1,'61','long'),0)
        self.assertEqual(x.custom_stake_amount('ADA/USDT',None,1,250,5,1000,3,'61','long'),0)
    def test_force_short_derivative_and_rebuy_tags_rejected(self):
        x=self.build()
        cases=[('ADA/USDT','force_entry','long'),('ADA/USDT','61','short'),('ADA/USDT:USDT','61','long'),('ADA/USDT',None,'long'),('ADA/USDT',x.long_rebuy_mode_tags[0],'long'),('ADA/USDT',x.long_grind_mode_tags[0],'long')]
        for pair,tag,side in cases:
            with self.subTest(pair=pair,tag=tag,side=side):
                self.assertFalse(x.confirm_trade_entry(pair,'limit',1,1,'FOK',None,tag,side))
    def test_valid_long_signal_delegates_upstream(self):
        x=self.build()
        with patch.object(module._vendor.NostalgiaForInfinityX8,'confirm_trade_entry',return_value=True) as upstream:
            self.assertTrue(x.confirm_trade_entry('ADA/USDT','limit',1,1,'FOK',None,'1','long'))
            upstream.assert_called_once()
    def test_exchange_protection_owns_automatic_stops_and_operator_exit_remains_available(self):
        x=self.build()
        for reason in ['stop_loss','trailing_stop_loss','stoploss_on_exchange']:
            self.assertFalse(x.confirm_trade_exit('ADA/USDT',None,'market',1,1,'GTC',reason,None))
        for reason in ['force_exit','emergency_exit']:
            self.assertTrue(x.confirm_trade_exit('ADA/USDT',None,'market',1,1,'GTC',reason,None))
        self.assertFalse(x.confirm_trade_exit('ADA/USDT',None,'limit',1,1.1,'GTC','roi',None))
        self.assertFalse(x.confirm_trade_exit('ADA/USDT',None,'limit',1,1.1,'GTC','exit_signal',None))

if __name__=='__main__': unittest.main()
