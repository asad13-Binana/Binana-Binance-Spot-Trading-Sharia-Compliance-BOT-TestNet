"""Validate persisted/public first-candle evidence before applying listing age."""
from __future__ import annotations

import logging
import time
from pathlib import Path

from services.common.atomic import atomic_write_json, read_json

log = logging.getLogger('universe')


class ListingAgeCache:
    """Unknown age is rejected; malformed cache data can be fetched again."""

    def __init__(self, path: Path, api):
        self.path = path
        self.api = api
        data = read_json(path, {})
        self.data = data if isinstance(data, dict) else {}

    def first_trade_ms(self, symbol: str) -> int | None:
        cached = self.data.get(symbol)
        # JSON booleans are Python ints, but are not Binance timestamps.
        if type(cached) is int and cached > 0:
            return cached
        try:
            rows = self.api.get('/api/v3/klines', {
                'symbol': symbol, 'interval': '1d', 'startTime': 0, 'limit': 1})
            first = rows[0][0] if isinstance(rows, list) and rows and isinstance(rows[0], list) and rows[0] else None
            if type(first) is int and first > 0:
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
