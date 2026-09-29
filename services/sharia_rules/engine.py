"""Deterministic evaluation of the V19.3 controller against retrieved sources.

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
from dataclasses import dataclass, field, replace
from urllib.parse import urlparse

from services.common.sharia_v19 import SCREENER_HOSTS
from services.sharia_screener.verdict_policy import (
    POSITIVE_SCREENER_VERDICT,
    canonical_screener_verdict,
    containing_sentence,
    quote_conflict_in_source,
)

MIN_QUOTE_WORDS = 15
# Conditions that cannot be established by pattern matching alone. The
# controller requires them for HARAM; the engine refuses to assert them and
# routes to the owner instead of inventing a judgement.
JUDGEMENT_CONDITIONS = ('C1_CONFIRMED_NARRATIVE', 'C3_ACTIVE_AND_MATERIAL',
                        'C4_ECONOMIC_LINK', 'C5_SPOT_RELEVANCE')
# TOKEN_TYPE_CLASSIFICATION_GATE.types, plus the schema's UNKNOWN. A type
# outside this set is not a classification, so it cannot satisfy the gate.
VALID_TOKEN_TYPES = frozenset({
    'PAYMENT', 'PAYMENT_CURRENCY', 'UTILITY', 'GOVERNANCE', 'TOKENIZED_ASSET',
    'EQUITY_SECURITY', 'STABLECOIN', 'WRAPPED_BRIDGED', 'NFT', 'HYBRID',
})


def canonical_token_type(value: str) -> str:
    """Reconcile the controller classification label with its output schema."""
    normalized = str(value).strip().upper()
    return 'PAYMENT' if normalized == 'PAYMENT_CURRENCY' else normalized


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
        try:
            parsed = urlparse(self.url)
            valid_url = (parsed.scheme.lower() == 'https' and
                         bool(parsed.hostname) and not parsed.username and
                         not parsed.password)
        except ValueError:
            valid_url = False
        return (
            self.http_status == 200 and bool(self.text.strip()) and valid_url and
            re.fullmatch(r'[0-9a-f]{64}', self.content_sha256) is not None and
            bool(self.retrieved_utc.strip())
        )


@dataclass(frozen=True)
class EvidenceClaim:
    """A positive fact tied to one exact retrieved document.

    ``value`` is the asserted classification or verdict. ``quote`` must occur
    verbatim (after whitespace normalisation) in the document identified by
    both ``url`` and ``content_sha256``.  This prevents a caller from turning
    a bare boolean or enum into a passed GREEN proof-card check.
    """
    value: str
    quote: str
    url: str
    content_sha256: str


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
    v193_assessment: dict = field(default_factory=dict)

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

    The quote is exactly the sentence containing the match — nothing is
    appended to reach the minimum. Two earlier versions inflated it: the first
    padded word-by-word across the document, the second joined one following
    sentence. Both could turn "We offer lending." into qualifying evidence by
    adjoining text about branding and site navigation. A contiguous extract
    can be verbatim and still be irrelevant to the matched claim, so length
    borrowed from unrelated prose is not evidence.

    A short sentence therefore yields a short quote, and the caller must treat
    the lead as unresolved — escalated to the owner, never auto-HARAM and
    never silently dropped.
    """
    if not text or start < 0 or end > len(text) or start >= end:
        return ''
    left, right = _sentence_bounds(text, start, end)
    return ' '.join(text[left:right].split())


# Cues that invert the meaning of a following phrase. A whitepaper stating
# "no fixed or guaranteed return is offered" contains the N3 phrase verbatim
# while asserting the opposite; treating that as a confirmed narrative would
# falsely block almost every honest project.
_NEGATION_CUES = (
    'no', 'not', 'never', 'without', 'none', 'neither', 'nor', 'cannot',
    "n't", 'free of', 'free from', 'absent', 'excludes', 'excluding',
    'nothing', 'disclaims', 'disclaim',
)
# Verbs that are themselves negative in polarity. A negation cue applied to
# one of these is a DOUBLE negative, which asserts the phrase rather than
# denying it: "does not prohibit a guaranteed return" permits the return.
# Treating that as a denial erased a genuine disclosure.
_POLARITY_REVERSING_VERBS = (
    'prohibit', 'prohibits', 'prohibited', 'prevent', 'prevents', 'prevented',
    'forbid', 'forbids', 'forbidden', 'exclude', 'excludes', 'excluded',
    'disallow', 'disallows', 'disallowed', 'ban', 'bans', 'banned',
    'restrict', 'restricts', 'restricted', 'preclude', 'precludes',
    'deny', 'denies', 'denied', 'bar', 'bars', 'barred',
)
_NEGATION_WINDOW_WORDS = 10


