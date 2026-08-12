from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.sharia_screener.source_discovery import (  # noqa: E402
    CoinGeckoSourceClient,
    CoinMarketCapSourceClient,
    SourceDiscovery,
    _safe_https_url,
)
from services.universe_service.external_signals.breaker import CircuitBreaker  # noqa: E402


class FakeResponse:
    def __init__(self, payload, status=200, headers=None):
        self._payload = payload
        self.status_code = status
        self.headers = headers or {}

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.headers = {}

    def get(self, url, **kwargs):
        self.calls.append({'url': url, **kwargs})
        return self.responses.pop(0)


class FakeBudget:
    name = 'fake-budget'

    def __init__(self):
        self.used = 0

    def try_acquire(self, cost=1):
        self.used += cost
        return True

    def stats(self):
        return {'used': self.used}


class StubProvider:
    def __init__(self, result=None, reason=''):
        self.result = result
        self.reason = reason
        self.calls = 0

    def discover(self, _base):
        self.calls += 1
        return self.result, self.reason

    def state(self):
        return {'calls': self.calls}


def candidate(provider='coingecko'):
    return {
        'provider': provider,
        'provider_asset_id': 'ethereum' if provider == 'coingecko' else 1027,
        'name': 'Ethereum',
        'symbol': 'ETH',
        'identity_basis': 'test identity binding',
        'official_website': 'https://ethereum.org/',
        'whitepaper': 'https://ethereum.org/whitepaper/',
    }


def binance_entry():
    return {
        'symbol': 'ETHUSDT', 'status': 'TRADING', 'baseAsset': 'ETH',
        'quoteAsset': 'USDT', 'isSpotTradingAllowed': True,
    }


class SourceUrlSafetyTests(unittest.TestCase):
    def test_only_public_name_based_https_candidates_survive(self):
        self.assertEqual(_safe_https_url('https://Example.org/docs#part'),
                         'https://example.org/docs')
        for blocked in (
                'http://example.org', 'https://user:pass@example.org',
                'https://127.0.0.1/a', 'https://localhost/a',
                'https://metadata.internal/a', 'not a url'):
            self.assertEqual(_safe_https_url(blocked), '')


