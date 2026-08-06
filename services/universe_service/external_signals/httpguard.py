from __future__ import annotations
"""Budget- and breaker-gated HTTP GET shared by the external API clients.

Every outbound call to CoinGecko or CoinMarketCap flows through
``guarded_get_json``. It reserves quota BEFORE sending (both providers count
failed requests toward quota), maps every failure class to a breaker action,
and never raises or blocks — the caller receives parsed JSON or ``None``.

``budget=None`` is reserved for the CMC ``/v1/key/info`` reconciliation
call: reconciliation is the sanctioned recovery path out of a quarantined
budget, so it cannot itself be budget-gated. It is instead interval-limited
by the enrichment orchestrator and its credit cost is booked after the fact
(V102-REM-003).
"""
import logging

import requests

log = logging.getLogger('universe.external')

# Never trust an arbitrarily large (or hostile) Retry-After header.
RETRY_AFTER_MAX_SECONDS = 900.0
RETRY_AFTER_DEFAULT_SECONDS = 60.0
# 401/402/403 mean a key, plan, or permission problem. Retrying cannot fix a
# configuration error, so back the provider off hard instead of burning quota.
AUTH_FAILURE_COOLDOWN_SECONDS = 21600.0


def _retry_after_seconds(response) -> float:
    raw = response.headers.get('Retry-After', '')
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return RETRY_AFTER_DEFAULT_SECONDS
    return min(max(value, 1.0), RETRY_AFTER_MAX_SECONDS)


def guarded_get_json(session, url, *, params, headers, timeout, budget,
                     breaker, cost: int = 1):
    """One fail-safe GET. Returns parsed JSON or ``None``; never raises."""
    if not breaker.allows():
        return None
    if budget is not None and not budget.try_acquire(cost):
        log.warning('%s budget exhausted — skipping %s', budget.name, url)
        return None
    try:
        response = session.get(url, params=params, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        breaker.record_failure()
        log.warning('%s request failed: %s',
                    budget.name if budget is not None else breaker.name, exc)
        return None
    name = budget.name if budget is not None else breaker.name
    if response.status_code == 429:
        wait = _retry_after_seconds(response)
        breaker.record_failure(cooldown_override=wait)
        log.warning('%s rate limited (429); backing off %.0fs', name, wait)
        return None
    if response.status_code in (401, 402, 403):
        breaker.record_failure(cooldown_override=AUTH_FAILURE_COOLDOWN_SECONDS)
        log.warning('%s auth/plan error HTTP %d — provider disabled for %.0fs; '
                    'check the API key and plan', name,
                    response.status_code, AUTH_FAILURE_COOLDOWN_SECONDS)
        return None
    if response.status_code >= 400:
        breaker.record_failure()
        log.warning('%s returned HTTP %d for %s', name, response.status_code, url)
        return None
    try:
        payload = response.json()
    except ValueError:
        breaker.record_failure()
        log.warning('%s returned a non-JSON payload for %s', name, url)
        return None
    breaker.record_success()
    return payload