def is_negated(text: str, start: int) -> bool:
    """True only when a negation cue clearly denies the match at ``start``.

    Bounded to the containing sentence so a denial in a previous sentence
    cannot excuse a later disclosure. Returns False when a polarity-reversing
    verb sits between the cue and the match, because that is a double negative
    that asserts the phrase.

    This is a heuristic and is deliberately biased toward *not* suppressing:
    a false negative here would hide real evidence of a haram feature, which
    is far worse than an extra item on the owner's review queue.
    """
    sentence_start, _ = _sentence_bounds(text, start, start + 1)
    prefix = text[sentence_start:start].lower()
    words = prefix.split()
    window_words = words[-_NEGATION_WINDOW_WORDS:]
    window = ' '.join(window_words)
    if not window:
        return False
    padded = f' {window} '
    cue_index = None
    for cue in _NEGATION_CUES:
        if cue == "n't":
            if "n't" in window:
                cue_index = max(
                    (i for i, w in enumerate(window_words) if "n't" in w),
                    default=None)
                break
        elif f' {cue} ' in padded:
            cue_index = max(
                (i for i, w in enumerate(window_words)
                 if w.strip('.,;:()') == cue.split()[-1]), default=None)
            break
    if cue_index is None:
        return False
    # Double negative between the cue and the match asserts the phrase.
    for word in window_words[cue_index + 1:]:
        if word.strip('.,;:()') in _POLARITY_REVERSING_VERBS:
            return False
    return True


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
            # EVERY occurrence is examined. An earlier version stopped at the
            # first match per phrase per document, so a leading disclaimer
            # ("no guaranteed return is offered in the legacy plan") hid a
            # genuine disclosure later in the same page. That is the exact
            # failure mode that lets a haram coin through.
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
    return haram_leads, clean_signals


