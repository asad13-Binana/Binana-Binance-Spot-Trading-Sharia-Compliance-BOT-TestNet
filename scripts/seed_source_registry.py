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
    apply     re-verify every quote against the stored bytes and merge the
              reviewed draft into the registry

Safety boundaries, all enforced here rather than documented:

* An official host is never derived from the asset symbol.
* A registry entry is not an approval. The owner's signed Telegram decision
  is still required before anything becomes tradeable.
* Every proposed quote must occur verbatim in the stored bytes for its URL.
* A blocked, failed or empty retrieval fails closed; it never yields a claim.
* ``apply`` re-reads the stored bytes and re-checks each quote, so editing the
  draft by hand cannot introduce a quote the source does not contain.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

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

MIN_QUOTE_WORDS = 6
MAX_CANDIDATES = 4

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


def sentences(text: str) -> list[str]:
    parts = re.split(r'(?<=[.!?])\s+', ' '.join((text or '').split()))
    return [p.strip() for p in parts if len(p.split()) >= MIN_QUOTE_WORDS]


def candidates(text: str, cues) -> list[str]:
    """Sentences containing a cue, longest first. Deterministic, no scoring."""
    found = []
    for sentence in sentences(text):
        low = sentence.lower()
        if any(cue in low for cue in cues):
            found.append(sentence)
    found.sort(key=lambda s: -len(s.split()))
    return found[:MAX_CANDIDATES]


def infer_verdict(quote: str) -> str:
    low = quote.lower()
    if 'non-compliant' in low or 'not compliant' in low or 'impermissible' in low:
        return 'haram'
    if 'haram' in low:
        return 'haram'
    if 'doubtful' in low:
        return 'doubtful'
    if 'halal' in low or 'compliant' in low or 'permissible' in low:
        return 'halal'
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
    store = EvidenceStore(args.evidence_dir)
    retriever = Retriever(evidence_store=store)
    drafts, review = {}, []
    failed = 0

    for base, spec in request.items():
        base = str(base).strip().upper()
        official_hosts = [str(h).strip() for h in spec.get('official_hosts') or []]
        if not official_hosts:
            print(f'{base}: SKIPPED - you must supply official_hosts; '
                  'this tool will not infer them from the symbol',
                  file=sys.stderr)
            failed += 1
            continue
        host_set = set(official_hosts)
        review.append(f'\n{"=" * 74}\n{base}   official hosts: {", ".join(official_hosts)}\n{"=" * 74}')
        sources, texts = [], {}
        for source in spec.get('sources') or []:
            url = str(source.get('url', ''))
            identity = bool(source.get('identity_match'))
            result = retriever.fetch(url, official_hosts=host_set,
                                     identity_match=identity)
            if not result.ok:
                review.append(f'  FETCH FAILED  {url}\n                {result.error}')
                failed += 1
                continue
            sources.append({'url': result.url, 'identity_match': identity})
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
                    picked = {'value': '', 'quote': options[0], 'url': url}
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
            screeners[name] = {'value': infer_verdict(options[0]),
                               'quote': options[0], 'url': url}
            review.append(f'\n  [screener {name}] proposed verdict '
                          f'"{screeners[name]["value"] or "<blank - fill in>"}"\n'
                          f'    "{options[0]}"')
        missing = sorted(SCREENER_SITES - set(screeners))
        if missing:
            review.append(f'\n  MISSING SCREENERS: {missing}\n'
                          '    add a source URL on each screener\'s own domain')
            failed += 1

        drafts[base] = {'official_hosts': official_hosts, 'sources': sources,
                        'claims': claims, 'screeners': screeners}

    Path(args.draft).write_text(
        json.dumps({'schema_version': 1, 'assets': drafts}, indent=2) + '\n',
        encoding='utf-8')
    Path(args.review).write_text('\n'.join(review) + '\n', encoding='utf-8')
    print(f'draft written  : {args.draft}')
    print(f'review sheet   : {args.review}')
    print(f'evidence bytes : {args.evidence_dir}')
    print()
    print('NEXT: read the review sheet, correct any quote or value in the draft,')
    print('      fill every empty "value", then run this script with `apply`.')
    if failed:
        print(f'\n{failed} item(s) need your attention before apply will succeed.')
    return 1 if failed else 0


def do_apply(args) -> int:
    draft = json.loads(Path(args.draft).read_text(encoding='utf-8'))
    store = EvidenceStore(args.evidence_dir)
    retriever = Retriever(evidence_store=store)
    registry_path = Path(args.registry)
    current = SourceRegistry(registry_path).load()
    problems = []

    for base, entry in (draft.get('assets') or {}).items():
        texts = {}
        for source in entry.get('sources') or []:
            result = retriever.fetch(
                source['url'],
                official_hosts=set(entry.get('official_hosts') or []),
                identity_match=bool(source.get('identity_match')))
            if not result.ok:
                problems.append(f'{base}: {source["url"]} no longer retrievable '
                                f'({result.error})')
                continue
            texts[result.url] = result.text
        for name, claim in {**entry.get('claims', {}),
                            **entry.get('screeners', {})}.items():
            value = str(claim.get('value', '')).strip()
            quote = str(claim.get('quote', '')).strip()
            url = normalize_evidence_url(claim.get('url'))
            if not value:
                problems.append(f'{base}.{name}: value is empty')
            if not quote:
                problems.append(f'{base}.{name}: quote is empty')
            elif url not in texts:
                problems.append(f'{base}.{name}: url {url} was not retrieved')
            elif not quote_is_in(quote, texts[url]):
                problems.append(
                    f'{base}.{name}: quote does NOT appear in the retrieved '
                    f'bytes for {url} - it was edited or the page changed')
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
    return args.func(args)


if __name__ == '__main__':
    raise SystemExit(main())
