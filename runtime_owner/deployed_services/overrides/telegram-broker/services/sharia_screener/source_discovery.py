from __future__ import annotations

"""Fail-closed discovery of candidate official sources for Sharia review.

Binance is authoritative for the executable Spot/USDT universe, but its
documented exchange-info API does not publish project websites or whitepapers.
This module therefore binds a candidate asset to the exact Binance market and
then discovers source links through CoinGecko, with CoinMarketCap as a fallback.

Discovery never edits the owner-maintained source registry and never grants a
tradeable Sharia verdict.  It creates a durable, reviewable reading-list
candidate.  The existing V19.1 evidence, claim, screener and signed owner-
approval gates remain the only path to GREEN/GREEN_AVOID_OPTIONAL.
"""

import hashlib
import ipaddress
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import requests

from services.common.atomic import atomic_write_json, read_json
from services.common.config_bounds import env_int
from services.common.paths import SHARIA_DISCOVERY_ARCHIVE_DIR, SHARIA_DISCOVERY_CURRENT_DIR
from services.universe_service.external_signals.breaker import CircuitBreaker
from services.universe_service.external_signals.budget import ApiBudget
from services.universe_service.external_signals.httpguard import guarded_get_json

log = logging.getLogger('sharia-screener.discovery')

COINGECKO_BASE = 'https://api.coingecko.com/api/v3'
CMC_BASE = 'https://pro-api.coinmarketcap.com'
USER_AGENT = 'V10.3-sharia-source-discovery/1.0'
SCHEMA_VERSION = 1
MAX_SYMBOL_CANDIDATES = 64
MAX_EXCHANGE_TICKER_PAGES = 10
COINGECKO_ID_RE = re.compile(r'^[a-z0-9][a-z0-9_-]{0,127}$')


