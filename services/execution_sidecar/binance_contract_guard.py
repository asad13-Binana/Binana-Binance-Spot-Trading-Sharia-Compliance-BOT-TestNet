"""Current Binance account/filter compatibility overlay.

The released execution core is byte-protected. This module adds a narrow,
fail-closed pre-cancel guard without editing that protected implementation.
The Oracle/Docker launcher installs it before importing the protected main
module.
"""

from __future__ import annotations

import copy
import threading
import time
from decimal import Decimal

from services.common.audit import audit
from services.execution_sidecar.filters import (
    FilterDataUnavailable,
    FilterViolation,
    OrderLeg,
    SpotFilterValidator,
    legs_from_request,
)

SUPPORTED_ACCOUNT_SYMBOL_FILTERS = {
    'PRICE_FILTER', 'PERCENT_PRICE', 'PERCENT_PRICE_BY_SIDE',
    'LOT_SIZE', 'MIN_NOTIONAL', 'NOTIONAL', 'ICEBERG_PARTS',
    'MARKET_LOT_SIZE', 'MAX_NUM_ORDERS', 'MAX_NUM_ALGO_ORDERS',
    'MAX_NUM_ICEBERG_ORDERS', 'MAX_POSITION', 'TRAILING_DELTA',
    'MAX_NUM_ORDER_AMENDS', 'MAX_NUM_ORDER_LISTS',
}
SUPPORTED_ACCOUNT_EXCHANGE_FILTERS = {
    'EXCHANGE_MAX_NUM_ORDERS', 'EXCHANGE_MAX_NUM_ALGO_ORDERS',
    'EXCHANGE_MAX_NUM_ICEBERG_ORDERS', 'EXCHANGE_MAX_NUM_ORDER_LISTS',
}
ALGO_TYPES = {
    'STOP_LOSS', 'STOP_LOSS_LIMIT', 'TAKE_PROFIT', 'TAKE_PROFIT_LIMIT',
}


class _EntryPublicProxy:
    """Supply one merged account/public entry while delegating price calls."""

    def __init__(self, public, symbol: str, entry: dict):
        self._public = public
        self._symbol = symbol
        self._entry = entry
        self.NO_REFERENCE_PRICE = public.NO_REFERENCE_PRICE

    def exchange_info(self, symbol):
        if symbol != self._symbol:
            raise RuntimeError('account-filter symbol identity mismatch')
        return {'symbols': [copy.deepcopy(self._entry)]}

    def __getattr__(self, name):
        return getattr(self._public, name)


