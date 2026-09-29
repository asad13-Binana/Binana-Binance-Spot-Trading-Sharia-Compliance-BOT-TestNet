"""Strict, non-authorising bridge from discovery to owner review.

The active Sharia discovery, registry, rules, runner and service modules are
hash protected.  This administrative adapter consumes their existing record
contract without modifying it.  It never writes the registry or grants a
verdict/trade permission.
"""
from __future__ import annotations

import json
from pathlib import Path

from services.common.atomic import read_json
from services.sharia_screener.source_discovery import (
    COINGECKO_ID_RE,
    _host,
    _record_digest,
    _safe_https_url,
    _valid_record,
    TARGET_ROLES,
)


def validated_candidate_record(payload: object) -> dict:
    """Return one exact-market-bound candidate or fail closed."""
    if not _valid_record(payload):
        raise ValueError(
            'discovery record schema, digest or fail-closed flags are invalid')
    assert isinstance(payload, dict)
    if payload.get('status') != 'VERIFIED_CANDIDATE':
        raise ValueError('discovery record is not a verified source candidate')
    base = str(payload.get('base', '')).upper().strip()
    pair = str(payload.get('pair', '')).upper().strip()
    if not base.isalnum() or pair != f'{base}/USDT':
        raise ValueError('discovery record base/pair binding is invalid')
    binance = payload.get('binance')
    if (not isinstance(binance, dict) or
            str(binance.get('symbol', '')).upper() != base + 'USDT' or
            str(binance.get('base_asset', '')).upper() != base or
            str(binance.get('quote_asset', '')).upper() != 'USDT' or
            str(binance.get('status', '')).upper() != 'TRADING' or
            binance.get('spot_trading_allowed') is not True):
        raise ValueError(
            'discovery record Binance identity binding is invalid')
    provider = payload.get('provider_identity')
    if not isinstance(provider, dict) or provider.get('provider') not in {
            'coingecko', 'coinmarketcap'}:
        raise ValueError('discovery record provider identity is invalid')
    provider_id = provider.get('provider_asset_id')
    if provider.get('provider') == 'coingecko':
        if (not isinstance(provider_id, str) or
                not COINGECKO_ID_RE.fullmatch(provider_id)):
            raise ValueError(
                'discovery record CoinGecko stable ID is invalid')
    elif (isinstance(provider_id, bool) or not isinstance(provider_id, int) or
          provider_id <= 0):
        raise ValueError(
            'discovery record CoinMarketCap numeric ID is invalid')
    if str(provider.get('symbol', '')).upper() != base:
        raise ValueError(
            'discovery record provider symbol binding is invalid')

    hosts = payload.get('official_hosts_candidates')
    sources = payload.get('source_candidates')
    if (not isinstance(hosts, list) or not hosts or
            not all(isinstance(h, str) and h.strip() for h in hosts) or
            not isinstance(sources, list) or not sources):
        raise ValueError('discovery record source candidates are invalid')
    official_hosts = [str(h).lower().strip().rstrip('.') for h in hosts]
    prepared_sources = []
    website_seen = False
    alternative_official_source_seen = False
    seen_urls: set[str] = set()
    for source in sources:
        if not isinstance(source, dict) or source.get('role') not in TARGET_ROLES.values():
            raise ValueError(
                'discovery record contains an unsupported source role')
        url = _safe_https_url(source.get('url'))
        identity = (source.get('role'), url)
        if not url or url != source.get('url') or identity in seen_urls:
            raise ValueError(
                'discovery record contains an unsafe or duplicate source URL')
        seen_urls.add(identity)
        if source.get('role') == 'official_website':
            if _host(url) not in official_hosts:
                raise ValueError(
                    'discovery record official website host binding is invalid')
            website_seen = True
        elif source.get('role') in {'official_docs', 'whitepaper', 'tokenomics_economics'}:
            alternative_official_source_seen |= _host(url) in official_hosts
        prepared_sources.append({**source, 'role': source['role'], 'url': url})
    if not website_seen and not alternative_official_source_seen:
        raise ValueError(
            'discovery record has no official website or documentation candidate')
    return {
        **payload,
        'base': base,
        'pair': pair,
        'official_hosts_candidates': official_hosts,
        'source_candidates': prepared_sources,
    }


def candidate_bases(current_dir: str | Path) -> set[str]:
    """List only digest-valid, strictly bound review candidates."""
    bases: set[str] = set()
    for path in Path(current_dir).glob('*.json'):
        if path.name.startswith('_'):
            continue
        payload = read_json(path, None)
        try:
            valid = validated_candidate_record(payload)
        except ValueError:
            continue
        bases.add(str(valid['base']))
    return bases


def record_digest(payload: dict) -> str:
    """Expose the protected record's canonical digest for regression fixtures."""
    return _record_digest(payload)


def load_candidate(path: str | Path) -> dict:
    """Read one bounded JSON candidate and validate it strictly."""
    candidate = Path(path)
    if candidate.stat().st_size > 1_048_576:
        raise ValueError('discovery file exceeds 1 MiB')
    try:
        payload = json.loads(candidate.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f'unreadable discovery JSON: {exc}') from exc
    return validated_candidate_record(payload)
