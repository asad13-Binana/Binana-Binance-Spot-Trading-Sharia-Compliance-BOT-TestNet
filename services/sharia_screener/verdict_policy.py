"""Canonical, fail-closed policy for owner-entered screener verdicts.

The local registry stores evidence from seven third-party Sharia screeners.
Those values are transport data, not a religious ruling by this software, but
they still feed the GREEN proof gate.  Keeping the accepted vocabulary and
the positive-quote guard here prevents the registry helper, registry loader
and rules engine from drifting into three different interpretations.
"""
from __future__ import annotations

from collections.abc import Iterable
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
# one identifier that the caller binds to the actual screener name or asset
# ticker.  No arbitrary word is treated as an identifier.
_SAFE_SUBJECT_WORDS = frozenset({
    'the', 'this', 'it', 'its', 'their', 'a', 'an', 'of', 'for',
    'asset', 'assets', 'token', 'tokens', 'coin', 'coins', 'project',
    'currency', 'crypto', 'cryptocurrency', 'screening', 'screen', 'result',
    'results', 'status', 'rating', 'assessment', 'verdict', 'classification',
    'analysis', 'listing', 'record',
})
_MAX_SUBJECT_TOKENS = 4
_BOUND_IDENTIFIER = re.compile(r"^[a-z0-9]{1,40}$")

# Only present-tense assertions can support a current verdict.  In particular,
# "was halal" and "were compliant" say nothing about the current state.
_AFFIRM = (r"(?:is|are|remains?)\s+"
           r"(?:currently\s+|fully\s+|deemed\s+|considered\s+|assessed\s+as\s+"
           r"|rated\s+(?:as\s+)?|classified\s+as\s+|certified\s+(?:as\s+)?)?")
# "Compliant" and "permissible" alone are domain-ambiguous: they can describe
# exchange, legal, technical or listing status.  Only the unambiguous word
# "halal" or an explicit Sharia/Islamic qualifier can support this gate.
_POSITIVE = r"(?:halal|(?:shariah|sharia|islamically)\s+(?:compliant|permissible))"
_AFFIRMATIVE_TEMPLATE = re.compile(
    rf"^(?P<subject>.+?)\s+{_AFFIRM}{_POSITIVE}"
    rf"(?:\s+(?P<tail>.+?))?\s*[.]?$", re.IGNORECASE)

# Tails are closed phrases, not a bounded free-form string.  A free-form tail
# accepted "for now", "by mistake", "as alleged" and "under appeal".  Each of
# those reverses or qualifies the apparent positive statement without using a
# word from the former negative blocklist.
_SAFE_TAILS = frozenset({
    'for this asset', 'for this token', 'for this coin', 'for this project',
    'for the asset', 'for the token', 'for the coin', 'for the project',
    'for spot holding', 'for spot trading',
    'for spot holding by this screener',
    'for spot trading by this screener',
    'by our scholars',
    'under shariah screening', 'under sharia screening',
    'under islamic screening', 'under shariah assessment',
    'under sharia assessment', 'under islamic assessment',
    'under shariah methodology', 'under sharia methodology',
    'under islamic methodology',
})


def _permitted_identifier_tokens(values: Iterable[object]) -> frozenset[str]:
    """Return single-token identifiers explicitly bound by the caller."""
    permitted = set()
    for value in values:
        normalized = _normalize(value).casefold()
        if _BOUND_IDENTIFIER.fullmatch(normalized):
            permitted.add(normalized)
    return frozenset(permitted)


def _subject_is_a_plain_noun_phrase(
        subject: str, provider_identifiers: frozenset[str],
        asset_identifiers: frozenset[str]) -> bool:
    """True only for closed words or an identifier in a fixed grammatical slot."""
    normalized = subject.casefold()
    tokens = normalized.split()
    if not tokens or len(tokens) > _MAX_SUBJECT_TOKENS:
        return False
    if all(token in _SAFE_SUBJECT_WORDS for token in tokens):
        return True

    # Provider and asset identifiers are not interchangeable and cannot occur
    # in an arbitrary position.  This keeps a real ticker such as "NO" from
    # making the English negation "NO asset is halal" look like an identifier.
    provider_subjects = {
        phrase
        for identifier in provider_identifiers
        for phrase in (
            f'{identifier} screening result', f'{identifier} screening status',
            f'{identifier} assessment', f'{identifier} verdict',
            f'{identifier} rating', f'{identifier} classification',
        )
    }
    asset_subjects = {
        phrase
        for identifier in asset_identifiers
        for phrase in (
            f'the asset {identifier}', f'this asset {identifier}',
            f'the token {identifier}', f'this token {identifier}',
            f'the coin {identifier}', f'this coin {identifier}',
            f'the project {identifier}', f'this project {identifier}',
            f'the {identifier} asset', f'this {identifier} asset',
            f'the {identifier} token', f'this {identifier} token',
            f'the {identifier} coin', f'this {identifier} coin',
            f'the {identifier} project', f'this {identifier} project',
        )
    }
    return normalized in provider_subjects or normalized in asset_subjects


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


