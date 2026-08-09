"""Tests for the registry seeding helper.

The helper exists to remove transcription, not to remove owner judgement.
These tests pin the safety boundaries: it must refuse to invent an official
host, and `apply` must reject a quote that does not occur in the retrieved
bytes -- otherwise a hand-edited draft could bind a fabricated quote to a
real source and the whole content-hash chain would mean nothing.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

sys.path.insert(0, str(ROOT / 'scripts'))
import seed_source_registry as seed  # noqa: E402

from services.common.sharia_v19 import SCREENER_HOSTS, SCREENER_SITES  # noqa: E402

OFFICIAL_HOST = 'exampleproject.org'
OFFICIAL_URL = f'https://{OFFICIAL_HOST}/docs'
OFFICIAL_TEXT = (
    'EXP is a native payment token used to pay network transaction fees on '
    'the chain. Protocol revenue consists solely of transaction fees paid by '
    'users for network use. Validator rewards are variable and derive from '
    'network inflation.'
)
VERDICT_TEXT = 'The asset EXP is assessed as halal for spot holding by this screener.'


class _Resp:
    status_code = 200
    history: list = []

    def __init__(self, body: bytes, url: str):
        self._body = body
        self.url = url
        self.headers = {'Content-Type': 'text/plain'}
        self.encoding = 'utf-8'

    def close(self):
        pass

    def iter_content(self, _n):
        yield self._body


class _Session:
    """Serves fixed bodies; never touches the network."""

    def get(self, url, **_kwargs):
        if OFFICIAL_HOST in url:
            return _Resp(OFFICIAL_TEXT.encode(), url)
        return _Resp(VERDICT_TEXT.encode(), url)


def _screener_sources():
    return [{'url': f'https://{min(SCREENER_HOSTS[name])}/exp',
             'identity_match': False} for name in sorted(SCREENER_SITES)]


class SeedHelperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.request = self.tmp / 'request.json'
        self.draft = self.tmp / 'draft.json'
        self.review = self.tmp / 'review.txt'
        self.evidence = self.tmp / 'evidence'
        self.registry = self.tmp / 'registry.json'
        self.request.write_text(json.dumps({'EXP': {
            'official_hosts': [OFFICIAL_HOST],
            'sources': [{'url': OFFICIAL_URL, 'identity_match': True},
                        *_screener_sources()],
        }}), encoding='utf-8')

    def _propose(self):
        with mock.patch('services.sharia_retriever.retriever.requests.Session',
                        _Session), \
             mock.patch('services.sharia_retriever.retriever._addresses_for',
                        lambda h: [__import__('ipaddress').ip_address('93.184.216.34')]):
            return seed.main(['propose', str(self.request),
                              '--draft', str(self.draft),
                              '--review', str(self.review),
                              '--evidence-dir', str(self.evidence)])

    def _apply(self):
        with mock.patch('services.sharia_retriever.retriever.requests.Session',
                        _Session), \
             mock.patch('services.sharia_retriever.retriever._addresses_for',
                        lambda h: [__import__('ipaddress').ip_address('93.184.216.34')]):
            return seed.main(['apply', str(self.draft),
                              '--registry', str(self.registry),
                              '--evidence-dir', str(self.evidence)])

    def test_propose_writes_candidate_quotes_from_the_fetched_bytes(self):
        self._propose()
        draft = json.loads(self.draft.read_text(encoding='utf-8'))
        entry = draft['assets']['EXP']
        for name in ('token_type', 'utility', 'revenue'):
            quote = entry['claims'][name]['quote']
            self.assertTrue(quote, f'{name} had no candidate')
            self.assertIn(quote.lower(), OFFICIAL_TEXT.lower(),
                          f'{name} quote is not verbatim from the source')
        self.assertEqual(set(entry['screeners']), set(SCREENER_SITES))
        for name in SCREENER_SITES:
            self.assertEqual(entry['screeners'][name]['value'], 'halal')

    def test_official_hosts_are_never_inferred_from_the_symbol(self):
        self.request.write_text(json.dumps(
            {'EXP': {'sources': [{'url': OFFICIAL_URL, 'identity_match': True}]}}),
            encoding='utf-8')
        self.assertEqual(self._propose(), 1)
        draft = json.loads(self.draft.read_text(encoding='utf-8'))
        self.assertNotIn('EXP', draft['assets'])

    def test_apply_rejects_a_quote_that_is_not_in_the_retrieved_bytes(self):
        self._propose()
        draft = json.loads(self.draft.read_text(encoding='utf-8'))
        for name in ('token_type', 'utility', 'revenue'):
            draft['assets']['EXP']['claims'][name]['value'] = 'PAYMENT'
        # A fabricated quote, as if the draft were edited by hand.
        draft['assets']['EXP']['claims']['utility']['quote'] = (
            'EXP guarantees an eight percent annual return to all holders.')
        self.draft.write_text(json.dumps(draft), encoding='utf-8')
        self.assertEqual(self._apply(), 1)
        self.assertFalse(self.registry.exists(),
                         'registry must not be written when a quote is fabricated')

    def test_apply_writes_the_registry_when_every_quote_verifies(self):
        self._propose()
        draft = json.loads(self.draft.read_text(encoding='utf-8'))
        draft['assets']['EXP']['claims']['token_type']['value'] = 'PAYMENT'
        draft['assets']['EXP']['claims']['utility']['value'] = 'real utility'
        draft['assets']['EXP']['claims']['revenue']['value'] = 'clean'
        self.draft.write_text(json.dumps(draft), encoding='utf-8')
        self.assertEqual(self._apply(), 0)
        written = json.loads(self.registry.read_text(encoding='utf-8'))
        self.assertIn('EXP', written['assets'])

    def test_apply_rejects_an_empty_value(self):
        self._propose()  # values are left blank for the owner to fill
        self.assertEqual(self._apply(), 1)
        self.assertFalse(self.registry.exists())


class QuoteHelperTests(unittest.TestCase):
    def test_quote_membership_is_whitespace_insensitive(self):
        self.assertTrue(seed.quote_is_in('a  b   c', 'x a b c y'))
        self.assertFalse(seed.quote_is_in('a b d', 'x a b c y'))

    def test_verdict_inference_handles_negative_wording(self):
        self.assertEqual(seed.infer_verdict('This asset is non-compliant.'), 'haram')
        self.assertEqual(seed.infer_verdict('This asset is compliant.'), 'halal')
        self.assertEqual(seed.infer_verdict('Status is doubtful here.'), 'doubtful')
        self.assertEqual(seed.infer_verdict('No verdict wording at all.'), '')

    def test_screener_is_identified_only_by_its_own_host(self):
        self.assertEqual(seed.screener_for('https://musaffa.com/asset/exp'), 'musaffa')
        self.assertIsNone(seed.screener_for('https://evil.example/musaffa'))


if __name__ == '__main__':
    unittest.main()