class CoinGeckoDiscoveryTests(unittest.TestCase):
    def _client(self, responses):
        return CoinGeckoSourceClient(
            api_key='demo', budget=FakeBudget(), breaker=CircuitBreaker('cg'),
            timeout=5, session=FakeSession(responses))

    def test_exact_binance_market_binds_stable_id_and_sources(self):
        rows = [
            {'id': 'ethereum', 'symbol': 'eth', 'name': 'Ethereum'},
            {'id': 'other-eth', 'symbol': 'eth', 'name': 'Other'},
        ]
        tickers = {'tickers': [
            {'base': 'ETH', 'target': 'USDT', 'coin_id': 'ethereum',
             'market': {'identifier': 'binance'}},
            {'base': 'ETH', 'target': 'USD', 'coin_id': 'other-eth',
             'market': {'identifier': 'other'}},
        ]}
        eth = {
            'id': 'ethereum', 'symbol': 'eth', 'name': 'Ethereum',
            'links': {'homepage': ['https://ethereum.org'],
                      'whitepaper': 'https://ethereum.org/whitepaper/'},
        }
        client = self._client(
            [FakeResponse(rows), FakeResponse(tickers), FakeResponse(eth)])
        result, reason = client.discover('ETH')
        self.assertEqual(reason, '')
        self.assertEqual(result['provider_asset_id'], 'ethereum')
        self.assertEqual(result['official_website'], 'https://ethereum.org/')
        self.assertEqual(len(client.session.calls), 3)
        ticker_call = client.session.calls[1]
        self.assertTrue(ticker_call['url'].endswith('/exchanges/binance/tickers'))
        self.assertEqual(ticker_call['params']['order'], 'base_target')
        self.assertEqual(ticker_call['params']['coin_ids'], 'ethereum,other-eth')

    def test_two_binance_matches_fail_closed_as_ambiguous(self):
        rows = [{'id': 'a', 'symbol': 'abc'}, {'id': 'b', 'symbol': 'abc'}]
        tickers = {'tickers': [
            {'base': 'ABC', 'target': 'USDT', 'coin_id': 'a',
             'market': {'identifier': 'binance'}},
            {'base': 'ABC', 'target': 'USDT', 'coin_id': 'b',
             'market': {'identifier': 'binance'}},
        ]}
        client = self._client([FakeResponse(rows), FakeResponse(tickers)])
        result, reason = client.discover('ABC')
        self.assertIsNone(result)
        self.assertIn('ambiguous', reason)

    def test_many_symbol_collisions_are_resolved_by_exact_exchange_coin_id(self):
        rows = [{'id': f'eth-{index}', 'symbol': 'eth'} for index in range(14)]
        tickers = {'tickers': [
            {'base': 'ETH', 'target': 'USDT', 'coin_id': 'eth-9',
             'market': {'identifier': 'binance'}},
            {'base': 'ETH', 'target': 'BTC', 'coin_id': 'eth-4',
             'market': {'identifier': 'binance'}},
        ]}
        details = {
            'id': 'eth-9', 'symbol': 'eth', 'name': 'Ethereum',
            'links': {'homepage': ['https://ethereum.org']},
        }
        client = self._client(
            [FakeResponse(rows), FakeResponse(tickers), FakeResponse(details)])
        result, reason = client.discover('ETH')
        self.assertEqual(reason, '')
        self.assertEqual(result['provider_asset_id'], 'eth-9')

    def test_non_usdt_or_wrong_exchange_ticker_never_binds(self):
        rows = [{'id': 'abc', 'symbol': 'abc'}]
        tickers = {'tickers': [
            {'base': 'ABC', 'target': 'BTC', 'coin_id': 'abc',
             'market': {'identifier': 'binance'}},
            {'base': 'ABC', 'target': 'USDT', 'coin_id': 'abc',
             'market': {'identifier': 'not-binance'}},
        ]}
        result, reason = self._client(
            [FakeResponse(rows), FakeResponse(tickers)]).discover('ABC')
        self.assertIsNone(result)
        self.assertIn('no exact Binance ABC/USDT', reason)

    def test_ticker_coin_id_must_come_from_exact_symbol_candidates(self):
        rows = [{'id': 'abc', 'symbol': 'abc'}]
        tickers = {'tickers': [
            {'base': 'ABC', 'target': 'USDT', 'coin_id': 'attacker-id',
             'market': {'identifier': 'binance'}},
        ]}
        result, reason = self._client(
            [FakeResponse(rows), FakeResponse(tickers)]).discover('ABC')
        self.assertIsNone(result)
        self.assertIn('no exact Binance ABC/USDT', reason)

    def test_exchange_ticker_pagination_is_stable_and_bounded(self):
        first_page = {'tickers': [
            {'base': f'A{index}', 'target': 'USDT', 'coin_id': 'abc',
             'market': {'identifier': 'binance'}} for index in range(100)
        ]}
        second_page = {'tickers': [
            {'base': 'ABC', 'target': 'USDT', 'coin_id': 'abc',
             'market': {'identifier': 'binance'}},
        ]}
        details = {
            'id': 'abc', 'symbol': 'abc', 'name': 'ABC',
            'links': {'homepage': ['https://abc.example']},
        }
        client = self._client([
            FakeResponse([{'id': 'abc', 'symbol': 'abc'}]),
            FakeResponse(first_page), FakeResponse(second_page),
            FakeResponse(details),
        ])
        result, reason = client.discover('ABC')
        self.assertEqual(reason, '')
        self.assertEqual(result['provider_asset_id'], 'abc')
        self.assertEqual(client.session.calls[1]['params']['page'], 1)
        self.assertEqual(client.session.calls[2]['params']['page'], 2)


class CoinMarketCapDiscoveryTests(unittest.TestCase):
    def _client(self, responses, key='free-key'):
        return CoinMarketCapSourceClient(
            api_key=key, budget=FakeBudget(), breaker=CircuitBreaker('cmc'),
            timeout=5, session=FakeSession(responses))

    def test_single_active_numeric_id_is_accepted_as_fallback_candidate(self):
        mapped = {'status': {'error_code': 0}, 'data': [
            {'id': 1027, 'name': 'Ethereum', 'symbol': 'ETH', 'is_active': 1},
        ]}
        info = {'status': {'error_code': 0}, 'data': {'1027': {
            'id': 1027, 'name': 'Ethereum', 'symbol': 'ETH',
            'urls': {'website': ['https://ethereum.org'],
                     'technical_doc': ['https://ethereum.org/whitepaper/']},
        }}}
        result, reason = self._client(
            [FakeResponse(mapped), FakeResponse(info)]).discover('ETH')
        self.assertEqual(reason, '')
        self.assertEqual(result['provider_asset_id'], 1027)

    def test_duplicate_active_ids_are_ambiguous(self):
        mapped = {'status': {'error_code': 0}, 'data': [
            {'id': 1, 'symbol': 'ABC', 'is_active': 1},
            {'id': 2, 'symbol': 'ABC', 'is_active': 1},
        ]}
        result, reason = self._client([FakeResponse(mapped)]).discover('ABC')
        self.assertIsNone(result)
        self.assertIn('not unique', reason)