class AccountAwareFilterGuard:
    """Validate current public plus authenticated account-relevant filters."""

    def __init__(self, validator: SpotFilterValidator):
        self.validator = validator
        self.max_age = validator.max_age
        self._account_cache: dict[str, tuple[float, dict]] = {}
        self._lock = threading.RLock()

    def _account_data(self, symbol: str, provider) -> dict:
        with self._lock:
            cached = self._account_cache.get(symbol)
            if cached and time.time() - cached[0] <= self.max_age:
                return copy.deepcopy(cached[1])
            try:
                data = provider(symbol)
                if not isinstance(data, dict):
                    raise FilterDataUnavailable(
                        f'{symbol} myFilters response must be an object')
                normalized: dict[str, list[dict]] = {}
                for key in ('exchangeFilters', 'symbolFilters', 'assetFilters'):
                    rows = data.get(key)
                    if not isinstance(rows, list):
                        raise FilterDataUnavailable(
                            f'{symbol} myFilters.{key} must be an array')
                    normalized_rows: list[dict] = []
                    for index, row in enumerate(rows):
                        if not isinstance(row, dict):
                            raise FilterDataUnavailable(
                                f'{symbol} myFilters.{key}[{index}] must be an object')
                        name = row.get('filterType')
                        if (not isinstance(name, str) or not name
                                or name != name.upper()):
                            raise FilterDataUnavailable(
                                f'{symbol} myFilters.{key}[{index}].filterType '
                                'is missing or non-canonical')
                        normalized_rows.append(dict(row))
                    normalized[key] = normalized_rows
            except Exception as exc:
                if cached:
                    raise FilterDataUnavailable(
                        f'current account filters for {symbol} are stale and '
                        f'refresh failed: {exc}') from exc
                raise FilterDataUnavailable(
                    f'current account filters for {symbol} unavailable: {exc}') from exc
            self._account_cache[symbol] = (time.time(), normalized)
            return copy.deepcopy(normalized)

    @staticmethod
    def _trailing_suffix(leg: OrderLeg) -> str:
        key = (leg.order_type.upper(), leg.side.upper())
        if key in {
            ('STOP_LOSS', 'BUY'), ('STOP_LOSS_LIMIT', 'BUY'),
            ('TAKE_PROFIT', 'SELL'), ('TAKE_PROFIT_LIMIT', 'SELL'),
        }:
            return 'Above'
        if key in {
            ('STOP_LOSS', 'SELL'), ('STOP_LOSS_LIMIT', 'SELL'),
            ('TAKE_PROFIT', 'BUY'), ('TAKE_PROFIT_LIMIT', 'BUY'),
        }:
            return 'Below'
        raise FilterDataUnavailable(
            f'trailingDelta is unsupported for {leg.order_type} {leg.side}')

    def _merged_entry(self, symbol: str, account_data: dict,
                      legs: list[OrderLeg]) -> tuple[dict, dict[str, dict]]:
        entry = copy.deepcopy(self.validator._symbol_entry(symbol))
        public_filter_rows = entry.get('filters')
        if not isinstance(public_filter_rows, list):
            raise FilterDataUnavailable(
                f'{symbol} public symbol filters are missing or malformed')
        public_filters = self.validator._filter_map(
            public_filter_rows, symbol)
        account_symbol = self.validator._filter_map(
            account_data['symbolFilters'],
            f'{symbol} myFilters.symbolFilters')
        unknown = sorted(
            set(account_symbol) - SUPPORTED_ACCOUNT_SYMBOL_FILTERS)
        if unknown:
            raise FilterDataUnavailable(
                f'{symbol} myFilters returned unsupported symbol filters: '
                + ', '.join(unknown))
        if 'MAX_POSITION' in public_filters or 'MAX_POSITION' in account_symbol:
            raise FilterDataUnavailable(
                f'{symbol} publishes MAX_POSITION; account-aware balance and '
                'open-BUY prevalidation is required before trading this symbol')

        merged = dict(public_filters)
        merged.update(account_symbol)
        trailing = merged.get('TRAILING_DELTA')
        assignments: dict[str, str] = {}
        for leg in legs:
            if leg.trailing_delta is None:
                continue
            old_suffix = 'Below' if leg.side.upper() == 'SELL' else 'Above'
            desired_suffix = self._trailing_suffix(leg)
            prior = assignments.get(old_suffix)
            if prior is not None and prior != desired_suffix:
                raise FilterDataUnavailable(
                    f'{symbol} request mixes incompatible trailing-order semantics')
            assignments[old_suffix] = desired_suffix
        if assignments:
            if not isinstance(trailing, dict):
                raise FilterDataUnavailable(
                    f'{symbol} required TRAILING_DELTA metadata is missing')
            trailing = dict(trailing)
            for old_suffix, desired_suffix in assignments.items():
                for prefix in ('min', 'max'):
                    source = f'{prefix}Trailing{desired_suffix}Delta'
                    target = f'{prefix}Trailing{old_suffix}Delta'
                    if source not in trailing:
                        raise FilterDataUnavailable(
                            f'{symbol} required TRAILING_DELTA.{source} is missing')
                    trailing[target] = trailing[source]
            merged['TRAILING_DELTA'] = trailing

        original_order = [
            row.get('filterType') for row in entry['filters']
            if isinstance(row, dict)
        ]
        entry['filters'] = [
            copy.deepcopy(merged[name]) for name in original_order
        ]
        entry['filters'].extend(
            copy.deepcopy(row) for name, row in account_symbol.items()
            if name not in public_filters
        )

        exchange_filters = self.validator._filter_map(
            account_data['exchangeFilters'],
            f'{symbol} myFilters.exchangeFilters')
        unknown_exchange = sorted(
            set(exchange_filters) - SUPPORTED_ACCOUNT_EXCHANGE_FILTERS)
        if unknown_exchange:
            raise FilterDataUnavailable(
                f'{symbol} myFilters returned unsupported exchange filters: '
                + ', '.join(unknown_exchange))
        return entry, exchange_filters

    def _asset_limits(self, symbol: str, rows: list[dict]) -> dict[str, Decimal]:
        limits: dict[str, Decimal] = {}
        for index, row in enumerate(rows):
            if row.get('filterType') != 'MAX_ASSET':
                raise FilterDataUnavailable(
                    f'{symbol} myFilters.assetFilters[{index}] has unsupported '
                    f'filterType {row.get("filterType")!r}')
            asset = row.get('asset')
            if (not isinstance(asset, str) or not asset
                    or asset != asset.upper()):
                raise FilterDataUnavailable(
                    f'{symbol} myFilters.assetFilters[{index}].asset is malformed')
            if asset in limits:
                raise FilterDataUnavailable(
                    f'{symbol} myFilters contains duplicate MAX_ASSET for {asset}')
            limits[asset] = self.validator._required_decimal(
                row, 'MAX_ASSET', 'limit')
        return limits

    @staticmethod
    def _open_lists(rows, symbol: str) -> tuple[list[dict], list[dict]]:
        if not isinstance(rows, list) or any(
                not isinstance(row, dict)
                or not isinstance(row.get('symbol'), str)
                or not row.get('symbol')
                or isinstance(row.get('orderListId'), bool)
                or not isinstance(row.get('orderListId'), int)
                or row.get('orderListId') <= 0
                for row in rows):
            raise FilterDataUnavailable(
                'open order-list enumeration returned malformed rows')
        return rows, [row for row in rows if row['symbol'] == symbol]

    @staticmethod
    def _exchange_orders(rows) -> list[dict]:
        if not isinstance(rows, list) or any(
                not isinstance(row, dict)
                or not isinstance(row.get('symbol'), str)
                or not row.get('symbol')
                or isinstance(row.get('orderId'), bool)
                or not isinstance(row.get('orderId'), int)
                or row.get('orderId') <= 0
                or not isinstance(row.get('type'), str)
                for row in rows):
            raise FilterDataUnavailable(
                'exchange-wide open-order enumeration returned malformed rows')
        return rows

    def validate(self, symbol: str, endpoint: str, params: dict, *,
                 account_filters_provider, open_orders_provider,
                 open_exchange_orders_provider,
                 open_order_lists_provider) -> dict:
        try:
            legs = legs_from_request(endpoint, params)
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            raise FilterDataUnavailable(
                f'{symbol} request fields are malformed: {exc}') from exc
        account_data = self._account_data(symbol, account_filters_provider)
        entry, exchange_filters = self._merged_entry(
            symbol, account_data, legs)
        proxy = _EntryPublicProxy(self.validator.public, symbol, entry)
        delegate = SpotFilterValidator(
            public_client=proxy, max_age_seconds=self.max_age)
        merged_filter_map = delegate._filter_map(entry['filters'], symbol)
        asset_limits = self._asset_limits(
            symbol, account_data['assetFilters'])
        base_asset = str(entry['baseAsset'])
        quote_asset = str(entry['quoteAsset'])
        account_checked: set[str] = set()
        quote_reference: Decimal | None = None
        for leg in legs:
            for asset, limit in asset_limits.items():
                if asset == base_asset:
                    amount = leg.qty
                elif asset == quote_asset:
                    price = leg.price or leg.stop_price
                    if price is None:
                        if quote_reference is None:
                            quote_reference = delegate._reference_price(
                                symbol, merged_filter_map)
                        price = quote_reference
                    amount = price * leg.qty
                else:
                    continue
                account_checked.add('MAX_ASSET')
                if amount > limit:
                    raise FilterViolation(
                        f'{symbol} {leg.order_type} transacts {amount} {asset}, '
                        f'above account MAX_ASSET limit {limit}')

        all_lists_cache: list[dict] | None = None

        def all_lists() -> list[dict]:
            nonlocal all_lists_cache
            if all_lists_cache is None:
                all_lists_cache, _ = self._open_lists(
                    open_order_lists_provider(), symbol)
            return all_lists_cache

        def symbol_lists() -> list[dict]:
            rows = all_lists()
            return [row for row in rows if row['symbol'] == symbol]

        new_orders = len(legs)
        order_limit_row = exchange_filters.get('EXCHANGE_MAX_NUM_ORDERS')
        algo_limit_row = exchange_filters.get('EXCHANGE_MAX_NUM_ALGO_ORDERS')
        if order_limit_row is not None or algo_limit_row is not None:
            exchange_orders = self._exchange_orders(
                open_exchange_orders_provider())
            if order_limit_row is not None:
                limit = self.validator._required_int(
                    order_limit_row, 'EXCHANGE_MAX_NUM_ORDERS',
                    'maxNumOrders', minimum=1)
                account_checked.add('EXCHANGE_MAX_NUM_ORDERS')
                if len(exchange_orders) + new_orders > limit:
                    raise FilterViolation(
                        f'account would exceed EXCHANGE_MAX_NUM_ORDERS {limit} '
                        f'({len(exchange_orders)} open + {new_orders} new)')
            if algo_limit_row is not None:
                limit = self.validator._required_int(
                    algo_limit_row, 'EXCHANGE_MAX_NUM_ALGO_ORDERS',
                    'maxNumAlgoOrders', minimum=1)
                open_algo = sum(
                    1 for row in exchange_orders
                    if row['type'].upper() in ALGO_TYPES)
                new_algo = sum(1 for leg in legs if leg.is_algo)
                account_checked.add('EXCHANGE_MAX_NUM_ALGO_ORDERS')
                if open_algo + new_algo > limit:
                    raise FilterViolation(
                        f'account would exceed EXCHANGE_MAX_NUM_ALGO_ORDERS '
                        f'{limit} ({open_algo} open + {new_algo} new)')

        if endpoint.startswith('orderList/'):
            exchange_list_row = exchange_filters.get(
                'EXCHANGE_MAX_NUM_ORDER_LISTS')
            if exchange_list_row is not None:
                limit = self.validator._required_int(
                    exchange_list_row, 'EXCHANGE_MAX_NUM_ORDER_LISTS',
                    'maxNumOrderLists', minimum=1)
                rows = all_lists()
                account_checked.add('EXCHANGE_MAX_NUM_ORDER_LISTS')
                if len(rows) + 1 > limit:
                    raise FilterViolation(
                        f'account would exceed EXCHANGE_MAX_NUM_ORDER_LISTS '
                        f'{limit} ({len(rows)} open + 1 new)')

        if any(str(key).lower().endswith('icebergqty') for key in params):
            raise FilterDataUnavailable(
                f'{symbol} iceberg requests are outside the protected bot contract')

        summary = delegate.validate_replacement(
            symbol, endpoint, params,
            open_orders_provider=open_orders_provider,
            open_order_lists_provider=symbol_lists,
        )
        for name in sorted(account_checked):
            if name not in summary['filters_checked']:
                summary['filters_checked'].append(name)
        audit('account_filters_validated', details={
            'symbol': symbol,
            'endpoint': endpoint,
            'filters_checked': sorted(account_checked),
        })
        return summary


