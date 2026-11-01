"""Exercise recovered owner universe policy without network or exchange orders."""
import importlib.util
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from tests.test_universe_selection_hardening import (
    FakeAge, FakeApi, FakeExternal, FakeSharia, exchange_entry,
)
from services.universe_service.snapshot_store import load_current, store

ROOT = Path(__file__).resolve().parents[1]
CAPTURE = ROOT / 'runtime_owner/deployed_services/overrides/universe/services/universe_service'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RecoveredUniverseBoundaries(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sharia = self.root / 'registry.json'
        self.sharia.write_text('{}', encoding='utf-8')
        self.scanner = load('audit_recovered_scanner', CAPTURE / 'scanner.py')
        self.ranker = load('audit_recovered_ranker', CAPTURE / 'ranker.py')

    def scan(self, entries, *, volumes=None, asks=None, age=30, limit=0):
        identities = sorted({entry['symbol'] for entry in entries})
        tickers = [{'symbol': symbol, 'priceChangePercent': '-1',
                    'quoteVolume': str((volumes or {}).get(symbol, 1_000_000))}
                   for symbol in identities]
        books = [{'symbol': symbol, 'bidPrice': '99.9',
                  'askPrice': str((asks or {}).get(symbol, 100))} for symbol in identities]
        api = FakeApi({'symbols': entries}, tickers, books)
        ages = FakeAge()
        ages.age_days = lambda symbol, now: age
        with patch.multiple(self.scanner, ROOT=self.root, SHARIA=self.sharia,
                            LEGACY_HALAL=self.root / 'halal.json', LIMIT=limit,
                            REFRESH=900, MIN_AGE=30, MIN_VOL=Decimal('1000000'),
                            MAX_SPREAD=Decimal('0.005'), TIMEOUT=15.0), \
                patch.object(self.scanner, 'BinancePublic', return_value=api), \
                patch.object(self.scanner, 'ShariaFilter', FakeSharia), \
                patch.object(self.scanner, 'ListingAgeCache', return_value=ages), \
                patch.object(self.scanner.ExternalSignals, 'from_env', return_value=FakeExternal()), \
                patch.object(self.scanner, 'rank', self.ranker.rank), \
                patch.object(self.scanner, 'audit'):
            snapshot = self.scanner.scan_once()
        return snapshot, json.loads((self.root / 'latest_rejections.json').read_text())['rejected']

    def test_all_eligible_has_no_120_pair_ceiling_or_padding(self):
        for count in (0, 1, 117, 120, 121, 376):
            with self.subTest(count=count):
                entries = [exchange_entry(f'C{i:04}') for i in range(count)]
                snapshot, rejected = self.scan(list(reversed(entries)))
                self.assertEqual(snapshot['pairs'], [f'C{i:04}/USDT' for i in range(count)])
                self.assertEqual(len(set(snapshot['pairs'])), count)
                self.assertEqual(rejected, [])
                status = json.loads((self.root / 'status.json').read_text())
                self.assertEqual(status['ready'], count > 0)
                loaded = load_current(self.root / 'current_pairlist.json', require_nonempty=False)
                self.assertEqual(loaded['pairs'], snapshot['pairs'])

    def test_fixed_top_n_limit_remains_explicit(self):
        snapshot, _ = self.scan([exchange_entry(f'C{i:04}') for i in range(121)], limit=120)
        self.assertEqual(len(snapshot['pairs']), 120)

    def test_age_volume_and_spread_thresholds_remain_enforced(self):
        entries = [exchange_entry(base) for base in ('ADA', 'LOW', 'WIDE')]
        snapshot, rejected = self.scan(entries, volumes={'LOWUSDT': '999999.99'}, asks={'WIDEUSDT': '101'})
        self.assertEqual(snapshot['pairs'], ['ADA/USDT'])
        reasons = {row['base']: row['reasons'] for row in rejected}
        self.assertIn('low_volume', reasons['LOW'])
        self.assertIn('wide_spread', reasons['WIDE'])
        snapshot, rejected = self.scan([exchange_entry('ADA')], age=29)
        self.assertEqual(snapshot['pairs'], [])
        self.assertIn('listing_too_new', rejected[0]['reasons'])

    def test_duplicate_and_excluded_bases_cannot_fill_watchlist_slots(self):
        entries = [exchange_entry('ADA'), exchange_entry('ADA'), exchange_entry('LTC')]
        entries += [exchange_entry(base) for base in ('BTC', 'ETH', 'BNB', 'SOL', 'XRP')]
        snapshot, rejected = self.scan(entries)
        self.assertEqual(snapshot['pairs'], ['LTC/USDT'])
        reasons = {row['base']: row['reasons'] for row in rejected}
        self.assertIn('duplicate_symbol', reasons['ADA'])
        for base in ('BTC', 'ETH', 'BNB', 'SOL', 'XRP'):
            self.assertIn('excluded_base', reasons[base])

    def test_limit_mode_does_not_allow_padding_invalid_counts_or_unknown_policy(self):
        for config in ({'limit': 51}, {'limit': True, 'limit_mode': 'all_eligible'},
                       {'limit': -1, 'limit_mode': 'all_eligible'},
                       {'limit': 1, 'limit_mode': 'all_eligible'},
                       {'limit': 201, 'limit_mode': 'top_n'},
                       {'limit': 0, 'limit_mode': 'top_n'},
                       {'limit': 1, 'limit_mode': 'unbounded'}):
            with self.subTest(config=config), self.assertRaises(ValueError):
                store(self.root / 'invalid', [], config, 900)


if __name__ == '__main__':
    unittest.main()
