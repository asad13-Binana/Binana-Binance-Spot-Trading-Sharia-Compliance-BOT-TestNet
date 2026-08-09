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
