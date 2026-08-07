from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import tests._harness  # noqa: E402,F401  (install deterministic test keys)
from services.universe_service import scanner  # noqa: E402
from services.universe_service.ranker import rank  # noqa: E402
from services.universe_service.snapshot_store import store  # noqa: E402


def valid_filters():
    return [
        {'filterType': 'NOTIONAL', 'minNotional': '5'},
        {'filterType': 'PRICE_FILTER', 'tickSize': '0.0001'},
        {'filterType': 'LOT_SIZE', 'stepSize': '0.01'},
        {'filterType': 'TRAILING_DELTA',
         'minTrailingAboveDelta': 10, 'maxTrailingAboveDelta': 2000,
         'minTrailingBelowDelta': 10, 'maxTrailingBelowDelta': 2000},
    ]


def exchange_entry(base, *, symbol=None, quote='USDT', filters=None):
    return {
        'symbol': symbol or base + 'USDT',
        'baseAsset': base,
        'quoteAsset': quote,
        'status': 'TRADING',
        'isSpotTradingAllowed': True,
        'ocoAllowed': True,
        'otoAllowed': True,
        'allowTrailingStop': True,
        'filters': copy.deepcopy(valid_filters() if filters is None else filters),
    }


class FakeApi:
    def __init__(self, exchange, tickers, books):
        self.responses = {
            '/api/v3/exchangeInfo': exchange,
            '/api/v3/ticker/24hr': tickers,
            '/api/v3/ticker/bookTicker': books,
        }

    def get(self, path, params=None):
        return copy.deepcopy(self.responses[path])


class FakeSharia:
    def __init__(self, _path):
        pass

    @staticmethod
    def sync_legacy_compat(_path):
        return []

    @staticmethod
    def decision(_base):
        return SimpleNamespace(
            allowed=True, status='GREEN', record={'source': 'offline-test'})


class FakeAge:
    @staticmethod
    def age_days(_symbol, _now_ms):
        return 365


class FakeExternal:
    @staticmethod
    def refresh_if_stale():
        return None

    @staticmethod
    def reject_reason(_base):
        return None

    @staticmethod
    def enrich(_base):
        return {}

    @staticmethod
    def config_summary():
        return {'role': 'offline-test'}

    @staticmethod
    def write_status():
        return None


class UniverseSelectionHardeningTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base_dir = Path(self.temporary.name)
        self.case_number = 0

    def _case_root(self):
        self.case_number += 1
        return self.base_dir / f'case-{self.case_number}'

    @staticmethod
    def _default_market_rows(entries):
        tickers, books, seen = [], [], set()
        for entry in entries:
            symbol = entry.get('symbol')
            if not isinstance(symbol, str) or symbol in seen:
                continue
            seen.add(symbol)
            tickers.append({'symbol': symbol, 'priceChangePercent': '1',
                            'quoteVolume': '2000000'})
            books.append({'symbol': symbol, 'bidPrice': '99', 'askPrice': '100'})
        return tickers, books

    def _scan(self, entries, *, exchange=None, tickers=None, books=None,
              limit=50, root=None):
        root = root or self._case_root()
        root.mkdir(parents=True, exist_ok=True)
        sharia = root / 'sharia_status.json'
        sharia.write_bytes(b'{}\n')
        default_tickers, default_books = self._default_market_rows(entries)
        api = FakeApi(
            {'symbols': entries} if exchange is None else exchange,
            default_tickers if tickers is None else tickers,
            default_books if books is None else books,
        )
        with mock.patch.multiple(
                scanner, ROOT=root, SHARIA=sharia,
                LEGACY_HALAL=root / 'halal_coins.json', LIMIT=limit,
                REFRESH=900, MIN_AGE=30, MIN_VOL=Decimal('1000000'),
                MAX_SPREAD=Decimal('0.02'), TIMEOUT=15.0), \
                mock.patch.object(scanner, 'BinancePublic', return_value=api), \
                mock.patch.object(scanner, 'ShariaFilter', FakeSharia), \
                mock.patch.object(scanner, 'ListingAgeCache', return_value=FakeAge()), \
                mock.patch.object(scanner.ExternalSignals, 'from_env',
                                  return_value=FakeExternal()), \
                mock.patch.object(scanner, 'audit'):
            snapshot = scanner.scan_once()
        return snapshot, root

    def _rejections(self, root):
        return json.loads((root / 'latest_rejections.json').read_text(
            encoding='utf-8'))['rejected']

    def test_reversed_exact_ties_have_the_same_ascending_identity_order(self):
        rows = [
            {'symbol': 'BBBUSDT', 'pair': 'BBB/USDT', 'change_pct': 1,
             'quote_volume': 2, 'spread_ratio': 0.01},
            {'symbol': 'AAAUSDT', 'pair': 'AAA/USDT', 'change_pct': 1,
             'quote_volume': 2, 'spread_ratio': 0.01},
        ]
        first = rank(copy.deepcopy(rows), 2)
        second = rank(list(reversed(copy.deepcopy(rows))), 2)
        expected = ['AAA/USDT', 'BBB/USDT']
        self.assertEqual([row['pair'] for row in first], expected)
        self.assertEqual([row['pair'] for row in second], expected)
        with self.assertRaisesRegex(ValueError, 'non-finite'):
            rank([dict(rows[0], change_pct=float('nan'))], 1)

    def test_duplicate_symbol_and_base_are_rejected_and_audited_once(self):
        entries = [exchange_entry('ETH'), exchange_entry('ETH'), exchange_entry('ADA')]
        snapshot, root = self._scan(entries)
        self.assertEqual(snapshot['pairs'], ['ADA/USDT'])
        ethereum = [row for row in self._rejections(root) if row['base'] == 'ETH']
        self.assertEqual(len(ethereum), 1)
        self.assertIn('duplicate_symbol', ethereum[0]['reasons'])
        self.assertIn('duplicate_base', ethereum[0]['reasons'])

        entries = [exchange_entry('SOL'),
                   exchange_entry('SOL', symbol='WRONGUSDT'), exchange_entry('ADA')]
        snapshot, root = self._scan(entries)
        self.assertEqual(snapshot['pairs'], ['ADA/USDT', 'SOL/USDT'])
        solana = [row for row in self._rejections(root) if row['base'] == 'SOL']
        self.assertEqual(len(solana), 1)
        self.assertNotIn('duplicate_base', solana[0]['reasons'])
        self.assertIn('symbol_identity_mismatch', solana[0]['reasons'])

    def test_non_usdt_listing_does_not_collide_with_valid_usdt_candidate(self):
        entries = [exchange_entry('ETH'),
                   exchange_entry('ETH', symbol='ETHUSDC', quote='USDC')]
        snapshot, root = self._scan(entries)
        self.assertEqual(snapshot['pairs'], ['ETH/USDT'])
        rejections = self._rejections(root)
        self.assertEqual(len(rejections), 1)
        self.assertEqual(rejections[0]['symbol'], 'ETHUSDC')
        self.assertNotIn('duplicate_symbol', rejections[0]['reasons'])
        self.assertNotIn('duplicate_base', rejections[0]['reasons'])

    def test_zero_and_structurally_invalid_required_filters_are_rejected(self):
        zero = [
            {'filterType': 'NOTIONAL', 'minNotional': '0'},
            {'filterType': 'PRICE_FILTER', 'tickSize': '0'},
            {'filterType': 'LOT_SIZE', 'stepSize': '0'},
            {'filterType': 'TRAILING_DELTA',
             'minTrailingAboveDelta': 0, 'maxTrailingAboveDelta': 0,
             'minTrailingBelowDelta': 0, 'maxTrailingBelowDelta': 0},
        ]
        snapshot, root = self._scan([exchange_entry('ZERO', filters=zero)])
        self.assertEqual(snapshot['pairs'], [])
        reasons = set(self._rejections(root)[0]['reasons'])
        self.assertTrue({'invalid_notional_filter', 'invalid_tick_size',
                         'invalid_step_size', 'invalid_trailing_delta_filter'} <= reasons)

        malformed = valid_filters()
        malformed[-1] = {
            'filterType': 'TRAILING_DELTA',
            'minTrailingAboveDelta': 20, 'maxTrailingAboveDelta': 10,
            'minTrailingBelowDelta': 1, 'maxTrailingBelowDelta': 10,
        }
        snapshot, root = self._scan([exchange_entry('BAD', filters=malformed)])
        self.assertEqual(snapshot['pairs'], [])
        self.assertIn('invalid_trailing_delta_filter',
                      self._rejections(root)[0]['reasons'])

    def test_nan_and_infinity_reject_only_the_affected_candidates(self):
        entries = [exchange_entry('ETH'), exchange_entry('ADA'), exchange_entry('XRP')]
        tickers, books = self._default_market_rows(entries)
        by_symbol = {row['symbol']: row for row in tickers}
        by_symbol['ETHUSDT']['priceChangePercent'] = 'NaN'
        by_symbol['ADAUSDT']['quoteVolume'] = 'Infinity'
        book_by_symbol = {row['symbol']: row for row in books}
        book_by_symbol['XRPUSDT']['askPrice'] = '-Infinity'
        snapshot, root = self._scan(
            entries, tickers=list(by_symbol.values()), books=list(book_by_symbol.values()))
        self.assertEqual(snapshot['pairs'], [])
        reasons = {row['base']: set(row['reasons']) for row in self._rejections(root)}
        self.assertIn('invalid_price_change', reasons['ETH'])
        self.assertIn('invalid_quote_volume', reasons['ADA'])
        self.assertIn('invalid_book_prices', reasons['XRP'])

    def test_malformed_public_response_collections_fail_before_publication(self):
        entry = exchange_entry('ETH')
        malformed_cases = [
            {'exchange': []},
            {'exchange': {'symbols': {}}},
            {'exchange': {'symbols': [None]}},
            {'tickers': {}},
            {'tickers': [None]},
            {'books': {}},
            {'books': [None]},
            {'tickers': [
                {'symbol': 'ETHUSDT', 'priceChangePercent': '1', 'quoteVolume': '2'},
                {'symbol': 'ETHUSDT', 'priceChangePercent': '1', 'quoteVolume': '2'},
            ]},
        ]
        for index, kwargs in enumerate(malformed_cases):
            with self.subTest(index=index):
                root = self._case_root()
                with self.assertRaises(ValueError):
                    self._scan([entry], root=root, **kwargs)
                self.assertFalse((root / 'current_pairlist.json').exists())

    def test_exotic_base_symbol_is_skipped_not_fatal(self):
        """UNIVERSE-EXOTIC-001: Binance lists live Spot symbols whose base asset
        is not [A-Z0-9] -- the pair below is real and was returned by
        /api/v3/ticker/24hr. Rejecting it as a corrupt RESPONSE aborted the
        whole scan, so the universe container never reached health in CI. Such a
        symbol can never be traded anyway (_basic_filter rejects a non-[A-Z0-9]
        base), so it must be skipped, not fatal -- while a genuinely malformed
        row stays fatal."""
        exotic = '币安人生USDT'
        entry = exchange_entry('ETH')
        default_tickers, _ = self._default_market_rows([entry])
        snapshot, _root = self._scan(
            [entry],
            tickers=[{'symbol': exotic, 'priceChangePercent': '5',
                      'quoteVolume': '9'}] + list(default_tickers),
        )
        self.assertEqual(snapshot['pairs'], ['ETH/USDT'],
                         'an exotic listing must not abort or distort the scan')
        self.assertNotIn(exotic, snapshot['pairs'])

        # A structurally malformed row in the same position is still fatal.
        with self.assertRaises(ValueError):
            self._scan([entry], tickers=[None])

    def test_exact_symbol_base_usdt_identity_is_required(self):
        entries = [
            exchange_entry('ETH', symbol='WRONGUSDT'),
            exchange_entry('ada', symbol='ADAUSDT'),
            exchange_entry('XRP', quote='usdt'),
        ]
        snapshot, root = self._scan(entries)
        self.assertEqual(snapshot['pairs'], [])
        rejected = self._rejections(root)
        self.assertTrue(all('symbol_identity_mismatch' in row['reasons']
                            for row in rejected))

    def test_zero_is_fail_closed_and_shortfall_is_explicit_without_padding(self):
        empty, empty_root = self._scan([])
        self.assertEqual(empty['pairs'], [])
        self.assertEqual(empty['selection']['state'], 'fail_closed_empty')
        empty_status = json.loads((empty_root / 'status.json').read_text())
        self.assertFalse(empty_status['ok'])
        self.assertFalse(empty_status['ready'])
        self.assertEqual(empty_status['shortfall_count'], 50)

        fewer, fewer_root = self._scan(
            [exchange_entry('ETH'), exchange_entry('ADA')], limit=50)
        self.assertEqual(fewer['pairs'], ['ADA/USDT', 'ETH/USDT'])
        self.assertEqual(fewer['selection']['state'], 'degraded_shortfall')
        self.assertEqual(fewer['selection']['candidate_count'], 2)
        self.assertEqual(fewer['selection']['shortfall_count'], 48)
        fewer_status = json.loads((fewer_root / 'status.json').read_text())
        self.assertTrue(fewer_status['ok'])
        self.assertTrue(fewer_status['ready'])
        self.assertTrue(fewer_status['degraded'])

        full, _ = self._scan([exchange_entry('ETH'), exchange_entry('ADA')], limit=2)
        self.assertEqual(full['selection']['state'], 'ready')
        self.assertFalse(full['selection']['degraded'])

    def test_snapshot_store_rejects_duplicate_or_mismatched_public_rows(self):
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            store(self._case_root(), [
                {'symbol': 'ETHUSDT', 'base': 'ETH', 'pair': 'ETH/USDT', 'rank': 1},
                {'symbol': 'ETHUSDT', 'base': 'ETH', 'pair': 'ETH/USDT', 'rank': 2},
            ], {'limit': 50}, 900)
        with self.assertRaisesRegex(ValueError, 'mismatch'):
            store(self._case_root(), [
                {'symbol': 'SOLUSDT', 'base': 'ETH', 'pair': 'ETH/USDT', 'rank': 1},
            ], {'limit': 50}, 900)

    def test_nonfinite_runtime_decimals_raise_clean_configuration_errors(self):
        with mock.patch.object(scanner, 'MIN_VOL', Decimal('NaN')):
            with self.assertRaisesRegex(ValueError, 'MIN_QUOTE_VOLUME_USDT'):
                scanner.validate_runtime_settings()
        with mock.patch.object(scanner, 'MAX_SPREAD', Decimal('Infinity')):
            with self.assertRaisesRegex(ValueError, 'MAX_SPREAD_RATIO'):
                scanner.validate_runtime_settings()


if __name__ == '__main__':
    unittest.main()
