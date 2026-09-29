"""Structured facts for the v19.3 stablecoin, wrapper and governance rules.

Facts are owner judgments bound to opened official evidence. This code derives
the permitted outcome; a generic passed=True assertion cannot replace pillars.
"""

PILLARS = {
    'STABLECOIN': ('RESERVE_AUDIT', 'ISSUER_MODEL', 'REDEMPTION', 'INTEREST_FLOW', 'CERTIFICATION'),
    'WRAPPED_BRIDGED': ('UNDERLYING', 'CUSTODY', 'DERIVATIVE_MECHANICS', 'REDEMPTION'),
    'GOVERNANCE': ('GOVERNED_PROTOCOL', 'CORE_FUNCTION', 'ECONOMIC_LINK', 'SEPARABILITY'),
}


def evaluate_framework(kind, review, documents):
    from services.sharia_rules.v193 import evidence_bound

    pillars = review.get('pillars', {})
    if not isinstance(pillars, dict):
        return False, False, [kind + ' pillar review malformed']
    valid = True
    for name in PILLARS[kind]:
        pillar = pillars.get(name, {})
        # Certification may be explicitly absent; the official identity and
        # search outcome still have to be recorded, not invented as a quote.
        absent_certificate = (kind == 'STABLECOIN' and name == 'CERTIFICATION'
            and isinstance(pillar, dict) and pillar.get('status') == 'NOT_FOUND'
            and bool(pillar.get('reason')))
        valid = valid and (absent_certificate or (
            isinstance(pillar, dict) and pillar.get('reviewed') is True
            and evidence_bound(pillar.get('evidence'), documents)))
    if not valid:
        return False, False, [kind + ' named pillars incomplete']
    facts = review.get('facts', {})
    if not isinstance(facts, dict):
        return False, False, [kind + ' facts malformed']
    notes = []
    optional = False
    if kind == 'STABLECOIN':
        keys = ('audited', 'issuer_earns_interest', 'holder_receives_interest',
                'redemption_one_to_one', 'redemption_restricted')
        valid = all(isinstance(facts.get(key), bool) for key in keys)
        valid = valid and facts.get('reserve_model') == 'FULLY_BACKED' and facts.get('audited') is True
        valid = valid and facts.get('redemption_one_to_one') is True and facts.get('redemption_restricted') is False
        if facts.get('holder_receives_interest') is True:
            valid = False
            notes.append('stablecoin holder interest requires prohibited-income review')
        optional = valid and facts.get('issuer_earns_interest') is True
    elif kind == 'WRAPPED_BRIDGED':
        valid = all(facts.get(key) is True for key in (
            'underlying_halal', 'custody_verified', 'redemption_one_to_one'))
        valid = valid and facts.get('redemption_restricted') is False
        automatic = facts.get('automatic_yield')
        source = facts.get('yield_source')
        valid = valid and isinstance(automatic, bool) and (
            (automatic is False and source == 'NONE') or
            (automatic is True and source == 'VARIABLE_POS_ONLY'))
        if source in {'LENDING_INTEREST', 'FIXED_GUARANTEED', 'MIXED_UNKNOWN'}:
            notes.append('automatic wrapped-token economics require prohibited-income review')
    else:
        valid = isinstance(facts.get('governed_protocol'), str) and bool(facts['governed_protocol'].strip())
        valid = valid and isinstance(facts.get('spot_separable'), bool)
        valid = valid and isinstance(facts.get('holder_linked_to_prohibited_income'), bool)
        valid = valid and facts.get('holder_linked_to_prohibited_income') is False
        valid = valid and (facts.get('core_function') == 'LAWFUL' or (
            facts.get('core_function') == 'MIXED' and facts.get('spot_separable') is True))
    if not valid:
        notes.append(kind + ' facts do not establish a permitted spot outcome')
    return bool(valid), bool(optional), notes