def positive_verdict_conflict(
        value: object, quote: object, *,
        permitted_provider_identifiers: Iterable[object] = (),
        permitted_asset_identifiers: Iterable[object] = ()) -> str:
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
    match = _AFFIRMATIVE_TEMPLATE.match(normalized)
    if not match:
        return ('positive verdict quote does not match a plain affirmative '
                'statement ("<subject> is halal/Shariah compliant"); '
                'send it to owner review instead of promoting it')
    providers = _permitted_identifier_tokens(permitted_provider_identifiers)
    assets = _permitted_identifier_tokens(permitted_asset_identifiers)
    if not _subject_is_a_plain_noun_phrase(
            match.group('subject'), providers, assets):
        return (f'positive verdict quote has a clause, not a plain subject, '
                f'before the affirmative phrase ({match.group("subject")!r}); '
                'send it to owner review instead of promoting it')
    tail = (match.group('tail') or '').casefold()
    if tail and tail not in _SAFE_TAILS:
        return (f'positive verdict quote has an unrecognised qualifier or tail '
                f'({match.group("tail")!r}); send it to owner review instead '
                'of promoting it')
    return ''


# --- QUOTE-TRUNCATION DEFENCE ---------------------------------------------
#
# Checking the owner's quote as a substring of the page let a negation be
# edited away. A page reading "It is false that this token is halal." accepts
# the quote "this token is halal", and every downstream check then sees only
# the laundered fragment: the affirmative template matches, no negative token
# is present, and the screener counts as positive.
#
# Reproduced on three pages ("It is false that...", "No authority says...",
# "Nobody has confirmed..."), and the same substring test existed in both the
# seeding helper and the runtime rules engine.
#
# The fix is to stop judging the fragment. Locate it in the source, expand to
# the complete sentence that contains it, and apply the verdict policy to
# that sentence. The negation is still in the source, so it cannot be removed
# by editing the draft.
_SENTENCE_END = '.!?\n'

# Tokens whose trailing period is NOT a sentence boundary. Treating one as a
# boundary let a negative prefix be cut off: "It is false, i.e. this token is
# halal." was reduced to the accepted sentence "this token is halal."
_ABBREVIATIONS = frozenset({
    'i.e', 'e.g', 'etc', 'vs', 'cf', 'al', 'approx', 'est', 'fig', 'no',
    'u.s', 'u.k', 'u.a.e', 'inc', 'ltd', 'llc', 'co', 'corp', 'plc',
    'dr', 'mr', 'mrs', 'ms', 'prof', 'sr', 'jr', 'st', 'vol', 'ch', 'pp',
})
_BOUNDARY = re.compile(r'[.!?]+\s+')


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    """Split into sentences, refusing to break on an abbreviation.

    A boundary counts only when the punctuation is not the tail of a known
    abbreviation AND the next character starts a new sentence (uppercase or
    a digit). Both conditions were missing: "i.e." split, and so did a period
    followed by a lowercase word.
    """
    starts = [0]
    for match in _BOUNDARY.finditer(text):
        prefix = text[:match.start()]
        last = re.split(r'[\s(\["]', prefix)[-1].casefold().rstrip('.')
        if last in _ABBREVIATIONS or len(last) == 1:
            continue  # "i.e." or an initial such as "J."
        nxt = text[match.end():match.end() + 1]
        if nxt and not (nxt.isupper() or nxt.isdigit()):
            continue  # a lowercase continuation is not a new sentence
        starts.append(match.end())
    ends = starts[1:] + [len(text)]
    return list(zip(starts, ends))


def containing_contexts(quote: object, text: object) -> list[str]:
    """Every sentence in ``text`` that contains ``quote``.

    ALL occurrences are returned, not just the first. Using ``find()`` meant
    a page reading "This token is halal. It is false that this token is
    halal." matched the affirmative sentence and ignored the contradictory
    one, because the draft records no offset saying which occurrence the
    owner actually reviewed.
    """
    needle = _normalize(quote).casefold()
    haystack = _normalize(text)
    if not needle or not haystack:
        return []
    folded = haystack.casefold()
    spans = _sentence_spans(haystack)
    found, cursor = [], 0
    while True:
        index = folded.find(needle, cursor)
        if index < 0:
            break
        for left, right in spans:
            if left <= index < right:
                found.append(haystack[left:right].strip())
                break
        cursor = index + 1
    return found


def containing_sentence(quote: object, text: object) -> str:
    """The single sentence containing ``quote``, or '' if it is ambiguous.

    Deliberately returns '' when the fragment appears more than once: with no
    recorded offset there is no way to know which occurrence was reviewed,
    and guessing the first one is how a contradictory occurrence got ignored.
    """
    contexts = containing_contexts(quote, text)
    return contexts[0] if len(contexts) == 1 else ''


def quote_conflict_in_source(value: object, quote: object, text: object,
                             **kwargs) -> str:
    """Apply the positive-verdict policy to the quote's FULL source sentence.

    ``quote`` may be any fragment the owner selected; what is judged is the
    sentence the source actually contains. A fragment that cannot be located
    at all is refused outright.
    """
    if canonical_screener_verdict(value) != POSITIVE_SCREENER_VERDICT:
        return ''
    contexts = containing_contexts(quote, text)
    if not contexts:
        return ('positive verdict quote was not found verbatim in the '
                'retrieved source')
    # EVERY occurrence must be affirmative. Judging only the first let a page
    # containing both "This token is halal." and "It is false that this token
    # is halal." pass on the affirmative one while the contradiction sat two
    # sentences away. The draft records no offset identifying which
    # occurrence the owner read, so until it does, ambiguity is refused.
    for sentence in contexts:
        conflict = positive_verdict_conflict(value, sentence, **kwargs)
        if conflict:
            where = ('' if len(contexts) == 1 else
                     f' (1 of {len(contexts)} occurrences of this fragment)')
            return (f'{conflict} (judged on the full source sentence '
                    f'{sentence!r}, not the supplied fragment){where}')
    return ''
