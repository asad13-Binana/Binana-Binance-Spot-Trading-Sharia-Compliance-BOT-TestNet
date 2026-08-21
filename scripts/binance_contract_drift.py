#!/usr/bin/env python3
"""Read-only Binance Spot contract drift check.

This scheduled/manual check never authenticates and never places orders. It
flags a newly active MAX_POSITION filter on a tradeable Spot/USDT symbol so the
account-aware balance/open-BUY implementation can be reviewed before LIVE
promotion. It also flags unknown symbol-filter types for manual contract review.
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

EXCHANGE_BASE_URLS = {
    'testnet': 'https://testnet.binance.vision',
    'live': 'https://api.binance.com',
}
KNOWN_SYMBOL_FILTERS = {
    'PRICE_FILTER', 'PERCENT_PRICE', 'PERCENT_PRICE_BY_SIDE',
    'LOT_SIZE', 'MIN_NOTIONAL', 'NOTIONAL', 'ICEBERG_PARTS',
    'MARKET_LOT_SIZE', 'MAX_NUM_ORDERS', 'MAX_NUM_ALGO_ORDERS',
    'MAX_NUM_ICEBERG_ORDERS', 'MAX_POSITION', 'TRAILING_DELTA',
    'MAX_NUM_ORDER_AMENDS', 'MAX_NUM_ORDER_LISTS',
}


class ContractDriftError(RuntimeError):
    """Current Binance metadata requires a deliberate compatibility review."""


def repository_mode(mode_file: Path | None = None) -> str:
    path = mode_file or Path(__file__).resolve().parents[1] / 'RELEASE_MODE'
    try:
        mode = path.read_text(encoding='utf-8').strip().lower()
    except OSError as exc:
        raise ContractDriftError(f'cannot read release mode: {exc}') from exc
    if mode not in EXCHANGE_BASE_URLS:
        raise ContractDriftError(f'unsupported release mode {mode!r}')
    return mode


def exchange_info_url(mode: str) -> str:
    try:
        base_url = EXCHANGE_BASE_URLS[mode]
    except KeyError as exc:
        raise ContractDriftError(f'unsupported release mode {mode!r}') from exc
    return (
        f'{base_url}/api/v3/exchangeInfo'
        '?symbolStatus=TRADING&showPermissionSets=false'
    )


def inspect_exchange_info(payload: dict) -> dict:
    if not isinstance(payload, dict) or not isinstance(payload.get('symbols'), list):
        raise ContractDriftError('exchangeInfo symbols array is missing or malformed')
    reviewed = 0
    seen_filters: set[str] = set()
    max_position_symbols: list[str] = []
    unknown: dict[str, list[str]] = {}
    for row in payload['symbols']:
        if not isinstance(row, dict):
            raise ContractDriftError('exchangeInfo contains a non-object symbol row')
        if (
            row.get('status') != 'TRADING'
            or row.get('isSpotTradingAllowed') is not True
            or row.get('quoteAsset') != 'USDT'
        ):
            continue
        symbol = row.get('symbol')
        filters = row.get('filters')
        if not isinstance(symbol, str) or not symbol or not isinstance(filters, list):
            raise ContractDriftError('tradeable Spot/USDT symbol metadata is malformed')
        reviewed += 1
        names: set[str] = set()
        for item in filters:
            if not isinstance(item, dict):
                raise ContractDriftError(f'{symbol} contains a non-object filter')
            name = item.get('filterType')
            if not isinstance(name, str) or not name:
                raise ContractDriftError(f'{symbol} contains a filter without filterType')
            names.add(name)
            seen_filters.add(name)
        if 'MAX_POSITION' in names:
            max_position_symbols.append(symbol)
        unsupported = sorted(names - KNOWN_SYMBOL_FILTERS)
        if unsupported:
            unknown[symbol] = unsupported
    if reviewed == 0:
        raise ContractDriftError('no tradeable Spot/USDT symbols were returned')
    if max_position_symbols:
        raise ContractDriftError(
            'MAX_POSITION is active on tradeable Spot/USDT symbols: '
            + ', '.join(sorted(max_position_symbols)))
    if unknown:
        details = '; '.join(
            f'{symbol}={"|".join(names)}'
            for symbol, names in sorted(unknown.items()))
        raise ContractDriftError('unknown Binance symbol filters require review: ' + details)
    return {
        'tradeable_spot_usdt_symbols': reviewed,
        'filter_types': sorted(seen_filters),
        'max_position_active': False,
    }


def fetch_exchange_info(url: str) -> dict:
    request = urllib.request.Request(
        url,
        headers={'User-Agent': 'binana-binance-contract-drift/1.0'})
    with urllib.request.urlopen(request, timeout=30) as response:
        if response.status != 200:
            raise ContractDriftError(
                f'exchangeInfo returned unexpected HTTP {response.status}')
        return json.loads(response.read().decode('utf-8'))


def main() -> int:
    try:
        mode = repository_mode()
        result = inspect_exchange_info(
            fetch_exchange_info(exchange_info_url(mode)))
        result['environment'] = mode
    except (ContractDriftError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(f'BINANCE_CONTRACT_REVIEW_REQUIRED: {exc}', file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
