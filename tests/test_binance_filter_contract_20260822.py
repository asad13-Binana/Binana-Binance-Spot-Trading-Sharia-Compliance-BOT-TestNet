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
    fetch_exchange_info,
    fetch_exchange_info_for_mode,
    inspect_exchange_info,
)
from services.execution_sidecar.binance_contract_guard import (
    AccountAwareFilterGuard,
)
from services.execution_sidecar.filters import (
    FilterDataUnavailable,
    FilterViolation,
    SpotFilterValidator,
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

    def __init__(self, filters):
        self.filters = filters

    def exchange_info(self, symbol):
        return {'symbols': [{
            'symbol': symbol, 'status': 'TRADING',
            'isSpotTradingAllowed': True,
            'baseAsset': 'ETH', 'quoteAsset': 'USDT',
            'filters': self.filters,
        }]}

    def reference_price(self, _symbol):
        return '100'

    def ticker_price(self, _symbol):
        return {'price': '100'}

    def get(self, _path, _params=None):
        return {'price': '100'}


def _guard(*extra: dict) -> AccountAwareFilterGuard:
    validator = SpotFilterValidator(
        _Public(_filters(*extra)), max_age_seconds=300)
    return AccountAwareFilterGuard(validator)


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


class TrailingDeltaContractTests(unittest.TestCase):
    def setUp(self):
        self.guard = _guard({
            'filterType': 'TRAILING_DELTA',
            'minTrailingAboveDelta': 100, 'maxTrailingAboveDelta': 200,
            'minTrailingBelowDelta': 10, 'maxTrailingBelowDelta': 20,
        })

    def test_take_profit_sell_uses_above_range(self):
        summary = _validate(
            self.guard, 'order',
            _order(order_type='TAKE_PROFIT_LIMIT', trailing_delta=150))
        self.assertIn('TRAILING_DELTA', summary['filters_checked'])

    def test_take_profit_buy_uses_below_range(self):
        summary = _validate(
            self.guard, 'order',
            _order(side='BUY', order_type='TAKE_PROFIT_LIMIT',
                   trailing_delta=15))
        self.assertIn('TRAILING_DELTA', summary['filters_checked'])

    def test_order_type_and_side_are_both_required(self):
        with self.assertRaisesRegex(FilterViolation, 'outside'):
            _validate(
                self.guard, 'order',
                _order(order_type='TAKE_PROFIT_LIMIT', trailing_delta=15))
        with self.assertRaisesRegex(FilterDataUnavailable, 'unsupported'):
            _validate(
                self.guard, 'order',
                _order(order_type='LIMIT', trailing_delta=150))


class OrderListScopeTests(unittest.TestCase):
    def setUp(self):
        self.guard = _guard({
            'filterType': 'MAX_NUM_ORDER_LISTS', 'maxNumOrderLists': 1,
        })

    def test_unrelated_symbol_list_does_not_consume_symbol_limit(self):
        summary = _validate(
            self.guard, 'orderList/oco', _oco(),
            lists=[{'symbol': 'BTCUSDT', 'orderListId': 7}])
        self.assertIn('MAX_NUM_ORDER_LISTS', summary['filters_checked'])

    def test_same_symbol_list_consumes_symbol_limit(self):
        with self.assertRaisesRegex(FilterViolation, 'MAX_NUM_ORDER_LISTS'):
            _validate(
                self.guard, 'orderList/oco', _oco(),
                lists=[{'symbol': 'ETHUSDT', 'orderListId': 7}])

    def test_exchange_limit_still_counts_all_symbols(self):
        account = _myfilters(exchange=[{
            'filterType': 'EXCHANGE_MAX_NUM_ORDER_LISTS',
            'maxNumOrderLists': 1,
        }])
        with self.assertRaisesRegex(
                FilterViolation, 'EXCHANGE_MAX_NUM_ORDER_LISTS'):
            _validate(
                self.guard, 'orderList/oco', _oco(), account=account,
                lists=[{'symbol': 'BTCUSDT', 'orderListId': 7}])

    def test_nonempty_order_list_row_requires_symbol_identity(self):
        with self.assertRaisesRegex(FilterDataUnavailable, 'malformed rows'):
            _validate(
                self.guard, 'orderList/oco', _oco(),
                lists=[{'orderListId': 7}])


class AccountFilterTests(unittest.TestCase):
    def test_max_asset_base_quantity_is_enforced(self):
        account = _myfilters(assets=[{
            'filterType': 'MAX_ASSET', 'asset': 'ETH', 'limit': '2',
        }])
        summary = _validate(
            _guard(), 'order', _order(quantity='2'), account=account)
        self.assertIn('MAX_ASSET', summary['filters_checked'])
        with self.assertRaisesRegex(FilterViolation, 'MAX_ASSET'):
            _validate(
                _guard(), 'order', _order(quantity='2.001'), account=account)

    def test_max_asset_quote_notional_is_enforced(self):
        account = _myfilters(assets=[{
            'filterType': 'MAX_ASSET', 'asset': 'USDT', 'limit': '200',
        }])
        _validate(
            _guard(), 'order', _order(quantity='2', price='100.00'),
            account=account)
        with self.assertRaisesRegex(FilterViolation, 'MAX_ASSET'):
            _validate(
                _guard(), 'order',
                _order(quantity='2.001', price='100.00'), account=account)

    def test_unrelated_testnet_jpy_filter_is_ignored(self):
        account = _myfilters(assets=[{
            'filterType': 'MAX_ASSET', 'asset': 'JPY', 'limit': '1000000',
        }])
        summary = _validate(_guard(), 'order', _order(), account=account)
        self.assertNotIn('MAX_ASSET', summary['filters_checked'])

    def test_myfilters_is_cached_and_malformed_or_unavailable_fails_closed(self):
        calls = []
        guard = _guard()

        def provider(symbol):
            calls.append(symbol)
            return _myfilters()

        kwargs = {
            'account_filters_provider': provider,
            'open_orders_provider': lambda _symbol: [],
            'open_exchange_orders_provider': list,
            'open_order_lists_provider': list,
        }
        guard.validate('ETHUSDT', 'order', _order(), **kwargs)
        guard.validate('ETHUSDT', 'order', _order(), **kwargs)
        self.assertEqual(calls, ['ETHUSDT'])

        with self.assertRaisesRegex(FilterDataUnavailable, 'assetFilters'):
            _validate(
                _guard(), 'order', _order(),
                account={'exchangeFilters': [], 'symbolFilters': []})

        def offline(_symbol):
            raise ConnectionError('offline')

        with self.assertRaisesRegex(FilterDataUnavailable, 'unavailable'):
            _guard().validate(
                'ETHUSDT', 'order', _order(),
                account_filters_provider=offline,
                open_orders_provider=lambda _symbol: [],
                open_exchange_orders_provider=list,
                open_order_lists_provider=list)

    def test_max_position_fails_closed_without_guessing_formula(self):
        active = {'filterType': 'MAX_POSITION', 'maxPosition': '10'}
        with self.assertRaisesRegex(FilterDataUnavailable, 'account-aware'):
            _validate(_guard(active), 'order', _order())
        account = _myfilters(symbol=[active])
        with self.assertRaisesRegex(FilterDataUnavailable, 'account-aware'):
            _validate(_guard(), 'order', _order(), account=account)

    def test_exchange_order_capacity_uses_account_wide_provider(self):
        account = _myfilters(exchange=[{
            'filterType': 'EXCHANGE_MAX_NUM_ORDERS', 'maxNumOrders': 1,
        }])
        with self.assertRaisesRegex(
                FilterViolation, 'EXCHANGE_MAX_NUM_ORDERS'):
            _validate(
                _guard(), 'order', _order(), account=account,
                exchange_orders=[{
                    'symbol': 'BTCUSDT', 'orderId': 7, 'type': 'LIMIT',
                }])


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


class DeploymentWiringTests(unittest.TestCase):
    def test_compose_uses_guarded_launcher(self):
        compose = (ROOT / 'docker-compose.yml').read_text(encoding='utf-8')
        self.assertIn(
            'command: python -m services.execution_sidecar.guarded_main',
            compose)

    def test_guarded_core_path_calls_authenticated_myfilters(self):
        from services.execution_sidecar import guarded_main

        calls = []

        class Client:
            def get_open_orders(self, symbol=None):
                calls.append(('openOrders', symbol))
                return []

            def _get(self, path, signed, data):
                calls.append((path, signed, dict(data)))
                if path == 'myFilters':
                    return _myfilters()
                if path == 'openOrderList':
                    return []
                raise AssertionError(path)

        class Broker:
            c = Client()

            @staticmethod
            def _sync_weight():
                return None

        adapter = guarded_main.core_adapter.CoreAdapter.__new__(
            guarded_main.core_adapter.CoreAdapter)
        adapter.filter_validator = SpotFilterValidator(
            _Public(_filters()), max_age_seconds=300)
        adapter._validate_replacement_filters(
            Broker(), 'ETHUSDT', 'order', _order())
        self.assertIn(
            ('myFilters', True, {'symbol': 'ETHUSDT'}), calls)


if __name__ == '__main__':
    unittest.main()
