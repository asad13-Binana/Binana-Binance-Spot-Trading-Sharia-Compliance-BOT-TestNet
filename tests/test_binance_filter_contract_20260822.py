from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import tests._harness as harness  # noqa: F401
from scripts.binance_contract_drift import (
    ContractDriftError,
    exchange_info_candidates,
    exchange_info_url,
    execution_rules_url,
    fetch_contract_for_mode,
    fetch_exchange_info,
    fetch_exchange_info_for_mode,
    inspect_exchange_info,
    inspect_execution_rules,
)
from services.common.binance_public import (
    BinancePublicClient,
    BinancePublicError,
)


def _filters(*extra: dict) -> list[dict]:
    return [
        {'filterType': 'PRICE_FILTER', 'tickSize': '0.01',
         'minPrice': '0.01', 'maxPrice': '100000'},
        {'filterType': 'LOT_SIZE', 'stepSize': '0.001',
         'minQty': '0.001', 'maxQty': '1000'},
        {'filterType': 'MARKET_LOT_SIZE', 'stepSize': '0',
         'minQty': '0', 'maxQty': '1000'},
        {'filterType': 'NOTIONAL', 'minNotional': '1',
         'maxNotional': '1000000', 'applyMinToMarket': False,
         'applyMaxToMarket': False},
        *extra,
    ]


class _Public:
    NO_REFERENCE_PRICE = object()

    def __init__(self, filters, *, execution_rule=False, reference='100'):
        self.filters = filters
        self.execution_rule = execution_rule
        self.reference = reference
        self.execution_rule_calls = 0

    def exchange_info(self, symbol):
        return {'symbols': [{
            'symbol': symbol, 'status': 'TRADING',
            'isSpotTradingAllowed': True,
            'baseAsset': 'ETH', 'quoteAsset': 'USDT',
            'filters': self.filters,
        }]}

    def reference_price(self, _symbol):
        return self.reference

    def execution_rules(self, symbol):
        self.execution_rule_calls += 1
        rules = [] if self.execution_rule is False else [self.execution_rule]
        return {'symbolRules': [{'symbol': symbol, 'rules': rules}]}

    def ticker_price(self, _symbol):
        return {'price': '100'}

    def get(self, _path, _params=None):
        return {'price': '100'}






def _order(*, side='SELL', order_type='LIMIT', quantity='1',
           price='100.00', trailing_delta=None) -> dict:
    out = {
        'symbol': 'ETHUSDT', 'side': side, 'type': order_type,
        'quantity': quantity, 'price': price, 'stopPrice': price,
    }
    if trailing_delta is not None:
        out['trailingDelta'] = trailing_delta
    return out


def _oco() -> dict:
    return {
        'symbol': 'ETHUSDT', 'side': 'SELL', 'quantity': '1',
        'aboveType': 'LIMIT_MAKER', 'abovePrice': '110.00',
        'belowType': 'STOP_LOSS_LIMIT', 'belowPrice': '89.00',
        'belowStopPrice': '90.00',
    }


def _myfilters(*, exchange=None, symbol=None, assets=None) -> dict:
    return {
        'exchangeFilters': list(exchange or []),
        'symbolFilters': list(symbol or []),
        'assetFilters': list(assets or []),
    }


def _validate(guard, endpoint, params, *, account=None,
              symbol_orders=None, exchange_orders=None, lists=None):
    return guard.validate(
        'ETHUSDT', endpoint, params,
        account_filters_provider=lambda _symbol: (
            account if account is not None else _myfilters()),
        open_orders_provider=lambda _symbol: list(symbol_orders or []),
        open_exchange_orders_provider=lambda: list(exchange_orders or []),
        open_order_lists_provider=lambda: list(lists or []),
    )








class PriceRangeExecutionRuleTests(unittest.TestCase):
    def test_public_client_requests_one_exact_usdt_symbol(self):
        client = BinancePublicClient.__new__(BinancePublicClient)
        calls = []
        client.get = lambda path, params=None: (
            calls.append((path, params)) or {'symbolRules': []})
        self.assertEqual(
            client.execution_rules('ethusdt'), {'symbolRules': []})
        self.assertEqual(
            calls,
            [('/api/v3/executionRules', {'symbol': 'ETHUSDT'})])
        with self.assertRaises(BinancePublicError):
            client.execution_rules('ETHBTC')