class SourceDiscoveryPersistenceTests(unittest.TestCase):
    def _discovery(self, root, cg, cmc):
        return SourceDiscovery(
            current_dir=root / 'current', archive_dir=root / 'archive',
            runtime_dir=root / 'runtime', coingecko=cg, coinmarketcap=cmc)

    def test_coingecko_first_cache_weekly_archive_and_no_trade_permission(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {
                'SHARIA_SOURCE_REFRESH_DAYS': '7',
                'SHARIA_ARCHIVE_RETENTION_DAYS': '90'}, clear=False):
            root = Path(td)
            cg = StubProvider(candidate())
            cmc = StubProvider(candidate('coinmarketcap'))
            discovery = self._discovery(root, cg, cmc)
            first = discovery.ensure('ETH', 'ETH/USDT', binance_entry())
            second = discovery.ensure('ETH', 'ETH/USDT', binance_entry())

            self.assertEqual(first['status'], 'VERIFIED_CANDIDATE')
            self.assertFalse(first['trade_permission'])
            self.assertFalse(first['owner_verified'])
            self.assertFalse(first['cache_hit'])
            self.assertTrue(second['cache_hit'])
            self.assertEqual(cg.calls, 1)
            self.assertEqual(cmc.calls, 0)
            self.assertEqual(len(list((root / 'archive').rglob('*.json'))), 1)
            persisted = json.loads((root / 'current/ETH.json').read_text())
            self.assertEqual(persisted['provider_identity']['provider'], 'coingecko')

    def test_cmc_fallback_and_ambiguous_status(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cmc = StubProvider(candidate('coinmarketcap'))
            discovery = self._discovery(
                root, StubProvider(None, 'CoinGecko unavailable'), cmc)
            record = discovery.ensure('ETH', 'ETH/USDT', binance_entry())
            self.assertEqual(record['provider_identity']['provider'], 'coinmarketcap')
            self.assertEqual(cmc.calls, 1)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            discovery = self._discovery(
                root, StubProvider(None, 'CoinGecko symbol is ambiguous (2 candidates)'),
                StubProvider(None, 'CoinMarketCap symbol is not unique (2 active ids)'))
            record = discovery.ensure('ETH', 'ETH/USDT', binance_entry())
            self.assertEqual(record['status'], 'AMBIGUOUS')
            self.assertFalse(record['trade_permission'])

    def test_tampered_current_record_is_not_used_as_cache(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cg = StubProvider(candidate())
            discovery = self._discovery(root, cg, StubProvider())
            discovery.ensure('ETH', 'ETH/USDT', binance_entry())
            current = root / 'current/ETH.json'
            payload = json.loads(current.read_text(encoding='utf-8'))
            payload['trade_permission'] = True
            current.write_text(json.dumps(payload), encoding='utf-8')

            refreshed = discovery.ensure('ETH', 'ETH/USDT', binance_entry())
            self.assertFalse(refreshed['cache_hit'])
            self.assertFalse(refreshed['trade_permission'])
            self.assertEqual(cg.calls, 2)

    def test_archive_records_older_than_configured_retention_are_removed(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {
                'SHARIA_ARCHIVE_RETENTION_DAYS': '90'}, clear=False):
            root = Path(td)
            old = root / 'archive/2026-01/ETH_old.json'
            old.parent.mkdir(parents=True)
            old.write_text('{}', encoding='utf-8')
            old_time = time.time() - 91 * 86_400
            os.utime(old, (old_time, old_time))
            discovery = self._discovery(
                root, StubProvider(candidate()), StubProvider())
            discovery.ensure('ETH', 'ETH/USDT', binance_entry())
            self.assertFalse(old.exists())

    def test_universe_index_exposes_missing_stale_and_delisted_without_permission(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            discovery = self._discovery(
                root, StubProvider(candidate()), StubProvider())
            discovery.ensure('ETH', 'ETH/USDT', binance_entry())
            index = discovery.write_universe_index({
                'ETH': binance_entry(),
                'ADA': {
                    'symbol': 'ADAUSDT', 'status': 'TRADING',
                    'baseAsset': 'ADA', 'quoteAsset': 'USDT',
                    'isSpotTradingAllowed': True,
                },
            })
            self.assertEqual(index['listed_base_count'], 2)
            self.assertEqual(index['missing_bases'], ['ADA'])
            self.assertFalse(index['fully_current'])
            self.assertFalse(index['trade_permission'])
            persisted = json.loads(
                (root / 'current/_binance_spot_usdt_index.json').read_text())
            self.assertEqual(persisted['record_sha256'], index['record_sha256'])
            self.assertEqual(discovery.stats()['current_records'], 1)


if __name__ == '__main__':
    unittest.main()
