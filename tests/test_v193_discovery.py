"""Provider metadata remains a reading list, never verified Sharia evidence."""
import tempfile
import unittest
from pathlib import Path

from tests.test_sharia_source_discovery import StubProvider, candidate, binance_entry
from services.sharia_screener.source_discovery import SourceDiscovery


class IndependentTargets(unittest.TestCase):
    def discover(self, first, second):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.cg, self.cmc = StubProvider(first), StubProvider(second)
        service = SourceDiscovery(current_dir=root/'current', archive_dir=root/'archive',
                                  runtime_dir=root/'runtime', coingecko=self.cg,
                                  coinmarketcap=self.cmc)
        return service.ensure('ETH', 'ETH/USDT', binance_entry())

    def test_website_does_not_stop_documentation_fallback(self):
        cmc = {**candidate('coinmarketcap'), 'official_docs': ['https://ethereum.org/developers/']}
        result = self.discover(candidate(), cmc)
        self.assertEqual(self.cmc.calls, 1)
        docs = [x for x in result['source_candidates'] if x['role'] == 'official_docs']
        self.assertEqual(docs[0]['url'], 'https://ethereum.org/developers/')
        self.assertEqual(docs[0]['status'], 'CANDIDATE_UNVERIFIED')
        self.assertFalse(docs[0]['opened'])
        self.assertFalse(result['trade_permission'])
        self.assertEqual(len(result['targets']), 6)
        self.assertEqual(result['audit_log'][0]['status'], 'NOT_EXPOSED_BY_PROVIDER')

    def test_conflicting_domains_are_preserved_for_identity_review(self):
        result = self.discover(candidate(), {**candidate('coinmarketcap'),
                                            'official_website': 'https://other.example/'})
        self.assertTrue(result['source_identity_conflicts'])
        self.assertEqual(result['source_identity_conflicts'][0]['resolved'], False)
        self.assertFalse(result['owner_verified'])

    def test_docs_only_fallback_does_not_replace_primary_website(self):
        result = self.discover(candidate(), {**candidate('coinmarketcap'),
                    'official_website': '', 'whitepaper': '',
                    'official_docs': ['https://ethereum.org/developers/']})
        self.assertEqual(result['official_hosts_candidates'], ['ethereum.org'])
        self.assertEqual(result['provider_identity']['provider'], 'coingecko')


if __name__ == '__main__':
    unittest.main()
