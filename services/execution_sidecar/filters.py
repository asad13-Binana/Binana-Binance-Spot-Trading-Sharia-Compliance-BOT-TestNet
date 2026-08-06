from __future__ import annotations
"""Complete Binance Spot filter prevalidation (fixes V101-NEW-002).

The previous replacement prevalidation checked only quantity/step, tick
multiples, minimum notional and trailing-delta bounds. Binance can reject an
order for PRICE_FILTER min/max, PERCENT_PRICE(_BY_SIDE) reference-price bands,
NOTIONAL maximum, or MAX_NUM_ORDERS / MAX_NUM_ALGO_ORDERS /
MAX_NUM_ORDER_LISTS capacity — none of which were checked before the active
protection was cancelled. A rejected replacement after cancellation leaves the
position unprotected.

This module fetches the symbol's complete current filter set (public
exchangeInfo), a current reference price, and — through caller-supplied
authenticated callables — the open order / order-list counts, then validates
every leg of the intended replacement BEFORE anything is cancelled.

Fail-closed rule: if the filter data cannot be fetched or is stale, the
conversion is refused before cancellation.
"""
import time
from dataclasses import dataclass, field
from decimal import Decimal

from services.common.audit import audit
from services.common.binance_public import BinancePublicClient
from services.common.config_bounds import env_int


class FilterDataUnavailable(RuntimeError):
    """Current exchange filter data could not be obtained. Refuse to convert."""


class FilterViolation(ValueError):
    """The intended replacement violates a current Binance filter."""


@dataclass
class OrderLeg:
    side: str                      # 'SELL' or 'BUY'
    order_type: str                # LIMIT_MAKER / STOP_LOSS_LIMIT / ...
    qty: Decimal
    price: Decimal | None = None
    stop_price: Decimal | None = None
    trailing_delta: int | None = None
    is_algo: bool = field(init=False, default=False)

    def __post_init__(self):
        self.is_algo = self.order_type.upper() in {
            'STOP_LOSS', 'STOP_LOSS_LIMIT', 'TAKE_PROFIT', 'TAKE_PROFIT_LIMIT'
        }


def _optional_trailing_delta(params: dict, key: str) -> int | None:
    value = params.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise FilterDataUnavailable(f'{key} must be a positive JSON integer')
    return value


