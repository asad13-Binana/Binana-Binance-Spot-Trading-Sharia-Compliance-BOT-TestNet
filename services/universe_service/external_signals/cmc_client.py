from __future__ import annotations
"""CoinMarketCap Basic-plan client for the active universe service.

Only Basic-plan endpoints are used (coinmarketcap.com/api/pricing):

  GET /v1/cryptocurrency/listings/latest  1 credit per started batch of
                                          200 rows; "trending" is derived
                                          from the top of a
                                          percent_change_24h-sorted listing
                                          (the legacy trending endpoint is
                                          NOT in the Basic plan)
  GET /v1/key/info                        authoritative monthly credit usage
                                          for reconciliation

V102-REM-003 (audit ISSUE 3): the monthly ledger is credit-authoritative.
Every listings response's ``status.credit_count`` is compared with the
pre-reserved estimate and any excess is booked immediately; missing or
malformed credit metadata keeps the conservative estimate. ``key_info()``
lets the orchestrator reconcile the local ledger with the provider's own
figure at a low, interval-limited frequency.

V102-REM-007 (audit ISSUE 7): rows carry the provider's stable numeric
``cmc_id``, and a ticker symbol that maps to more than one distinct CMC id
in the fetched listing is treated as AMBIGUOUS and excluded from the data
set entirely (returned separately for observability).
"""
import logging
import math

import requests

from services.universe_service.external_signals.httpguard import guarded_get_json

log = logging.getLogger('universe.external.cmc')

BASE = 'https://pro-api.coinmarketcap.com'
USER_AGENT = 'V10.2-universe-external/1.0'
ROWS_PER_CREDIT = 200


class CoinMarketCapClient:
    def __init__(self, api_key: str, budget, breaker, timeout: float,
                 listing_limit: int = 200):
        self.api_key = (api_key or '').strip()
        self.budget = budget
        self.breaker = breaker
        self.timeout = float(timeout)
        self.listing_limit = max(1, min(int(listing_limit), 500))
        self.session = requests.Session()
        self.session.headers['User-Agent'] = USER_AGENT

    def _headers(self) -> dict:
        return {'X-CMC_PRO_API_KEY': self.api_key, 'Accept': 'application/json'}

    def credit_cost(self) -> int:
        return math.ceil(self.listing_limit / ROWS_PER_CREDIT)

    def _book_actual_credits(self, payload: dict, estimate: int) -> None:
        """Reconcile the reservation with the provider's actual charge.

        Only ever books MORE than the estimate. A lower, missing, negative,
        or malformed ``credit_count`` keeps the conservative estimate — the
        provider may have charged even for a failed or odd response.
        """
        status = payload.get('status')
        actual = status.get('credit_count') if isinstance(status, dict) else None
        if isinstance(actual, bool) or not isinstance(actual, int):
            return
        if actual > estimate:
            self.budget.record_extra(actual - estimate)
            log.info('CMC charged %d credits (estimated %d); booked the excess',
                     actual, estimate)

    def listings(self) -> tuple[dict[str, dict], list[str]] | None:
        """(UPPERCASE symbol -> data, ambiguous symbols) or ``None``.

        Data comes from a 24h-momentum-sorted listing; ``momentum_rank`` is
        the coin's position in it (1 = strongest 24h gainer market-wide).
        Symbols mapping to multiple distinct CMC ids are excluded from the
        data dict and reported in the ambiguous list instead.
        """
        if not self.api_key:
            return None
        estimate = self.credit_cost()
        payload = guarded_get_json(
            self.session, BASE + '/v1/cryptocurrency/listings/latest',
            params={'limit': self.listing_limit, 'sort': 'percent_change_24h',
                    'sort_dir': 'desc', 'convert': 'USD'},
            headers=self._headers(),
            timeout=self.timeout, budget=self.budget, breaker=self.breaker,
            cost=estimate)
        if not isinstance(payload, dict):
            return None
        self._book_actual_credits(payload, estimate)
        status = payload.get('status') or {}
        error_code = status.get('error_code')
        if error_code not in (0, None):
            # HTTP 200 with an in-band error (CMC does this for some plan and
            # key problems). Treat it as a provider failure.
            self.breaker.record_failure()
            log.warning('CMC error %s: %s', error_code, status.get('error_message'))
            return None
        out: dict[str, dict] = {}
        ambiguous: set[str] = set()
        for position, row in enumerate(payload.get('data') or [], 1):
            if not isinstance(row, dict):
                continue
            symbol = str(row.get('symbol', '')).strip().upper()
            if not symbol:
                continue
            cmc_id = row.get('id')
            if isinstance(cmc_id, bool) or not isinstance(cmc_id, int):
                cmc_id = None
            if symbol in out:
                if out[symbol].get('cmc_id') != cmc_id:
                    ambiguous.add(symbol)
                continue
            if symbol in ambiguous:
                continue
            quote = (row.get('quote') or {}).get('USD') or {}
            out[symbol] = {
                'cmc_id': cmc_id,
                'cmc_rank': row.get('cmc_rank'),
                'market_cap_usd': quote.get('market_cap'),
                'change_24h_pct': quote.get('percent_change_24h'),
                'volume_24h_usd': quote.get('volume_24h'),
                'momentum_rank': position,
            }
        for symbol in ambiguous:
            out.pop(symbol, None)
        if ambiguous:
            log.info('CMC: %d ambiguous ticker symbol(s) excluded: %s',
                     len(ambiguous), sorted(ambiguous)[:10])
        return out, sorted(ambiguous)

    def key_info_month_credits_used(self) -> int | None:
        """Authoritative credits used this month from ``/v1/key/info``.

        Deliberately NOT budget-gated: reconciliation is the sanctioned
        recovery path out of a quarantined budget, so it cannot depend on
        that budget. The orchestrator interval-limits it and books its cost
        after the fact. Returns ``None`` when unavailable or malformed.
        """
        if not self.api_key:
            return None
        # V102-REM-014 (deep-audit HIGH): the reconciliation call bypasses the
        # budget gate because it is the recovery path OUT of a quarantined
        # budget (it cannot depend on the thing it repairs). But when the
        # budget is healthy and already at its monthly cap, there is no reason
        # to spend an extra credit probing usage — skip it. When quarantined,
        # always probe: that is the only way back to a trusted ledger.
        stats = self.budget.stats()
        if not stats.get('quarantined') and stats.get('month_used', 0) >= stats.get('month_cap', 0):
            return None
        payload = guarded_get_json(
            self.session, BASE + '/v1/key/info', params=None,
            headers=self._headers(), timeout=self.timeout,
            budget=None, breaker=self.breaker, cost=1)
        # Conservative: assume the call itself cost a credit even on failure.
        self.budget.record_extra(1)
        if not isinstance(payload, dict):
            return None
        status = payload.get('status') or {}
        if status.get('error_code') not in (0, None):
            self.breaker.record_failure()
            log.warning('CMC key/info error %s: %s', status.get('error_code'),
                        status.get('error_message'))
            return None
        usage = (((payload.get('data') or {}).get('usage') or {})
                 .get('current_month') or {})
        used = usage.get('credits_used')
        if isinstance(used, bool) or not isinstance(used, int) or used < 0:
            return None
        return used
