from datetime import date
import unittest
from freqtrade.binana.registry import OwnerRegistry, RegistryError


class RegistrySchema(unittest.TestCase):
    def document(self):
        return {'schema_version':1,'version':'owner-v1','symbols':['ADAUSDT','ETHUSDT'],
                'last_reviewed':'2026-09-01','next_review':'2026-12-01'}

    def parse(self, document):
        return OwnerRegistry._extract_assets(document,today=date(2026,9,20))

    def test_exact_owner_schema_includes_eth_without_ticker_rewriting(self):
        self.assertEqual(self.parse(self.document()),{'ADA','ETH'})
        document=self.document();document['symbols']=['USDTFOOUSDT']
        self.assertEqual(self.parse(document),{'USDTFOO'})

    def test_ambiguous_boolean_object_legacy_and_expired_records_fail_closed(self):
        for change in [{'schema_version':True},{'symbols':['adausdt']},{'symbols':['ADA/USDT']},
                       {'symbols':[{'symbol':'ADAUSDT','approved':False}]},
                       {'symbols':['ADAUSDT','ADAUSDT']},{'next_review':'2026-09-20'},
                       {'last_reviewed':'2026-09-21'},{'version':''}]:
            document=self.document();document.update(change)
            with self.subTest(change=change),self.assertRaises(RegistryError):self.parse(document)
        with self.assertRaises(RegistryError):self.parse({'ADA':{'approved':False}})

    def test_empty_registry_is_valid_deny_all(self):
        self.assertEqual(self.parse({'schema_version':1,'version':'empty','symbols':[]}),set())