def legs_from_request(endpoint: str, params: dict) -> list[OrderLeg]:
    """Translate a factory-built replacement request into validation legs."""
    if endpoint == 'order':
        return [OrderLeg(side=str(params.get('side', 'SELL')),
                         order_type=str(params.get('type', 'STOP_LOSS_LIMIT')),
                         qty=Decimal(str(params['quantity'])),
                         price=Decimal(str(params['price'])) if params.get('price') else None,
                         stop_price=Decimal(str(params['stopPrice'])) if params.get('stopPrice') else None,
                         trailing_delta=_optional_trailing_delta(params, 'trailingDelta'))]
    if endpoint not in {'orderList/oto', 'orderList/oco', 'orderList/otoco'}:
        raise FilterDataUnavailable(f'unsupported Spot endpoint for prevalidation: {endpoint!r}')
    if params.get('workingQuantity') is not None:
        # OTO/OTOCO entry request: validate the BUY working order as well as
        # every pending SELL leg. The older replacement-only parser skipped
        # these field names, which let the default entry mode bypass strict
        # filters and order/list capacities.
        working = OrderLeg(
            side=str(params.get('workingSide', 'BUY')),
            order_type=str(params.get('workingType', 'LIMIT')),
            qty=Decimal(str(params['workingQuantity'])),
            price=(Decimal(str(params['workingPrice']))
                   if params.get('workingPrice') else None),
        )
        pending_qty = Decimal(str(params['pendingQuantity']))
        if endpoint == 'orderList/oto':
            return [working, OrderLeg(
                side=str(params.get('pendingSide', 'SELL')),
                order_type=str(params.get('pendingType', 'STOP_LOSS_LIMIT')),
                qty=pending_qty,
                price=(Decimal(str(params['pendingPrice']))
                       if params.get('pendingPrice') else None),
                stop_price=(Decimal(str(params['pendingStopPrice']))
                            if params.get('pendingStopPrice') else None),
                trailing_delta=_optional_trailing_delta(params, 'pendingTrailingDelta'),
            )]
        return [working, OrderLeg(
            side=str(params.get('pendingSide', 'SELL')),
            order_type=str(params.get('pendingAboveType', 'LIMIT_MAKER')),
            qty=pending_qty,
            price=(Decimal(str(params['pendingAbovePrice']))
                   if params.get('pendingAbovePrice') else None),
        ), OrderLeg(
            side=str(params.get('pendingSide', 'SELL')),
            order_type=str(params.get('pendingBelowType', 'STOP_LOSS_LIMIT')),
            qty=pending_qty,
            price=(Decimal(str(params['pendingBelowPrice']))
                   if params.get('pendingBelowPrice') else None),
            stop_price=(Decimal(str(params['pendingBelowStopPrice']))
                        if params.get('pendingBelowStopPrice') else None),
            trailing_delta=_optional_trailing_delta(params, 'pendingBelowTrailingDelta'),
        )]
    qty = Decimal(str(params['quantity']))
    above = OrderLeg(side='SELL', order_type=str(params.get('aboveType', 'LIMIT_MAKER')), qty=qty,
                     price=Decimal(str(params['abovePrice'])) if params.get('abovePrice') else None)
    below = OrderLeg(side='SELL', order_type=str(params.get('belowType', 'STOP_LOSS_LIMIT')), qty=qty,
                     price=Decimal(str(params['belowPrice'])) if params.get('belowPrice') else None,
                     stop_price=Decimal(str(params['belowStopPrice'])) if params.get('belowStopPrice') else None,
                     trailing_delta=_optional_trailing_delta(params, 'belowTrailingDelta'))
    return [above, below]


