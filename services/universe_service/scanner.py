from __future__ import annotations
import hashlib, json, math, os, re, time, logging, requests
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from services.common.audit import audit
from services.common.atomic import atomic_write_json, read_json
from services.universe_service.sharia_filter import ShariaFilter
from services.universe_service.ranker import rank
from services.universe_service.snapshot_store import store
from services.universe_service.external_signals import ExternalSignals

log = logging.getLogger('universe')
BASE = os.getenv('BINANCE_PUBLIC_BASE', 'https://api.binance.com')
ROOT = Path(os.getenv('UNIVERSE_ROOT', '/app/shared/universe'))
SHARIA = Path(os.getenv('SHARIA_FILE', '/app/shared/sharia/sharia_status.json'))
LEGACY_HALAL = Path(os.getenv('LEGACY_HALAL_FILE', '/app/shared/sharia/halal_coins.json'))
REFRESH = int(os.getenv('UNIVERSE_REFRESH_SECONDS', '900'))
LIMIT = int(os.getenv('UNIVERSE_LIMIT', '50'))
MIN_AGE = int(os.getenv('MIN_LISTING_AGE_DAYS', '30'))
MIN_VOL = Decimal(os.getenv('MIN_QUOTE_VOLUME_USDT', '1000000'))
MAX_SPREAD = Decimal(os.getenv('MAX_SPREAD_RATIO', '0.005'))
TIMEOUT = float(os.getenv('HTTP_TIMEOUT_SECONDS', '15'))
STABLES = {x.strip().upper() for x in os.getenv('STABLECOINS', 'USDC,FDUSD,TUSD,USDP,DAI,EUR,AEUR,BUSD').split(',') if x.strip()}
EXCLUDED = {'BTC', 'BNB'}
LEV_SUFFIX = ('UP', 'DOWN', 'BULL', 'BEAR')



def validate_runtime_settings():
    errors = []
    if not 1 <= LIMIT <= 50:
        errors.append('UNIVERSE_LIMIT must be within 1–50')
    if REFRESH < 60:
        errors.append('UNIVERSE_REFRESH_SECONDS must be at least 60')
    if MIN_AGE < 0:
        errors.append('MIN_LISTING_AGE_DAYS must be non-negative')
    if not isinstance(MIN_VOL, Decimal) or not MIN_VOL.is_finite() or MIN_VOL <= 0:
        errors.append('MIN_QUOTE_VOLUME_USDT must be positive')
    if (
        not isinstance(MAX_SPREAD, Decimal)
        or not MAX_SPREAD.is_finite()
        or not Decimal('0') < MAX_SPREAD < Decimal('1')
    ):
        errors.append('MAX_SPREAD_RATIO must be within (0, 1)')
    if not math.isfinite(TIMEOUT) or not 1 <= TIMEOUT <= 120:
        errors.append('HTTP_TIMEOUT_SECONDS must be finite and within 1–120')
    if errors:
        raise ValueError('; '.join(errors))