def install_binance_contract_guard(core_adapter_cls) -> None:
    """Install once on the byte-protected CoreAdapter class."""
    current = core_adapter_cls._validate_replacement_filters
    if getattr(current, '_binana_contract_guard', False):
        return
    original = current

    def hardened(self, broker, symbol: str, endpoint: str, params: dict):
        validator = getattr(self, 'filter_validator', None)
        if validator is None:
            validator = SpotFilterValidator()
            self.filter_validator = validator
        # Preserve unit-test/fake-validator behavior and patch only the real
        # production validator.
        if not isinstance(validator, SpotFilterValidator):
            return original(self, broker, symbol, endpoint, params)
        guard = getattr(self, '_binance_contract_guard', None)
        if (
            not isinstance(guard, AccountAwareFilterGuard)
            or guard.validator is not validator
        ):
            guard = AccountAwareFilterGuard(validator)
            self._binance_contract_guard = guard

        def open_orders_for(sym_name):
            out = broker.c.get_open_orders(symbol=sym_name)
            broker._sync_weight()
            if not isinstance(out, list):
                raise TypeError('unexpected openOrders response')
            return out

        def open_exchange_orders():
            out = broker.c.get_open_orders()
            broker._sync_weight()
            if not isinstance(out, list):
                raise TypeError('unexpected exchange-wide openOrders response')
            return out

        def open_lists():
            out = broker.c._get('openOrderList', True, data={})
            broker._sync_weight()
            if not isinstance(out, list):
                raise TypeError('unexpected openOrderList response')
            return out

        def account_filters_for(sym_name):
            out = broker.c._get('myFilters', True, data={'symbol': sym_name})
            broker._sync_weight()
            if not isinstance(out, dict):
                raise TypeError('unexpected myFilters response')
            return out

        return guard.validate(
            symbol, endpoint, params,
            account_filters_provider=account_filters_for,
            open_orders_provider=open_orders_for,
            open_exchange_orders_provider=open_exchange_orders,
            open_order_lists_provider=open_lists,
        )

    hardened._binana_contract_guard = True
    hardened._binana_original = original
    core_adapter_cls._validate_replacement_filters = hardened