def evaluate_haram_gate(controller: dict, leads: list[KeywordHit]
                        ) -> tuple[dict[str, bool], str, list[str]]:
    """Evaluate HARAM_GATE_FIVE_CONDITIONS. Returns (conditions, narrative, notes).

    Only C2 is mechanically decidable. A keyword never proves a narrative,
    materiality, an economic link, or relevance to an ordinary spot holder.
    """
    gate = (controller.get('HARAM_GATE_FIVE_CONDITIONS', {})
            .get('must_prove_all_five', {}))
    conditions = {name: False for name in gate}
    notes: list[str] = []

    quoted = [h for h in leads if h.quote_is_sufficient and
              h.tier.upper() in {'TIER_1', 'TIER_1_OFFICIAL'}]
    narrative = ''
    if quoted:
        conditions['C2_VERBATIM_EVIDENCE'] = True
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
                        expected_screeners: set[str],
                        fact_evidence: dict[str, EvidenceClaim],
                        screener_evidence: dict[str, EvidenceClaim],
                        asset_identifier: str = '',
                        ) -> dict[str, bool]:
    """Mechanically evaluate GREEN_PROOF_GATE.green_requires_all.

    Positive facts are bound to retained evidence wherever the controller
    allows it. Three checks were previously satisfiable by a bare caller
    assertion — an invalid ``token_type`` such as ``BANANA`` counted as
    classified because the string was non-empty, an unbound five-word string
    counted as an official utility quote, and seven blank screener values
    counted as a completed screener check. Together those produced a
    ``PROPOSE_GREEN`` on a one-sentence page, which would have shown the owner
    a proof card claiming all twelve checks passed. A caller boolean is not
    Sharia evidence.

    Adverse leads block GREEN regardless of quote length. A short quote means
    the narrative is unproven, not absent; the lead is unresolved and belongs
    in front of the owner.
    """
    tier1_opened = [d for d in documents
                    if d.is_tier1 and d.opened and d.identity_match]
    def claim_is_bound(claim: object, *, expected_value: str | None = None,
                       min_words: int = 3,
                       tier1_only: bool = False) -> bool:
        if not isinstance(claim, EvidenceClaim):
            return False
        value = str(claim.value).strip()
        if expected_value is not None and value.casefold() != expected_value.casefold():
            return False
        quote = ' '.join(str(claim.quote).split())
        if len(quote.split()) < min_words:
            return False
        for document in documents:
            if not document.opened:
                continue
            if tier1_only and not (document.is_tier1 and document.identity_match):
                continue
            if (document.url != claim.url or
                    document.content_sha256 != claim.content_sha256):
                continue
            # A bare substring test let a negation be edited away: a page
            # reading "It is false that this token is halal." accepted the
            # fragment "this token is halal", and every later check then saw
            # only the laundered text. The quote must therefore resolve to a
            # complete sentence in the source, and callers judge that
            # sentence rather than the fragment.
            if containing_sentence(quote, document.text):
                return True
        return False

    token_claim = fact_evidence.get('token_type')
    utility_claim = fact_evidence.get('utility')
    revenue_claim = fact_evidence.get('revenue')
    token_is_bound = (
        token_type.strip().upper() in VALID_TOKEN_TYPES and
        claim_is_bound(token_claim, expected_value=token_type.strip().upper(),
                       min_words=3, tier1_only=True)
    )
    utility_is_bound = (
        isinstance(utility_claim, EvidenceClaim) and
        ' '.join(utility_claim.quote.split()).casefold() ==
        ' '.join(utility_quote.split()).casefold() and
        claim_is_bound(utility_claim, min_words=5, tier1_only=True)
    )
    revenue_value = (revenue_claim.value.strip().casefold()
                     if isinstance(revenue_claim, EvidenceClaim) else '')
    revenue_is_bound = (
        bool(revenue_clean) and revenue_value in {'clean', 'neutral', 'officially absent', 'non-material'} and
        claim_is_bound(revenue_claim, min_words=5, tier1_only=True)
    )
    known = {k.lower().replace('_', '').replace('-', ''): str(v or '').strip()
             for k, v in screener_results.items()}
    bound_screeners = {
        str(k).lower().replace('_', '').replace('-', ''): v
        for k, v in screener_evidence.items()
    }

    def screener_claim_is_bound(name: str) -> bool:
        claim = bound_screeners.get(name)
        if not isinstance(claim, EvidenceClaim):
            return False
        try:
            claim_host = (urlparse(claim.url).hostname or '').lower().rstrip('.')
        except ValueError:
            return False
        permitted = SCREENER_HOSTS.get(name, frozenset())
        if not any(claim_host == host or claim_host.endswith('.' + host)
                   for host in permitted):
            return False
        verdict = canonical_screener_verdict(known.get(name))
        if verdict != POSITIVE_SCREENER_VERDICT:
            return False
        # Judge the sentence the SOURCE contains, not the fragment the caller
        # supplied. Judging the fragment let a negative prefix be trimmed off
        # ("It is false that this token is halal." -> "this token is halal"),
        # which laundered a negative page into positive evidence.
        source = next((d.text for d in documents
                       if d.opened and d.url == claim.url and
                       d.content_sha256 == claim.content_sha256), '')
        if not source:
            return False
        if quote_conflict_in_source(
                verdict, claim.quote, source,
                permitted_provider_identifiers={name},
                permitted_asset_identifiers={asset_identifier}):
            return False
        return claim_is_bound(
            claim, expected_value=verdict, min_words=3)

    screeners_complete = (
        bool(expected_screeners) and
        expected_screeners <= set(known) and
        expected_screeners <= set(bound_screeners) and
        all(canonical_screener_verdict(known.get(name)) ==
            POSITIVE_SCREENER_VERDICT and screener_claim_is_bound(name)
            for name in expected_screeners)
    )
    adverse = [h for h in leads if not h.negated]
    return {
        'identity_verified': bool(identity_confirmed) and bool(tier1_opened),
        'token_type_classified': token_is_bound,
        'tier1_official_source_opened': bool(tier1_opened),
        'real_utility_official_quote': utility_is_bound,
        'revenue_clean_or_non_material': revenue_is_bound,
        'no_confirmed_haram_narrative': not adverse,
        'no_automatic_haram_income': not any(
            h.narrative == 'N6' for h in adverse),
        'no_unresolved_yield_treasury_reward': not any(
            h.narrative in {'N2', 'N3', 'N9'} for h in adverse),
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
    non_positive = {
        name: value for name, value in verdicts.items()
        if canonical_screener_verdict(value) != POSITIVE_SCREENER_VERDICT
    }
    if non_positive:
        fired.append(
            'one or more named Shariah screeners are non-positive or unknown: '
            + ', '.join(f'{name}={value or "<blank>"}'
                        for name, value in sorted(non_positive.items())))
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
             contradictions=None, expected_screeners=None, fact_evidence=None,
             screener_evidence=None, asset_identifier: str = '',
             material_review=None, discovery_record=None) -> RulesFinding:
    """Evaluate v19.3 evidence; GREEN is only an unsigned owner proposal."""
    from services.common.sharia_v19 import V19_CONTROLLER_SHA256
    from services.sharia_rules.v193 import assess

    token_type = canonical_token_type(token_type)
    fact_evidence = dict(fact_evidence or {})
    token_claim = fact_evidence.get('token_type')
    if isinstance(token_claim, EvidenceClaim):
        fact_evidence['token_type'] = replace(
            token_claim, value=canonical_token_type(token_claim.value))
    leads, clean = scan_keywords(controller, documents)
    finding = RulesFinding(disposition=Disposition.AUTO_NO_TRADE_INFO,
        hits=leads, clean_hits=clean,
        keyword_scan_completed=any(d.opened for d in documents))
    conditions, _, _ = evaluate_haram_gate(controller, leads)
    finding.haram_conditions = conditions
    finding.green_checks = evaluate_green_gate(
        documents, leads, screener_results, identity_confirmed, token_type,
        utility_quote, revenue_clean, list(contradictions or []),
        finding.keyword_scan_completed, set(expected_screeners or set()),
        dict(fact_evidence or {}), dict(screener_evidence or {}), asset_identifier)
    # External screeners are advisory under v19.3, including unavailable or
    # negative results. They never substitute for project economic evidence.
    finding.green_checks.pop('shariah_screener_check_completed', None)
    result = assess(material_review, documents=documents, hits=leads + clean,
                    token_type=token_type, discovery=discovery_record,
                    controller_sha256=V19_CONTROLLER_SHA256)
    # Advisory absence/negativity is neutral, but a supplied forged advisory
    # citation is still malformed evidence and must not appear on a proof card.
    for name, claim in (screener_evidence or {}).items():
        if claim is None:
            continue
        normalized = name.lower().replace('_', '').replace('-', '')
        document = next((d for d in documents if d.opened and d.url == claim.url
                         and d.content_sha256 == claim.content_sha256), None)
        host = (urlparse(claim.url).hostname or '').lower().rstrip('.')
        hosts = SCREENER_HOSTS.get(normalized, ())
        verdict = canonical_screener_verdict(claim.value)
        if (not any(host == h or host.endswith('.' + h) for h in hosts)
                or document is None or not verdict
                or not containing_sentence(claim.quote, document.text)
                or quote_conflict_in_source(verdict, claim.quote, document.text,
                    permitted_provider_identifiers={normalized},
                    permitted_asset_identifiers={asset_identifier})):
            result['issues'].append('invalid advisory evidence binding: ' + normalized)
    finding.v193_assessment = result
    if asset_identifier and (not isinstance(discovery_record, dict)
                             or discovery_record.get('base') != asset_identifier.upper()):
        result['checks']['no_unresolved_identity_conflict'] = False
        result['issues'].append('discovery record does not match the requested asset')
    finding.green_checks.update(result['checks'])
    if contradictions:
        unresolved = [c for c in contradictions if not isinstance(c, dict) or not (
            c.get('material') is False or c.get('resolved') is True)]
        if unresolved:
            finding.green_checks['no_unresolved_material_contradiction'] = False
            result['issues'].append('independently detected material contradiction remains unresolved')
    if result['proven_paths']:
        finding.disposition = Disposition.ESCALATE
        finding.reasons.append('evidence-bound N1-N10 economic path requires owner decision')
        return finding
    failed = sorted(k for k, v in finding.green_checks.items() if not v)
    if failed or result['issues']:
        finding.reasons = list(result['issues'])
        if failed:
            finding.reasons.append('Incomplete material proof checks: ' + ', '.join(failed))
        return finding
    finding.disposition = Disposition.PROPOSE_GREEN
    finding.reasons.append('All 15 v19.3 proof checks passed; exact-evidence owner approval remains required')
    return finding
