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
            # A quote is proposed; the verdict is not. The owner reads the
            # quote and types the value.
            self.assertTrue(entry['screeners'][name]['quote'])
            self.assertEqual(entry['screeners'][name]['value'], '')

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
        # The owner reads each screener quote and supplies the verdict.
        for name in draft['assets']['EXP']['screeners']:
            draft['assets']['EXP']['screeners'][name]['value'] = 'halal'
        self.draft.write_text(json.dumps(draft), encoding='utf-8')
        self.assertEqual(self._apply(), 0)
        written = json.loads(self.registry.read_text(encoding='utf-8'))
        self.assertIn('EXP', written['assets'])

    def test_apply_rejects_an_empty_value(self):
        self._propose()  # values are left blank for the owner to fill
        self.assertEqual(self._apply(), 1)
        self.assertFalse(self.registry.exists())

    def test_propose_leaves_every_screener_value_blank(self):
        self._propose()
        draft = json.loads(self.draft.read_text(encoding='utf-8'))
        for name, claim in draft['assets']['EXP']['screeners'].items():
            with self.subTest(screener=name):
                self.assertEqual(claim['value'], '',
                                 'the tool must not propose a verdict')

    def test_proposal_records_the_reviewed_content_digest(self):
        self._propose()
        draft = json.loads(self.draft.read_text(encoding='utf-8'))
        for source in draft['assets']['EXP']['sources']:
            with self.subTest(url=source['url']):
                self.assertRegex(source.get('content_sha256', ''),
                                 r'^[0-9a-f]{64}$')

    def test_apply_refuses_a_page_that_changed_after_review(self):
        # The owner reviewed version A. If the page becomes version B, the
        # context they judged is gone even when the selected sentence
        # survives the edit, so apply must refuse rather than accept.
        self._propose()
        draft = json.loads(self.draft.read_text(encoding='utf-8'))
        for name in ('token_type', 'utility', 'revenue'):
            draft['assets']['EXP']['claims'][name]['value'] = 'PAYMENT'
        for name in draft['assets']['EXP']['screeners']:
            draft['assets']['EXP']['screeners'][name]['value'] = 'halal'
        self.draft.write_text(json.dumps(draft), encoding='utf-8')

        changed = (OFFICIAL_TEXT +
                   ' The treasury also operates a lending market that pays '
                   'interest to depositors from borrower repayments.')
        original = _Session.get

        def mutated(self_, url, **kw):
            if OFFICIAL_HOST in url:
                return _Resp(changed.encode(), url)
            return _Resp(VERDICT_TEXT.encode(), url)

        try:
            _Session.get = mutated
            rc = self._apply()
        finally:
            _Session.get = original
        self.assertEqual(rc, 1)
        self.assertFalse(self.registry.exists(),
                         'a materially changed page must not be applied')

    def test_negative_screener_statement_cannot_be_applied_as_halal(self):
        negative = 'This asset is not Shariah compliant for spot holding.'
        original = _Session.get

        def negative_screeners(self_, url, **kw):
            if OFFICIAL_HOST in url:
                return _Resp(OFFICIAL_TEXT.encode(), url)
            return _Resp(negative.encode(), url)

        try:
            _Session.get = negative_screeners
            self._propose()
            draft = json.loads(self.draft.read_text(encoding='utf-8'))
            for name in ('token_type', 'utility', 'revenue'):
                draft['assets']['EXP']['claims'][name]['value'] = 'PAYMENT'
            # The owner mistakenly types halal against a negative quote.
            for name in draft['assets']['EXP']['screeners']:
                draft['assets']['EXP']['screeners'][name]['value'] = 'halal'
            self.draft.write_text(json.dumps(draft), encoding='utf-8')
            rc = self._apply()
        finally:
            _Session.get = original
        self.assertEqual(rc, 1)
        self.assertFalse(self.registry.exists(),
                         'halal must never be applied against a negative quote')

    def test_string_identity_match_is_rejected_not_coerced(self):
        self.request.write_text(json.dumps({'EXP': {
            'official_hosts': [OFFICIAL_HOST],
            'sources': [{'url': OFFICIAL_URL, 'identity_match': 'false'}],
        }}), encoding='utf-8')
        self.assertEqual(self._propose(), 1)
        draft = json.loads(self.draft.read_text(encoding='utf-8'))
        self.assertEqual(draft['assets'].get('EXP', {}).get('sources', []), [],
                         'a string identity_match must not become True')


