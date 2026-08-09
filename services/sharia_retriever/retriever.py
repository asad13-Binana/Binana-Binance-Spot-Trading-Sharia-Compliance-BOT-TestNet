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
import io
import ipaddress
import logging
import re
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import requests
from pypdf import PdfReader

from services.common.sharia_v19 import normalize_evidence_url
from services.sharia_retriever.store import EvidenceStore, EvidenceStoreError

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
MAX_PDF_PAGES = 250
MAX_EXTRACTED_TEXT_CHARS = 2_000_000


class RetrievalBlocked(RuntimeError):
    """The URL may not be fetched at all (scheme, host, or policy)."""


class _TextExtractor(HTMLParser):
    """Collect visible text, dropping script/style/head noise.

    Skip state is a *stack of open container tags*, not a counter. An earlier
    version counted depth and listed the void elements ``meta`` and ``link``
    as skippable. Void elements never emit an end tag, so the counter never
    returned to zero and an entirely ordinary page — any page with
    ``<meta charset>`` in its head, i.e. effectively all of them — yielded an
    empty document. Every screening would then have failed closed for the
    wrong reason. Void elements carry no text and are simply not tracked.
    """

    # Container elements whose contents are not visible page text.
    _SKIP = frozenset(
        {'script', 'style', 'head', 'noscript', 'svg', 'template', 'title'})
    # Void elements: no end tag, no text content, never tracked.
    _VOID = frozenset({
        'meta', 'link', 'br', 'hr', 'img', 'input', 'source', 'area',
        'base', 'col', 'embed', 'param', 'track', 'wbr',
    })

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_stack: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self._VOID:
            return
        if tag in self._SKIP:
            self._skip_stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        return  # <foo /> opens and closes; nothing to track

    def handle_endtag(self, tag):
        if tag in self._VOID:
            return
        if tag in self._skip_stack:
            # Unwind to this tag so stray/misnested end tags cannot strand
            # the extractor in a permanently skipping state.
            while self._skip_stack:
                if self._skip_stack.pop() == tag:
                    break

    def handle_data(self, data):
        if not self._skip_stack and data.strip():
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
    except Exception:  # noqa: BLE001 - malformed markup must fail closed
        log.warning('HTML parse failed; falling back to tag stripping')
        return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', payload)).strip()
    return re.sub(r'\s+', ' ', parser.text()).strip()


def pdf_to_text(payload: bytes) -> str:
    """Extract bounded text from a PDF or fail closed.

    Exact PDF bytes are retained separately by ``EvidenceStore``. Encrypted,
    malformed, image-only, oversized-page-count and text-bomb PDFs are not
    treated as evidence.
    """
    try:
        reader = PdfReader(io.BytesIO(payload), strict=True)
        if reader.is_encrypted:
            raise RetrievalBlocked('encrypted PDF cannot be screened')
        if not reader.pages or len(reader.pages) > MAX_PDF_PAGES:
            raise RetrievalBlocked(
                f'PDF page count must be within 1..{MAX_PDF_PAGES}')
        chunks: list[str] = []
        size = 0
        for page in reader.pages:
            text = str(page.extract_text() or '').strip()
            size += len(text)
            if size > MAX_EXTRACTED_TEXT_CHARS:
                raise RetrievalBlocked(
                    f'PDF extracted text exceeded '
                    f'{MAX_EXTRACTED_TEXT_CHARS} characters')
            if text:
                chunks.append(text)
    except RetrievalBlocked:
        raise
    except Exception as exc:
        raise RetrievalBlocked(f'PDF text extraction failed: {exc}') from exc
    normalized = re.sub(r'\s+', ' ', ' '.join(chunks)).strip()
    if not normalized:
        raise RetrievalBlocked('PDF contains no extractable text')
    return normalized


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
    return host.removeprefix('www.')


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
    content_path: str = ''
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
            'content_path': self.content_path,
            'source_tier': self.tier,
        }


def _addresses_for(host: str) -> list[ipaddress._BaseAddress]:
    """Resolve a host to IP objects. An IP literal resolves to itself."""
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise RetrievalBlocked(f'cannot resolve {host!r}: {exc}') from exc
    resolved = []
    for info in infos:
        try:
            resolved.append(ipaddress.ip_address(info[4][0]))
        except ValueError:
            continue
    if not resolved:
        raise RetrievalBlocked(f'{host!r} resolved to no usable address')
    return resolved


def assert_fetchable(url: str) -> str:
    """Return the canonical URL or raise.

    HTTPS only, never a model-inference host, and never an address that is not
    globally routable. The address check is the substantive control: a
    hostname blacklist alone cannot prove no paid AI is used, because any
    proxy or new hostname walks around it. Refusing loopback, private,
    link-local, multicast and reserved destinations is what stops the
    screening service from being turned into an SSRF primitive against the
    Oracle host or its cloud metadata endpoint.
    """
    canonical = normalize_evidence_url(url)
    if not canonical:
        raise RetrievalBlocked(f'not a fetchable HTTPS URL: {url!r}')
    try:
        parsed = urlparse(canonical)
        host = (parsed.hostname or '').lower()
        port = parsed.port
    except ValueError as exc:
        # urlparse raises on a malformed port, e.g. a bracketed IPv6 literal
        # that survived canonicalisation. Fail closed rather than letting the
        # ValueError escape as an unhandled error.
        raise RetrievalBlocked(f'unparseable URL authority in {url!r}: {exc}') from exc
    if port not in (None, 443):
        raise RetrievalBlocked(f'refusing non-443 port {port} on {host!r}')
    if not host:
        raise RetrievalBlocked(f'no host in {url!r}')
    if host in AI_ENDPOINT_HOSTS or any(
            host.endswith('.' + h) for h in AI_ENDPOINT_HOSTS):
        raise RetrievalBlocked(
            f'refusing to contact model-inference host {host!r}: screening '
            'must not depend on an external AI service')
    for address in _addresses_for(host):
        if not address.is_global or address.is_multicast:
            raise RetrievalBlocked(
                f'refusing non-public destination {address} for host {host!r} '
                '(loopback, private, link-local, multicast or reserved)')
    return canonical


