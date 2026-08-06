from __future__ import annotations
"""Env-controlled CoinGecko + CoinMarketCap enrichment for the universe scan.

Design contract (V10.2-EXT-001, remediated per the 2026-07-23 independent
audit as V102-REM-002/003/005/006/007):

- ADVISORY ONLY. External data annotates universe rows and may optionally
  exclude coins below a market-cap floor. It never reorders the
  Binance-derived ranking and never adds coins to the universe.
- FAIL-SAFE. Any provider error, quota exhaustion, or stale cache degrades
  to Binance-only scanning. No exception escapes into ``scan_once()``.
- BAN-SAFE. Every call flows through persisted ``ApiBudget`` caps. The HARD
  ceilings are 4% UNDER the documented free tiers — configuration can lower
  the budgets but can NEVER reach the providers' full quotas (audit ISSUE
  2). Minute windows, breaker cool-downs, and monthly counters all survive
  restarts; corrupted ledgers fail closed.
- CREDIT-TRUE. CMC accounting books the provider's actual
  ``status.credit_count`` when it exceeds the estimate, and reconciles with
  ``/v1/key/info`` at a low, interval-limited frequency (audit ISSUE 3).
- IDENTITY-SAFE. Provider rows carry stable provider ids; cached ambiguous
  ticker symbols are enforced again at consumption. The optional market-cap
  floor fires ONLY when a trusted caller supplies a verified canonical
  Binance-base <-> CoinGecko-id <-> CMC-id binding and both independently
  report the bound asset below the floor. Provider-cache ids and ticker text
  alone are advisory and can never hard-reject a pair (audit ISSUE 7/P45-GH-006).
- The byte-preserved legacy core is untouched; this package re-implements
  its CoinGecko/CMC signals for the active service.

Documented free-tier quotas (fetched 2026-07-22, see docs/EXTERNAL_SIGNALS.md):

  CoinGecko Demo   100 calls/min, 10,000 call credits/month
                   docs.coingecko.com + coingecko.com/en/api/pricing
  CoinMarketCap    50 requests/min, 15,000 call credits/month (Basic)
                   coinmarketcap.com/api/pricing

Operator requirement: budgets run 4% BELOW quota, permanently.
"""
import logging
import math
import os
import time
from pathlib import Path

from services.common.atomic import atomic_write_json, read_json
from services.common.audit import audit
from services.universe_service.external_signals.breaker import CircuitBreaker
from services.universe_service.external_signals.budget import ApiBudget
from services.universe_service.external_signals.cmc_client import CoinMarketCapClient
from services.universe_service.external_signals.coingecko_client import CoinGeckoClient
from services.universe_service.external_signals.writer_lock import (
    acquire_writer_lease, locking_backend,
)

log = logging.getLogger('universe.external')

# Documented free-tier quotas (reference only — never used as clamp ceilings).
COINGECKO_FREE_PER_MINUTE = 100
COINGECKO_FREE_MONTHLY = 10_000
CMC_FREE_PER_MINUTE = 50
CMC_FREE_MONTHLY = 15_000

QUOTA_SAFETY = 0.96  # operator requirement: budgets 4% under the free quota

# HARD safety ceilings (audit ISSUE 2): env overrides are clamped to THESE,
# so no configuration path can reach 100% of a provider quota. They are also
# the defaults.
COINGECKO_SAFE_PER_MINUTE = int(COINGECKO_FREE_PER_MINUTE * QUOTA_SAFETY)  # 96
COINGECKO_SAFE_MONTHLY = int(COINGECKO_FREE_MONTHLY * QUOTA_SAFETY)        # 9600
CMC_SAFE_PER_MINUTE = int(CMC_FREE_PER_MINUTE * QUOTA_SAFETY)              # 48
CMC_SAFE_MONTHLY = int(CMC_FREE_MONTHLY * QUOTA_SAFETY)                    # 14400

