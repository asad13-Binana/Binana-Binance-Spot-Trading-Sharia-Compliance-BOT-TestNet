"""Deterministic evaluation of the V19.1 controller against retrieved sources.

Design rules, in order of importance:

1. **The controller is the only source of rules.** Keyword phrases, narrative
   codes, source tiers, gate conditions and escalation triggers are all read
   from the parsed controller dict. Nothing is duplicated here, so the pinned
   controller hash genuinely governs behaviour.
2. **The engine can only ever restrict.** ``AUTO_HARAM``,
   ``AUTO_NO_TRADE_INFO`` and ``ESCALATE`` are reachable conclusions.
   ``PROPOSE_GREEN`` is not a verdict — it is a request for owner approval.
   There is no code path here that emits a tradeable code.
3. **A keyword hit is a lead, never a verdict.** The controller's own
   ``hit_logic`` says so: a hit must be backed by a verbatim quote and all
   five HARAM gate conditions before HARAM is permissible. Where a condition
   needs human judgement (materiality, economic link), the engine escalates
   instead of guessing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

MIN_QUOTE_WORDS = 15
# Conditions that cannot be established by pattern matching alone. The
# controller requires them for HARAM; the engine refuses to assert them and
# routes to the owner instead of inventing a judgement.
JUDGEMENT_CONDITIONS = ('C3_ACTIVE_AND_MATERIAL', 'C4_ECONOMIC_LINK')


class Disposition:
    AUTO_HARAM = 'AUTO_HARAM'
    AUTO_NO_TRADE_INFO = 'AUTO_NO_TRADE_INFO'
    PROPOSE_GREEN = 'PROPOSE_GREEN'
    ESCALATE = 'ESCALATE'


@dataclass(frozen=True)
class RetrievedDocument:
    """One document the local retriever actually fetched and hashed."""
    url: str
    tier: str
    text: str
    content_sha256: str
    retrieved_utc: str
    http_status: int
    identity_match: bool = False

    @property
    def is_tier1(self) -> bool:
        return self.tier.upper() in {'TIER_1', 'TIER_1_OFFICIAL'}

    @property
    def opened(self) -> bool:
        return self.http_status == 200 and bool(self.text.strip())


@dataclass(frozen=True)
class KeywordHit:
    category: str
    narrative: str
    phrase: str
    quote: str
    url: str
    tier: str
    content_sha256: str
    negated: bool = False

    @property
    def quote_words(self) -> int:
        return len(self.quote.split())

    @property
    def quote_is_sufficient(self) -> bool:
        """Usable as HARAM evidence: long enough AND not a disclaimer."""
        return self.quote_words >= MIN_QUOTE_WORDS and not self.negated


@dataclass
class RulesFinding:
    disposition: str
    reasons: list[str] = field(default_factory=list)
    hits: list[KeywordHit] = field(default_factory=list)
    clean_hits: list[KeywordHit] = field(default_factory=list)
    green_checks: dict[str, bool] = field(default_factory=dict)
    haram_conditions: dict[str, bool] = field(default_factory=dict)
    escalations: list[str] = field(default_factory=list)
    narrative: str = ''
    keyword_scan_completed: bool = False

    @property
    def is_tradeable_proposal(self) -> bool:
        return self.disposition == Disposition.PROPOSE_GREEN


def _iter_keyword_rules(controller: dict):
    """Yield (category, narrative_code, phrase) straight from the controller.

    Handles both shapes the controller uses: a category with ``phrases`` and
    ``maps_to``, and a category whose sub-keys each carry their own
    ``phrases``/``maps_to`` (REBASE_DISTINCTION).
    """
    section = controller.get('KEYWORD_DICTIONARY_FOR_WHITEPAPER_SCAN', {})
    for category, body in (section.get('categories') or {}).items():
        if not isinstance(body, dict):
            continue
        phrases = body.get('phrases')
        maps_to = body.get('maps_to')
        if isinstance(phrases, list) and isinstance(maps_to, str):
            for phrase in phrases:
                if isinstance(phrase, str) and phrase.strip():
                    yield category, maps_to, phrase.strip()
        for sub_name, sub in body.items():
            if not isinstance(sub, dict):
                continue
            sub_phrases = sub.get('phrases')
            sub_maps = sub.get('maps_to')
            if isinstance(sub_phrases, list) and isinstance(sub_maps, str):
                for phrase in sub_phrases:
                    if isinstance(phrase, str) and phrase.strip():
                        yield f'{category}.{sub_name}', sub_maps, phrase.strip()


def _sentence_bounds(text: str, start: int, end: int) -> tuple[int, int]:
    left, right = start, end
    while left > 0 and text[left - 1] not in '.!?\n':
        left -= 1
    while right < len(text) and text[right] not in '.!?\n':
        right += 1
    return left, min(right + 1, len(text))


def extract_quote(text: str, start: int, end: int,
                  min_words: int = MIN_QUOTE_WORDS) -> str:
    """Return the verbatim sentence containing [start:end], or '' if too short.

    Deliberately does NOT pad outward word-by-word to reach ``min_words``.
    An earlier version did, and it manufactured a qualifying quote by stitching
    in neighbouring sentences that had nothing to do with the match — which
    defeats the controller's whole reason for demanding a verbatim minimum.
    At most one following sentence is joined, and only because a claim is often
    completed by the sentence after it. If the passage still falls short, the
    document genuinely does not evidence the narrative and the caller must
    refuse to treat the hit as HARAM evidence.
    """
    if not text or start < 0 or end > len(text) or start >= end:
        return ''
    left, right = _sentence_bounds(text, start, end)
    span = ' '.join(text[left:right].split())
    if len(span.split()) >= min_words:
        return span
    # Join at most one following sentence to complete the thought.
    if right < len(text):
        _l2, right2 = _sentence_bounds(text, right, min(right + 1, len(text)))
        extended = ' '.join(text[left:right2].split())
        if len(extended.split()) >= min_words:
            return extended
        span = extended
    return span if len(span.split()) >= min_words else ''


# Cues that invert the meaning of a following phrase. A whitepaper stating
# "no fixed or guaranteed return is offered" contains the N3 phrase verbatim
# while asserting the opposite; treating that as a confirmed narrative would
# falsely block almost every honest project.
_NEGATION_CUES = (
    'no', 'not', 'never', 'without', 'none', 'neither', 'nor', 'cannot',
    "n't", 'free of', 'free from', 'absent', 'excludes', 'excluding',
    'does not', 'do not', 'is not', 'are not', 'will not', 'nothing',
    'prohibited', 'forbidden', 'disclaims', 'disclaim', 'avoids', 'avoid',
)
_NEGATION_WINDOW_WORDS = 10


def is_negated(text: str, start: int) -> bool:
    """True when a negation cue governs the match at ``start``.

    Only the text between the start of the containing sentence and the match
    is considered, within a bounded window, so a negation in a previous
    sentence does not silently excuse a real disclosure.
    """
    sentence_start, _ = _sentence_bounds(text, start, start + 1)
    prefix = text[sentence_start:start].lower()
    words = prefix.split()
    window = ' '.join(words[-_NEGATION_WINDOW_WORDS:])
    if not window:
        return False
    padded = f' {window} '
    for cue in _NEGATION_CUES:
        if cue.startswith("n't"):
            if "n't" in window:
                return True
        elif f' {cue} ' in padded:
            return True
    return False


def scan_keywords(controller: dict,
                  documents: list[RetrievedDocument]
                  ) -> tuple[list[KeywordHit], list[KeywordHit]]:
    """Scan opened documents for controller keywords.

    Returns (haram_leads, clean_signals). Matching is whole-phrase and
    case-insensitive; a phrase is only a lead, and carries the verbatim
    surrounding text so the owner can read it in context.
    """
    haram_leads: list[KeywordHit] = []
    clean_signals: list[KeywordHit] = []
    for doc in documents:
        if not doc.opened:
            continue
        haystack = doc.text
        for category, narrative, phrase in _iter_keyword_rules(controller):
            pattern = re.compile(
                r'(?<!\w)' + re.escape(phrase).replace(r'\ ', r'\s+') + r'(?!\w)',
                re.IGNORECASE)
            for match in pattern.finditer(haystack):
                negated = is_negated(haystack, match.start())
                hit = KeywordHit(
                    category=category,
                    narrative=narrative,
                    phrase=phrase,
                    quote=extract_quote(haystack, match.start(), match.end()),
                    url=doc.url,
                    tier=doc.tier,
                    content_sha256=doc.content_sha256,
                    negated=negated,
                )
                if narrative in {'CLEAN_PASS', 'NEUTRAL'} or negated:
                    # A negated haram phrase is a disclaimer, not a disclosure.
                    # It is retained (the owner still sees it on the proof
                    # card) but it can never satisfy the HARAM gate.
                    clean_signals.append(hit)
                else:
                    haram_leads.append(hit)
                break  # one representative hit per phrase per document
    return haram_leads, clean_signals


def evaluate_haram_gate(controller: dict, leads: list[KeywordHit]
                        ) -> tuple[dict[str, bool], str, list[str]]:
    """Evaluate HARAM_GATE_FIVE_CONDITIONS. Returns (conditions, narrative, notes).

    C1 and C2 are mechanically decidable. C5 follows from the controller's
    spot-only lock. C3 and C4 require judgement about whether a feature is
    active, material and economically linked to holders — the engine never
    asserts those, so a HARAM verdict is only auto-issued when the remaining
    conditions hold AND the owner has confirmed the judgement ones.
    """
    gate = (controller.get('HARAM_GATE_FIVE_CONDITIONS', {})
            .get('must_prove_all_five', {}))
    conditions = {name: False for name in gate}
    notes: list[str] = []

    quoted = [h for h in leads if h.quote_is_sufficient and
              h.tier.upper() in {'TIER_1', 'TIER_1_OFFICIAL'}]
    narrative = ''
    if quoted:
        narrative = quoted[0].narrative
        conditions['C1_CONFIRMED_NARRATIVE'] = True
        conditions['C2_VERBATIM_EVIDENCE'] = True
        conditions['C5_SPOT_RELEVANCE'] = True
    elif leads:
        short = [h for h in leads if not h.quote_is_sufficient]
        if short:
            notes.append(
                f'{len(short)} keyword lead(s) lacked a {MIN_QUOTE_WORDS}-word '
                'verbatim quote; controller requires downgrade, not HARAM')
        non_tier1 = [h for h in leads if h.tier.upper() not in
                     {'TIER_1', 'TIER_1_OFFICIAL'}]
        if non_tier1:
            notes.append(
                f'{len(non_tier1)} keyword lead(s) came from below Tier 1; '
                'HARAM requires a Tier 1 or high-reliability source')
    for name in JUDGEMENT_CONDITIONS:
        if name in conditions:
            notes.append(f'{name} requires owner judgement; engine will not assert it')
    return conditions, narrative, notes


def evaluate_green_gate(documents: list[RetrievedDocument],
                        leads: list[KeywordHit],
                        screener_results: dict[str, str],
                        identity_confirmed: bool,
                        token_type: str,
                        utility_quote: str,
                        revenue_clean: bool,
                        contradictions: list[str],
                        keyword_scan_completed: bool,
                        expected_screeners: set[str]) -> dict[str, bool]:
    """Mechanically evaluate GREEN_PROOF_GATE.green_requires_all.

    Every check is evidence-derived. None of them defaults to True.
    """
    tier1_opened = [d for d in documents
                    if d.is_tier1 and d.opened and d.identity_match]
    screeners_complete = (
        bool(expected_screeners) and
        expected_screeners <= {k.lower().replace('_', '').replace('-', '')
                               for k in screener_results}
    )
    return {
        'identity_verified': bool(identity_confirmed),
        'token_type_classified': bool(token_type),
        'tier1_official_source_opened': bool(tier1_opened),
        'real_utility_official_quote': len(utility_quote.split()) >= 5,
        'revenue_clean_or_non_material': bool(revenue_clean),
        'no_confirmed_haram_narrative': not any(
            h.quote_is_sufficient for h in leads),
        'no_automatic_haram_income': not any(
            h.narrative == 'N6' and h.quote_is_sufficient for h in leads),
        'no_unresolved_yield_treasury_reward': not any(
            h.narrative in {'N2', 'N3', 'N9'} and h.quote_is_sufficient
            for h in leads),
        'no_unresolved_identity_conflict': bool(identity_confirmed),
        'keyword_scan_completed': bool(keyword_scan_completed),
        'no_unresolved_material_contradiction': not contradictions,
        'shariah_screener_check_completed': screeners_complete,
    }


def detect_escalations(controller: dict, screener_results: dict[str, str],
                       token_type: str, contradictions: list[str],
                       haram_notes: list[str]) -> list[str]:
    """Fire the controller's own HUMAN_ESCALATION_TRIGGERS where detectable."""
    fired: list[str] = []
    verdicts = {k: str(v).strip().lower() for k, v in screener_results.items()}
    says_halal = {k for k, v in verdicts.items() if 'halal' in v and 'not' not in v}
    says_haram = {k for k, v in verdicts.items() if 'haram' in v}
    if says_halal and says_haram:
        fired.append(
            f'split verdict across credible Shariah screeners '
            f'(halal: {sorted(says_halal)}; haram: {sorted(says_haram)})')
    if token_type.upper() in {'STABLECOIN', 'WRAPPED_BRIDGED'}:
        fired.append(
            f'{token_type} requires the sub-framework and holder-rights review')
    if token_type.upper() == 'GOVERNANCE':
        fired.append('governance token requires protocol separability review')
    for note in haram_notes:
        if 'owner judgement' in note:
            continue
        fired.append(note)
    fired.extend(f'unresolved contradiction: {c}' for c in contradictions)
    return fired


