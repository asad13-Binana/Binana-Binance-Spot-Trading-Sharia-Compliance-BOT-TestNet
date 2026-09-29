import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from freqtrade.binana.environment import validate_runtime_contract, BinanaConfigError, resolve_stake_amount
from freqtrade.binana.execution_manager import BinanaExecutionManager, AdmissionRejected


class StakePolicy(unittest.TestCase):
    def test_json_numbers_retain_decimal_precision(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'amount.json'
            path.write_text('{"usdt":250.000000000000000001}')
            config = {'stake_amount':250, 'binana':{'allocation_usdt':1000}}
            self.assertEqual(resolve_stake_amount(config, override_path=path), Decimal('250.000000000000000001'))

    def test_fractional_allocation_cannot_pass_exact_contract(self):
        config = {'binana': {'enabled': True, 'environment': 'testnet', 'spot_only': True,
                            'allow_live': False, 'single_halal_decision': True, 'allocation_usdt': 1000.9,
                            'testnet_epoch_id': 'test'}, 'trading_mode': 'spot', 'stake_currency': 'USDT',
                  'stake_amount': 250, 'max_open_trades': 4, 'timeframe': '5m',
                  'exchange': {'name': 'binance'}}
        with self.assertRaisesRegex(BinanaConfigError, 'allocation'):
            validate_runtime_contract(config)

    def test_malformed_override_blocks_instead_of_defaulting(self):
        manager = object.__new__(BinanaExecutionManager)
        manager.config = {'stake_amount': 250, 'binana': {'allocation_usdt': 1000}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'trade_size.json'
            for contents in ['null', '{broken', '{"usdt":"NaN"}', '{"usdt":-1}', '{"usdt":true}', '{}']:
                path.write_text(contents)
                with self.subTest(contents=contents), patch('freqtrade.binana.execution_manager.Path', return_value=path):
                    with self.assertRaises(AdmissionRejected):
                        manager._requested_nominal_usdt()


if __name__ == '__main__':
    unittest.main()
