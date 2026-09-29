import unittest
from freqtrade.binana.environment import assert_spot_pair, BinanaConfigError


class PairExclusions(unittest.TestCase):
    def test_excluded_assets_are_blocked_at_order_owner(self):
        for base in ['BTC','BNB','SOL','XRP','ETH','USDT','USDC','FDUSD','USDE','USD1','EURI']:
            with self.subTest(base=base), self.assertRaises(BinanaConfigError):
                assert_spot_pair(base+'/USDT',side='long',leverage=1)
        for base in ['ADA','PAXG']:
            assert_spot_pair(base+'/USDT',side='long',leverage=1)