def _utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_time(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _safe_https_url(value: object) -> str:
    """Return a canonical fetchable HTTPS URL, or an empty string.

    Provider metadata is untrusted input. Credentials, IP literals, local
    names and malformed ports are rejected here; the retriever performs its
    own DNS and peer-IP checks again before fetching approved evidence.
    """
    raw = str(value or '').strip()
    if not raw or len(raw) > 2048:
        return ''
    try:
        parsed = urlsplit(raw)
        host = (parsed.hostname or '').strip().lower().rstrip('.')
        _ = parsed.port
    except ValueError:
        return ''
    if parsed.scheme.lower() != 'https' or not host or parsed.username or parsed.password:
        return ''
    if host in {'localhost', 'localhost.localdomain'} or host.endswith(('.local', '.internal')):
        return ''
    try:
        ipaddress.ip_address(host)
        return ''
    except ValueError:
        pass
    netloc = host if parsed.port in (None, 443) else f'{host}:{parsed.port}'
    return urlunsplit(('https', netloc, parsed.path or '/', parsed.query, ''))


def _host(url: str) -> str:
    return (urlsplit(url).hostname or '').lower().removeprefix('www.')


def _first_url(values: object) -> str:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        return ''
    for value in values:
        clean = _safe_https_url(value)
        if clean:
            return clean
    return ''


def _record_digest(payload: dict) -> str:
    unsigned = {key: value for key, value in payload.items()
                if key != 'record_sha256'}
    encoded = json.dumps(unsigned, sort_keys=True, separators=(',', ':')).encode()
    return hashlib.sha256(encoded).hexdigest()


def _valid_record(payload: object) -> bool:
    return (
        isinstance(payload, dict)
        and payload.get('schema_version') == SCHEMA_VERSION
        and payload.get('status') in {
            'VERIFIED_CANDIDATE', 'AMBIGUOUS', 'UNAVAILABLE'}
        and payload.get('owner_verified') is False
        and payload.get('trade_permission') is False
        and payload.get('record_sha256') == _record_digest(payload)
    )


class _ProviderClient:
    def __init__(self, *, name: str, api_key: str, budget: ApiBudget,
                 breaker: CircuitBreaker, timeout: int,
                 session: requests.Session | None = None):
        self.name = name
        self.api_key = str(api_key or '').strip()
        self.budget = budget
        self.breaker = breaker
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers['User-Agent'] = USER_AGENT

    def get(self, url: str, *, params: dict | None, headers: dict) -> object:
        return guarded_get_json(
            self.session, url, params=params, headers=headers,
            timeout=self.timeout, budget=self.budget, breaker=self.breaker)

    def state(self) -> dict:
        return {'budget': self.budget.stats(), 'breaker': self.breaker.state()}


class CoinGeckoSourceClient(_ProviderClient):
    def __init__(self, *, api_key: str, budget: ApiBudget,
                 breaker: CircuitBreaker, timeout: int,
                 session: requests.Session | None = None):
        super().__init__(name='coingecko', api_key=api_key, budget=budget,
                         breaker=breaker, timeout=timeout, session=session)
        self._list_cache: tuple[float, list] = (0.0, [])

    def _headers(self) -> dict:
        return {'x-cg-demo-api-key': self.api_key} if self.api_key else {}

    def coin_list(self) -> list | None:
        cached_at, rows = self._list_cache
        if rows and time.time() - cached_at < 86_400:
            return rows
        payload = self.get(
            COINGECKO_BASE + '/coins/list',
            params={'include_platform': 'true'}, headers=self._headers())
        if not isinstance(payload, list):
            return None
        rows = [row for row in payload if isinstance(row, dict)]
        self._list_cache = (time.time(), rows)
        return rows

    def details(self, coin_id: str) -> dict | None:
        payload = self.get(
            COINGECKO_BASE + f'/coins/{coin_id}',
            params={
                'localization': 'false', 'tickers': 'false',
                'market_data': 'false', 'community_data': 'false',
                'developer_data': 'false', 'sparkline': 'false',
            },
            headers=self._headers())
        return payload if isinstance(payload, dict) else None

    def exchange_tickers(self, coin_ids: list[str]) -> list[dict] | None:
        """Return a complete, stably paginated Binance ticker subset.

        CoinGecko symbols are not identities.  The exchange endpoint binds the
        candidate IDs from ``/coins/list`` to the exact Binance BASE/USDT
        market.  A response that reaches our hard page ceiling is rejected as
        incomplete rather than allowing a false unique match.
        """
        joined = ','.join(sorted(coin_ids))
        rows: list[dict] = []
        for page in range(1, MAX_EXCHANGE_TICKER_PAGES + 1):
            payload = self.get(
                COINGECKO_BASE + '/exchanges/binance/tickers',
                params={
                    'coin_ids': joined,
                    'page': page,
                    'order': 'base_target',
                    'include_exchange_logo': 'false',
                },
                headers=self._headers())
            if not isinstance(payload, dict) or not isinstance(
                    payload.get('tickers'), list):
                return None
            page_rows = [row for row in payload['tickers']
                         if isinstance(row, dict)]
            rows.extend(page_rows)
            if len(page_rows) < 100:
                return rows
        return None

    @staticmethod
    def _binance_market_ids(tickers: list[dict], base: str,
                            allowed_ids: set[str]) -> set[str]:
        matches: set[str] = set()
        for ticker in tickers:
            market = ticker.get('market') or {}
            if str(market.get('identifier', '')).casefold() != 'binance':
                continue
            if (str(ticker.get('base', '')).upper() != base or
                    str(ticker.get('target', '')).upper() != 'USDT'):
                continue
            coin_id = str(ticker.get('coin_id', '')).strip()
            if coin_id in allowed_ids:
                matches.add(coin_id)
        return matches

    def discover(self, base: str) -> tuple[dict | None, str]:
        rows = self.coin_list()
        if rows is None:
            return None, 'CoinGecko coin list unavailable'
        candidates = [row for row in rows
                      if str(row.get('symbol', '')).upper() == base]
        if not candidates:
            return None, 'CoinGecko has no exact symbol candidate'
        if len(candidates) > MAX_SYMBOL_CANDIDATES:
            return None, f'CoinGecko symbol is ambiguous ({len(candidates)} candidates)'

        candidate_ids = {
            str(row.get('id', '')).strip() for row in candidates
            if COINGECKO_ID_RE.fullmatch(str(row.get('id', '')).strip())
        }
        if not candidate_ids:
            return None, 'CoinGecko exact-symbol candidates have no valid stable IDs'
        tickers = self.exchange_tickers(sorted(candidate_ids))
        if tickers is None:
            return None, 'CoinGecko Binance ticker binding is unavailable or incomplete'
        matches = self._binance_market_ids(tickers, base, candidate_ids)
        if not matches:
            return None, f'CoinGecko has no exact Binance {base}/USDT identity binding'
        if len(matches) != 1:
            return None, (f'CoinGecko Binance {base}/USDT identity is ambiguous '
                          f'({len(matches)} stable IDs)')

        coin_id = next(iter(matches))
        details = self.details(coin_id)
        if (not details or str(details.get('id', '')).strip() != coin_id or
                str(details.get('symbol', '')).upper() != base):
            return None, 'CoinGecko metadata identity mismatch'
        links = details.get('links') or {}
        website = _first_url(links.get('homepage'))
        whitepaper = _first_url(links.get('whitepaper'))
        if not website:
            return None, 'CoinGecko-bound asset has no valid HTTPS official website'
        return {
            'provider': 'coingecko',
            'provider_asset_id': coin_id,
            'name': str(details.get('name', '')).strip(),
            'symbol': base,
            'identity_basis': (
                'exact CoinGecko id from stably paginated Binance exchange '
                'ticker data with exact BASE/USDT binding'),
            'official_website': website,
            'whitepaper': whitepaper,
        }, ''


class CoinMarketCapSourceClient(_ProviderClient):
    def __init__(self, *, api_key: str, budget: ApiBudget,
                 breaker: CircuitBreaker, timeout: int,
                 session: requests.Session | None = None):
        super().__init__(name='coinmarketcap', api_key=api_key, budget=budget,
                         breaker=breaker, timeout=timeout, session=session)

    def _headers(self) -> dict:
        return {'X-CMC_PRO_API_KEY': self.api_key, 'Accept': 'application/json'}

    @staticmethod
    def _data(payload: object) -> object:
        if not isinstance(payload, dict):
            return None
        status = payload.get('status') or {}
        if isinstance(status, dict) and status.get('error_code') not in (0, None):
            return None
        return payload.get('data')

    def discover(self, base: str) -> tuple[dict | None, str]:
        if not self.api_key:
            return None, 'CoinMarketCap free API key is not configured'
        mapped = self.get(
            CMC_BASE + '/v1/cryptocurrency/map',
            params={'symbol': base, 'listing_status': 'active', 'limit': 100},
            headers=self._headers())
        rows = self._data(mapped)
        if not isinstance(rows, list):
            return None, 'CoinMarketCap map unavailable'
        candidates = [row for row in rows if isinstance(row, dict)
                      and str(row.get('symbol', '')).upper() == base
                      and row.get('is_active', 1) in (1, True)]
        ids = {row.get('id') for row in candidates
               if isinstance(row.get('id'), int) and not isinstance(row.get('id'), bool)}
        if len(ids) != 1:
            return None, f'CoinMarketCap symbol is not unique ({len(ids)} active ids)'
        cmc_id = next(iter(ids))
        info = self.get(
            CMC_BASE + '/v2/cryptocurrency/info',
            params={'id': cmc_id}, headers=self._headers())
        data = self._data(info)
        if not isinstance(data, dict):
            return None, 'CoinMarketCap metadata unavailable'
        raw = data.get(str(cmc_id), data.get(cmc_id))
        if isinstance(raw, list):
            raw = raw[0] if len(raw) == 1 else None
        if not isinstance(raw, dict) or str(raw.get('symbol', '')).upper() != base:
            return None, 'CoinMarketCap metadata identity mismatch'
        urls = raw.get('urls') or {}
        website = _first_url(urls.get('website'))
        whitepaper = _first_url(urls.get('technical_doc'))
        if not website:
            return None, 'CoinMarketCap-bound asset has no valid HTTPS official website'
        return {
            'provider': 'coinmarketcap',
            'provider_asset_id': cmc_id,
            'name': str(raw.get('name', '')).strip(),
            'symbol': base,
            'identity_basis': 'single active CoinMarketCap id for exact symbol',
            'official_website': website,
            'whitepaper': whitepaper,
        }, ''


class SourceDiscovery:
    """Discover, cache and archive non-authoritative source candidates."""

    def __init__(self, *, current_dir: str | Path = SHARIA_DISCOVERY_CURRENT_DIR,
                 archive_dir: str | Path = SHARIA_DISCOVERY_ARCHIVE_DIR,
                 runtime_dir: str | Path, coingecko: CoinGeckoSourceClient | None = None,
                 coinmarketcap: CoinMarketCapSourceClient | None = None):
        self.current_dir = Path(current_dir)
        self.archive_dir = Path(archive_dir)
        self.runtime_dir = Path(runtime_dir)
        self.refresh_days = env_int('SHARIA_SOURCE_REFRESH_DAYS', 7, 1, 30)
        self.failure_refresh_hours = env_int(
            'SHARIA_SOURCE_FAILURE_REFRESH_HOURS', 24, 1, 168)
        self.retention_days = env_int('SHARIA_ARCHIVE_RETENTION_DAYS', 90, 7, 365)
        timeout = env_int('SHARIA_SOURCE_API_TIMEOUT_SECONDS', 15, 3, 60)
        cooldown = env_int('SHARIA_SOURCE_BREAKER_COOLDOWN_SECONDS', 900, 60, 21_600)

        self.current_dir.mkdir(parents=True, exist_ok=True)
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self._stats_cache: tuple[float, dict] = (0.0, {})
        self.coingecko = coingecko or CoinGeckoSourceClient(
            api_key=os.getenv('COINGECKO_API_KEY', ''),
            budget=ApiBudget(
                'sharia-coingecko', self.runtime_dir / 'coingecko_budget.json',
                per_minute=env_int('SHARIA_COINGECKO_PER_MINUTE_LIMIT', 12, 1, 96),
                per_month=env_int('SHARIA_COINGECKO_MONTHLY_LIMIT', 8000, 100, 9600)),
            breaker=CircuitBreaker(
                'sharia-coingecko', cooldown_seconds=cooldown,
                state_path=self.runtime_dir / 'coingecko_breaker.json'),
            timeout=timeout)
        self.coinmarketcap = coinmarketcap or CoinMarketCapSourceClient(
            api_key=os.getenv('COINMARKETCAP_API_KEY', os.getenv('CMC_API_KEY', '')),
            budget=ApiBudget(
                'sharia-coinmarketcap', self.runtime_dir / 'cmc_budget.json',
                per_minute=env_int('SHARIA_CMC_PER_MINUTE_LIMIT', 6, 1, 48),
                per_month=env_int('SHARIA_CMC_MONTHLY_LIMIT', 3000, 100, 14_400)),
            breaker=CircuitBreaker(
                'sharia-coinmarketcap', cooldown_seconds=cooldown,
                state_path=self.runtime_dir / 'cmc_breaker.json'),
            timeout=timeout)

    def _current_path(self, base: str) -> Path:
        return self.current_dir / f'{base}.json'

    def _cached(self, base: str, pair: str, now: datetime) -> dict | None:
        payload = read_json(self._current_path(base), None)
        if not isinstance(payload, dict):
            return None
        if (not _valid_record(payload) or payload.get('base') != base or
                payload.get('pair') != pair):
            return None
        refresh_due = _parse_time(payload.get('refresh_due_at'))
        return payload if refresh_due and now < refresh_due else None

    def _persist(self, payload: dict, now: datetime) -> None:
        digest = _record_digest(payload)
        payload = {**payload, 'record_sha256': digest}
        atomic_write_json(self._current_path(str(payload['base'])), payload)
        month = self.archive_dir / now.strftime('%Y-%m')
        stamp = now.strftime('%Y%m%dT%H%M%S%fZ')
        atomic_write_json(month / f'{payload["base"]}_{stamp}_{digest[:12]}.json', payload)
        self._stats_cache = (0.0, {})
        self._prune(now)

    def write_universe_index(self, binance_entries: dict[str, dict]) -> dict:
        """Write a non-authoritative coverage index for the current universe.

        The index makes missing, stale and delisted records visible on Oracle
        without granting any trading permission.  A completed timestamp is
        retained only when every currently listed Binance Spot/USDT base has a
        valid, non-stale discovery record.
        """
        now = _utc()
        listed = sorted({
            str(base).upper().strip() for base, entry in binance_entries.items()
            if str(base).upper().strip().isalnum()
            and str(entry.get('symbol', '')).upper() ==
            str(base).upper().strip() + 'USDT'
            and str(entry.get('baseAsset', '')).upper() ==
            str(base).upper().strip()
            and str(entry.get('quoteAsset', '')).upper() == 'USDT'
            and str(entry.get('status', '')).upper() == 'TRADING'
            and entry.get('isSpotTradingAllowed', False)
        })
        records: dict[str, dict] = {}
        for path in self.current_dir.glob('*.json'):
            if path.name.startswith('_'):
                continue
            payload = read_json(path, None)
            if _valid_record(payload):
                records[str(payload.get('base', '')).upper()] = payload
        missing = [base for base in listed if base not in records]
        stale = []
        for base in listed:
            record = records.get(base)
            if record is None:
                continue
            refresh_due = _parse_time(record.get('refresh_due_at'))
            if refresh_due is None or refresh_due <= now:
                stale.append(base)
        orphaned = sorted(set(records) - set(listed))
        status_counts = {'VERIFIED_CANDIDATE': 0, 'AMBIGUOUS': 0,
                         'UNAVAILABLE': 0}
        for base in listed:
            status = str((records.get(base) or {}).get('status', ''))
            if status in status_counts:
                status_counts[status] += 1
        index_path = self.current_dir / '_binance_spot_usdt_index.json'
        previous = read_json(index_path, {})
        fully_current = bool(listed) and not missing and not stale
        last_full = (_iso(now) if fully_current else
                     str(previous.get('last_full_sweep_completed_at', ''))
                     if isinstance(previous, dict) else '')
        payload = {
            'schema_version': SCHEMA_VERSION,
            'generated_at': _iso(now),
            'source': 'Binance Spot exchangeInfo',
            'quote_asset': 'USDT',
            'listed_base_count': len(listed),
            'valid_record_count': sum(1 for base in listed if base in records),
            'status_counts': status_counts,
            'missing_bases': missing,
            'stale_bases': stale,
            'orphaned_delisted_record_bases': orphaned,
            'fully_current': fully_current,
            'last_full_sweep_completed_at': last_full,
            'universe_sha256': hashlib.sha256(
                '\n'.join(listed).encode('utf-8')).hexdigest(),
            'owner_verified': False,
            'trade_permission': False,
        }
        payload['record_sha256'] = _record_digest(payload)
        atomic_write_json(index_path, payload)
        return payload

    def _prune(self, now: datetime) -> int:
        cutoff = now.timestamp() - self.retention_days * 86_400
        removed = 0
        try:
            files = list(self.archive_dir.rglob('*.json'))
        except OSError:
            return 0
        for path in files:
            try:
                payload = read_json(path, None)
                discovered = (_parse_time(payload.get('discovered_at'))
                              if isinstance(payload, dict) else None)
                recorded_at = (discovered.timestamp() if discovered else
                               path.stat().st_mtime)
                if recorded_at < cutoff:
                    path.unlink(missing_ok=True)
                    removed += 1
            except OSError:
                continue
        for directory in sorted(
                (p for p in self.archive_dir.rglob('*') if p.is_dir()), reverse=True):
            try:
                directory.rmdir()
            except OSError:
                pass
        return removed

    def ensure(self, base: str, pair: str, binance_entry: dict) -> dict:
        base = str(base).upper().strip()
        pair = str(pair).upper().strip()
        if not base.isalnum() or pair != f'{base}/USDT':
            raise ValueError('source discovery base/pair binding is invalid')
        if (str(binance_entry.get('symbol', '')).upper() != base + 'USDT' or
                str(binance_entry.get('baseAsset', '')).upper() != base or
                str(binance_entry.get('quoteAsset', '')).upper() != 'USDT' or
                str(binance_entry.get('status', '')).upper() != 'TRADING' or
                not binance_entry.get('isSpotTradingAllowed', False)):
            raise ValueError('source discovery requires an eligible Binance exchangeInfo row')
        now = _utc()
        cached = self._cached(base, pair, now)
        if cached is not None:
            return {**cached, 'cache_hit': True}

        errors: list[str] = []
        candidate, reason = self.coingecko.discover(base)
        if reason:
            errors.append(reason)
        if candidate is None:
            candidate, reason = self.coinmarketcap.discover(base)
            if reason:
                errors.append(reason)

        status = 'VERIFIED_CANDIDATE' if candidate else (
            'AMBIGUOUS' if any('ambiguous' in item or 'not unique' in item
                               for item in errors) else 'UNAVAILABLE')
        refresh_delta = (timedelta(days=self.refresh_days) if candidate else
                         timedelta(hours=self.failure_refresh_hours))
        sources = []
        official_hosts = []
        provider = {}
        if candidate:
            website = str(candidate.get('official_website', ''))
            whitepaper = str(candidate.get('whitepaper', ''))
            sources.append({'role': 'official_website', 'url': website})
            if whitepaper and whitepaper != website:
                sources.append({'role': 'whitepaper', 'url': whitepaper})
            official_hosts = [_host(website)]
            provider = {key: value for key, value in candidate.items()
                        if key not in {'official_website', 'whitepaper'}}

        payload = {
            'schema_version': SCHEMA_VERSION,
            'base': base,
            'pair': pair,
            'status': status,
            'discovered_at': _iso(now),
            'refresh_due_at': _iso(now + refresh_delta),
            'retention_days': self.retention_days,
            'binance': {
                'symbol': str(binance_entry.get('symbol', base + 'USDT')),
                'status': str(binance_entry.get('status', '')),
                'base_asset': str(binance_entry.get('baseAsset', base)),
                'quote_asset': str(binance_entry.get('quoteAsset', 'USDT')),
                'spot_trading_allowed': bool(
                    binance_entry.get('isSpotTradingAllowed', False)),
                'identity_basis': 'documented Binance Spot exchangeInfo',
            },
            'provider_identity': provider,
            'official_hosts_candidates': official_hosts,
            'source_candidates': sources,
            'errors': errors,
            'owner_verified': False,
            'trade_permission': False,
            'owner_action': (
                'Review identity and sources, then bind exact evidence and claims '
                'in source_registry.json before approving any Sharia result'),
        }
        self._persist(payload, now)
        return {**payload, 'cache_hit': False}

    def stats(self) -> dict:
        cached_at, cached = self._stats_cache
        if cached and time.time() - cached_at < 60:
            return cached
        counts = {'VERIFIED_CANDIDATE': 0, 'AMBIGUOUS': 0, 'UNAVAILABLE': 0,
                  'INVALID': 0}
        for path in self.current_dir.glob('*.json'):
            if path.name.startswith('_'):
                continue
            payload = read_json(path, None)
            valid = _valid_record(payload)
            status = str(payload.get('status', 'INVALID')) if valid else 'INVALID'
            counts[status if status in counts else 'INVALID'] += 1
        result = {
            'enabled': True,
            'current_records': sum(counts.values()),
            'status_counts': counts,
            'refresh_days': self.refresh_days,
            'archive_retention_days': self.retention_days,
            'coingecko': self.coingecko.state(),
            'coinmarketcap': self.coinmarketcap.state(),
            'trade_permission_from_discovery': False,
            'universe_index': read_json(
                self.current_dir / '_binance_spot_usdt_index.json', {}),
        }
        self._stats_cache = (time.time(), result)
        return result
