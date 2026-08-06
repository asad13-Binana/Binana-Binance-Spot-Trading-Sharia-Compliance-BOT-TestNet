from __future__ import annotations
"""CoinGecko client for the active universe service (advisory data only).

Endpoints — both included in the free Demo plan (docs.coingecko.com):

  GET /api/v3/coins/markets    market cap, rank and 24h change per coin
                               (1 call credit per request/page)
  GET /api/v3/search/trending  top trending coins (1 call credit)

A Demo API key (free, https://www.coingecko.com/en/developers/dashboard) is
strongly recommended. Keyless access is IP-based and shared with every other
tenant on the same address — on Oracle Cloud that pool is busy, so the
enrichment layer clamps keyless usage to a tiny per-minute budget.

V102-REM-007 (audit ISSUE 7): rows carry CoinGecko's stable string ``id``,
and a ticker symbol that maps to more than one distinct id in the fetched
pages is AMBIGUOUS and excluded from the data set (returned separately for
observability). Ticker symbols are not globally unique; ambiguity must
never attach the wrong coin's data to a Binance asset.
"""
import logging

import requests

from services.universe_service.external_signals.httpguard import guarded_get_json

log = logging.getLogger('universe.external.coingecko')

BASE = 'https://api.coingecko.com/api/v3'
USER_AGENT = 'V10.2-universe-external/1.0'


class CoinGeckoClient:
    def __init__(self, api_key: str, budget, breaker, timeout: float,
                 markets_pages: int = 2, per_page: int = 250):
        self.api_key = (api_key or '').strip()
        self.budget = budget
        self.breaker = breaker
        self.timeout = float(timeout)
        self.markets_pages = max(1, min(int(markets_pages), 4))
        self.per_page = max(50, min(int(per_page), 250))
        self.session = requests.Session()
        self.session.headers['User-Agent'] = USER_AGENT

    def _headers(self) -> dict:
        # The Demo key header (x-cg-demo-api-key) is the documented
        # authentication for the free plan; keyless sends no auth header.
        return {'x-cg-demo-api-key': self.api_key} if self.api_key else {}

    def markets(self) -> tuple[dict[str, dict], list[str]] | None:
        """(UPPERCASE symbol -> market data, ambiguous symbols), or ``None``
        when nothing was fetched.

        Pages are ordered by market cap. The same coin id appearing twice
        (pagination overlap) is harmless and deduplicated; the same SYMBOL
        with two different coin ids is ambiguous and excluded entirely.
        """
        merged: dict[str, dict] = {}
        ambiguous: set[str] = set()
        got_any = False
        for page in range(1, self.markets_pages + 1):
            rows = guarded_get_json(
                self.session, BASE + '/coins/markets',
                params={'vs_currency': 'usd', 'order': 'market_cap_desc',
                        'per_page': self.per_page, 'page': page,
                        'sparkline': 'false'},
                headers=self._headers(), timeout=self.timeout,
                budget=self.budget, breaker=self.breaker)
            if not isinstance(rows, list):
                break
            got_any = True
            for row in rows:
                if not isinstance(row, dict):
                    continue
                symbol = str(row.get('symbol', '')).strip().upper()
                if not symbol:
                    continue
                coin_id = row.get('id')
                coin_id = str(coin_id) if isinstance(coin_id, str) and coin_id else None
                if symbol in merged:
                    if merged[symbol].get('coingecko_id') != coin_id:
                        ambiguous.add(symbol)
                    continue
                if symbol in ambiguous:
                    continue
                merged[symbol] = {
                    'coingecko_id': coin_id,
                    'market_cap_usd': row.get('market_cap'),
                    'market_cap_rank': row.get('market_cap_rank'),
                    'change_24h_pct': row.get('price_change_percentage_24h'),
                }
        if not got_any:
            return None
        for symbol in ambiguous:
            merged.pop(symbol, None)
        if ambiguous:
            log.info('CoinGecko: %d ambiguous ticker symbol(s) excluded: %s',
                     len(ambiguous), sorted(ambiguous)[:10])
        return merged, sorted(ambiguous)

    def trending(self) -> list[str] | None:
        """Trending coin symbols (uppercase, CoinGecko order), or ``None``."""
        payload = guarded_get_json(
            self.session, BASE + '/search/trending', params=None,
            headers=self._headers(), timeout=self.timeout,
            budget=self.budget, breaker=self.breaker)
        if not isinstance(payload, dict):
            return None
        symbols: list[str] = []
        for item in payload.get('coins') or []:
            coin = item.get('item') if isinstance(item, dict) else None
            if not isinstance(coin, dict):
                continue
            symbol = str(coin.get('symbol', '')).strip().upper()
            if symbol and symbol not in symbols:
                symbols.append(symbol)
        return symbols