class SpotFilterValidator:
    def __init__(self, public_client: BinancePublicClient | None = None,
                 max_age_seconds: int | None = None):
        self.public = public_client or BinancePublicClient()
        self.max_age = max_age_seconds if max_age_seconds is not None else env_int(
            'SPOT_FILTER_MAX_AGE_SECONDS', 300, 10, 3600)
        self._cache: dict[str, tuple[float, dict]] = {}

    # ---- data acquisition (fail closed) ----
    def _symbol_entry(self, symbol: str) -> dict:
        cached = self._cache.get(symbol)
        if cached and time.time() - cached[0] <= self.max_age:
            return cached[1]
        try:
            info = self.public.exchange_info(symbol)
            symbols = info.get('symbols') if isinstance(info, dict) else None
            if not isinstance(symbols, list) or len(symbols) != 1:
                raise FilterDataUnavailable(
                    f'{symbol} exchangeInfo must contain exactly one symbol row')
            entry = symbols[0]
            if not isinstance(entry, dict) or entry.get('symbol') != symbol:
                raise FilterDataUnavailable(f'{symbol} exchangeInfo symbol identity mismatch')
            base = entry.get('baseAsset')
            quote = entry.get('quoteAsset')
            if (
                not isinstance(base, str) or not base
                or not isinstance(quote, str) or quote != 'USDT'
                or base + quote != symbol
            ):
                raise FilterDataUnavailable(
                    f'{symbol} exchangeInfo base/quote identity binding is malformed')
        except Exception as exc:
            if cached:
                raise FilterDataUnavailable(
                    f'current exchange filters for {symbol} are stale and refresh failed: {exc}') from exc
            raise FilterDataUnavailable(
                f'current exchange filters for {symbol} unavailable: {exc}') from exc
        self._cache[symbol] = (time.time(), entry)
        return entry

    def _reference_price(self, symbol: str, filters: dict) -> Decimal:
        """Current reference price for the PERCENT_PRICE(_BY_SIDE)/NOTIONAL bands.

        F23E-001 / Binance 2026-05-06: these filters use the exchange
        referencePrice (GET /api/v3/referencePrice) when it exists and is
        non-null, and fall back to the previous avgPrice/last-price behavior
        only when it is null or the symbol never had one (error -2043). Any
        OTHER failure (timeout, 5xx, rate limit, malformed body) fails closed —
        cancelling the active protection against a wrong band is unsafe.
        """
        try:
            ref = self.public.reference_price(symbol)
        except Exception as exc:
            raise FilterDataUnavailable(
                f'reference price for {symbol} unavailable: {exc}') from exc
        if ref is not self.public.NO_REFERENCE_PRICE:
            try:
                value = Decimal(str(ref))
            except Exception as exc:
                raise FilterDataUnavailable(
                    f'malformed reference price for {symbol}: {ref!r}') from exc
            if not value.is_finite() or value <= 0:
                raise FilterDataUnavailable(
                    f'non-positive reference price for {symbol}: {value}')
            return value
        # Documented fallback: null / never-set reference price -> prior behavior.
        pps = filters.get('PERCENT_PRICE_BY_SIDE') or filters.get('PERCENT_PRICE') or {}
        try:
            if int(pps.get('avgPriceMins', 0) or 0) > 0:
                data = self.public.get('/api/v3/avgPrice', {'symbol': symbol})
                value = Decimal(str(data['price']))
            else:
                data = self.public.ticker_price(symbol)
                value = Decimal(str(data['price']))
            if not value.is_finite() or value <= 0:
                raise ValueError(f'invalid fallback price {value}')
            return value
        except Exception as exc:
            raise FilterDataUnavailable(
                f'fallback reference price for {symbol} unavailable: {exc}') from exc

    def _last_price(self, symbol: str) -> Decimal:
        try:
            data = self.public.ticker_price(symbol)
            value = Decimal(str(data['price']))
        except Exception as exc:
            raise FilterDataUnavailable(
                f'last traded price for {symbol} unavailable: {exc}') from exc
        if not value.is_finite() or value <= 0:
            raise FilterDataUnavailable(
                f'invalid last traded price for {symbol}: {value}')
        return value

    # ---- validation ----
    @staticmethod
    def _dec(mapping: dict, key: str) -> Decimal | None:
        if key not in mapping or mapping.get(key) in (None, ''):
            raise FilterDataUnavailable(f'required percent-price field {key} is missing')
        try:
            value = Decimal(str(mapping[key]))
        except Exception as exc:
            raise FilterDataUnavailable(
                f'malformed percent-price field {key}: {mapping.get(key)!r}') from exc
        if not value.is_finite() or value <= 0:
            raise FilterDataUnavailable(
                f'invalid percent-price field {key}: {mapping.get(key)!r}')
        return value

    @staticmethod
    def _required_filter(filters: dict, name: str) -> dict:
        value = filters.get(name)
        if not isinstance(value, dict):
            raise FilterDataUnavailable(f'required {name} metadata is missing')
        return value

    @staticmethod
    def _required_decimal(mapping: dict, filter_name: str, key: str) -> Decimal:
        if key not in mapping or mapping.get(key) in (None, ''):
            raise FilterDataUnavailable(f'required {filter_name}.{key} metadata is missing')
        try:
            value = Decimal(str(mapping[key]))
        except Exception as exc:
            raise FilterDataUnavailable(
                f'malformed {filter_name}.{key} metadata: {mapping.get(key)!r}') from exc
        if not value.is_finite() or value < 0:
            raise FilterDataUnavailable(
                f'invalid {filter_name}.{key} metadata: {mapping.get(key)!r}')
        return value

    @staticmethod
    def _required_int(mapping: dict, filter_name: str, key: str, *,
                      minimum: int = 0) -> int:
        if key not in mapping:
            raise FilterDataUnavailable(f'required {filter_name}.{key} metadata is missing')
        value = mapping[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise FilterDataUnavailable(
                f'invalid {filter_name}.{key} metadata: {value!r}')
        return value

    @staticmethod
    def _required_bool(mapping: dict, filter_name: str, key: str) -> bool:
        if key not in mapping or not isinstance(mapping[key], bool):
            raise FilterDataUnavailable(
                f'required {filter_name}.{key} must be a JSON boolean')
        return mapping[key]

    @staticmethod
    def _filter_map(raw_filters: list, symbol: str) -> dict[str, dict]:
        filters: dict[str, dict] = {}
        for index, item in enumerate(raw_filters):
            if not isinstance(item, dict):
                raise FilterDataUnavailable(
                    f'{symbol} filters[{index}] must be an object')
            name = item.get('filterType')
            if not isinstance(name, str) or not name or name != name.upper():
                raise FilterDataUnavailable(
                    f'{symbol} filters[{index}].filterType is missing or non-canonical')
            if name in filters:
                raise FilterDataUnavailable(
                    f'{symbol} exchangeInfo contains duplicate {name} filters')
            filters[name] = item
        return filters

    @staticmethod
    def _validate_legs(symbol: str, legs: list[OrderLeg]) -> None:
        if not legs:
            raise FilterDataUnavailable(f'{symbol} request produced no order legs')
        for leg in legs:
            if leg.side.upper() not in {'BUY', 'SELL'} or not leg.order_type.strip():
                raise FilterDataUnavailable(f'{symbol} order leg side/type is malformed')
            if not leg.qty.is_finite() or leg.qty <= 0:
                raise FilterViolation(f'{symbol} order quantity must be positive and finite')
            for value in (leg.price, leg.stop_price):
                if value is not None and (not value.is_finite() or value <= 0):
                    raise FilterViolation(f'{symbol} order price must be positive and finite')
            if leg.trailing_delta is not None and (
                    isinstance(leg.trailing_delta, bool) or leg.trailing_delta <= 0):
                raise FilterDataUnavailable(
                    f'{symbol} trailingDelta must be a positive integer')

    def _validate_oco_relationship(self, symbol: str, endpoint: str,
                                   params: dict) -> Decimal | None:
        if endpoint not in {'orderList/oco', 'orderList/otoco'}:
            return None
        prefix = 'pending' if endpoint == 'orderList/otoco' else ''
        side_key = 'pendingSide' if prefix else 'side'
        side = str(params.get(side_key, 'SELL')).upper()
        if side != 'SELL':
            raise FilterDataUnavailable(
                f'{symbol} only protective SELL OCO/OTOCO requests are supported')
        above_key = 'pendingAbovePrice' if prefix else 'abovePrice'
        below_price_key = 'pendingBelowPrice' if prefix else 'belowPrice'
        below_stop_key = 'pendingBelowStopPrice' if prefix else 'belowStopPrice'
        above_type_key = 'pendingAboveType' if prefix else 'aboveType'
        below_type_key = 'pendingBelowType' if prefix else 'belowType'
        if params.get(above_type_key) != 'LIMIT_MAKER' or \
                params.get(below_type_key) != 'STOP_LOSS_LIMIT':
            raise FilterDataUnavailable(
                f'{symbol} protective OCO/OTOCO leg types are non-canonical')
        try:
            above = Decimal(str(params[above_key]))
            below_price = Decimal(str(params[below_price_key]))
            below_stop = (Decimal(str(params[below_stop_key]))
                          if params.get(below_stop_key) not in (None, '') else None)
        except (KeyError, TypeError, ValueError) as exc:
            raise FilterDataUnavailable(
                f'{symbol} protective OCO/OTOCO relationship fields are malformed') from exc
        if any(not value.is_finite() or value <= 0
               for value in (above, below_price, *(() if below_stop is None else (below_stop,)))):
            raise FilterDataUnavailable(
                f'{symbol} protective OCO/OTOCO prices must be positive and finite')
        last = self._last_price(symbol)
        lower_trigger = below_stop if below_stop is not None else below_price
        if not above > last > lower_trigger:
            raise FilterViolation(
                f'{symbol} SELL OCO relationship requires abovePrice > lastPrice > '
                f'below trigger ({above} > {last} > {lower_trigger})')
        if below_stop is not None and below_price > below_stop:
            raise FilterViolation(
                f'{symbol} SELL stop-limit price must not exceed its stop price')
        return last

    def validate_replacement(self, symbol: str, endpoint: str, params: dict, *,
                             open_orders_provider=None, open_order_lists_provider=None) -> dict:
        """Validate every leg against the complete current filter set.

        Raises FilterViolation / FilterDataUnavailable. Returns a summary dict
        with the validated filter names for the audit trail.
        """
        entry = self._symbol_entry(symbol)
        if entry.get('status') != 'TRADING':
            raise FilterViolation(f'{symbol} status is {entry.get("status")!r}, not TRADING')
        if entry.get('isSpotTradingAllowed') is not True:
            raise FilterViolation(f'{symbol} spot trading is not allowed')
        if str(entry.get('quoteAsset', '')).upper() != 'USDT':
            raise FilterViolation(f'{symbol} is not USDT-quoted')
        raw_filters = entry.get('filters')
        if not isinstance(raw_filters, list):
            raise FilterDataUnavailable(f'{symbol} filter list is missing or malformed')
        filters = self._filter_map(raw_filters, symbol)
        try:
            legs = legs_from_request(endpoint, params)
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            raise FilterDataUnavailable(
                f'{symbol} request fields are malformed: {exc}') from exc
        self._validate_legs(symbol, legs)
        last_price = self._validate_oco_relationship(symbol, endpoint, params)
        checked: list[str] = []

        def mark(name: str) -> None:
            if name not in checked:
                checked.append(name)

        # Core Spot filters must be present and structurally usable. A missing
        # field is UNKNOWN exchange policy, not an exchange-disabled zero.
        price_filter = self._required_filter(filters, 'PRICE_FILTER')
        tick = self._required_decimal(price_filter, 'PRICE_FILTER', 'tickSize')
        min_price = self._required_decimal(price_filter, 'PRICE_FILTER', 'minPrice')
        max_price = self._required_decimal(price_filter, 'PRICE_FILTER', 'maxPrice')
        if tick <= 0:
            raise FilterDataUnavailable('PRICE_FILTER.tickSize must be positive')
        if min_price > 0 and max_price > 0 and min_price > max_price:
            raise FilterDataUnavailable('PRICE_FILTER minPrice exceeds maxPrice')
        lot = self._required_filter(filters, 'LOT_SIZE')
        step = self._required_decimal(lot, 'LOT_SIZE', 'stepSize')
        min_qty = self._required_decimal(lot, 'LOT_SIZE', 'minQty')
        max_qty = self._required_decimal(lot, 'LOT_SIZE', 'maxQty')
        if step <= 0 or min_qty <= 0 or max_qty <= 0:
            raise FilterDataUnavailable(
                'LOT_SIZE stepSize/minQty/maxQty must be positive')
        if min_qty > max_qty:
            raise FilterDataUnavailable('LOT_SIZE minQty exceeds maxQty')
        if 'NOTIONAL' in filters:
            notional_name = 'NOTIONAL'
            notional = self._required_filter(filters, notional_name)
            min_notional = self._required_decimal(notional, notional_name, 'minNotional')
            max_notional = self._required_decimal(notional, notional_name, 'maxNotional')
        elif 'MIN_NOTIONAL' in filters:
            notional_name = 'MIN_NOTIONAL'
            notional = self._required_filter(filters, notional_name)
            min_notional = self._required_decimal(notional, notional_name, 'minNotional')
            max_notional = Decimal('0')
        else:
            raise FilterDataUnavailable(
                f'required NOTIONAL or MIN_NOTIONAL metadata is missing for {symbol}')
        if max_notional > 0 and min_notional > max_notional:
            raise FilterDataUnavailable(
                f'{notional_name}.minNotional exceeds maxNotional')

        market_legs = [leg for leg in legs if leg.order_type.upper() == 'MARKET']
        market_lot = None
        market_step = market_min_qty = market_max_qty = Decimal('0')
        if market_legs:
            market_lot = self._required_filter(filters, 'MARKET_LOT_SIZE')
            market_step = self._required_decimal(
                market_lot, 'MARKET_LOT_SIZE', 'stepSize')
            market_min_qty = self._required_decimal(
                market_lot, 'MARKET_LOT_SIZE', 'minQty')
            market_max_qty = self._required_decimal(
                market_lot, 'MARKET_LOT_SIZE', 'maxQty')
        trailing = filters.get('TRAILING_DELTA') or {}
        pps = filters.get('PERCENT_PRICE_BY_SIDE')
        pp = filters.get('PERCENT_PRICE')

        percent_ranges: dict[str, tuple[Decimal, Decimal]] = {}
        if pps is not None:
            if not isinstance(pps, dict):
                raise FilterDataUnavailable('PERCENT_PRICE_BY_SIDE metadata is malformed')
            self._required_int(pps, 'PERCENT_PRICE_BY_SIDE', 'avgPriceMins')
            for side in {leg.side.upper() for leg in legs}:
                prefix = 'ask' if side == 'SELL' else 'bid'
                down = self._dec(pps, prefix + 'MultiplierDown')
                up = self._dec(pps, prefix + 'MultiplierUp')
                if down > up:
                    raise FilterDataUnavailable(
                        f'PERCENT_PRICE_BY_SIDE {prefix} multiplier range is inverted')
                percent_ranges[side] = (down, up)
        elif pp is not None:
            if not isinstance(pp, dict):
                raise FilterDataUnavailable('PERCENT_PRICE metadata is malformed')
            self._required_int(pp, 'PERCENT_PRICE', 'avgPriceMins')
            down = self._dec(pp, 'multiplierDown')
            up = self._dec(pp, 'multiplierUp')
            if down > up:
                raise FilterDataUnavailable('PERCENT_PRICE multiplier range is inverted')
            for side in {leg.side.upper() for leg in legs}:
                percent_ranges[side] = (down, up)

        trailing_ranges: dict[str, tuple[int, int]] = {}
        if any(leg.trailing_delta is not None for leg in legs):
            if not isinstance(trailing, dict) or not trailing:
                raise FilterDataUnavailable(
                    'required TRAILING_DELTA metadata is missing for trailing order')
            for suffix in ('Above', 'Below'):
                lo = self._required_int(
                    trailing, 'TRAILING_DELTA', f'minTrailing{suffix}Delta', minimum=1)
                hi = self._required_int(
                    trailing, 'TRAILING_DELTA', f'maxTrailing{suffix}Delta', minimum=1)
                if lo > hi:
                    raise FilterDataUnavailable(
                        f'TRAILING_DELTA {suffix.lower()} range is inverted')
                trailing_ranges[suffix] = (lo, hi)

        reference = None
        market_notional_applies = False
        if market_legs:
            if notional_name == 'NOTIONAL':
                apply_min_market = self._required_bool(
                    notional, 'NOTIONAL', 'applyMinToMarket')
                apply_max_market = self._required_bool(
                    notional, 'NOTIONAL', 'applyMaxToMarket')
                market_notional_applies = apply_min_market or apply_max_market
            else:
                apply_market = self._required_bool(
                    notional, 'MIN_NOTIONAL', 'applyToMarket')
                market_notional_applies = apply_market
        if pps or pp or market_notional_applies:
            reference = self._reference_price(symbol, filters)

        for leg in legs:
            prices = [p for p in (leg.price, leg.stop_price) if p is not None]
            for price in prices:
                mark('PRICE_FILTER')
                if price <= 0:
                    raise FilterViolation(f'{symbol} {leg.order_type} price {price} <= 0')
                if tick and price % tick != 0:
                    raise FilterViolation(f'{symbol} {leg.order_type} price {price} not a multiple of tickSize {tick}')
                if min_price and price < min_price:
                    raise FilterViolation(f'{symbol} {leg.order_type} price {price} below PRICE_FILTER.minPrice {min_price}')
                if max_price and price > max_price:
                    raise FilterViolation(f'{symbol} {leg.order_type} price {price} above PRICE_FILTER.maxPrice {max_price}')
                if reference is not None:
                    if pps:
                        mark('PERCENT_PRICE_BY_SIDE')
                        down, up = percent_ranges[leg.side.upper()]
                    else:
                        mark('PERCENT_PRICE')
                        down, up = percent_ranges[leg.side.upper()]
                    if price < reference * down:
                        raise FilterViolation(
                            f'{symbol} {leg.order_type} price {price} below percent-price band '
                            f'({reference} x {down}) — Binance would reject the replacement')
                    if price > reference * up:
                        raise FilterViolation(
                            f'{symbol} {leg.order_type} price {price} above percent-price band '
                            f'({reference} x {up}) — Binance would reject the replacement')
            mark('LOT_SIZE')
            if step > 0 and leg.qty % step != 0:
                raise FilterViolation(f'{symbol} qty {leg.qty} not a multiple of stepSize {step}')
            if min_qty > 0 and leg.qty < min_qty:
                raise FilterViolation(f'{symbol} qty {leg.qty} below LOT_SIZE.minQty {min_qty}')
            if max_qty > 0 and leg.qty > max_qty:
                raise FilterViolation(f'{symbol} qty {leg.qty} above LOT_SIZE.maxQty {max_qty}')

            if leg.order_type.upper() == 'MARKET':
                mark('MARKET_LOT_SIZE')
                if market_step > 0 and leg.qty % market_step != 0:
                    raise FilterViolation(
                        f'{symbol} MARKET qty {leg.qty} not a multiple of '
                        f'MARKET_LOT_SIZE.stepSize {market_step}')
                if market_min_qty > 0 and leg.qty < market_min_qty:
                    raise FilterViolation(
                        f'{symbol} MARKET qty {leg.qty} below '
                        f'MARKET_LOT_SIZE.minQty {market_min_qty}')
                if market_max_qty > 0 and leg.qty > market_max_qty:
                    raise FilterViolation(
                        f'{symbol} MARKET qty {leg.qty} above '
                        f'MARKET_LOT_SIZE.maxQty {market_max_qty}')

            notional_price = leg.price or leg.stop_price
            if notional_price is None and leg.order_type.upper() == 'MARKET' \
                    and market_notional_applies:
                notional_price = reference
            if notional_price is not None:
                mark(notional_name)
                apply_min = leg.order_type.upper() != 'MARKET' or (
                    notional['applyMinToMarket'] if notional_name == 'NOTIONAL'
                    else notional['applyToMarket'])
                apply_max = leg.order_type.upper() != 'MARKET' or (
                    notional['applyMaxToMarket'] if notional_name == 'NOTIONAL'
                    else False)
                if apply_min and min_notional > 0 and notional_price * leg.qty < min_notional:
                    raise FilterViolation(
                        f'{symbol} {leg.order_type} notional {notional_price * leg.qty} below minNotional {min_notional}')
                if apply_max and max_notional > 0 and notional_price * leg.qty > max_notional:
                    raise FilterViolation(
                        f'{symbol} {leg.order_type} notional {notional_price * leg.qty} above maxNotional {max_notional}')
            if leg.trailing_delta is not None:
                mark('TRAILING_DELTA')
                prefix = 'Below' if leg.side.upper() == 'SELL' else 'Above'
                lo, hi = trailing_ranges[prefix]
                if not lo <= leg.trailing_delta <= hi:
                    raise FilterViolation(
                        f'{symbol} trailingDelta {leg.trailing_delta} outside [{lo},{hi}]')

        # Order/list count capacity. The caller supplies authenticated
        # enumeration callables; a failed enumeration is fail-closed because a
        # capacity rejection after cancellation would leave the position naked.
        new_orders = len(legs)
        if open_orders_provider is not None:
            max_orders_filter = filters.get('MAX_NUM_ORDERS')
            max_algo_filter = filters.get('MAX_NUM_ALGO_ORDERS')
            limit = (self._required_int(
                self._required_filter(filters, 'MAX_NUM_ORDERS'),
                'MAX_NUM_ORDERS', 'maxNumOrders', minimum=1)
                if max_orders_filter is not None else 0)
            algo_limit = (self._required_int(
                self._required_filter(filters, 'MAX_NUM_ALGO_ORDERS'),
                'MAX_NUM_ALGO_ORDERS', 'maxNumAlgoOrders', minimum=1)
                if max_algo_filter is not None else 0)
            if limit or algo_limit:
                try:
                    open_orders = open_orders_provider(symbol)
                except Exception as exc:
                    raise FilterDataUnavailable(f'open-order enumeration failed for {symbol}: {exc}') from exc
                if not isinstance(open_orders, list) or any(
                        not isinstance(order, dict)
                        or order.get('symbol') != symbol
                        or isinstance(order.get('orderId'), bool)
                        or not isinstance(order.get('orderId'), int)
                        or order.get('orderId') <= 0
                        or not isinstance(order.get('type'), str)
                        for order in open_orders):
                    raise FilterDataUnavailable(
                        f'open-order enumeration returned malformed rows for {symbol}')
            if limit:
                mark('MAX_NUM_ORDERS')
                if len(open_orders) + new_orders > limit:
                    raise FilterViolation(
                        f'{symbol} would exceed MAX_NUM_ORDERS {limit} '
                        f'({len(open_orders)} open + {new_orders} new)')
            if algo_limit:
                mark('MAX_NUM_ALGO_ORDERS')
                open_algo = sum(1 for o in open_orders if str(o.get('type', '')).upper() in {
                    'STOP_LOSS', 'STOP_LOSS_LIMIT', 'TAKE_PROFIT', 'TAKE_PROFIT_LIMIT'})
                new_algo = sum(1 for leg in legs if leg.is_algo)
                if open_algo + new_algo > algo_limit:
                    raise FilterViolation(
                        f'{symbol} would exceed MAX_NUM_ALGO_ORDERS {algo_limit} '
                        f'({open_algo} open + {new_algo} new)')
        if endpoint.startswith('orderList/') and open_order_lists_provider is not None:
            list_filter = filters.get('MAX_NUM_ORDER_LISTS')
            limit = (self._required_int(
                self._required_filter(filters, 'MAX_NUM_ORDER_LISTS'),
                'MAX_NUM_ORDER_LISTS', 'maxNumOrderLists', minimum=1)
                if list_filter is not None else 0)
            if limit:
                mark('MAX_NUM_ORDER_LISTS')
                try:
                    open_lists = open_order_lists_provider()
                except Exception as exc:
                    raise FilterDataUnavailable(f'open order-list enumeration failed: {exc}') from exc
                if not isinstance(open_lists, list) or any(
                        not isinstance(order_list, dict)
                        or isinstance(order_list.get('orderListId'), bool)
                        or not isinstance(order_list.get('orderListId'), int)
                        or order_list.get('orderListId') <= 0
                        for order_list in open_lists):
                    raise FilterDataUnavailable(
                        'open order-list enumeration returned malformed rows')
                if len(open_lists) + 1 > limit:
                    raise FilterViolation(
                        f'account would exceed MAX_NUM_ORDER_LISTS {limit} ({len(open_lists)} open + 1 new)')
        summary = {'symbol': symbol, 'endpoint': endpoint, 'legs': len(legs),
                   'last_price': str(last_price) if last_price is not None else None,
                   'reference_price': str(reference) if reference is not None else None,
                   'filters_checked': checked}
        audit('replacement_filters_validated', details=summary)
        return summary
