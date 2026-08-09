"""Propose Sharia source-registry entries for owner review. No AI, no guessing.

The registry deliberately never infers which website belongs to a ticker: a
ticker collision would bind the wrong project's site to a Binance asset. That
safety property costs the owner roughly ten exact verbatim quotes per asset,
transcribed by hand, which for a realistic universe is several hundred.

This tool removes the transcription without weakening the rule. The owner
still supplies and confirms the canonical identity and every official host;
the tool only fetches those confirmed URLs, retains the exact bytes, and
*proposes* candidate quotes that the owner accepts or corrects.

Two phases, deliberately separated so nothing is written from a single run:

    propose   fetch the owner's confirmed URLs, store the exact bytes, and
              write a draft entry plus a readable review sheet
    apply     re-fetch every source, require its SHA-256 to match the exact
              reviewed bytes, then merge the reviewed draft into the registry

Safety boundaries, all enforced here rather than documented:

* An official host is never derived from the asset symbol.
* A registry entry is not an approval. The owner's signed Telegram decision
  is still required before anything becomes tradeable.
* Every proposed quote must occur verbatim in the stored bytes for its URL.
* A blocked, failed or empty retrieval fails closed; it never yields a claim.
* ``apply`` requires the re-fetched SHA-256 to match the proposal and re-checks
  each quote, so neither a changed page nor a hand-edited quote is accepted.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.common.sharia_v19 import (  # noqa: E402
    SCREENER_HOSTS, SCREENER_SITES, normalize_evidence_url,
)
from services.sharia_retriever.retriever import Retriever  # noqa: E402
from services.sharia_retriever.store import EvidenceStore  # noqa: E402
from services.sharia_screener.source_registry import (  # noqa: E402
    SourceRegistry, SourceRegistryError,
)
from services.sharia_screener.evidence_binding import (  # noqa: E402
    EXTRACTOR_VERSION,
    EvidenceBindingError,
    bind_reviewed_block,
    extracted_text_sha256,
    text_blocks,
    verify_reviewed_block,
)
from services.sharia_screener.verdict_policy import (  # noqa: E402
    CANONICAL_SCREENER_VERDICTS,
    canonical_screener_verdict,
    positive_verdict_conflict,
)

MIN_QUOTE_WORDS = 6
MAX_CANDIDATES = 4


def require_pinned_proxy_environment() -> None:
    """Refuse administrative fetches outside the isolated proxy container."""
    enabled = os.environ.get(
        'SHARIA_PINNED_EGRESS_PROXY', '').strip().lower() == 'true'
    proxy = os.environ.get('HTTPS_PROXY') or os.environ.get('https_proxy') or ''
    try:
        parsed = urlparse(proxy)
        valid_proxy = (
            parsed.scheme == 'http' and parsed.hostname == 'sharia-egress-proxy'
            and parsed.port == 8080 and not parsed.username and
            not parsed.password and not parsed.path.rstrip('/'))
    except ValueError:
        valid_proxy = False
    if not enabled or not valid_proxy:
        raise RuntimeError(
            'registry fetches must run inside the network-isolated '
            'sharia-screener container with HTTPS_PROXY='
            'http://sharia-egress-proxy:8080')

# Deterministic cues for each required claim. These select *candidate*
# sentences for the owner to read; they never decide anything.
CLAIM_CUES = {
    'token_type': (
        'payment', 'utility token', 'governance token', 'stablecoin',
        'wrapped', 'native token', 'native asset', 'native currency',
        'is a token', 'token is', 'medium of exchange',
    ),
    'utility': (
        'used to pay', 'used for', 'pays for', 'enables', 'powers',
        'secures the network', 'transaction fee', 'gas fee', 'network fee',
        'stake', 'staking', 'settle', 'settlement',
    ),
    'revenue': (
        'revenue', 'fees are', 'fee revenue', 'treasury', 'protocol income',
        'protocol earns', 'burned', 'buyback', 'distributed to',
    ),
}
VERDICT_CUES = ('halal', 'haram', 'compliant', 'non-compliant',
                'not compliant', 'permissible', 'impermissible', 'doubtful')

def positive_value_conflicts_with_quote(
        value: str, quote: str, *,
        permitted_provider_identifiers=(), permitted_asset_identifiers=()) -> str:
    """Backward-compatible wrapper around the shared fail-closed policy."""
    return positive_verdict_conflict(
        value, quote,
        permitted_provider_identifiers=permitted_provider_identifiers,
        permitted_asset_identifiers=permitted_asset_identifiers)


def strict_bool(value, field: str):
    """Require a real JSON boolean.

    ``bool("false")`` is ``True`` in Python, so accepting a string here would
    silently promote an unconfirmed source to an identity-matched official
    one — the exact substitution the registry exists to prevent.
    """
    if not isinstance(value, bool):
        raise ValueError(
            f'{field} must be a JSON true/false, got {type(value).__name__} '
            f'{value!r}')
    return value


def sentences(text: str) -> list[str]:
    parts = re.split(r'(?<=[.!?])\s+', ' '.join((text or '').split()))
    return [p.strip() for p in parts if len(p.split()) >= MIN_QUOTE_WORDS]


def candidates(text: str, cues) -> list[str]:
    """Complete extracted-text blocks containing a cue, longest first.

    This intentionally does not split English sentences.  Sentence guessing
    repeatedly cut negative prefixes at unrecognised abbreviations.  A block
    is emitted by the HTML/PDF extractor, offset-bound, shown to the owner and
    replayed exactly at runtime.
    """
    found = []
    for block in text_blocks(text):
        candidate = block.text
        if len(candidate.split()) < MIN_QUOTE_WORDS:
            continue
        low = candidate.lower()
        if any(cue in low for cue in cues):
            found.append(candidate)
    found.sort(key=lambda s: -len(s.split()))
    return found[:MAX_CANDIDATES]


def infer_verdict(quote: str) -> str:
    """Deliberately always returns '' — this tool never reads a verdict.

    An earlier version pattern-matched the sentence and returned a verdict.
    It read "This token is not Shariah compliant." as **halal**, because
    "not compliant" is not a substring of "not Shariah compliant" and the
    fallback matched the bare word "compliant". Four separate negative
    phrasings produced halal. The unit test missed it because the fixture
    used the one phrasing that happened to work.

    No keyword rule decides a religious ruling here. The owner reads the
    quote and types the value; ``apply`` then refuses a positive value whose
    own quote contains negative or ambiguous wording.
    """
    return ''


def screener_for(url: str) -> str | None:
    host = (normalize_evidence_url(url) or '').split('/')[2:3]
    host = host[0].lower() if host else ''
    host = host.removeprefix('www.')
    for name, hosts in SCREENER_HOSTS.items():
        if any(host == h or host.endswith('.' + h) for h in hosts):
            return name
    return None


def quote_is_in(quote: str, text: str) -> bool:
    return ' '.join(quote.split()).lower() in ' '.join(text.split()).lower()


def do_propose(args) -> int:
    request = json.loads(Path(args.input).read_text(encoding='utf-8'))
    if not isinstance(request, dict):
        raise ValueError('proposal input must be a JSON object keyed by asset')
    store = EvidenceStore(args.evidence_dir)
    retriever = Retriever(evidence_store=store)
    drafts, review = {}, []
    failed = 0

    for base, spec in request.items():
        base = str(base).strip().upper()
        if not isinstance(spec, dict):
            print(f'{base}: SKIPPED - asset specification must be an object',
                  file=sys.stderr)
            failed += 1
            continue
        raw_hosts = spec.get('official_hosts') or []
        if not isinstance(raw_hosts, list):
            print(f'{base}: SKIPPED - official_hosts must be an array',
                  file=sys.stderr)
            failed += 1
            continue
        official_hosts = [str(h).strip() for h in raw_hosts]
        if not official_hosts:
            print(f'{base}: SKIPPED - you must supply official_hosts; '
                  'this tool will not infer them from the symbol',
                  file=sys.stderr)
            failed += 1
            continue
        host_set = set(official_hosts)
        review.append(f'\n{"=" * 74}\n{base}   official hosts: {", ".join(official_hosts)}\n{"=" * 74}')
        sources, texts = [], {}
        requested_sources = spec.get('sources') or []
        if not isinstance(requested_sources, list):
            review.append('  REJECTED - sources must be an array')
            failed += 1
            continue
        for source in requested_sources:
            if not isinstance(source, dict):
                review.append('  REJECTED - every source must be an object')
                failed += 1
                continue
            url = str(source.get('url', ''))
            try:
                identity = strict_bool(source.get('identity_match'),
                                       f'{base} source {url} identity_match')
            except ValueError as exc:
                review.append(f'  REJECTED  {url}\n            {exc}')
                failed += 1
                continue
            result = retriever.fetch(url, official_hosts=host_set,
                                     identity_match=identity)
            if not result.ok:
                review.append(f'  FETCH FAILED  {url}\n                {result.error}')
                failed += 1
                continue
            # The digest is recorded so apply can prove it is verifying the
            # SAME bytes the owner reviewed. Without it, apply re-fetched the
            # page and would accept a materially changed version whenever the
            # selected sentence happened to survive the edit.
            sources.append({
                'url': result.url,
                'identity_match': identity,
                'content_sha256': result.content_sha256,
                'text_sha256': extracted_text_sha256(result.text),
                'extractor_version': EXTRACTOR_VERSION,
            })
            texts[result.url] = result.text
            review.append(f'  fetched  {result.url}\n'
                          f'           tier={result.tier} sha256={result.content_sha256[:16]}... '
                          f'{len(result.text.split())} words')

        identity_urls = [s['url'] for s in sources if s['identity_match']]
        if not identity_urls:
            review.append('  -> no identity-matched official source retrieved; '
                          'entry cannot be completed')
            failed += 1
            continue

        claims = {}
        for name, cues in CLAIM_CUES.items():
            picked = None
            for url in identity_urls:
                options = candidates(texts[url], cues)
                if options:
                    try:
                        picked = {
                            'value': '', 'quote': options[0], 'url': url,
                            **bind_reviewed_block(texts[url], options[0]),
                        }
                    except EvidenceBindingError as exc:
                        review.append(
                            f'\n  [{name}] AMBIGUOUS BLOCK - {exc}; choose '
                            'a source with a unique complete block')
                        failed += 1
                        continue
                    review.append(f'\n  [{name}] candidates from {url}:')
                    for i, option in enumerate(options, 1):
                        mark = '*' if i == 1 else ' '
                        review.append(f'    {mark}{i}. "{option}"')
                    break
            if picked is None:
                review.append(f'\n  [{name}] NO CANDIDATE FOUND - supply a quote by hand')
                picked = {'value': '', 'quote': '', 'url': identity_urls[0]}
                failed += 1
            claims[name] = picked

        screeners = {}
        for url, text in texts.items():
            name = screener_for(url)
            if not name:
                continue
            options = candidates(text, VERDICT_CUES)
            if not options:
                review.append(f'\n  [screener {name}] NO VERDICT SENTENCE FOUND at {url}')
                failed += 1
                continue
            # The value is left blank on purpose. This tool extracts evidence;
            # it does not read a religious ruling out of a sentence.
            try:
                screeners[name] = {
                    'value': '', 'quote': options[0], 'url': url,
                    **bind_reviewed_block(text, options[0]),
                }
            except EvidenceBindingError as exc:
                review.append(
                    f'\n  [screener {name}] AMBIGUOUS BLOCK at {url}: {exc}')
                failed += 1
                continue
            review.append(f'\n  [screener {name}] READ THIS AND ENTER THE VALUE '
                          f'YOURSELF ({url}). Allowed values: '
                          f'{", ".join(sorted(CANONICAL_SCREENER_VERDICTS))}:\n'
                          f'    "{options[0]}"')
            for extra in options[1:]:
                review.append(f'    (also found) "{extra}"')
        missing = sorted(SCREENER_SITES - set(screeners))
        if missing:
            review.append(f'\n  MISSING SCREENERS: {missing}\n'
                          '    add a source URL on each screener\'s own domain')
            failed += 1

        drafts[base] = {
            'official_hosts': official_hosts,
            'context_confirmed': False,
            'sources': sources,
            'claims': claims,
            'screeners': screeners,
        }

    Path(args.draft).write_text(
        json.dumps({'schema_version': 1, 'assets': drafts}, indent=2) + '\n',
        encoding='utf-8')
    Path(args.review).write_text('\n'.join(review) + '\n', encoding='utf-8')
    print(f'draft written  : {args.draft}')
    print(f'review sheet   : {args.review}')
    print(f'evidence bytes : {args.evidence_dir}')
    print()
    print('NEXT: read the review sheet, correct any quote or value in the draft,')
    print('      fill every empty "value", using only halal, haram, doubtful,')
    print('      or unknown for screener verdicts; read every bound context and')
    print('      set the asset-level "context_confirmed" to true, then run `apply`.')
    if failed:
        print(f'\n{failed} item(s) need your attention before apply will succeed.')
    return 1 if failed else 0


def do_apply(args) -> int:
    draft = json.loads(Path(args.draft).read_text(encoding='utf-8'))
    if (not isinstance(draft, dict) or draft.get('schema_version') != 1 or
            not isinstance(draft.get('assets'), dict)):
        print('REGISTRY NOT UPDATED - draft schema_version/assets is invalid',
              file=sys.stderr)
        return 1
    store = EvidenceStore(args.evidence_dir)
    retriever = Retriever(evidence_store=store)
    registry_path = Path(args.registry)
    current = SourceRegistry(registry_path).load()
    problems = []

    for base, entry in (draft.get('assets') or {}).items():
        if not isinstance(entry, dict):
            problems.append(f'{base}: asset entry must be an object')
            continue
        texts = {}
        try:
            context_confirmed = strict_bool(
                entry.get('context_confirmed'),
                f'{base} context_confirmed')
        except ValueError as exc:
            problems.append(f'{base}: {exc}')
            context_confirmed = False
        if not context_confirmed:
            problems.append(
                f'{base}: context_confirmed must be true after the owner reads '
                'every complete bound context in the review sheet')
        draft_sources = entry.get('sources') or []
        if not isinstance(draft_sources, list):
            problems.append(f'{base}: sources must be an array')
            continue
        for source in draft_sources:
            if not isinstance(source, dict):
                problems.append(f'{base}: every source must be an object')
                continue
            try:
                strict_bool(source.get('identity_match'),
                            f'{base} source {source.get("url")} identity_match')
            except ValueError as exc:
                problems.append(f'{base}: {exc}')
                continue
            proposed_digest = str(source.get('content_sha256', '')).strip()
            if re.fullmatch(r'[0-9a-f]{64}', proposed_digest) is None:
                problems.append(
                    f'{base}: {source.get("url")} has no proposed content_sha256; '
                    're-run propose so the reviewed bytes are recorded')
                continue
            if source.get('extractor_version') != EXTRACTOR_VERSION:
                problems.append(
                    f'{base}: {source.get("url")} has an unsupported extractor '
                    'version; re-run propose')
                continue
            proposed_text_digest = str(source.get('text_sha256', '')).strip()
            if re.fullmatch(r'[0-9a-f]{64}', proposed_text_digest) is None:
                problems.append(
                    f'{base}: {source.get("url")} has no extracted-text '
                    'SHA-256; re-run propose')
                continue
            result = retriever.fetch(
                source['url'],
                official_hosts=set(entry.get('official_hosts') or []),
                identity_match=source['identity_match'])
            if not result.ok:
                problems.append(f'{base}: {source["url"]} no longer retrievable '
                                f'({result.error})')
                continue
            # The owner reviewed specific bytes. If the page changed at all,
            # the surrounding context they judged is gone even when the one
            # selected sentence survives, so a fresh proposal and a fresh
            # review are required rather than a silent accept.
            if result.content_sha256 != proposed_digest:
                problems.append(
                    f'{base}: {source["url"]} CHANGED since it was reviewed '
                    f'(proposed {proposed_digest[:16]}..., now '
                    f'{result.content_sha256[:16]}...); re-run propose and '
                    're-review before applying')
                continue
            actual_text_digest = extracted_text_sha256(result.text)
            if actual_text_digest != proposed_text_digest:
                problems.append(
                    f'{base}: {source["url"]} extracted text changed since '
                    'review; re-run propose with this extractor version')
                continue
            texts[result.url] = result.text
        claims = entry.get('claims', {})
        screeners = entry.get('screeners', {})
        if not isinstance(claims, dict) or not isinstance(screeners, dict):
            problems.append(f'{base}: claims and screeners must be objects')
            continue
        for name, claim in {**claims, **screeners}.items():
            if not isinstance(claim, dict):
                problems.append(f'{base}.{name}: claim must be an object')
                continue
            value = str(claim.get('value', '')).strip()
            quote = str(claim.get('quote', '')).strip()
            url = normalize_evidence_url(claim.get('url'))
            if not value:
                problems.append(f'{base}.{name}: value is empty - the owner '
                                'must supply every verdict explicitly')
            if name in screeners:
                verdict = canonical_screener_verdict(value)
                if not verdict:
                    problems.append(
                        f'{base}.{name}: screener verdict must be exactly one of '
                        f'{sorted(CANONICAL_SCREENER_VERDICTS)}')
                else:
                    claim['value'] = verdict
            if not quote:
                problems.append(f'{base}.{name}: quote is empty')
            elif url not in texts:
                problems.append(
                    f'{base}.{name}: url {url} was not retrieved or failed '
                    'digest verification')
            else:
                ok, binding_error = verify_reviewed_block(
                    texts[url], quote, claim)
                if not ok:
                    problems.append(
                        f'{base}.{name}: exact evidence binding failed: '
                        f'{binding_error}; re-run propose and review again')
            is_screener = name in screeners
            # The quote is now one complete, exact, offset-bound source block.
            # The language policy therefore receives the reviewed block
            # directly and does not relocate a substring or guess where an
            # English sentence begins.
            conflict = positive_verdict_conflict(
                value, quote,
                permitted_provider_identifiers=({name} if is_screener else set()),
                permitted_asset_identifiers=({base} if is_screener else set()))
            if conflict:
                problems.append(f'{base}.{name}: {conflict}')
        current['assets'][str(base).strip().upper()] = entry

    if problems:
        print('REGISTRY NOT UPDATED. Fix these first:\n', file=sys.stderr)
        for problem in problems:
            print(f'  - {problem}', file=sys.stderr)
        return 1

    # Final gate: the registry's own loader must accept every entry.
    tmp = registry_path.with_suffix('.candidate.json')
    tmp.write_text(json.dumps(current, indent=2) + '\n', encoding='utf-8')
    try:
        candidate = SourceRegistry(tmp)
        for base in current['assets']:
            candidate.asset(base)
    except SourceRegistryError as exc:
        tmp.unlink(missing_ok=True)
        print(f'REGISTRY NOT UPDATED - loader rejected the result: {exc}',
              file=sys.stderr)
        return 1
    tmp.replace(registry_path)
    print(f'registry updated: {registry_path}')
    print(f'assets present  : {sorted(current["assets"])}')
    print()
    print('A registry entry is NOT an approval. Each asset still requires your')
    print('signed Telegram decision before it can ever become tradeable.')
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest='command', required=True)

    propose = sub.add_parser('propose', help='fetch confirmed sources, propose quotes')
    propose.add_argument('input', help='JSON of owner-confirmed hosts and source URLs')
    propose.add_argument('--draft', default='registry_draft.json')
    propose.add_argument('--review', default='registry_review.txt')
    propose.add_argument('--evidence-dir', default='runtime/sharia_evidence')
    propose.set_defaults(func=do_propose)

    apply_cmd = sub.add_parser('apply', help='verify the reviewed draft and merge it')
    apply_cmd.add_argument('draft')
    apply_cmd.add_argument('--registry',
                           default='shared/sharia/source_registry.json')
    apply_cmd.add_argument('--evidence-dir', default='runtime/sharia_evidence')
    apply_cmd.set_defaults(func=do_apply)

    args = parser.parse_args(argv)
    require_pinned_proxy_environment()
    return args.func(args)


if __name__ == '__main__':
    raise SystemExit(main())
