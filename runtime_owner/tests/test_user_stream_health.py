import unittest
from freqtrade.binana.user_stream import ENDPOINT, health_is_fresh


class UserStreamHealth(unittest.TestCase):
    def test_connected_flag_cannot_keep_stale_stream_healthy(self):
        good={'owner':'FREQTRADE','endpoint':ENDPOINT,'ok':True,'subscribed':True,
              'last_verified_at':100, 'valid_until':135}
        self.assertTrue(health_is_fresh(good,now=110))
        self.assertFalse(health_is_fresh(good,now=140))
        self.assertFalse(health_is_fresh({**good,'subscribed':False},now=110))
        self.assertFalse(health_is_fresh({**good,'endpoint':'wss://ws-api.binance.com/ws-api/v3'},now=110))
        self.assertFalse(health_is_fresh({'connected':True,'ok':True},now=110))


if __name__=='__main__':
    unittest.main()