# Backwards-compatible aliases (tests and docs referenced these names).
DEFAULT_COINGECKO_PER_MINUTE = COINGECKO_SAFE_PER_MINUTE
DEFAULT_COINGECKO_MONTHLY = COINGECKO_SAFE_MONTHLY
DEFAULT_CMC_PER_MINUTE = CMC_SAFE_PER_MINUTE
DEFAULT_CMC_MONTHLY = CMC_SAFE_MONTHLY

# Keyless public access is IP-based and shared with other tenants on the same
# address (undocumented allowance). Oracle egress IPs are busy pools: stay tiny.
COINGECKO_KEYLESS_PER_MINUTE = 5

MIN_REFRESH_SECONDS = 300
MAX_REFRESH_SECONDS = 86_400
# Data older than this many refresh intervals is discarded entirely
# (annotation and the optional market-cap floor both stop using it).
STALE_INTERVALS = 4

MIN_RECONCILE_SECONDS = 3_600
MAX_RECONCILE_SECONDS = 604_800

STATE_DIRNAME = 'external'
STATUS_FILENAME = 'external_signals.json'


def _env_bool(name: str, default: str = 'false') -> bool:
    return os.getenv(name, default).strip().lower() == 'true'


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, '').strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f'{name} must be an integer, got {raw!r}') from exc


def _clamped_budget(name: str, requested: int, safe_ceiling: int) -> int:
    """Clamp a configured budget to the 96%-of-free-tier safety ceiling.

    Zero/negative values are configuration errors (fail loudly); values
    above the ceiling are clamped and logged — never honored.
    """
    if requested < 1:
        raise ValueError(f'{name} must be >= 1')
    if requested > safe_ceiling:
        log.warning('%s=%d exceeds the safety ceiling %d (96%% of the '
                    'documented free tier); clamping. The 4%% reserve is '
                    'permanent and cannot be configured away.',
                    name, requested, safe_ceiling)
        return safe_ceiling
    return requested