class QuoteHelperTests(unittest.TestCase):
    def test_quote_membership_is_whitespace_insensitive(self):
        self.assertTrue(seed.quote_is_in('a  b   c', 'x a b c y'))
        self.assertFalse(seed.quote_is_in('a b d', 'x a b c y'))

    def test_screener_is_identified_only_by_its_own_host(self):
        self.assertEqual(seed.screener_for('https://musaffa.com/asset/exp'), 'musaffa')
        self.assertIsNone(seed.screener_for('https://evil.example/musaffa'))


class NoAutomaticVerdictTests(unittest.TestCase):
    """The tool must never read a religious ruling out of a sentence.

    The previous version returned 'halal' for four different negative
    phrasings, because "not compliant" is not a substring of "not Shariah
    compliant" and a bare "compliant" fallback then matched. The unit test
    missed it by using the one phrasing that happened to work.
    """

    NEGATIVE = (
        'This token is not Shariah compliant.',
        'The asset is not considered compliant.',
        'This product is not fully compliant.',
        'It cannot be considered permissible.',
        'This token is non-compliant.',
        'The scholars did not find it permissible.',
        'This asset is no longer compliant.',
    )

    def test_no_sentence_ever_yields_a_verdict(self):
        for sentence in self.NEGATIVE + ('This token is compliant.',
                                         'The asset is halal.'):
            with self.subTest(sentence=sentence):
                self.assertEqual(seed.infer_verdict(sentence), '')

    def test_positive_value_is_refused_against_negative_wording(self):
        for sentence in self.NEGATIVE:
            with self.subTest(sentence=sentence):
                reason = seed.positive_value_conflicts_with_quote('halal', sentence)
                self.assertTrue(reason, f'{sentence!r} accepted a halal value')

    def test_positive_value_is_refused_against_conditional_wording(self):
        for sentence in ('Compliant unless the treasury lends funds.',
                         'Compliant except during the migration period.',
                         'This may be permissible pending further review.',
                         'Compliant, however the staking module is disputed.'):
            with self.subTest(sentence=sentence):
                self.assertTrue(
                    seed.positive_value_conflicts_with_quote('halal', sentence))

    def test_clean_positive_wording_is_still_allowed(self):
        self.assertEqual(
            seed.positive_value_conflicts_with_quote(
                'halal', 'This asset is Shariah compliant for spot holding.'), '')

    def test_bare_compliant_or_permissible_is_domain_ambiguous(self):
        for sentence in ('This asset is compliant.',
                         'This asset is permissible.'):
            with self.subTest(sentence=sentence):
                self.assertTrue(
                    seed.positive_value_conflicts_with_quote('halal', sentence))

    def test_positive_synonyms_cannot_bypass_the_canonical_enum(self):
        for value in ('approved', 'allowed', 'green', 'pass', 'yes',
                      'SHARIAH_OK', 'permitted', 'halal-ish'):
            with self.subTest(value=value):
                self.assertEqual(seed.canonical_screener_verdict(value), '')

    def test_obfuscated_negative_wording_blocks_halal(self):
        for sentence in (
                'This asset is NOT‑Shariah compliant.',
                'This asset is not\u200b Shariah compliant.',
                'This asset is halal?',
                'This asset is halal but remains disputed.',
                'This asset may be halal.',
        ):
            with self.subTest(sentence=sentence):
                self.assertTrue(
                    seed.positive_value_conflicts_with_quote('halal', sentence))

    def test_halal_requires_an_affirmative_phrase_in_the_quote(self):
        self.assertTrue(seed.positive_value_conflicts_with_quote(
            'halal', 'The screening page lists this asset under spot holdings.'))

    def test_negative_value_is_never_blocked(self):
        self.assertEqual(
            seed.positive_value_conflicts_with_quote(
                'haram', 'This token is not Shariah compliant.'), '')


class StrictBooleanTests(unittest.TestCase):
    """bool("false") is True, which would promote an unconfirmed source."""

    def test_only_real_booleans_are_accepted(self):
        self.assertIs(seed.strict_bool(True, 'f'), True)
        self.assertIs(seed.strict_bool(False, 'f'), False)
        for bad in ('false', 'true', 'no', 0, 1, None, '', [], {}):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                seed.strict_bool(bad, 'f')


if __name__ == '__main__':
    unittest.main()
