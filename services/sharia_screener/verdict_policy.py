"""Canonical, fail-closed policy for owner-entered screener verdicts.

The local registry stores evidence from seven third-party Sharia screeners.
Those values are transport data, not a religious ruling by this software, but
they still feed the GREEN proof gate.  Keeping the accepted vocabulary and
the positive-quote guard here prevents the registry helper, registry loader
and rules engine from drifting into three different interpretations.
"""
from __future__ import annotations

import re
import unicodedata


CANONICAL_SCREENER_VERDICTS = frozenset({
    'halal', 'haram', 'doubtful', 'unknown',
})
POSITIVE_SCREENER_VERDICT = 'halal'

# Zero-width characters become a SPACE, never nothing. Deleting them let
# "not<ZWSP>Shariah compliant" collapse to "notShariah compliant", where a
# \bnot\b blocklist no longer matched \u2014 the invisible character defeated the
# exact check it was added to survive.
_ZERO_WIDTH = dict.fromkeys(map(ord, '\u200b\u200c\u200d\u2060\ufeff'), ' ')

# An ALLOWLIST, not a blocklist.
#
# Three rounds of adding negative phrases each left more bypasses: "has no
# halal certification", "lacks halal certification", "fails halal screening",
# "status was revoked", "previously halal", "certification denied",
# "questionable", "suspended" \u2014 thirteen found in one sitting, and English
# offers unlimited more. A list of ways to say no can never be complete, and
# every gap in it fails OPEN on a religious-compliance decision.
#
# So the quote must instead match a narrow affirmative template. Anything the
# template does not recognise \u2014 including perfectly reasonable wording \u2014 is
# refused and escalated to the owner. Unrecognised text fails CLOSED, which
# is the only safe default here. The cost is more owner review; the owner is
# required to approve every asset anyway.
# The subject is an ALLOWLIST of tokens, not free text.
#
# A bounded free-form subject looked safe and was not: a negating clause
# simply hides inside it. "Nobody claims the token is halal", "We doubt the
# token is halal", "It is false that the token is halal" and "Critics dispute
# whether it is halal" all satisfied a six-word-noun-phrase pattern. Widening
# the negative-word list to cover them is the same losing game as before, so
# the subject may contain only words that cannot carry a claim, plus at most
# one identifier (the screener name or the asset ticker).
_SAFE_SUBJECT_WORDS = frozenset({
    'the', 'this', 'its', 'their', 'a', 'an', 'of', 'for',
    'asset', 'assets', 'token', 'tokens', 'coin', 'coins', 'project',
    'currency', 'crypto', 'cryptocurrency', 'screening', 'screen', 'result',
    'results', 'status', 'rating', 'assessment', 'verdict', 'classification',
    'analysis', 'listing', 'record',
})
_MAX_SUBJECT_TOKENS = 4
_IDENTIFIER = re.compile(r"^[A-Za-z0-9'’.]{1,20}$")

_AFFIRM = (r"(?:is|are|was|were|remains?)\s+"
           r"(?:currently\s+|fully\s+|deemed\s+|considered\s+|assessed\s+as\s+"
           r"|rated\s+(?:as\s+)?|classified\s+as\s+|certified\s+(?:as\s+)?)?")
_POSITIVE = r"(?:shariah\s+|sharia\s+|islamically\s+)?(?:halal|compliant|permissible)"
_TAIL = r"(?:\s+(?:for|under|by|as|on)\s+[A-Za-z0-9'’\s]{1,40})?"
_AFFIRMATIVE_TEMPLATE = re.compile(
    rf"^(?P<subject>.+?)\s+{_AFFIRM}{_POSITIVE}{_TAIL}\s*[.]?$", re.IGNORECASE)


def _subject_is_a_plain_noun_phrase(subject: str) -> bool:
    """True when the subject cannot itself carry or deny a claim."""
    tokens = subject.split()
    if not tokens or len(tokens) > _MAX_SUBJECT_TOKENS:
        return False
    identifiers = 0
    for token in tokens:
        if token.casefold() in _SAFE_SUBJECT_WORDS:
            continue
        if _IDENTIFIER.match(token):
            identifiers += 1
            if identifiers > 1:
                return False  # two unknown words can form a clause
            continue
        return False
    return True

# Hard stop regardless of the template: any token that denies, hedges,
# conditions or defers the claim. The two checks are deliberately
# overlapping — the template refuses shapes it cannot parse, and this
# refuses meanings it can, so a bypass has to defeat both.
_HARD_NEGATIVE = re.compile(
    r"\b(?:not|non|never|cannot|no|haram|impermissible|prohibited|forbidden"
    r"|revoked|denied|rejected|suspended|withdrawn|lacks?|fails?|failed"
    r"|previously|formerly|questionable|disputed|doubtful|pending|unclear"
    r"|may|might|maybe|unless|except|however|but|provisional(?:ly)?"
    r"|incomplete|partial(?:ly)?|conditional(?:ly)?|assume[ds]?|treat"
    r"|uncertain|review|awaiting|subject)\b"
    r"|n't\b|[?]",
    re.IGNORECASE,
)


def _normalize(text: object) -> str:
    """NFKC-fold, neutralise invisible characters, unify hyphens, collapse space."""
    normalized = unicodedata.normalize('NFKC', str(text or ''))
    normalized = normalized.translate(_ZERO_WIDTH)
    normalized = re.sub(r'[\u2010-\u2015\u2212-]', ' ', normalized)
    return ' '.join(normalized.split())


def canonical_screener_verdict(value: object) -> str:
    """Return the accepted canonical value, or ``''`` for anything else."""
    normalized = _normalize(value).casefold()
    return normalized if normalized in CANONICAL_SCREENER_VERDICTS else ''


def positive_verdict_conflict(value: object, quote: object) -> str:
    """Explain why a positive verdict is not supported by its own quote.

    Only an exact canonical ``halal`` value can contribute to GREEN, and only
    when its evidence sentence matches the narrow affirmative template above.
    Everything else returns a reason, which routes the asset to owner review
    rather than into a mechanically promotable proof card.

    This is deliberately biased toward refusing: a sentence this cannot parse
    is not evidence that the asset is halal, it is evidence that a human
    should read it.
    """
    if canonical_screener_verdict(value) != POSITIVE_SCREENER_VERDICT:
        return ''
    normalized = _normalize(quote)
    if not normalized:
        return 'positive verdict has no supporting quote'
    hard = _HARD_NEGATIVE.search(normalized)
    if hard:
        return (f'positive verdict is contradicted by {hard.group(0)!r} in its '
                f'own quote')
    match = _AFFIRMATIVE_TEMPLATE.match(normalized)
    if not match:
        return ('positive verdict quote does not match a plain affirmative '
                'statement ("<subject> is halal/compliant/permissible"); '
                'send it to owner review instead of promoting it')
    if not _subject_is_a_plain_noun_phrase(match.group('subject')):
        return (f'positive verdict quote has a clause, not a plain subject, '
                f'before the affirmative phrase ({match.group("subject")!r}); '
                'send it to owner review instead of promoting it')
    return ''