class BinancePublic:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers['User-Agent'] = 'V8.1-universe/1.0'

    def get(self, path, params=None):
        r = self.s.get(BASE + path, params=params, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()

class ListingAgeCache:
    """Persistent first-candle cache. Unknown age is rejected, never guessed."""
    def __init__(self, path: Path, api: BinancePublic):
        self.path = path
        self.api = api
        self.data = read_json(path, {}) or {}

    def first_trade_ms(self, symbol: str) -> int | None:
        cached = self.data.get(symbol)
        if isinstance(cached, int) and cached > 0:
            return cached
        try:
            rows = self.api.get('/api/v3/klines', {'symbol': symbol, 'interval': '1d', 'startTime': 0, 'limit': 1})
            first = int(rows[0][0]) if rows else 0
            if first > 0:
                self.data[symbol] = first
                atomic_write_json(self.path, self.data)
                time.sleep(0.03)
                return first
        except Exception as exc:
            log.warning('listing-age lookup failed for %s: %s', symbol, exc)
        return None

    def age_days(self, symbol: str, now_ms: int) -> int | None:
        first = self.first_trade_ms(symbol)
        return int((now_ms - first) / 86_400_000) if first else None

def _basic_filter(symbol_info):
    raw_symbol = symbol_info.get('symbol')
    raw_base = symbol_info.get('baseAsset')
    raw_quote = symbol_info.get('quoteAsset')
    symbol = raw_symbol if isinstance(raw_symbol, str) else ''
    base = raw_base.upper() if isinstance(raw_base, str) else ''
    quote = raw_quote if isinstance(raw_quote, str) else ''
    reasons = []
    if (
        not symbol
        or not base
        or re.fullmatch(r'[A-Z0-9]+', base) is None
        or raw_base != base
        or quote != 'USDT'
        or symbol != base + 'USDT'
    ):
        reasons.append('symbol_identity_mismatch')
    if symbol_info.get('status') != 'TRADING': reasons.append('not_trading')
    if symbol_info.get('isSpotTradingAllowed', False) is not True: reasons.append('spot_not_allowed')
    if quote != 'USDT': reasons.append('not_usdt_quote')
    if base in EXCLUDED: reasons.append('excluded_base')
    if base in STABLES: reasons.append('stablecoin')
    if any(base.endswith(s) for s in LEV_SUFFIX): reasons.append('leveraged_token')
    if symbol_info.get('ocoAllowed') is not True: reasons.append('oco_not_allowed')
    if symbol_info.get('otoAllowed') is not True: reasons.append('oto_not_allowed')
    if symbol_info.get('allowTrailingStop') is not True: reasons.append('trailing_not_allowed')
    return symbol, base, reasons


def _canonical_row(row: dict) -> str:
    return json.dumps(row, sort_keys=True, separators=(',', ':'), default=str)


def _exchange_rows(payload: object) -> list[dict]:
    if not isinstance(payload, dict):
        raise ValueError('Binance exchangeInfo response must be an object')
    rows = payload.get('symbols')
    if not isinstance(rows, list):
        raise ValueError('Binance exchangeInfo.symbols must be a list')
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError('Binance exchangeInfo.symbols entries must be objects')
    return sorted(rows, key=_canonical_row)


def _indexed_public_rows(payload: object, endpoint: str) -> dict[str, dict]:
    if not isinstance(payload, list):
        raise ValueError(f'Binance {endpoint} response must be a list')
    indexed: dict[str, dict] = {}
    skipped: list[str] = []
    for row in payload:
        if not isinstance(row, dict):
            raise ValueError(f'Binance {endpoint} entries must be objects')
        symbol = row.get('symbol')
        if not isinstance(symbol, str) or not symbol:
            raise ValueError(f'Binance {endpoint} entry has invalid symbol identity')
        # UNIVERSE-EXOTIC-001: Binance really does list symbols whose base asset
        # is not [A-Z0-9] -- for example the live Spot pair whose base is four
        # Han characters. Such a symbol can never be a candidate: _basic_filter
        # already rejects a non-[A-Z0-9] base, and only *USDT pairs are eligible.
        # Treating it as a corrupt RESPONSE, however, aborted the entire scan and
        # the universe container never reached health. A symbol outside the
        # tradeable identity shape is not malformed data, it is simply not a
        # candidate, so skip it here exactly as the candidate filter does.
        # Structural defects below and above stay fatal.
        if symbol != symbol.strip().upper() or re.fullmatch(r'[A-Z0-9]+', symbol) is None:
            skipped.append(symbol)
            continue
        if symbol in indexed:
            raise ValueError(f'Binance {endpoint} contains duplicate symbol {symbol}')
        indexed[symbol] = row
    if skipped:
        log.info('Binance %s: skipped %d symbol(s) outside the tradeable '
                 'identity shape (never eligible): %s',
                 endpoint, len(skipped), ', '.join(sorted(skipped)[:5]))
    return indexed


def _duplicate_identities(rows: list[dict]) -> tuple[set[str], set[str]]:
    candidate_identities = []
    for row in rows:
        symbol = row.get('symbol')
        base = row.get('baseAsset')
        if (
            isinstance(symbol, str)
            and isinstance(base, str)
            and row.get('quoteAsset') == 'USDT'
            and base == base.strip().upper()
            and re.fullmatch(r'[A-Z0-9]+', base) is not None
            and symbol == base + 'USDT'
        ):
            candidate_identities.append((symbol, base))
    symbols = [symbol for symbol, _base in candidate_identities]
    bases = [base for _symbol, base in candidate_identities]
    symbol_counts, base_counts = Counter(symbols), Counter(bases)
    return (
        {symbol for symbol, count in symbol_counts.items() if count > 1},
        {base for base, count in base_counts.items() if count > 1},
    )


def _finite_decimal(row: dict, field: str) -> Decimal | None:
    raw = row.get(field)
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not value.is_finite():
        return None
    try:
        if not math.isfinite(float(value)):
            return None
    except (OverflowError, ValueError):
        return None
    return value


def _filter_map(info: dict) -> tuple[dict[str, dict], list[str]]:
    raw_filters = info.get('filters')
    if not isinstance(raw_filters, list):
        return {}, ['malformed_filters']
    grouped: dict[str, list[dict]] = {}
    reasons: list[str] = []
    for item in raw_filters:
        if not isinstance(item, dict):
            reasons.append('malformed_filters')
            continue
        filter_type = item.get('filterType')
        if (
            not isinstance(filter_type, str)
            or not filter_type
            or filter_type != filter_type.strip().upper()
        ):
            reasons.append('malformed_filters')
            continue
        grouped.setdefault(filter_type, []).append(item)
    mapped: dict[str, dict] = {}
    for filter_type, items in grouped.items():
        ordered = sorted(items, key=_canonical_row)
        mapped[filter_type] = ordered[0]
        if len(ordered) > 1:
            reasons.append('duplicate_filter_type')
    return mapped, reasons


def _validated_filters(info: dict) -> tuple[dict, list[str]]:
    filters, reasons = _filter_map(info)
    notional = filters.get('NOTIONAL')
    if notional is None:
        notional = filters.get('MIN_NOTIONAL')
    price_filter = filters.get('PRICE_FILTER')
    lot_filter = filters.get('LOT_SIZE')
    trailing_filter = filters.get('TRAILING_DELTA')

    if notional is None:
        reasons.append('missing_notional_filter')
    elif (value := _finite_decimal(notional, 'minNotional')) is None or value <= 0:
        reasons.append('invalid_notional_filter')
    if price_filter is None:
        reasons.append('missing_tick_size')
    elif (value := _finite_decimal(price_filter, 'tickSize')) is None or value <= 0:
        reasons.append('invalid_tick_size')
    if lot_filter is None:
        reasons.append('missing_step_size')
    elif (value := _finite_decimal(lot_filter, 'stepSize')) is None or value <= 0:
        reasons.append('invalid_step_size')

    trailing_fields = (
        'minTrailingAboveDelta', 'maxTrailingAboveDelta',
        'minTrailingBelowDelta', 'maxTrailingBelowDelta',
    )
    trailing_values: dict[str, Decimal] = {}
    if trailing_filter is None:
        reasons.append('missing_trailing_delta_filter')
    else:
        for field in trailing_fields:
            value = _finite_decimal(trailing_filter, field)
            if value is None or value <= 0 or value != value.to_integral_value():
                reasons.append('invalid_trailing_delta_filter')
                break
            trailing_values[field] = value
        if len(trailing_values) == len(trailing_fields) and (
            trailing_values['minTrailingAboveDelta'] > trailing_values['maxTrailingAboveDelta']
            or trailing_values['minTrailingBelowDelta'] > trailing_values['maxTrailingBelowDelta']
        ):
            reasons.append('invalid_trailing_delta_filter')

    normalized = {
        'tick_size': price_filter.get('tickSize') if price_filter else None,
        'step_size': lot_filter.get('stepSize') if lot_filter else None,
        'min_notional': notional.get('minNotional') if notional else None,
        'trailing_delta': dict(trailing_filter) if trailing_filter else None,
    }
    return normalized, sorted(set(reasons))


def _deduplicate_rejections(rows: list[dict]) -> list[dict]:
    merged: dict[tuple[str, str], dict] = {}
    for row in sorted(rows, key=_canonical_row):
        key = (str(row.get('base') or ''), str(row.get('symbol') or ''))
        if key not in merged:
            merged[key] = dict(row)
            merged[key]['reasons'] = sorted(set(row.get('reasons') or []))
        else:
            merged[key]['reasons'] = sorted(set(
                merged[key]['reasons'] + list(row.get('reasons') or [])
            ))
    return sorted(merged.values(), key=lambda row: (
        str(row.get('base') or ''), str(row.get('symbol') or ''),
        tuple(row.get('reasons') or []),
    ))

def scan_once():
    validate_runtime_settings()
    api = BinancePublic()
    sf = ShariaFilter(SHARIA)
    try:
        sf.sync_legacy_compat(LEGACY_HALAL)
    except OSError as exc:
        # V102-FIX-001: this container mounts /app/shared/sharia read-only in
        # production; the sharia-screener owns the legacy projection there.
        # Writing stays best-effort for dev/simulation hosts with rw mounts.
        log.debug('legacy halal projection not writable here (%s); '
                  'sharia-screener owns it in production', exc)
    ext = ExternalSignals.from_env(ROOT)
    ext.refresh_if_stale()
    age_cache = ListingAgeCache(ROOT / 'listing_age_cache.json', api)
    exchange_rows = _exchange_rows(
        api.get('/api/v3/exchangeInfo', {'showPermissionSets': 'false'}))
    tickers = _indexed_public_rows(api.get('/api/v3/ticker/24hr'), 'ticker/24hr')
    books = _indexed_public_rows(api.get('/api/v3/ticker/bookTicker'), 'ticker/bookTicker')
    duplicate_symbols, duplicate_bases = _duplicate_identities(exchange_rows)
    accepted, rejected = [], []
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    for info in exchange_rows:
        symbol, base, reasons = _basic_filter(info)
        if symbol.upper() in duplicate_symbols:
            reasons.append('duplicate_symbol')
        if base in duplicate_bases:
            reasons.append('duplicate_base')
        # Required order: market eligibility -> explicit Sharia -> age -> liquidity/spread -> filters.
        dec = sf.decision(base)
        if not dec.allowed:
            reasons.append('sharia_' + dec.status.lower())

        listing_age_days = None
        if not reasons:
            listing_age_days = age_cache.age_days(symbol, now_ms)
            if listing_age_days is None: reasons.append('listing_age_unknown')
            elif listing_age_days < MIN_AGE: reasons.append('listing_too_new')

        ticker = tickers.get(symbol)
        change = vol = None
        if ticker is None:
            reasons.append('missing_24h_ticker')
        else:
            change = _finite_decimal(ticker, 'priceChangePercent')
            vol = _finite_decimal(ticker, 'quoteVolume')
            if change is None:
                reasons.append('invalid_price_change')
            elif change <= 0:
                reasons.append('not_positive_gainer')
            if vol is None:
                reasons.append('invalid_quote_volume')
            elif vol < MIN_VOL:
                reasons.append('low_volume')

        book = books.get(symbol)
        spread = None
        if book is None:
            reasons.append('missing_book_ticker')
        else:
            bid = _finite_decimal(book, 'bidPrice')
            ask = _finite_decimal(book, 'askPrice')
            if bid is None or ask is None or bid <= 0 or ask <= 0 or bid > ask:
                reasons.append('invalid_book_prices')
            else:
                spread = (ask - bid) / ask
                if spread > MAX_SPREAD:
                    reasons.append('wide_spread')

        exchange_filters, filter_reasons = _validated_filters(info)
        reasons.extend(filter_reasons)

        # Advisory CoinGecko/CMC enrichment. Fail-open by design: an unknown
        # coin or provider outage never rejects; only an explicitly enabled
        # market-cap floor with KNOWN below-floor data can add a reason.
        ext_reason = ext.reject_reason(base)
        if ext_reason: reasons.append(ext_reason)
        ext_data = ext.enrich(base)

        detail = {
            'symbol': symbol, 'base': base, 'listing_age_days': listing_age_days,
            'change_pct': float(change) if change is not None else None,
            'quote_volume': float(vol) if vol is not None else None,
            'spread_ratio': float(spread) if spread is not None else None,
            'sharia_status': dec.status, 'reasons': sorted(set(reasons))
        }
        if ext_data:
            detail['external_signals'] = ext_data
        if reasons:
            rejected.append(detail)
            continue
        row = {
            'symbol': symbol, 'pair': f'{base}/USDT', 'base': base,
            'listing_age_days': listing_age_days, 'change_pct': float(change),
            'quote_volume': float(vol), 'spread_ratio': float(spread),
            'sharia_status': dec.status, 'sharia_source': dec.record.get('source'),
            'exchange_filters': exchange_filters,
            'rejection_reasons': []
        }
        if ext_data:
            row['external_signals'] = ext_data
        accepted.append(row)

    rejected = _deduplicate_rejections(rejected)
    rows = rank(accepted, LIMIT)
    config = {
        'limit': LIMIT, 'min_listing_age_days': MIN_AGE,
        'min_quote_volume': str(MIN_VOL), 'max_spread_ratio': str(MAX_SPREAD),
        'excluded': sorted(EXCLUDED), 'stablecoins': sorted(STABLES),
        'sharia_policy': 'explicit-current-HALAL-only', 'unknown_listing_age_policy': 'reject',
        'sharia_dataset_sha256': hashlib.sha256(SHARIA.read_bytes()).hexdigest(),
        'selection_health_policy': 'zero-fail-closed-positive-shortfall-degraded',
        'external_signals': ext.config_summary(),
    }
    snap = store(ROOT, rows, config, REFRESH)
    atomic_write_json(ROOT / 'latest_rejections.json', {'generated_at': snap['generated_at'], 'rejected': rejected})
    ext.write_status()
    audit('universe_snapshot', details={
        'count': len(rows), 'hash': snap['configuration_hash'],
        'rejected': len(rejected), 'selection': snap['selection'],
    })
    return snap

def main():
    logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO'), format='%(asctime)s %(levelname)s %(name)s %(message)s')
    once = os.getenv('RUN_ONCE', 'false').lower() == 'true'
    while True:
        try:
            snapshot = scan_once()
            log.info('published %d eligible pairs', len(snapshot['pairs']))
        except Exception as exc:
            log.exception('universe scan failed')
            audit('universe_scan_failed', severity='ERROR', details={'error': str(exc)})
        if once:
            return
        time.sleep(REFRESH)

if __name__ == '__main__':
    main()
