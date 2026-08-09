"""Adversarial tests for the screener-verdict policy.

This guard decides whether an owner-entered ``halal`` value may contribute
mechanically to a GREEN proof card. It is the last automated check before a
religious-compliance claim reaches the owner's approval screen, so every gap
in it fails OPEN on exactly the decision that must not be got wrong.

It was rewritten from a blocklist to an allowlist after three rounds of
adding negative phrases each left more bypasses. The attack set below is the
accumulated evidence: thirteen of these passed a blocklist that had already
been hardened twice.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.sharia_screener.verdict_policy import (  # noqa: E402
    CANONICAL_SCREENER_VERDICTS,
    canonical_screener_verdict,
    positive_verdict_conflict,
)

# Every one of these must be refused when paired with a 'halal' value.
CONTRADICTORY_QUOTES = (
    # Plain negations that defeated the first implementation.
    'This token is not Shariah compliant.',
    'The asset is not considered compliant.',
    'This product is not fully compliant.',
    'It cannot be considered permissible.',
    'This token is non-compliant.',
    # Absence and failure, which name no negative word at all.
    'This token has no halal certification.',
    'This asset lacks halal certification.',
    'The project fails halal screening.',
    'Halal certification denied.',
    'Rejected for halal listing.',
    'Withdrawn from the halal index.',
    # Revocation and tense: true once, false now.
    'Its halal status was revoked last year.',
    'This was previously halal.',
    'This was formerly compliant.',
    'Halal status: suspended.',
    # Hedged, conditional or disputed.
    'The halal claim is questionable.',
    'May be halal.',
    'Halal but disputed.',
    'Compliant? Under review.',
    'Screening incomplete; treat as halal provisionally.',
    'The asset is halal, however staking is disputed.',
    'Compliant unless the treasury lends funds.',
    'Compliant except during the migration period.',
    # A negating clause hidden in the SUBJECT. These defeated a bounded
    # free-form noun phrase, which is why the subject is now an allowlist.
    'Nobody claims the token is halal.',
    'We doubt the token is halal.',
    'Few scholars consider it halal.',
    'It is false that the token is halal.',
    'Critics dispute whether it is halal.',
    'Hardly anyone calls it compliant.',
    'We disagree that the asset is halal.',
    'The claim it is halal is wrong.',
    'Nothing here is halal.',
    'Barely compliant.',
    'Some scholars reject the view that it is halal.',
    'The regulator has yet to confirm the token is halal.',
)

# Real screener phrasings that must remain usable, or the mechanical path is
# worthless and every asset escalates.
SCREENER_PHRASINGS = (
    'musaffa screening result is halal for this asset.',
    'sharlife screening result is halal for this asset.',
    'The project is certified halal.',
    'This token is halal.',
)

# These all matched the previous supposedly closed template.  Past tense was
# explicitly allowed, while the subject admitted one arbitrary word and the
# tail admitted forty arbitrary characters.
SECOND_GENERATION_BYPASSES = (
    'The asset was halal.',
    'The asset were halal.',
    'The asset is halal for now.',
    'The asset is halal by mistake.',
    'The asset is halal as alleged.',
    'The asset is halal on paper only.',
    'The asset is halal under appeal.',
    'The asset is halal under duress.',
    'Unverified asset is halal.',
    'Neither asset is halal.',
    'Allegedly token is halal.',
    'Purportedly token is halal.',
    'Counterfeit token is halal.',
    'n0t token is halal.',
    'The asset is halal for money laundering.',
)

# Invisible-character and homoglyph attempts.
OBFUSCATED_QUOTES = (
    'This token is not​Shariah compliant.',   # zero-width space
    'This token is not‍Shariah compliant.',   # zero-width joiner
    'This token is non‑compliant.',           # non-breaking hyphen
    'This token is non—compliant.',           # em dash
    'This token is non−compliant.',           # minus sign
)

# Plain affirmative statements that must still be usable.
LEGITIMATE_QUOTES = (
    'This asset is Shariah compliant for spot holding.',
    'The token is halal.',
    'The asset EXP is assessed as halal for spot trading.',
    'The coin is Shariah compliant.',
    'It is Islamically permissible.',
    'The asset is rated halal by our scholars.',
)


class ContradictoryQuoteTests(unittest.TestCase):
    def test_every_contradictory_quote_is_refused(self):
        for quote in CONTRADICTORY_QUOTES:
            with self.subTest(quote=quote):
                self.assertTrue(
                    positive_verdict_conflict('halal', quote),
                    f'{quote!r} was accepted as evidence for halal')

    def test_invisible_characters_cannot_hide_a_negation(self):
        # Deleting zero-width characters joined "not" to the next word, so a
        # word-boundary check stopped matching. They are folded to a space.
        for quote in OBFUSCATED_QUOTES:
            with self.subTest(quote=quote):
                self.assertTrue(
                    positive_verdict_conflict('halal', quote),
                    f'{quote!r} smuggled a negation past the guard')

    def test_empty_or_missing_quote_is_refused(self):
        for quote in ('', '   ', None):
            with self.subTest(quote=quote):
                self.assertTrue(positive_verdict_conflict('halal', quote))

    def test_unparseable_wording_escalates_rather_than_passing(self):
        # The allowlist refuses what it cannot parse. This sentence carries no
        # negative token at all, yet is not a plain affirmative claim.
        self.assertTrue(positive_verdict_conflict(
            'halal', 'Our committee met on Tuesday to discuss halal criteria '
                     'for digital assets in general.'))

    def test_past_tense_free_subject_and_free_tail_bypasses_are_refused(self):
        for quote in SECOND_GENERATION_BYPASSES:
            with self.subTest(quote=quote):
                self.assertTrue(
                    positive_verdict_conflict('halal', quote),
                    f'{quote!r} bypassed the closed affirmative grammar')

    def test_domain_ambiguous_compliance_words_are_refused(self):
        for quote in ('The asset is compliant.', 'The asset is permissible.'):
            with self.subTest(quote=quote):
                self.assertTrue(positive_verdict_conflict('halal', quote))


class LegitimatePositiveTests(unittest.TestCase):
    def test_plain_affirmative_statements_are_accepted(self):
        for quote in LEGITIMATE_QUOTES:
            with self.subTest(quote=quote):
                asset_ids = {'EXP'} if ' EXP ' in quote else set()
                self.assertEqual(
                    positive_verdict_conflict(
                        'halal', quote,
                        permitted_asset_identifiers=asset_ids), '',
                    f'{quote!r} should be usable evidence')

        for quote in SCREENER_PHRASINGS:
            with self.subTest(quote=quote):
                provider = quote.split()[0]
                self.assertEqual(
                    positive_verdict_conflict(
                        'halal', quote,
                        permitted_provider_identifiers={provider}), '',
                    f'{quote!r} should be usable evidence')

    def test_the_guard_is_not_so_strict_that_nothing_passes(self):
        # A guard that refuses everything is safe and useless: the whole
        # mechanical path would collapse into permanent escalation.
        accepted = sum(
            1 for q in LEGITIMATE_QUOTES
            if not positive_verdict_conflict(
                'halal', q,
                permitted_asset_identifiers=({'EXP'} if ' EXP ' in q else set())))
        accepted += sum(
            1 for q in SCREENER_PHRASINGS
            if not positive_verdict_conflict(
                'halal', q,
                permitted_provider_identifiers={q.split()[0]}))
        self.assertEqual(accepted, len(LEGITIMATE_QUOTES) + len(SCREENER_PHRASINGS))

    def test_an_identifier_must_be_bound_to_the_actual_context(self):
        quote = 'musaffa screening result is halal for this asset.'
        self.assertTrue(positive_verdict_conflict('halal', quote))
        self.assertTrue(positive_verdict_conflict(
            'halal', quote, permitted_provider_identifiers={'sharlife'}))
        self.assertEqual(positive_verdict_conflict(
            'halal', quote, permitted_provider_identifiers={'musaffa'}), '')

        asset_quote = 'The asset EXP is halal for spot holding.'
        self.assertTrue(positive_verdict_conflict('halal', asset_quote))
        self.assertTrue(positive_verdict_conflict(
            'halal', asset_quote, permitted_asset_identifiers={'XYZ'}))
        self.assertEqual(positive_verdict_conflict(
            'halal', asset_quote, permitted_asset_identifiers={'EXP'}), '')

    def test_a_bound_identifier_cannot_move_into_a_negating_position(self):
        self.assertTrue(positive_verdict_conflict(
            'halal', 'NO asset is halal.',
            permitted_asset_identifiers={'NO'}))


class NonPositiveVerdictTests(unittest.TestCase):
    def test_guard_never_blocks_a_restrictive_verdict(self):
        # haram/doubtful/unknown are already fail-closed downstream; the
        # guard exists only to stop an unsupported positive.
        for value in ('haram', 'doubtful', 'unknown'):
            for quote in ('This token is not compliant.', 'anything at all'):
                with self.subTest(value=value, quote=quote):
                    self.assertEqual(positive_verdict_conflict(value, quote), '')


class CanonicalVocabularyTests(unittest.TestCase):
    def test_only_four_values_are_canonical(self):
        self.assertEqual(CANONICAL_SCREENER_VERDICTS,
                         frozenset({'halal', 'haram', 'doubtful', 'unknown'}))

    def test_synonyms_are_not_accepted(self):
        for value in ('approved', 'green', 'pass', 'yes', 'ok', 'SHARIAH_OK',
                      'halal-ish', 'HALAL!', 'compliant', 'permissible'):
            with self.subTest(value=value):
                self.assertEqual(canonical_screener_verdict(value), '')

    def test_canonical_values_survive_case_and_surrounding_space(self):
        for raw in ('halal', 'HALAL', ' Halal ', '\thalal\n', 'halal '):
            with self.subTest(raw=raw):
                self.assertEqual(canonical_screener_verdict(raw), 'halal')

    def test_a_value_with_an_embedded_invisible_is_refused(self):
        # Zero-width characters fold to a space, so "hal<ZWSP>al" becomes
        # "hal al" and is not the word halal. Refusing it is correct: an
        # invisible character inside a verdict is obfuscation, not a typo,
        # and the value field has no legitimate reason to contain one.
        for raw in ('hal​al', 'ha‌lal', 'halal‍'.replace('halal', 'hal‍al')):
            with self.subTest(raw=raw):
                self.assertEqual(canonical_screener_verdict(raw), '')

    def test_a_synonym_cannot_reach_the_positive_path(self):
        # A non-canonical positive-sounding value must not be treated as
        # halal by the conflict guard either.
        self.assertEqual(positive_verdict_conflict('approved',
                                                   'The token is halal.'), '')
        self.assertEqual(canonical_screener_verdict('approved'), '')


if __name__ == '__main__':
    unittest.main()


class QuoteTruncationTests(unittest.TestCase):
    """A negation must not be removable by trimming the quote.

    Both the seeding helper and the runtime engine checked the owner's quote
    as a plain substring of the page, and then judged that fragment. Deleting
    a negative prefix therefore laundered a negative page into positive
    evidence: the affirmative template matched, no negative token remained,
    and the screener counted as positive. Proven on three real phrasings.
    """

    TRUNCATION_ATTACKS = (
        ('It is false that this token is halal.', 'this token is halal'),
        ('No authority says this token is halal.', 'this token is halal'),
        ('Nobody has confirmed this token is halal.', 'this token is halal'),
        ('We cannot say the asset is compliant.', 'the asset is compliant'),
        ('It is untrue that the coin is permissible.', 'the coin is permissible'),
        ('Contrary to reports, the token is halal only for staking.',
         'the token is halal'),
    )

    def test_a_trimmed_negation_is_judged_on_the_full_source_sentence(self):
        from services.sharia_screener.verdict_policy import quote_conflict_in_source
        for page, fragment in self.TRUNCATION_ATTACKS:
            with self.subTest(page=page):
                self.assertTrue(
                    quote_conflict_in_source('halal', fragment, page),
                    f'trimming turned {page!r} into accepted halal evidence')

    def test_a_genuinely_affirmative_page_still_passes(self):
        from services.sharia_screener.verdict_policy import quote_conflict_in_source
        self.assertEqual(
            quote_conflict_in_source('halal', 'This token is halal',
                                     'This token is halal.'), '')

    def test_a_fragment_absent_from_the_source_is_refused(self):
        from services.sharia_screener.verdict_policy import quote_conflict_in_source
        self.assertTrue(quote_conflict_in_source(
            'halal', 'this token is halal', 'A completely unrelated page.'))

    def test_containing_sentence_returns_the_whole_sentence(self):
        from services.sharia_screener.verdict_policy import containing_sentence
        found = containing_sentence(
            'this token is halal', 'Intro text. It is false that this token '
                                   'is halal. Later text.')
        self.assertEqual(found, 'It is false that this token is halal.')

    def test_containing_sentence_is_invisible_character_safe(self):
        from services.sharia_screener.verdict_policy import containing_sentence
        self.assertTrue(containing_sentence(
            'this token is halal', 'It is false that this​ token is halal.'))


class ConnectedPeerVerificationTests(unittest.TestCase):
    """P2 — the address validated must be the address connected to.

    assert_fetchable resolves the hostname and rejects non-global answers,
    but the HTTP stack resolves it again when opening the socket. A hostile
    or compromised approved domain can answer public during validation and
    private during connection. Checking the live peer closes that window.
    """

    @staticmethod
    def _response_connected_to(address: str | None):
        import socket as _socket

        class _Sock:
            def getpeername(self):
                if address is None:
                    raise OSError('no peer')
                return (address, 443)

        class _Conn:
            sock = _Sock()

        class _Raw:
            _connection = _Conn()

        class _Resp:
            raw = _Raw()

        del _socket
        return _Resp()

    def test_a_private_peer_is_refused_after_a_public_resolution(self):
        from services.sharia_retriever.retriever import (
            RetrievalBlocked, assert_peer_is_public)
        for address in ('127.0.0.1', '169.254.169.254', '10.0.0.5',
                        '192.168.1.1', '::1'):
            with self.subTest(address=address):
                with self.assertRaises(RetrievalBlocked):
                    assert_peer_is_public(
                        self._response_connected_to(address), 'approved.example')

    def test_a_public_peer_is_accepted(self):
        from services.sharia_retriever.retriever import assert_peer_is_public
        assert_peer_is_public(
            self._response_connected_to('93.184.216.34'), 'approved.example')

    def test_an_undeterminable_peer_fails_closed(self):
        # A security check that cannot be performed has not passed.
        from services.sharia_retriever.retriever import (
            RetrievalBlocked, assert_peer_is_public)
        with self.assertRaises(RetrievalBlocked):
            assert_peer_is_public(
                self._response_connected_to(None), 'approved.example')

    def test_peer_verification_is_on_by_default(self):
        from services.sharia_retriever.retriever import Retriever
        self.assertTrue(Retriever().verify_peer,
                        'production must verify the connected peer')


class SentenceBoundaryAndOccurrenceTests(unittest.TestCase):
    """The context extracted around a quote must not be trimmable.

    Two further bypasses of the source-sentence fix, both reproduced:
    a dotted abbreviation immediately before the fragment split the sentence
    and cut the negation off, and a repeated fragment matched only its first
    (affirmative) occurrence while a contradictory one sat elsewhere.
    """

    ABBREVIATION_TRUNCATION = (
        'It is false, i.e. this token is halal.',
        'This claim is untrue, e.g. this token is halal.',
        'Per U.S. rules it is untrue that this token is halal.',
        'Reviewed by J. Smith. It is false that this token is halal.',
        'It is false. this token is halal.',
    )
    REPEATED_OCCURRENCE = (
        'This token is halal. It is false that this token is halal.',
        'It is false that this token is halal. This token is halal.',
        'This token is halal. This token is halal. '
        'It is false that this token is halal.',
    )

    def test_an_abbreviation_cannot_cut_the_negation_away(self):
        from services.sharia_screener.verdict_policy import quote_conflict_in_source
        for source in self.ABBREVIATION_TRUNCATION:
            with self.subTest(source=source):
                self.assertTrue(
                    quote_conflict_in_source('halal', 'this token is halal',
                                             source),
                    f'{source!r} was trimmed into accepted halal evidence')

    def test_every_occurrence_must_be_affirmative(self):
        from services.sharia_screener.verdict_policy import quote_conflict_in_source
        for source in self.REPEATED_OCCURRENCE:
            with self.subTest(source=source):
                self.assertTrue(
                    quote_conflict_in_source('halal', 'this token is halal',
                                             source),
                    'a contradictory occurrence elsewhere was ignored')

    def test_all_occurrences_are_returned_not_just_the_first(self):
        from services.sharia_screener.verdict_policy import containing_contexts
        found = containing_contexts(
            'this token is halal',
            'This token is halal. It is false that this token is halal.')
        self.assertEqual(len(found), 2)

    def test_containing_sentence_refuses_an_ambiguous_fragment(self):
        from services.sharia_screener.verdict_policy import containing_sentence
        self.assertEqual(containing_sentence(
            'this token is halal',
            'This token is halal. This token is halal.'), '')

    def test_a_single_affirmative_sentence_still_passes(self):
        from services.sharia_screener.verdict_policy import quote_conflict_in_source
        self.assertEqual(quote_conflict_in_source(
            'halal', 'this token is halal', 'This token is halal.'), '')


class RejectedResponseIsClosedTests(unittest.TestCase):
    """A security refusal must not leak the socket it refused."""

    def test_a_peer_rejected_response_is_closed_before_raising(self):
        import ipaddress
        from unittest import mock as _mock
        from services.sharia_retriever.retriever import Retriever

        closed = []

        class _Resp:
            status_code = 200
            history: list = []
            headers = {'Content-Type': 'text/plain'}
            encoding = 'utf-8'
            url = 'https://approved.example/'

            def close(self):
                closed.append(True)

            def iter_content(self, _n):
                yield b'body'

        class _Session:
            def get(self, _url, **_kw):
                return _Resp()

        with _mock.patch(
                'services.sharia_retriever.retriever._addresses_for',
                lambda _h: [ipaddress.ip_address('93.184.216.34')]), \
             _mock.patch(
                'services.sharia_retriever.retriever.connected_peer_address',
                lambda _r: ipaddress.ip_address('127.0.0.1')):
            result = Retriever(session=_Session()).fetch(
                'https://approved.example/')

        self.assertFalse(result.ok)
        self.assertIn('non-public address', result.error)
        self.assertTrue(closed, 'the rejected response was never closed')