def _same_redirect_origin(current: str, target: str) -> bool:
    """Allow only HTTPS redirects that preserve the asserted source origin.

    A Tier-1 project URL must not become a proof channel for content fetched
    from an unrelated public host. ``www.`` is treated as the same canonical
    host; all other host or effective-port changes fail closed.
    """
    try:
        before = urlparse(current)
        after = urlparse(target)
        before_host = _canonical_host(before.hostname or '')
        after_host = _canonical_host(after.hostname or '')
        before_port = before.port or 443
        after_port = after.port or 443
    except ValueError:
        return False
    return (before.scheme.lower() == after.scheme.lower() == 'https' and
            before_host == after_host and before_port == after_port)


class Retriever:
    """Fetches and attests sources. One instance per screening run."""

    def __init__(self, *, session: requests.Session | None = None,
                 timeout: int = TIMEOUT_SECONDS, max_bytes: int = MAX_BYTES,
                 evidence_store: EvidenceStore | None = None):
        self.session = session or requests.Session()
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.evidence_store = evidence_store

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
            # Redirects are followed MANUALLY. With allow_redirects=True the
            # library completes the whole chain before returning, so
            # inspecting response.history afterwards reported a blocked hop
            # only after the prohibited request had already been made. Each
            # Location is now validated before the next request is issued,
            # and MAX_REDIRECTS is actually enforced.
            target = canonical
            response = None
            for hop in range(MAX_REDIRECTS + 1):
                response = self.session.get(
                    target, timeout=self.timeout, allow_redirects=False,
                    stream=True,
                    headers={'User-Agent': 'binance-sharia-screener/1.0'})
                if response.status_code not in (301, 302, 303, 307, 308):
                    break
                location = response.headers.get('Location') or ''
                response.close()
                if hop == MAX_REDIRECTS:
                    return FetchResult(
                        url=canonical, http_status=0, content_sha256='',
                        text='', retrieved_utc=now, tier=tier,
                        error=f'exceeded {MAX_REDIRECTS} redirects')
                next_target = assert_fetchable(urljoin(target, location))
                if not _same_redirect_origin(target, next_target):
                    return FetchResult(
                        url=canonical, http_status=0, content_sha256='',
                        text='', retrieved_utc=now, tier=tier,
                        error=f'cross-origin redirect blocked: {target} -> '
                              f'{next_target}')
                target = next_target
            # The final URL is the URL whose bytes were actually hashed.  Do
            # not attest the original URL/tier after a redirect.
            final_url = target
            tier = classify_tier(final_url, official_hosts)
            body = b''
            try:
                for chunk in response.iter_content(65536):
                    body += chunk
                    if len(body) > self.max_bytes:
                        return FetchResult(
                            url=final_url, http_status=response.status_code,
                            content_sha256='', text='', retrieved_utc=now,
                            tier=tier,
                            error=f'body exceeded {self.max_bytes} bytes')
            finally:
                response.close()
        except RetrievalBlocked as exc:
            return FetchResult(url=canonical, http_status=0, content_sha256='',
                               text='', retrieved_utc=now, tier=tier,
                               error=f'redirect blocked: {exc}')
        except Exception as exc:  # noqa: BLE001 - network failures fail closed
            return FetchResult(url=canonical, http_status=0, content_sha256='',
                               text='', retrieved_utc=now, tier=tier,
                               error=f'{type(exc).__name__}: {exc}')

        digest = hashlib.sha256(body).hexdigest()
        if response.status_code != 200:
            return FetchResult(url=final_url, http_status=response.status_code,
                               content_sha256=digest, text='', retrieved_utc=now,
                               tier=tier, error=f'HTTP {response.status_code}')
        content_type = (response.headers.get('Content-Type') or '').lower()
        is_pdf = 'application/pdf' in content_type or body.startswith(b'%PDF-')
        try:
            if is_pdf:
                text = pdf_to_text(body)
            else:
                charset = response.encoding or 'utf-8'
                try:
                    payload = body.decode(charset, errors='replace')
                except LookupError:
                    payload = body.decode('utf-8', errors='replace')
                text = (html_to_text(payload) if 'html' in content_type else
                        re.sub(r'\s+', ' ', payload).strip())
        except RetrievalBlocked as exc:
            return FetchResult(
                url=final_url, http_status=response.status_code,
                content_sha256=digest, text='', retrieved_utc=now,
                tier=tier, error=str(exc))
        content_path = ''
        if self.evidence_store is not None:
            try:
                stored_digest, content_path = self.evidence_store.put(body)
            except EvidenceStoreError as exc:
                return FetchResult(
                    url=final_url, http_status=0, content_sha256='', text='',
                    retrieved_utc=now, tier=tier,
                    error=f'evidence storage failed: {exc}')
            if stored_digest != digest:
                return FetchResult(
                    url=final_url, http_status=0, content_sha256='', text='',
                    retrieved_utc=now, tier=tier,
                    error='evidence storage returned a mismatched digest')
        return FetchResult(
            url=final_url, http_status=200, content_sha256=digest,
            content_path=content_path, text=text, retrieved_utc=now, tier=tier,
            identity_match=identity_match)