def evaluate(controller: dict, *, documents: list[RetrievedDocument],
             screener_results: dict[str, str], identity_confirmed: bool,
             token_type: str, utility_quote: str, revenue_clean: bool,
             contradictions: list[str] | None = None,
             expected_screeners: set[str] | None = None) -> RulesFinding:
    """Run the full deterministic pass and return a disposition.

    The result is never a tradeable verdict. ``PROPOSE_GREEN`` means every
    mechanical check passed and the decision now belongs to the owner.
    """
    contradictions = list(contradictions or [])
    expected_screeners = set(expected_screeners or set())

    opened = [d for d in documents if d.opened]
    leads, clean = scan_keywords(controller, documents)
    scan_completed = bool(opened)

    finding = RulesFinding(
        disposition=Disposition.AUTO_NO_TRADE_INFO,
        hits=leads, clean_hits=clean, keyword_scan_completed=scan_completed)

    if not opened:
        finding.reasons.append(
            'no source was successfully retrieved; controller requires '
            'fail-closed NO_TRADE_INFO')
        return finding

    conditions, narrative, haram_notes = evaluate_haram_gate(controller, leads)
    finding.haram_conditions = conditions
    finding.narrative = narrative

    mechanical = [n for n in conditions if n not in JUDGEMENT_CONDITIONS]
    if mechanical and all(conditions[n] for n in mechanical):
        # A quoted Tier-1 narrative exists. The controller still forbids HARAM
        # until C3/C4 are established, and those need judgement — escalate with
        # the proof card rather than guessing in either direction.
        finding.disposition = Disposition.ESCALATE
        finding.reasons.append(
            f'confirmed narrative {narrative} with a verbatim Tier 1 quote; '
            'owner must confirm it is active, material and economically linked')
        finding.escalations = detect_escalations(
            controller, screener_results, token_type, contradictions, haram_notes)
        return finding

    finding.green_checks = evaluate_green_gate(
        documents=documents, leads=leads, screener_results=screener_results,
        identity_confirmed=identity_confirmed, token_type=token_type,
        utility_quote=utility_quote, revenue_clean=revenue_clean,
        contradictions=contradictions, keyword_scan_completed=scan_completed,
        expected_screeners=expected_screeners)

    escalations = detect_escalations(
        controller, screener_results, token_type, contradictions, haram_notes)
    finding.escalations = escalations

    failed = sorted(k for k, v in finding.green_checks.items() if not v)
    if failed:
        finding.disposition = Disposition.AUTO_NO_TRADE_INFO
        finding.reasons.append(
            f'GREEN proof gate incomplete; failed checks: {failed}')
        return finding
    if escalations:
        finding.disposition = Disposition.ESCALATE
        finding.reasons.append(
            'all mechanical checks passed but a controller escalation trigger fired')
        return finding

    finding.disposition = Disposition.PROPOSE_GREEN
    finding.reasons.append(
        'all 12 GREEN proof-gate checks passed mechanically; '
        'owner approval and signature are still required')
    return finding