class ExternalSignals:
    """Cached, budgeted, fail-safe CoinGecko + CMC enrichment."""

    def __init__(self, root: str | Path, *, coingecko_enabled: bool,
                 cmc_enabled: bool, coingecko_api_key: str, cmc_api_key: str,
                 refresh_seconds: int, min_market_cap_usd: int,
                 cmc_trending_limit: int, cg_per_minute: int, cg_monthly: int,
                 cmc_per_minute: int, cmc_monthly: int,
                 breaker_cooldown_seconds: int, http_timeout: float,
                 cmc_reconcile_seconds: int = 21_600,
                 configured_budgets: dict | None = None,
                 verified_identity_bindings: dict | None = None):
        self.root = Path(root)
        self.state_dir = self.root / STATE_DIRNAME
        self.coingecko_enabled = bool(coingecko_enabled)
        self.coingecko_api_key = (coingecko_api_key or '').strip()
        self.cmc_api_key = (cmc_api_key or '').strip()
        self.cmc_requested = bool(cmc_enabled)
        # A missing optional key must never take the scanner down: CMC simply
        # stays off (and the status file says why) until a key is supplied.
        self.cmc_enabled = self.cmc_requested and bool(self.cmc_api_key)
        if self.cmc_requested and not self.cmc_enabled:
            log.warning('ENABLE_CMC_TRENDING=true but no COINMARKETCAP_API_KEY/'
                        'CMC_API_KEY is set; CMC enrichment stays disabled')
        if self.coingecko_enabled and not self.coingecko_api_key:
            log.info('CoinGecko enrichment is keyless; per-minute budget is '
                     'clamped to %d. A free Demo key raises it — '
                     'https://www.coingecko.com/en/developers/dashboard',
                     COINGECKO_KEYLESS_PER_MINUTE)
        self.refresh_seconds = int(refresh_seconds)
        self.min_market_cap_usd = int(min_market_cap_usd)
        self.cmc_trending_limit = int(cmc_trending_limit)
        self.http_timeout = float(http_timeout)
        self.breaker_cooldown_seconds = int(breaker_cooldown_seconds)
        self.cmc_reconcile_seconds = int(cmc_reconcile_seconds)
        self._configured = dict(configured_budgets or {})
        # Trusted boundary: these bindings must be verified by a canonical
        # Binance/provider identity resolver before construction. They are
        # deliberately never loaded from env or the writable provider cache.
        self._verified_identity_bindings = self._normalize_identity_bindings(
            verified_identity_bindings
        )
        # A-004: a real cross-process single-writer lease. If another process
        # already owns the quota state, disable enrichment for THIS process
        # (fail closed to Binance-only) instead of racing the ledger.
        self.writer_lease_held = True
        if self.coingecko_enabled or self.cmc_enabled:
            self.writer_lease_held = acquire_writer_lease(self.state_dir / '.writer.lock')
            if not self.writer_lease_held:
                log.critical('another process already holds the external-signals '
                             'writer lease (%s); disabling enrichment in this '
                             'process to protect the free-tier quota. Running more '
                             'than one universe service against the same shared '
                             'volume is unsupported.', self.state_dir)
                try:
                    audit('external_signals_writer_conflict', severity='CRITICAL',
                          details={'state_dir': str(self.state_dir),
                                   'locking_backend': locking_backend()})
                except Exception:
                    pass
                self.coingecko_enabled = False
                self.cmc_enabled = False
        self._cg_budget = ApiBudget(
            'CoinGecko', self.state_dir / 'coingecko_budget.json',
            per_minute=cg_per_minute, per_month=cg_monthly)
        self._cmc_budget = ApiBudget(
            'CMC', self.state_dir / 'cmc_budget.json',
            per_minute=cmc_per_minute, per_month=cmc_monthly)
        self._cg_breaker = CircuitBreaker(
            'CoinGecko', cooldown_seconds=self.breaker_cooldown_seconds,
            state_path=self.state_dir / 'coingecko_breaker.json')
        self._cmc_breaker = CircuitBreaker(
            'CMC', cooldown_seconds=self.breaker_cooldown_seconds,
            state_path=self.state_dir / 'cmc_breaker.json')
        self._cache = read_json(self.state_dir / 'cache.json', {}) or {}
        if not isinstance(self._cache, dict):
            self._cache = {}

    # ── construction ────────────────────────────────────────────────────

    @classmethod
    def from_env(
        cls,
        root: str | Path,
        *,
        verified_identity_bindings: dict | None = None,
    ) -> 'ExternalSignals':
        """Build from environment. Raises ValueError on invalid settings so
        misconfiguration fails loudly at scan time (same policy as
        ``validate_runtime_settings``)."""
        errors: list[str] = []
        refresh = _env_int('EXTERNAL_SIGNALS_REFRESH_SECONDS', 1800)
        if not MIN_REFRESH_SECONDS <= refresh <= MAX_REFRESH_SECONDS:
            errors.append('EXTERNAL_SIGNALS_REFRESH_SECONDS must be within '
                          f'{MIN_REFRESH_SECONDS}-{MAX_REFRESH_SECONDS}')
        min_cap = _env_int('EXTERNAL_MIN_MARKET_CAP_USD', 0)
        if min_cap < 0:
            errors.append('EXTERNAL_MIN_MARKET_CAP_USD must be >= 0')
        trending_limit = _env_int('CMC_TRENDING_LIMIT', 20)
        if not 1 <= trending_limit <= 100:
            errors.append('CMC_TRENDING_LIMIT must be within 1-100')
        cooldown = _env_int('EXTERNAL_BREAKER_COOLDOWN_SECONDS', 900)
        if not 60 <= cooldown <= 86_400:
            errors.append('EXTERNAL_BREAKER_COOLDOWN_SECONDS must be within 60-86400')
        reconcile = _env_int('CMC_RECONCILE_SECONDS', 21_600)
        if not MIN_RECONCILE_SECONDS <= reconcile <= MAX_RECONCILE_SECONDS:
            errors.append('CMC_RECONCILE_SECONDS must be within '
                          f'{MIN_RECONCILE_SECONDS}-{MAX_RECONCILE_SECONDS}')
        try:
            timeout = float(os.getenv('HTTP_TIMEOUT_SECONDS', '15'))
        except ValueError:
            timeout = 0.0
        if not 1 <= timeout <= 120:
            errors.append('HTTP_TIMEOUT_SECONDS must be within 1-120')
        if errors:
            raise ValueError('; '.join(errors))

        cg_key = os.getenv('COINGECKO_API_KEY', '').strip()
        # Accept both spellings, exactly like the preserved core (audit M-04).
        cmc_key = (os.getenv('COINMARKETCAP_API_KEY') or
                   os.getenv('CMC_API_KEY') or '').strip()
        configured = {
            'coingecko_per_minute': _env_int('COINGECKO_PER_MINUTE_LIMIT',
                                             COINGECKO_SAFE_PER_MINUTE),
            'coingecko_monthly': _env_int('COINGECKO_MONTHLY_LIMIT',
                                          COINGECKO_SAFE_MONTHLY),
            'cmc_per_minute': _env_int('CMC_PER_MINUTE_LIMIT',
                                       CMC_SAFE_PER_MINUTE),
            'cmc_monthly': _env_int('CMC_MONTHLY_LIMIT', CMC_SAFE_MONTHLY),
        }
        cg_per_minute = _clamped_budget(
            'COINGECKO_PER_MINUTE_LIMIT', configured['coingecko_per_minute'],
            COINGECKO_SAFE_PER_MINUTE)
        if not cg_key:
            cg_per_minute = min(cg_per_minute, COINGECKO_KEYLESS_PER_MINUTE)
        cg_monthly = _clamped_budget(
            'COINGECKO_MONTHLY_LIMIT', configured['coingecko_monthly'],
            COINGECKO_SAFE_MONTHLY)
        cmc_per_minute = _clamped_budget(
            'CMC_PER_MINUTE_LIMIT', configured['cmc_per_minute'],
            CMC_SAFE_PER_MINUTE)
        cmc_monthly = _clamped_budget(
            'CMC_MONTHLY_LIMIT', configured['cmc_monthly'], CMC_SAFE_MONTHLY)
        return cls(
            root,
            coingecko_enabled=_env_bool('ENABLE_COINGECKO_SIGNALS'),
            cmc_enabled=_env_bool('ENABLE_CMC_TRENDING'),
            coingecko_api_key=cg_key, cmc_api_key=cmc_key,
            refresh_seconds=refresh, min_market_cap_usd=min_cap,
            cmc_trending_limit=trending_limit,
            cg_per_minute=cg_per_minute, cg_monthly=cg_monthly,
            cmc_per_minute=cmc_per_minute, cmc_monthly=cmc_monthly,
            breaker_cooldown_seconds=cooldown, http_timeout=timeout,
            cmc_reconcile_seconds=reconcile, configured_budgets=configured,
            verified_identity_bindings=verified_identity_bindings)

    # ── refresh ─────────────────────────────────────────────────────────

    @staticmethod
    def _normalize_identity_bindings(bindings: object) -> dict[str, dict]:
        """Retain only complete, explicitly verified canonical bindings.

        Cache or environment content never reaches this method. Invalid or
        partial input is ignored so the market-cap floor fails open.
        """
        if not isinstance(bindings, dict):
            return {}
        normalized: dict[str, dict] = {}
        for key, value in bindings.items():
            if not isinstance(value, dict) or value.get('verified') is not True:
                continue
            base = str(key or '').strip().upper()
            bound_base = str(value.get('binance_base') or '').strip().upper()
            cg_id = value.get('coingecko_id')
            cmc_id = value.get('cmc_id')
            if (
                not base
                or bound_base != base
                or not isinstance(cg_id, str)
                or not cg_id.strip()
                or isinstance(cmc_id, bool)
                or not isinstance(cmc_id, int)
                or cmc_id <= 0
            ):
                continue
            normalized[base] = {
                'binance_base': base,
                'coingecko_id': cg_id.strip(),
                'cmc_id': cmc_id,
                'verified': True,
            }
        return normalized

    @staticmethod
    def _timestamp_age(fetched_at: object) -> float | None:
        if (
            isinstance(fetched_at, bool)
            or not isinstance(fetched_at, (int, float))
        ):
            return None
        value = float(fetched_at)
        if not math.isfinite(value) or value <= 0:
            return None
        now = time.time()
        # A future timestamp used to become age zero and could suppress
        # refresh indefinitely after clock/cache corruption. Reject any
        # future value; locally written timestamps are always <= this read.
        if value > now:
            return None
        return now - value

    @staticmethod
    def _cached_ambiguous(entry: dict) -> set[str] | None:
        """Normalize a cache ambiguity list; malformed content fails closed."""
        raw = entry.get('ambiguous', [])
        if raw is None:
            raw = []
        if not isinstance(raw, (list, tuple, set)):
            return None
        return {
            str(symbol).strip().upper()
            for symbol in raw
            if str(symbol).strip()
        }

    def _provider_age(
        self, provider: str, component: str | None = None
    ) -> float | None:
        entry = self._cache.get(provider)
        if not isinstance(entry, dict):
            return None
        key = 'fetched_at'
        if provider == 'coingecko' and component in {'markets', 'trending'}:
            key = f'{component}_fetched_at'
            # Backward-compatible read of the old shared-clock schema. Once
            # either component refreshes, both legacy values are migrated to
            # explicit clocks before the shared field is removed.
            fetched_at = entry.get(key) if key in entry else entry.get('fetched_at')
        else:
            fetched_at = entry.get(key)
        return self._timestamp_age(fetched_at)

    def _provider_data(
        self, provider: str, component: str | None = None
    ) -> dict | None:
        """Provider cache entry, or ``None`` once it exceeds the stale window."""
        age = self._provider_age(provider, component)
        if age is None or age > self.refresh_seconds * STALE_INTERVALS:
            return None
        entry = self._cache.get(provider)
        return entry if isinstance(entry, dict) else None

    def refresh_if_stale(self) -> None:
        """Fetch providers whose cache is older than the TTL. Never raises;
        a failed fetch keeps serving the previous data until the stale
        window closes, then the provider drops out entirely."""
        try:
            if self.coingecko_enabled:
                self._refresh_coingecko()
            if self.cmc_enabled:
                self._refresh_cmc()
        except Exception as exc:  # defense in depth: enrichment must not stop scans
            log.exception('external signal refresh failed unexpectedly')
            try:
                audit('external_signals_refresh_failed', severity='WARNING',
                      details={'error': str(exc)})
            except Exception:
                pass

    def _refresh_coingecko(self) -> None:
        markets_age = self._provider_age('coingecko', 'markets')
        trending_age = self._provider_age('coingecko', 'trending')
        refresh_markets = markets_age is None or markets_age >= self.refresh_seconds
        refresh_trending = trending_age is None or trending_age >= self.refresh_seconds
        if not (refresh_markets or refresh_trending):
            return
        client = CoinGeckoClient(self.coingecko_api_key, self._cg_budget,
                                 self._cg_breaker, self.http_timeout)
        markets_result = client.markets() if refresh_markets else None
        trending_result = client.trending() if refresh_trending else None
        markets_succeeded = refresh_markets and markets_result is not None
        trending_succeeded = refresh_trending and trending_result is not None
        if not (markets_succeeded or trending_succeeded):
            log.warning('CoinGecko refresh produced no data; keeping previous cache')
            audit('external_signals_refresh_failed', severity='WARNING',
                  details={'provider': 'coingecko',
                           'components': [
                               name for name, requested in (
                                   ('markets', refresh_markets),
                                   ('trending', refresh_trending),
                               ) if requested
                           ],
                           'breaker': self._cg_breaker.state(),
                           'budget': self._cg_budget.stats()})
            return
        previous = self._cache.get('coingecko')
        entry = dict(previous) if isinstance(previous, dict) else {}
        # Migrate an old shared timestamp to two explicit component clocks
        # before changing either. This preserves each component's real age;
        # a success from one endpoint can never rejuvenate the other's bytes.
        legacy_fetched_at = entry.get('fetched_at')
        if 'markets_fetched_at' not in entry and legacy_fetched_at is not None:
            entry['markets_fetched_at'] = legacy_fetched_at
        if 'trending_fetched_at' not in entry and legacy_fetched_at is not None:
            entry['trending_fetched_at'] = legacy_fetched_at
        entry.pop('fetched_at', None)
        now = time.time()
        if markets_succeeded:
            markets, ambiguous = markets_result
            entry['markets'] = markets
            entry['ambiguous'] = list(ambiguous)
            entry['markets_fetched_at'] = now
        if trending_succeeded:
            entry['trending'] = list(trending_result)
            entry['trending_fetched_at'] = now
        ambiguous = set(entry.get('ambiguous') or [])
        # Store a clean view and enforce this list again at consumption so a
        # stale, restored, or manually injected cache cannot bypass it.
        entry['trending'] = [
            symbol for symbol in (entry.get('trending') or [])
            if symbol not in ambiguous
        ]
        self._cache['coingecko'] = entry
        self._write_cache()
        failed = [
            name for name, requested, succeeded in (
                ('markets', refresh_markets, markets_succeeded),
                ('trending', refresh_trending, trending_succeeded),
            ) if requested and not succeeded
        ]
        if failed:
            log.warning('CoinGecko component refresh failed for %s; retained '
                        'the previous component timestamp/data', failed)
            audit('external_signals_refresh_failed', severity='WARNING',
                  details={'provider': 'coingecko', 'components': failed,
                           'breaker': self._cg_breaker.state(),
                           'budget': self._cg_budget.stats()})
        markets = entry.get('markets') or {}
        trending = entry.get('trending') or []
        log.info('CoinGecko refresh: %d market rows, %d trending symbols, '
                 '%d ambiguous excluded', len(markets), len(trending),
                 len(ambiguous))

    def _refresh_cmc(self) -> None:
        age = self._provider_age('cmc')
        if age is not None and age < self.refresh_seconds:
            return
        client = CoinMarketCapClient(self.cmc_api_key, self._cmc_budget,
                                     self._cmc_breaker, self.http_timeout)
        listings_result = client.listings()
        if listings_result is None:
            log.warning('CMC refresh produced no data; keeping previous cache')
            audit('external_signals_refresh_failed', severity='WARNING',
                  details={'provider': 'cmc',
                           'breaker': self._cmc_breaker.state(),
                           'budget': self._cmc_budget.stats()})
            self._maybe_reconcile_cmc(client)  # recovery path stays reachable
            return
        listings, ambiguous = listings_result
        self._cache['cmc'] = {'fetched_at': time.time(), 'listings': listings,
                              'ambiguous': list(ambiguous)}
        self._write_cache()
        log.info('CMC refresh: %d listing rows, %d ambiguous excluded',
                 len(listings), len(ambiguous))
        self._maybe_reconcile_cmc(client)

    def _reconcile_state_path(self) -> Path:
        return self.state_dir / 'cmc_reconcile.json'

    def _maybe_reconcile_cmc(self, client: CoinMarketCapClient) -> None:
        """Interval-limited authoritative usage reconciliation (ISSUE 3).

        Runs no more than once per CMC_RECONCILE_SECONDS. This is also the
        sanctioned recovery from a quarantined CMC budget, so it must run
        even when the budget itself refuses new spending.
        """
        try:
            state = read_json(self._reconcile_state_path(), {}) or {}
            last = state.get('last_reconcile_epoch')
            now = time.time()
            if isinstance(last, (int, float)) and 0 < now - float(last) < self.cmc_reconcile_seconds:
                return
            used = client.key_info_month_credits_used()
            atomic_write_json(self._reconcile_state_path(), {
                'last_reconcile_epoch': now,
                'provider_month_credits_used': used,
            })
            if used is None:
                log.info('CMC key/info reconciliation unavailable this cycle')
                return
            effective = self._cmc_budget.reconcile_month(used)
            log.info('CMC monthly usage reconciled: provider=%d local=%s',
                     used, effective)
        except Exception:
            log.exception('CMC usage reconciliation failed')

    def _write_cache(self) -> None:
        atomic_write_json(self.state_dir / 'cache.json', self._cache)

    # ── consumption (used by scan_once) ─────────────────────────────────

    def enrich(self, base: str) -> dict:
        """Advisory annotation for one base asset. ``{}`` when unavailable.

        Rows carry the stable provider ids and are matched by ticker symbol
        with ambiguous symbols already excluded; the annotation is advisory
        metadata, never a trade decision.
        """
        try:
            base = str(base or '').strip().upper()
            if not base:
                return {}
            out: dict = {}
            if self.coingecko_enabled:
                raw_entry = self._cache.get('coingecko')
                raw_entry = raw_entry if isinstance(raw_entry, dict) else {}
                ambiguous = self._cached_ambiguous(raw_entry)
                if ambiguous is not None and base not in ambiguous:
                    markets_entry = self._provider_data('coingecko', 'markets')
                    trending_entry = self._provider_data('coingecko', 'trending')
                    row = ((markets_entry.get('markets') or {}).get(base)
                           if markets_entry else None)
                    trending = bool(
                        trending_entry
                        and base in (trending_entry.get('trending') or [])
                    )
                    if row or trending:
                        cg: dict = {'trending': trending}
                        if isinstance(row, dict):
                            cg.update(row)
                        out['coingecko'] = cg
            if self.cmc_enabled:
                entry = self._provider_data('cmc')
                if entry:
                    ambiguous = self._cached_ambiguous(entry)
                    row = ((entry.get('listings') or {}).get(base)
                           if ambiguous is not None and base not in ambiguous
                           else None)
                    if isinstance(row, dict):
                        momentum = row.get('momentum_rank')
                        out['cmc'] = dict(row, trending=bool(
                            isinstance(momentum, int)
                            and momentum <= self.cmc_trending_limit))
            return out
        except Exception:
            log.exception('external enrichment failed for %s', base)
            return {}

    @staticmethod
    def _known_cap(row: object) -> float | None:
        if not isinstance(row, dict):
            return None
        cap = row.get('market_cap_usd')
        if isinstance(cap, bool) or not isinstance(cap, (int, float)) or cap <= 0:
            return None
        return float(cap)

    def reject_reason(self, base: str) -> str | None:
        """Optional market-cap floor — cross-verified and fail-open.

        Fires ONLY when every one of these holds (audit ISSUE 7: ticker
        symbols are not unique, so symbol-only single-provider data must
        never hard-reject a tradeable pair):

        - the floor is enabled and BOTH providers are enabled;
        - both providers have fresh, unambiguous data for the base;
        - a trusted caller supplied a verified canonical binding from that
          Binance base to the exact CoinGecko and CMC ids in those rows;
        - BOTH providers independently report a known cap below the floor.

        Anything else — unknown coin, one provider missing, ambiguity,
        provider conflict, stale cache — never rejects. The hard gates
        (Sharia, liquidity, spread, listing age) already ran.
        """
        try:
            if self.min_market_cap_usd <= 0:
                return None
            if not (self.coingecko_enabled and self.cmc_enabled):
                return None
            base = str(base or '').strip().upper()
            cg_entry = self._provider_data('coingecko', 'markets')
            cmc_entry = self._provider_data('cmc')
            if not cg_entry or not cmc_entry:
                return None
            cg_ambiguous = self._cached_ambiguous(cg_entry)
            cmc_ambiguous = self._cached_ambiguous(cmc_entry)
            if cg_ambiguous is None or cmc_ambiguous is None:
                return None
            if base in cg_ambiguous or base in cmc_ambiguous:
                return None
            cg_row = (cg_entry.get('markets') or {}).get(base)
            cmc_row = (cmc_entry.get('listings') or {}).get(base)
            if not isinstance(cg_row, dict) or not isinstance(cmc_row, dict):
                return None
            cg_id = cg_row.get('coingecko_id')
            cmc_id = cmc_row.get('cmc_id')
            if (
                not isinstance(cg_id, str)
                or not cg_id
                or isinstance(cmc_id, bool)
                or not isinstance(cmc_id, int)
                or cmc_id <= 0
            ):
                return None
            binding = self._verified_identity_bindings.get(base)
            if (
                not binding
                or binding.get('binance_base') != base
                or binding.get('coingecko_id') != cg_id
                or binding.get('cmc_id') != cmc_id
            ):
                return None
            cg_cap = self._known_cap(cg_row)
            cmc_cap = self._known_cap(cmc_row)
            if cg_cap is None or cmc_cap is None:
                return None
            if cg_cap < self.min_market_cap_usd and cmc_cap < self.min_market_cap_usd:
                return 'below_min_market_cap'
            return None
        except Exception:
            log.exception('market-cap floor check failed for %s', base)
            return None

    # ── observability ───────────────────────────────────────────────────

    def config_summary(self) -> dict:
        """Stable summary for the snapshot configuration (hash-relevant)."""
        return {
            'coingecko_enabled': self.coingecko_enabled,
            'cmc_trending_enabled': self.cmc_enabled,
            'min_market_cap_usd': self.min_market_cap_usd,
            'market_cap_floor_policy': (
                'verified-canonical-identity-and-both-providers-below-floor-only'
            ),
            'verified_identity_binding_count': len(
                self._verified_identity_bindings
            ),
            'refresh_seconds': self.refresh_seconds,
            'cmc_trending_limit': self.cmc_trending_limit,
            'quota_policy': '96pct-of-documented-free-tier-hard-ceiling',
            'role': 'advisory-annotation-and-optional-cap-floor-only',
        }

    def status_snapshot(self) -> dict:
        cg_markets_age = self._provider_age('coingecko', 'markets')
        cg_trending_age = self._provider_age('coingecko', 'trending')
        cg_age = (
            max(cg_markets_age, cg_trending_age)
            if cg_markets_age is not None and cg_trending_age is not None
            else None
        )
        cmc_age = self._provider_age('cmc')
        cg_entry = self._cache.get('coingecko') or {}
        cmc_entry = self._cache.get('cmc') or {}
        reconcile_state = read_json(self._reconcile_state_path(), {}) or {}
        return {
            'generated_at': time.time(),
            'writer_lease_held': self.writer_lease_held,
            'writer_lock_backend': locking_backend(),
            'coingecko': {
                'enabled': self.coingecko_enabled,
                'keyless': self.coingecko_enabled and not self.coingecko_api_key,
                'budget': self._cg_budget.stats(),
                'configured_per_minute': self._configured.get('coingecko_per_minute'),
                'configured_monthly': self._configured.get('coingecko_monthly'),
                'breaker': self._cg_breaker.state(),
                'cache_age_seconds': round(cg_age, 1) if cg_age is not None else None,
                'markets_cache_age_seconds': (
                    round(cg_markets_age, 1) if cg_markets_age is not None else None
                ),
                'trending_cache_age_seconds': (
                    round(cg_trending_age, 1) if cg_trending_age is not None else None
                ),
                'ambiguous_symbols_excluded': len(cg_entry.get('ambiguous') or []),
            },
            'cmc': {
                'enabled': self.cmc_enabled,
                'requested_but_missing_key': self.cmc_requested and not self.cmc_enabled,
                'budget': self._cmc_budget.stats(),
                'configured_per_minute': self._configured.get('cmc_per_minute'),
                'configured_monthly': self._configured.get('cmc_monthly'),
                'breaker': self._cmc_breaker.state(),
                'cache_age_seconds': round(cmc_age, 1) if cmc_age is not None else None,
                'ambiguous_symbols_excluded': len(cmc_entry.get('ambiguous') or []),
                'last_reconcile_epoch': reconcile_state.get('last_reconcile_epoch'),
                'provider_month_credits_used': reconcile_state.get('provider_month_credits_used'),
            },
            'config': self.config_summary(),
        }

    def write_status(self) -> None:
        try:
            atomic_write_json(self.root / STATUS_FILENAME, self.status_snapshot())
        except Exception:
            log.exception('failed to write external signal status file')
