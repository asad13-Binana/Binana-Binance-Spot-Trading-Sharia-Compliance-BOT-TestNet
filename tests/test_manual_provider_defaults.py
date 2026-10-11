"""Manual registry deployments do not require optional research providers."""
import unittest
from scripts.api_readiness import check_coingecko, check_coinmarketcap


class ManualProviderDefaults(unittest.TestCase):
    def test_manual_default_is_credential_free(self):
        for check in (check_coingecko,check_coinmarketcap):
            result=check({},lambda *a:self.fail('manual mode requested provider'))
            self.assertEqual(result.status,'SKIPPED')
            self.assertFalse(result.required)

    def test_explicit_discovery_still_requires_credentials(self):
        for check in (check_coingecko,check_coinmarketcap):
            result=check({'SHARIA_AUTO_SOURCE_DISCOVERY_ENABLED':'true'},lambda *a:None)
            self.assertEqual(result.status,'FAIL')
            self.assertTrue(result.required)
