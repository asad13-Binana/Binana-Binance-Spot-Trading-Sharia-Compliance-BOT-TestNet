from __future__ import annotations
"""Public (unauthenticated) Binance Spot market-data client.

Fixes M-004: the universe scanner previously used a bare requests wrapper with
generic error handling; a 429/418 was indistinguishable from any other failure
and Retry-After was ignored. This client:

  * honors Retry-After on 429 (rate limit) and 418 (IP ban) with bounded,
    jittered backoff and a persistent in-process ban-until latch;
  * retries 5xx/network faults a bounded number of times;
  * never retries 4xx client errors other than 429/418;
  * exposes helpers for the exact reference data the services need.

No authenticated endpoint is ever called from this module and no order can be
placed through it.
"""
import logging
import os
import random
import time

import requests

from services.common.audit import audit
from services.common.config_bounds import env_float, env_int

log = logging.getLogger('binance-public')

DEFAULT_BASE = 'https://api.binance.com'


class BinancePublicError(RuntimeError):
    pass


class BinanceRateLimitError(BinancePublicError):
    def __init__(self, status: int, retry_after: float):
        super().__init__(f'Binance rate limit (HTTP {status}); retry after {retry_after:.0f}s')
        self.status = status
        self.retry_after = retry_after


class BinancePublicClient:
    def __init__(self, base: str | None = None, *, timeout: float | None = None,
                 max_attempts: int | None = None, user_agent: str = 'binance-freqtrade-v101/1.0'):
        self.base = (base or os.getenv('BINANCE_PUBLIC_BASE', DEFAULT_BASE)).rstrip('/')
        self.timeout = timeout if timeout is not None else env_float('HTTP_TIMEOUT_SECONDS', 15, 1, 120)
        self.max_attempts = max_attempts if max_attempts is not None else env_int('PUBLIC_HTTP_MAX_ATTEMPTS', 4, 1, 10)
        self.session = requests.Session()
        self.session.headers['User-Agent'] = user_agent
        self._banned_until = 0.0

    @staticmethod
    def _retry_after_seconds(response, default: float, maximum: float) -> float:
        try:
            value = float(response.headers.get('Retry-After', default))
        except Exception:
            value = default
        return max(1.0, min(value, maximum))

    def get(self, path: str, params: dict | None = None):
        remaining_ban = self._banned_until - time.time()
        if remaining_ban > 0:
            raise BinanceRateLimitError(418, remaining_ban)
        last_error: Exception | None = None
        for attempt in range(self.max_attempts):
            try:
                response = self.session.get(self.base + path, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                last_error = exc
            else:
                status = response.status_code
                if status == 418:
                    retry_after = self._retry_after_seconds(response, 3600, 259_200)
                    self._banned_until = time.time() + retry_after
                    audit('binance_public_ip_ban', severity='CRITICAL',
                          details={'path': path, 'retry_after': retry_after})
                    raise BinanceRateLimitError(418, retry_after)
                if status == 429:
                    retry_after = self._retry_after_seconds(response, 2, 120)
                    audit('binance_public_rate_limited', severity='WARNING',
                          details={'path': path, 'retry_after': retry_after, 'attempt': attempt + 1})
                    if attempt == self.max_attempts - 1:
                        raise BinanceRateLimitError(429, retry_after)
                    time.sleep(retry_after + random.uniform(0, 0.5))
                    continue
                if 500 <= status < 600:
                    last_error = BinancePublicError(f'HTTP {status} from {path}')
                else:
                    response.raise_for_status()
                    return response.json()
            if attempt == self.max_attempts - 1:
                break
            backoff = min(2.0 * (2 ** attempt), 30.0) + random.uniform(0, 0.5)
            log.warning('public GET %s attempt %d failed (%s); backoff %.1fs',
                        path, attempt + 1, last_error, backoff)
            time.sleep(backoff)
        raise BinancePublicError(f'public GET {path} failed after {self.max_attempts} attempts: {last_error}')

    # ---- reference-data helpers ----
    def exchange_info(self, symbol: str | None = None) -> dict:
        params = {'symbol': symbol} if symbol else {'showPermissionSets': 'false'}
        return self.get('/api/v3/exchangeInfo', params)

    def ticker_24h(self) -> list:
        return self.get('/api/v3/ticker/24hr')

    def book_ticker(self) -> list:
        return self.get('/api/v3/ticker/bookTicker')

    def ticker_price(self, symbol: str) -> dict:
        return self.get('/api/v3/ticker/price', {'symbol': symbol})

    # Sentinel returned by reference_price() when the symbol has no reference
    # price (referencePrice null, or error -2043). Per the Binance 2026-05-06
    # change, the affected filters then fall back to their previous behavior.
    NO_REFERENCE_PRICE = object()

    def reference_price(self, symbol: str):
        """Current Binance Spot reference price for the symbol (F23E-001).

        Binance added GET /api/v3/referencePrice (2026-03-09) and, from
        2026-05-06, PERCENT_PRICE / PERCENT_PRICE_BY_SIDE / MIN_NOTIONAL /
        NOTIONAL use it when it exists and is non-null, falling back to the
        previous avg/last-price behavior only when it is null or unset.

        Returns:
          * a price string when a non-null reference price exists;
          * NO_REFERENCE_PRICE when referencePrice is null OR the symbol has
            never had one (documented error -2043) -> caller falls back;
          * raises BinancePublicError / BinanceRateLimitError on any other
            failure (timeout, 5xx, rate limit, malformed body) so the caller
            can fail closed instead of validating against a wrong band.
        """
        try:
            data = self.get('/api/v3/referencePrice', {'symbol': symbol})
        except requests.HTTPError as exc:
            # -2043 ("symbol doesn't have a reference price") arrives as a 4xx
            # body; it is a documented FALLBACK condition, not an error state.
            resp = getattr(exc, 'response', None)
            if resp is not None:
                try:
                    body = resp.json()
                except ValueError:
                    raise BinancePublicError(
                        f'referencePrice for {symbol} returned a non-JSON error body') from exc
                if isinstance(body, dict) and body.get('code') == -2043:
                    return self.NO_REFERENCE_PRICE
            raise BinancePublicError(f'referencePrice for {symbol} failed: {exc}') from exc
        if not isinstance(data, dict):
            raise BinancePublicError(f'referencePrice for {symbol} returned {type(data).__name__}')
        # A body may also carry an in-band error code on HTTP 200 for some hosts.
        if data.get('code') == -2043:
            return self.NO_REFERENCE_PRICE
        if 'referencePrice' not in data:
            raise BinancePublicError(f'referencePrice for {symbol} response lacks referencePrice')
        ref = data.get('referencePrice')
        if ref is None:
            return self.NO_REFERENCE_PRICE
        return ref

    def spot_usdt_trading_symbols(self) -> dict:
        """Current TRADING, spot-allowed, USDT-quoted symbols: base -> info."""
        info = self.exchange_info()
        out: dict[str, dict] = {}
        for entry in info.get('symbols', []):
            if entry.get('status') != 'TRADING':
                continue
            if not entry.get('isSpotTradingAllowed', False):
                continue
            if str(entry.get('quoteAsset', '')).upper() != 'USDT':
                continue
            out[str(entry.get('baseAsset', '')).upper()] = entry
        return out