class ContractDriftTests(unittest.TestCase):
    @staticmethod
    def _payload(filters):
        return {'symbols': [{
            'symbol': 'ETHUSDT', 'status': 'TRADING',
            'isSpotTradingAllowed': True, 'quoteAsset': 'USDT',
            'filters': filters,
        }]}

    def test_current_known_filters_pass(self):
        result = inspect_exchange_info(self._payload([
            {'filterType': 'PRICE_FILTER'},
            {'filterType': 'MAX_NUM_ORDER_LISTS'},
        ]))
        self.assertFalse(result['max_position_active'])

    def test_max_position_on_tradeable_usdt_requires_review(self):
        with self.assertRaisesRegex(ContractDriftError, 'MAX_POSITION'):
            inspect_exchange_info(self._payload([
                {'filterType': 'MAX_POSITION', 'maxPosition': '10'},
            ]))

    def test_unknown_filter_requires_review(self):
        with self.assertRaisesRegex(ContractDriftError, 'NEW_ORDER_RULE'):
            inspect_exchange_info(self._payload([
                {'filterType': 'NEW_ORDER_RULE'},
            ]))

    def test_environment_endpoints_are_immutably_separated(self):
        self.assertEqual(
            exchange_info_url('testnet'),
            'https://testnet.binance.vision/api/v3/exchangeInfo'
            '?symbolStatus=TRADING&showPermissionSets=false')
        self.assertEqual(
            exchange_info_url('live'),
            'https://api.binance.com/api/v3/exchangeInfo'
            '?symbolStatus=TRADING&showPermissionSets=false')
        with self.assertRaises(ContractDriftError):
            exchange_info_url('simulation')
        self.assertEqual(
            execution_rules_url('testnet'),
            'https://testnet.binance.vision/api/v3/executionRules'
            '?symbolStatus=TRADING')

    def test_testnet_451_uses_official_public_data_with_degraded_scope(self):
        payload = self._payload([{'filterType': 'PRICE_FILTER'}])
        calls = []

        def fetcher(url):
            calls.append(url)
            if url.startswith('https://testnet.binance.vision'):
                raise OSError('HTTP Error 451')
            return payload

        result, source = fetch_exchange_info_for_mode('testnet', fetcher)
        self.assertIs(result, payload)
        self.assertEqual(
            calls,
            [url for url, _environment in exchange_info_candidates('testnet')],
        )
        self.assertTrue(source['endpoint_fallback'])
        self.assertFalse(source['exact_environment'])
        self.assertEqual(source['contract_environment'], 'live')
        self.assertIn('HTTP Error 451', source['unavailable_endpoints'][0])

    def test_live_official_fallback_keeps_exact_contract_scope(self):
        payload = self._payload([{'filterType': 'PRICE_FILTER'}])
        candidates = exchange_info_candidates('live')

        def fetcher(url):
            if url != candidates[-1][0]:
                raise OSError('HTTP Error 451')
            return payload

        result, source = fetch_exchange_info_for_mode('live', fetcher)
        self.assertIs(result, payload)
        self.assertTrue(source['endpoint_fallback'])
        self.assertTrue(source['exact_environment'])
        self.assertEqual(source['contract_environment'], 'live')

    def test_all_official_endpoints_unavailable_fails_closed(self):
        def unavailable(_url):
            raise OSError('HTTP Error 451')

        with self.assertRaisesRegex(
                ContractDriftError, 'all official Binance'):
            fetch_exchange_info_for_mode('live', unavailable)

    def test_direct_fetch_rejects_unapproved_url_before_network_access(self):
        with self.assertRaisesRegex(ContractDriftError, 'approved Binance'):
            fetch_exchange_info('https://example.invalid/api/v3/exchangeInfo')

    def test_execution_rule_drift_accepts_price_range_and_rejects_new_rules(self):
        result = inspect_execution_rules({
            'symbolRules': [{
                'symbol': 'ETHUSDT',
                'rules': [{
                    'ruleType': 'PRICE_RANGE',
                    'bidLimitMultDown': '0.85',
                    'bidLimitMultUp': '1.15',
                    'askLimitMultDown': '0.85',
                    'askLimitMultUp': '1.15',
                }],
            }],
        }, {'ETHUSDT'})
        self.assertEqual(result['execution_rule_types'], ['PRICE_RANGE'])
        with self.assertRaisesRegex(ContractDriftError, 'unknown'):
            inspect_execution_rules({
                'symbolRules': [{
                    'symbol': 'ETHUSDT',
                    'rules': [{'ruleType': 'NEW_EXECUTION_RULE'}],
                }],
            }, {'ETHUSDT'})

    def test_execution_rule_drift_rejects_malformed_or_inverted_bounds(self):
        for rule in (
            {'ruleType': 'PRICE_RANGE', 'askLimitMultDown': 'NaN'},
            {'ruleType': 'PRICE_RANGE', 'askLimitMultDown': '2',
             'askLimitMultUp': '1'},
        ):
            with self.subTest(rule=rule), self.assertRaises(
                    ContractDriftError):
                inspect_execution_rules({
                    'symbolRules': [{
                        'symbol': 'ETHUSDT', 'rules': [rule],
                    }],
                }, {'ETHUSDT'})

    def test_contract_bundle_never_mixes_testnet_and_live_payloads(self):
        payload = self._payload([{'filterType': 'PRICE_FILTER'}])
        calls = []

        def info(url):
            calls.append(('info', url))
            if url.startswith('https://testnet.binance.vision'):
                raise OSError('HTTP Error 451')
            return payload

        def rules(url):
            calls.append(('rules', url))
            return {'symbolRules': []}

        actual, execution, source = fetch_contract_for_mode(
            'testnet', info, rules)
        self.assertIs(actual, payload)
        self.assertEqual(execution, {'symbolRules': []})
        self.assertFalse(source['exact_environment'])
        self.assertEqual(calls[0][0], 'info')
        self.assertEqual(calls[1][0], 'info')
        self.assertEqual(calls[2][0], 'rules')




if __name__ == '__main__':
    unittest.main()
