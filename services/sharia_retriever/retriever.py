"""HTTPS source retrieval with content attestation. No AI, no API key.

Fails closed everywhere: a non-HTTPS URL, a redirect that leaves HTTPS, an
oversized body, a timeout, a non-200 status or an AI-inference host all
produce a recorded failure rather than an exception that a caller might
swallow into a passing verdict.

HTML is reduced to text with the standard library only. The service
dependency lock ships no HTML parser, and adding one to pull in a screening
feature would widen the supply-chain surface for no benefit.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urlparse

import requests

from services.common.sharia_v19 import normalize_evidence_url

log = logging.getLogger('sharia-retriever')

# Hosts that serve model inference. Blocking them structurally is what keeps
# the screening path free of a billed dependency: a future edit that tries to
# call one fails here instead of quietly reintroducing the cost.
AI_ENDPOINT_HOSTS = frozenset({
    'api.openai.com', 'openai.com', 'api.anthropic.com', 'anthropic.com',
    'generativelanguage.googleapis.com', 'api.cohere.ai', 'api.mistral.ai',
    'api.groq.com', 'api.together.xyz', 'api.deepseek.com',
    'api.x.ai', 'openrouter.ai', 'api.perplexity.ai',
})

# SOURCE_QUALITY_AND_CONFIDENCE_SCORING. SharLife is regulated and carries the
# highest Tier 2 weight; the remaining public screeners are unregulated and
# therefore Tier 3 under the controller's own tiering.
_SCREENER_TIERS = {
    'sharlife.my': 'TIER_2_PRIMARY_MARKET',
    'cryptoummah.com': 'TIER_3_SECONDARY',
    'islamicfinanceguru.com': 'TIER_3_SECONDARY',
    'saraf.app': 'TIER_3_SECONDARY',
    'halalscreener.app': 'TIER_3_SECONDARY',
    'gethalalcrypto.com': 'TIER_3_SECONDARY',
    'musaffa.com': 'TIER_3_SECONDARY',
}

MAX_BYTES = 4 * 1024 * 1024
TIMEOUT_SECONDS = 20
MAX_REDIRECTS = 5


class RetrievalBlocked(RuntimeError):
    """The URL may not be fetched at all (scheme, host, or policy)."""


class _TextExtractor(HTMLParser):
    """Collect visible text, dropping script/style/head noise."""

    _SKIP = {'script', 'style', 'head', 'meta', 'link', 'noscript', 'svg'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._depth += 1

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._depth:
            self._depth -= 1

    def handle_data(self, data):
        if not self._depth and data.strip():
            self._chunks.append(data.strip())

    def text(self) -> str:
        return ' '.join(self._chunks)


def html_to_text(payload: str) -> str:
    """Reduce HTML to visible text. Sentence punctuation is preserved so the
    rules engine can still find sentence boundaries for quote extraction."""
    parser = _TextExtractor()
    try:
        parser.feed(payload)
        parser.close()
    except Exception:  # malformed markup must not abort a screening
        log.warning('HTML parse failed; falling back to tag stripping')
        return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', payload)).strip()
    return re.sub(r'\s+', ' ', parser.text()).strip()


def _canonical_host(value: str) -> str:
    """Lowercase a host and drop one leading ``www.`` label.

    Deliberately not ``lstrip('www.')``: that strips a *character set*, so
    ``web3.foundation`` became ``eb3.foundation`` and ``w3.org`` became
    ``3.org``. The damage was not cosmetic — a legitimate subdomain such as
    ``docs.web3.foundation`` then failed the ``endswith`` suffix test against
    the mangled official host, was classified Tier 3, and could never satisfy
    the GREEN gate's Tier 1 requirement.
    """
    host = (value or '').strip().lower().rstrip('.')
    return host[4:] if host.startswith('www.') else host


def classify_tier(url: str, official_hosts: set[str] | None = None) -> str:
    """Assign a controller source tier to a URL.

    An official-project host is Tier 1. A named Sharia screener carries its
    own tier. Everything else is Tier 3 at best, which the GREEN gate will
    not accept as the required Tier 1 evidence.
    """
    host = _canonical_host(urlparse(url).hostname or '')
    for screener_host, tier in _SCREENER_TIERS.items():
        if host == screener_host or host.endswith('.' + screener_host):
            return tier
    for official in {_canonical_host(h) for h in (official_hosts or set())}:
        if official and (host == official or host.endswith('.' + official)):
            return 'TIER_1_OFFICIAL'
    return 'TIER_3_SECONDARY'


@dataclass(frozen=True)
class FetchResult:
    url: str
    http_status: int
    content_sha256: str
    text: str
    retrieved_utc: str
    tier: str
    identity_match: bool = False
    error: str = ''

    @property
    def ok(self) -> bool:
        return self.http_status == 200 and not self.error and bool(self.text)

    def as_evidence_record(self, index: int) -> dict:
        """Shape required by the local-oracle-v1 evidence provider schema."""
        return {
            'id': f'ret_{index}',
            'status': 'completed',
            'action_type': 'open_page',
            'source_urls': [self.url],
            'url': self.url,
            'retrieved_utc': self.retrieved_utc,
            'http_status': self.http_status,
            'content_sha256': self.content_sha256,
            'source_tier': self.tier,
        }


def assert_fetchable(url: str) -> str:
    """Return the canonical URL or raise. HTTPS only; never an AI host."""
    canonical = normalize_evidence_url(url)
    if not canonical:
        raise RetrievalBlocked(f'not a fetchable HTTPS URL: {url!r}')
    host = (urlparse(canonical).hostname or '').lower()
    if host in AI_ENDPOINT_HOSTS or any(
            host.endswith('.' + h) for h in AI_ENDPOINT_HOSTS):
        raise RetrievalBlocked(
            f'refusing to contact model-inference host {host!r}: screening '
            'must not depend on an external AI service')
    return canonical


class Retriever:
    """Fetches and attests sources. One instance per screening run."""

    def __init__(self, *, session: requests.Session | None = None,
                 timeout: int = TIMEOUT_SECONDS, max_bytes: int = MAX_BYTES):
        self.session = session or requests.Session()
        self.timeout = timeout
        self.max_bytes = max_bytes

    def fetch(self, url: str, *, official_hosts: set[str] | None = None,
              identity_match: bool = False) -> FetchResult:
        now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        try:
            canonical = assert_fetchable(url)
        except RetrievalBlocked as exc:
            return FetchResult(url=str(url), http_status=0, content_sha256='',
                               text='', retrieved_utc=now,
                               tier='TIER_4_WEAK_REJECT', error=str(exc))
        tier = classify_tier(canonical, official_hosts)
        try:
            response = self.session.get(
                canonical, timeout=self.timeout, allow_redirects=True,
                stream=True, headers={'User-Agent': 'binance-sharia-screener/1.0'})
            # A redirect chain must not leave HTTPS or land on an AI host.
            for hop in list(response.history) + [response]:
                assert_fetchable(hop.url)
            body = b''
            for chunk in response.iter_content(65536):
                body += chunk
                if len(body) > self.max_bytes:
                    return FetchResult(
                        url=canonical, http_status=response.status_code,
                        content_sha256='', text='', retrieved_utc=now, tier=tier,
                        error=f'body exceeded {self.max_bytes} bytes')
        except RetrievalBlocked as exc:
            return FetchResult(url=canonical, http_status=0, content_sha256='',
                               text='', retrieved_utc=now, tier=tier,
                               error=f'redirect blocked: {exc}')
        except Exception as exc:
            return FetchResult(url=canonical, http_status=0, content_sha256='',
                               text='', retrieved_utc=now, tier=tier,
                               error=f'{type(exc).__name__}: {exc}')

        digest = hashlib.sha256(body).hexdigest()
        if response.status_code != 200:
            return FetchResult(url=canonical, http_status=response.status_code,
                               content_sha256=digest, text='', retrieved_utc=now,
                               tier=tier, error=f'HTTP {response.status_code}')
        content_type = (response.headers.get('Content-Type') or '').lower()
        charset = response.encoding or 'utf-8'
        try:
            payload = body.decode(charset, errors='replace')
        except LookupError:
            payload = body.decode('utf-8', errors='replace')
        text = html_to_text(payload) if 'html' in content_type else \
            re.sub(r'\s+', ' ', payload).strip()
        return FetchResult(url=canonical, http_status=200, content_sha256=digest,
                           text=text, retrieved_utc=now, tier=tier,
                           identity_match=identity_match)
