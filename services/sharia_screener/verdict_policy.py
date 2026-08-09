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

_ZERO_WIDTH = dict.fromkeys(map(ord, '\u200b\u200c\u200d\u2060\ufeff'), None)
_POSITIVE_PHRASE = re.compile(
    r'\b(?:halal|permissible|compliant)\b', re.IGNORECASE)
_AMBIGUOUS_OR_NEGATIVE = re.compile(
    r"\b(?:not|never|non|cannot|can\s+not|no\s+longer|without|unless|except|"
    r"however|but|partial(?:ly)?|may|might|unclear|uncertain|doubtful|"
    r"pending|depends?|conditional|disputed|haram|impermissible|prohibited|"
    r"avoid|failed?|red)\b|n't\b|[?]",
    re.IGNORECASE,
)


def canonical_screener_verdict(value: object) -> str:
    """Return the accepted canonical value, or ``''`` for anything else."""
    normalized = unicodedata.normalize('NFKC', str(value or ''))
    normalized = normalized.translate(_ZERO_WIDTH).strip().casefold()
    return normalized if normalized in CANONICAL_SCREENER_VERDICTS else ''


def positive_verdict_conflict(value: object, quote: object) -> str:
    """Explain why a positive verdict is not supported by its own quote.

    Only an exact canonical ``halal`` value can contribute to GREEN.  Its
    evidence sentence must contain an affirmative compliance phrase and no
    negative, conditional or ambiguous wording.  The guard is intentionally
    conservative: uncertain language belongs in owner escalation, never in a
    mechanically promotable proof card.
    """
    if canonical_screener_verdict(value) != POSITIVE_SCREENER_VERDICT:
        return ''
    normalized = unicodedata.normalize('NFKC', str(quote or ''))
    normalized = normalized.translate(_ZERO_WIDTH)
    normalized = re.sub(r'[\u2010-\u2015-]', ' ', normalized)
    normalized = ' '.join(normalized.split())
    if not _POSITIVE_PHRASE.search(normalized):
        return 'positive verdict has no affirmative halal/compliance phrase in its quote'
    match = _AMBIGUOUS_OR_NEGATIVE.search(normalized)
    if match:
        return (f'positive verdict is paired with negative, conditional or '
                f'ambiguous wording {match.group(0)!r}')
    return ''
