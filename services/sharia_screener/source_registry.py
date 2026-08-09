"""Strict owner-maintained source and claim registry for local screening."""
from __future__ import annotations

import json
import ipaddress
import re
from pathlib import Path
from urllib.parse import urlparse

from services.common.sharia_v19 import (
    SCREENER_HOSTS,
    SCREENER_SITES,
    normalize_evidence_url,
)
from services.sharia_screener.verdict_policy import (
    CANONICAL_SCREENER_VERDICTS,
    canonical_screener_verdict,
)


class SourceRegistryError(ValueError):
    """The local source registry is malformed or identity-unsafe."""


def _host(value: str) -> str:
    try:
        parsed = urlparse(str(value).strip())
    except ValueError as exc:
        raise SourceRegistryError(f'invalid source URL {value!r}') from exc
    if (parsed.scheme.lower() != 'https' or not parsed.hostname or
            parsed.username or parsed.password):
        raise SourceRegistryError(f'source URL must be credential-free HTTPS: {value!r}')
    return parsed.hostname.lower().rstrip('.')


def _official_host(value: str) -> str:
    host = str(value).strip().lower().rstrip('.')
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise SourceRegistryError('official_hosts cannot contain an IP literal')
    labels = host.split('.')
    if (len(labels) < 2 or any(
            not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
            for label in labels)):
        raise SourceRegistryError(
            f'invalid or overly broad official host {value!r}')
    return host


class SourceRegistry:
    """Load sources whose asset identity and positive claims the owner reviewed.

    Discovery is deliberately not guessed from ticker text. A ticker collision
    can bind the wrong project's website to a Binance asset, so an absent or
    malformed entry fails closed and is surfaced for owner action.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> dict:
        try:
            payload = json.loads(self.path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return {'schema_version': 1, 'assets': {}}
        except (OSError, json.JSONDecodeError) as exc:
            raise SourceRegistryError(f'source registry is unreadable: {exc}') from exc
        if not isinstance(payload, dict) or payload.get('schema_version') != 1:
            raise SourceRegistryError('source registry schema_version must be 1')
        if not isinstance(payload.get('assets'), dict):
            raise SourceRegistryError('source registry assets must be an object')
        return payload

    def asset(self, base: str) -> dict | None:
        raw = self.load()['assets'].get(str(base).strip().upper())
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise SourceRegistryError(f'{base} registry entry must be an object')
        official_hosts = raw.get('official_hosts')
        sources = raw.get('sources')
        claims = raw.get('claims', {})
        screeners = raw.get('screeners', {})
        if (not isinstance(official_hosts, list) or not official_hosts or
                not all(isinstance(item, str) and item.strip()
                        for item in official_hosts)):
            raise SourceRegistryError(f'{base} requires non-empty official_hosts')
        normalized_hosts = {_official_host(item) for item in official_hosts}
        if not isinstance(sources, list) or not sources:
            raise SourceRegistryError(f'{base} requires at least one source')
        normalized_sources = []
        source_urls = set()
        has_identity_source = False
        for index, source in enumerate(sources):
            if not isinstance(source, dict):
                raise SourceRegistryError(f'{base} source {index} must be an object')
            url = normalize_evidence_url(source.get('url'))
            if not url:
                raise SourceRegistryError(
                    f'{base} source {index} must be credential-free HTTPS')
            host = _host(url)
            identity_match = source.get('identity_match') is True
            if identity_match and not any(
                    host == official or host.endswith('.' + official)
                    for official in normalized_hosts):
                raise SourceRegistryError(
                    f'{base} identity source host {host!r} is not official')
            has_identity_source = has_identity_source or identity_match
            if url in source_urls:
                raise SourceRegistryError(f'{base} contains duplicate source URL {url!r}')
            source_urls.add(url)
            normalized_sources.append({'url': url, 'identity_match': identity_match})
        if not has_identity_source:
            raise SourceRegistryError(f'{base} requires an identity-matched official source')
        if not isinstance(claims, dict) or not isinstance(screeners, dict):
            raise SourceRegistryError(f'{base} claims and screeners must be objects')
        required_claims = {'token_type', 'utility', 'revenue'}
        if set(claims) != required_claims:
            raise SourceRegistryError(
                f'{base} claims must be exactly {sorted(required_claims)}')
        if set(claims) & set(screeners):
            raise SourceRegistryError(f'{base} claim and screener names overlap')
        normalized_claims = {}
        normalized_screeners = {}
        for name, claim in {**claims, **screeners}.items():
            if not isinstance(claim, dict):
                raise SourceRegistryError(f'{base} claim {name!r} must be an object')
            url = normalize_evidence_url(claim.get('url'))
            if url not in source_urls:
                raise SourceRegistryError(
                    f'{base} claim {name!r} references an unfetched source')
            if not str(claim.get('value', '')).strip() or not str(
                    claim.get('quote', '')).strip():
                raise SourceRegistryError(
                    f'{base} claim {name!r} requires value and verbatim quote')
            normalized = {
                'value': str(claim['value']).strip(),
                'quote': str(claim['quote']).strip(),
                'url': url,
            }
            if name in claims:
                normalized_claims[name] = normalized
            else:
                verdict = canonical_screener_verdict(normalized['value'])
                if not verdict:
                    raise SourceRegistryError(
                        f'{base} screener {name!r} verdict must be one of '
                        f'{sorted(CANONICAL_SCREENER_VERDICTS)}')
                normalized['value'] = verdict
                normalized_screeners[name] = normalized
        seen_screener_names = set()
        canonical_screeners = {}
        for name, claim in normalized_screeners.items():
            normalized_name = str(name).lower().replace('_', '').replace('-', '')
            if normalized_name not in SCREENER_SITES:
                raise SourceRegistryError(f'{base} names unknown screener {name!r}')
            if normalized_name in seen_screener_names:
                raise SourceRegistryError(
                    f'{base} contains duplicate normalized screener {normalized_name!r}')
            seen_screener_names.add(normalized_name)
            host = _host(str(claim.get('url', '')))
            allowed = SCREENER_HOSTS[normalized_name]
            if not any(host == item or host.endswith('.' + item) for item in allowed):
                raise SourceRegistryError(
                    f'{base} screener {name!r} is bound to the wrong host')
            canonical_screeners[normalized_name] = claim
        return {
            'official_hosts': normalized_hosts,
            'sources': normalized_sources,
            'claims': normalized_claims,
            'screeners': canonical_screeners,
        }
