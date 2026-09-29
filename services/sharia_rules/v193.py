"""Evidence-bound owner judgments required by the v19.3 controller.

The deterministic engine can check evidence identity and completeness, but it
cannot infer materiality or project economics from a word. These judgments
come from the owner-reviewed registry and remain proposals until signed.
"""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

from services.sharia_screener.evidence_binding import (
    EvidenceBindingError, bind_reviewed_block, verify_reviewed_block,
)

CONTEXTS = frozenset({
    'PROJECT_CORE_ACTIVITY', 'PROJECT_REVENUE_ACTIVITY',
    'PROJECT_TREASURY_OR_TOKENOMICS', 'OPTIONAL_PROJECT_FEATURE',
    'THIRD_PARTY_PROTOCOL', 'EXCHANGE_PRODUCT', 'ECOSYSTEM_APPLICATION',
    'HISTORICAL_OR_DEPRECATED', 'ROADMAP_ONLY', 'RISK_DISCLOSURE_ONLY',
    'NEGATED_STATEMENT', 'UNCLEAR_CONTEXT',
})
INACTIVE_CONTEXTS = frozenset({
    'HISTORICAL_OR_DEPRECATED', 'ROADMAP_ONLY', 'RISK_DISCLOSURE_ONLY',
    'NEGATED_STATEMENT',
})
TARGETS = ('OFFICIAL_WEBSITE', 'OFFICIAL_DOCS', 'OFFICIAL_WHITEPAPER',
           'OFFICIAL_TOKENOMICS_OR_ECONOMICS', 'OFFICIAL_GITHUB', 'PRIMARY_EXPLORER')
NODES = ('ACTIVITY', 'REVENUE_SOURCE', 'REVENUE_RECIPIENT',
         'PROJECT_OR_PROTOCOL_TREASURY', 'TOKEN_VALUE_CAPTURE_OR_DISTRIBUTION',
         'ORDINARY_SPOT_HOLDER')
# These sources can confirm a stop without being the token project's own
# domain. They never become Tier 1 project economics or identity evidence.
TECH_AUTHORITY_HOSTS = frozenset({'treasury.gov', 'ofac.treas.gov', 'un.org',
                                 'europa.eu', 'binance.com'})


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False).encode()).hexdigest()


def hit_id(hit) -> str:
    return digest({key: getattr(hit, key) for key in (
        'category', 'phrase', 'quote', 'url', 'content_sha256')})


def evidence_bound(items, documents, *, minimum_words=3, official=True) -> bool:
    if not isinstance(items, list) or not items:
        return False
    for item in items:
        if not isinstance(item, dict):
            return False
        quote = str(item.get('quote', ''))
        if len(quote.split()) < minimum_words:
            return False
        document = next((d for d in documents
            if d.opened and (not official or (d.is_tier1 and d.identity_match))
            and d.url == item.get('url') and d.content_sha256 == item.get('content_sha256')), None)
        if document is None:
            return False
        if any(key in item for key in ('quote_start', 'context_sha256', 'extractor_version')):
            valid, _ = verify_reviewed_block(document.text, quote, item)
            if not valid:
                return False
        else:
            # Unambiguous complete blocks are safe without explicit offsets;
            # fragments and repeated blocks require the full reviewed binding.
            try:
                bind_reviewed_block(document.text, quote)
            except EvidenceBindingError:
                return False
    return True


def material_gap(item: dict) -> bool:
    """The controller's Q1 AND (Q2 OR Q3), never missing-data => prohibition."""
    return (item.get('could_change_verdict') is True and
            (item.get('direct_holder_connection') is True or
             item.get('current_active_connection') is True))


def tech_evidence_bound(items, documents):
    if not evidence_bound(items, documents, official=False):
        return False
    for item in items:
        doc = next(d for d in documents if d.url == item['url']
                   and d.content_sha256 == item['content_sha256'])
        host = (urlparse(doc.url).hostname or '').lower().rstrip('.')
        if not ((doc.is_tier1 and doc.identity_match) or
                any(host == root or host.endswith('.' + root) for root in TECH_AUTHORITY_HOSTS)):
            return False
    return True


def discovery_order_valid(record) -> bool:
    if not isinstance(record, dict) or record.get('schema_version') != 2:
        return False
    base, market = record.get('base', ''), record.get('binance', {})
    if (not isinstance(base, str) or not base.isalnum()
            or record.get('pair') != base + '/USDT'
            or not isinstance(market, dict) or market.get('symbol') != base + 'USDT'
            or market.get('base_asset') != base or market.get('quote_asset') != 'USDT'
            or market.get('status') != 'TRADING' or market.get('spot_trading_allowed') is not True
            or record.get('trade_permission') is not False or record.get('owner_verified') is not False):
        return False
    ranks = {'binance': 0, 'coingecko': 1, 'coinmarketcap': 2, 'official_project_sources': 3}
    audit = record.get('audit_log')
    if not isinstance(audit, list) or not audit:
        return False
    prior, seen, cg_missing = -1, set(), False
    for step in audit:
        if (not isinstance(step, dict) or step.get('provider') not in ranks
                or step.get('target_type') not in TARGETS
                or not {'tool_or_method', 'query', 'candidate_url', 'status', 'identity_checks',
                        'opened', 'final_accept_or_reject_reason'} <= set(step)):
            return False
        rank = ranks[step['provider']]
        if rank < prior or step['query'] != base or not isinstance(step['opened'], bool):
            return False
        prior = rank
        seen.add((step['provider'], step['target_type']))
        if step['provider'] == 'coingecko' and not step['candidate_url']:
            cg_missing = True
    required = {'binance', 'coingecko', 'official_project_sources'}
    if cg_missing:
        required.add('coinmarketcap')
    return all((provider, target) in seen for provider in required for target in TARGETS)


def assess(review, *, documents, hits, token_type, discovery, controller_sha256):
    """Return verified checks and report fields, never a trading permission."""
    issues = []
    review = review if isinstance(review, dict) else {}
    bound = (review.get('controller_sha256') == controller_sha256
             and review.get('context_confirmed') is True
             and evidence_bound(review.get('evidence'), documents))
    if not bound:
        issues.append('v19.3 material economics review is missing or not evidence-bound')
    contexts = review.get('keyword_context_results', {})
    contexts = contexts if isinstance(contexts, dict) else {}
    relevant = [h for h in hits if h.narrative not in ('NEUTRAL', 'CLEAN_PASS')]
    classified = bound
    unresolved_yield = False
    material_hits = set()
    for hit in relevant:
        item = contexts.get(hit_id(hit), {})
        valid = (isinstance(item, dict) and item.get('context') in CONTEXTS
                 and isinstance(item.get('material'), bool)
                 and isinstance(item.get('current'), bool)
                 and bool(str(item.get('reason', '')).strip())
                 and evidence_bound(item.get('evidence'), documents, official=False)
                 and any(e.get('url') == hit.url
                         and e.get('content_sha256') == hit.content_sha256
                         and hit.quote in str(e.get('quote', ''))
                         for e in item.get('evidence', []) if isinstance(e, dict)))
        if valid and item['context'] in INACTIVE_CONTEXTS and item['current']:
            valid = False
        classified = classified and valid
        if valid and item['material'] and item['current']:
            material_hits.add(hit_id(hit))
        if valid and hit.tier not in {'TIER_1', 'TIER_1_OFFICIAL'}:
            # A secondary mention establishes only what the secondary source
            # said. Resolve its relevance against official project economics.
            classified = classified and evidence_bound(item.get('resolution_evidence'), documents)
        if valid and item['context'] == 'UNCLEAR_CONTEXT' and item['material']:
            issues.append('material keyword context remains unresolved')
            unresolved_yield = unresolved_yield or hit.narrative in {'N2', 'N3', 'N6', 'N9'}
    if not classified:
        issues.append('keyword context classification is incomplete or unbound')

    gaps = review.get('material_missing_info', {})
    gaps = gaps if isinstance(gaps, dict) else {}
    gaps_valid = bound and isinstance(gaps.get('items'), list)
    blocking_gaps = []
    for item in gaps.get('items', []) if isinstance(gaps.get('items'), list) else []:
        if (not isinstance(item, dict) or not str(item.get('reason', '')).strip()
                or any(not isinstance(item.get(k), bool) for k in (
                    'could_change_verdict', 'direct_holder_connection',
                    'current_active_connection', 'resolved'))):
            gaps_valid = False
            continue
        if material_gap(item) and not item['resolved']:
            # Six ordered retries are required by the operational rule. The
            # controller's final-audit prose still says five; do not edit it.
            blocking_gaps.append(item.get('item', 'unnamed material fact'))
        elif material_gap(item) and not evidence_bound(item.get('resolution_evidence'), documents):
            gaps_valid = False
            issues.append('material gap resolution lacks source evidence')
    if blocking_gaps:
        issues.append('material research remains incomplete: ' + ', '.join(blocking_gaps))

    discovery_review = review.get('official_source_discovery', {})
    targets = discovery_review.get('targets', {}) if isinstance(discovery_review, dict) else {}
    discovery_valid = (bound and discovery_order_valid(discovery)
                       and review.get('discovery_sha256') == digest({
                           k: v for k, v in discovery.items()
                           if k not in ('cache_hit', 'record_sha256')}))
    verified_urls = {}
    for target in TARGETS:
        item = targets.get(target, {}) if isinstance(targets, dict) else {}
        valid = isinstance(item, dict) and isinstance(item.get('material'), bool)
        if valid and item.get('status') == 'VERIFIED':
            urls = item.get('urls')
            valid = (isinstance(urls, list) and bool(urls)
                     and evidence_bound(item.get('evidence'), documents)
                     and all(any(d.url == url and d.opened and d.identity_match
                                 for d in documents) for url in urls))
            if valid:
                verified_urls[target] = urls
        elif valid:
            valid = (item['material'] is False and bool(item.get('reason'))
                     and item.get('status') in {'NOT_APPLICABLE', 'NOT_FOUND', 'BLOCKED',
                                               'NOT_EXPOSED_BY_PROVIDER'})
        discovery_valid = discovery_valid and valid
    conflicts = review.get('source_identity_conflicts', [])
    identity_clean = (bound and isinstance(conflicts, list)
                      and all(isinstance(c, dict) and c.get('resolved') is True
                              and evidence_bound(c.get('evidence'), documents) for c in conflicts))
    # A discovered disagreement cannot disappear merely by omitting it from
    # the reviewed record. Resolution must name the same competing URLs.
    for conflict in (discovery or {}).get('source_identity_conflicts', []):
        identity_clean = identity_clean and any(
            set(c.get('urls', [])) == set(conflict.get('urls', [])) for c in conflicts)

    graph = review.get('economic_link_graph', {})
    graph = graph if isinstance(graph, dict) else {}
    paths = graph.get('paths')
    graph_valid = bound and isinstance(paths, list)
    proven = []
    covered_hits = set()
    for path in paths if isinstance(paths, list) else []:
        if not isinstance(path, dict):
            graph_valid = False
            continue
        nodes = path.get('nodes', {})
        complete = (isinstance(nodes, dict) and all(
            evidence_bound(nodes.get(node), documents) for node in NODES))
        narrative = path.get('narrative')
        raw_proportion = path.get('revenue_percentage')
        percentage_required = (
            token_type in ('PAYMENT', 'PAYMENT_CURRENCY', 'UTILITY', 'GOVERNANCE')
            and path.get('active') is True and path.get('spot_relevance') is True
            and path.get('status') != 'BROKEN_LINK'
            and path.get('earmarked_token_support') is not True)
        try:
            proportion = Decimal(str(raw_proportion))
            finite = proportion.is_finite() and 0 <= proportion <= 100
        except (InvalidOperation, ValueError):
            finite = False
            proportion = Decimal(0)
        # Unknown revenue is not zero. A proven broken link or earmarked
        # support can resolve materiality independently of a revenue ratio.
        ratio_valid = finite or (raw_proportion is None and not percentage_required)
        valid = (ratio_valid and narrative in {f'N{i}' for i in range(1, 11)}
                 and path.get('status') in {'PROVEN', 'BROKEN_LINK', 'UNRESOLVED'}
                 and all(isinstance(path.get(key), bool) for key in (
                     'active', 'spot_relevance', 'confirmed_narrative', 'earmarked_token_support',
                     'material', 'unresolved_material')))
        if path.get('status') == 'PROVEN':
            valid = valid and complete and evidence_bound(path.get('proof'), documents, minimum_words=15)
        elif path.get('status') == 'BROKEN_LINK':
            valid = (valid and not complete and bool(path.get('missing_nodes'))
                     and set(path.get('missing_nodes', [])) <= set(NODES)
                     and evidence_bound(path.get('broken_link_evidence'), documents)
                     and bool(path.get('reason')))
        elif path.get('status') == 'UNRESOLVED':
            valid = valid and evidence_bound(path.get('evidence'), documents) and bool(path.get('reason'))
        if not valid:
            graph_valid = False
            issues.append('malformed or incomplete economic path evidence')
            continue
        related = path.get('hit_ids', [])
        if not isinstance(related, list) or not all(isinstance(value, str) for value in related):
            graph_valid = False
            issues.append('invalid economic path keyword references')
            continue
        related_hits = {hit_id(hit): hit for hit in relevant}
        if any(value not in related_hits or related_hits[value].narrative != narrative for value in related):
            graph_valid = False
            issues.append('economic path does not match its referenced activity narrative')
            continue
        if path['status'] == 'PROVEN' and path['active'] and any(
                not isinstance(contexts.get(value), dict)
                or contexts[value].get('current') is not True
                or contexts[value].get('context') in INACTIVE_CONTEXTS
                for value in related):
            graph_valid = False
            issues.append('active economic path contradicts its keyword context')
            continue
        covered_hits.update(related)
        material = ((finite and proportion > 5) or path.get('earmarked_token_support') is True)
        if token_type not in ('PAYMENT', 'PAYMENT_CURRENCY', 'UTILITY', 'GOVERNANCE'):
            material = path.get('material') is True
        if (path['status'] == 'PROVEN' and complete and material
                and path.get('active') is True and path.get('spot_relevance') is True
                and path.get('confirmed_narrative') is True
                and evidence_bound(path.get('proof'), documents, minimum_words=15)):
            proven.append(path)
        elif path.get('unresolved_material') is True or (
                path['status'] == 'UNRESOLVED' and material and path['active']):
            issues.append('unresolved material economic path')
            unresolved_yield = True

    if material_hits - covered_hits:
        graph_valid = False
        issues.append('current material activity lacks an economic path assessment')

    tech_review = review.get('tech_stop_review', {})
    tech_triggers = []
    tech_valid = isinstance(tech_review, dict) and tech_review.get('reviewed') is True
    trigger_reviews = tech_review.get('triggers', {}) if tech_valid else {}
    if not isinstance(trigger_reviews, dict):
        trigger_reviews = {}
    for trigger in ('T1', 'T2', 'T3'):
        item = trigger_reviews.get(trigger, {})
        valid = (isinstance(item, dict) and isinstance(item.get('confirmed'), bool)
                 and bool(item.get('reason')))
        if valid and item['confirmed']:
            valid = tech_evidence_bound(item.get('evidence'), documents)
            if valid:
                tech_triggers.append(trigger)
        tech_valid = tech_valid and valid
    if not tech_valid:
        issues.append('TECH_STOP review incomplete or not evidence-bound')
    if tech_triggers:
        issues.append('confirmed TECH_STOP: ' + ', '.join(tech_triggers))

    frameworks = review.get('special_frameworks', {})
    special_types = {'STABLECOIN', 'WRAPPED_BRIDGED', 'GOVERNANCE',
                     'TOKENIZED_ASSET', 'EQUITY_SECURITY', 'NFT'}
    characteristics = review.get('hybrid_characteristics', [])
    hybrid_valid = (isinstance(characteristics, list) and len(set(characteristics)) >= 2
                    and set(characteristics) <= special_types | {'PAYMENT', 'PAYMENT_CURRENCY', 'UTILITY'})
    applicable = ({token_type} if token_type in special_types
                  else set(characteristics) & special_types
                  if token_type == 'HYBRID' and hybrid_valid else set())
    frameworks_valid = bound and isinstance(frameworks, dict)
    if not isinstance(frameworks, dict):
        frameworks = {}
    framework_optional = False
    if token_type == 'HYBRID' and not hybrid_valid:
        frameworks_valid = False
    for framework in applicable:
        item = frameworks.get(framework, {})
        if not isinstance(item, dict):
            frameworks_valid = False
            continue
        frameworks_valid = (frameworks_valid and framework in special_types
            and item.get('complete') is True and item.get('passed') is True
            and evidence_bound(item.get('evidence'), documents))
        if framework == 'TOKENIZED_ASSET':
            frameworks_valid = (frameworks_valid and item.get('underlying_asset_halal') is True
                                and item.get('custody_verified') is True)
        if framework == 'EQUITY_SECURITY':
            ratios = item.get('ratios', {})
            for key, ceiling in (('interest_bearing_debt', 30),
                                 ('cash_and_interest_bearing_securities', 30),
                                 ('impermissible_income', 5)):
                try:
                    value = Decimal(str(ratios.get(key)))
                    valid = value.is_finite() and 0 <= value < ceiling
                except (InvalidOperation, ValueError, AttributeError):
                    valid = False
                frameworks_valid = frameworks_valid and valid
        if framework in {'STABLECOIN', 'WRAPPED_BRIDGED', 'GOVERNANCE'}:
            from services.sharia_rules.special_frameworks import evaluate_framework
            valid, optional_outcome, notes = evaluate_framework(framework, item, documents)
            frameworks_valid = frameworks_valid and valid
            framework_optional = framework_optional or optional_outcome
            issues.extend(notes)
    contradictions = review.get('contradictions_found', [])
    contradiction_clean = (bound and isinstance(contradictions, list) and all(
        isinstance(c, dict) and (c.get('material') is False or
          (c.get('resolved') is True and evidence_bound(c.get('evidence'), documents)))
        for c in contradictions))
    optional = review.get('optional_features', {})
    if not isinstance(optional, dict):
        optional = {}
        issues.append('malformed optional feature review')
    if 'present' in optional and not isinstance(optional['present'], bool):
        issues.append('optional feature presence must be a boolean')
    avoid_optional = (isinstance(optional, dict) and optional.get('present') is True
                     and optional.get('automatic_holder_yield') is False
                     and optional.get('required_for_spot') is False
                     and optional.get('confirmed_haram_funding') is False
                     and evidence_bound(optional.get('evidence'), documents))
    if optional.get('present') is True and not avoid_optional:
        issues.append('optional feature economics require further review')
    gharar = review.get('gharar_speculation_review', {})
    if (not isinstance(gharar, dict) or gharar.get('reviewed') is not True
            or not isinstance(gharar.get('material_absence_of_lawful_function'), bool)):
        issues.append('separate gharar review incomplete')
    elif gharar.get('material_absence_of_lawful_function') is True:
        issues.append('documented absence of lawful function requires separate DOUBTFUL review')

    checks = {
        'material_source_discovery_completed': bool(discovery_valid and gaps_valid and not blocking_gaps),
        'no_confirmed_haram_narrative': bool(graph_valid and not proven),
        'no_automatic_haram_income': bool(bound and review.get('automatic_haram_income') is False),
        'no_unresolved_yield_treasury_reward': bool(bound and not unresolved_yield and not blocking_gaps),
        'no_unresolved_identity_conflict': bool(identity_clean),
        'no_unresolved_material_contradiction': bool(contradiction_clean),
        'material_keyword_hits_context_classified': bool(classified),
        'no_proven_prohibited_economic_link': bool(graph_valid and not proven),
        'applicable_special_frameworks_completed': bool(frameworks_valid),
    }
    return {'checks': checks, 'issues': issues, 'proven_paths': proven,
            'verified_urls': verified_urls, 'avoid_optional': bool(avoid_optional or framework_optional),
            'tech_stop_trigger': next(iter(tech_triggers), 'NONE'),
            'applicable_frameworks': sorted(applicable), 'review': review}


def report_projection(assessment, discovery, controller):
    """One projection shared by report creation and exact-evidence replay."""
    review = assessment['review']
    result = {name: review.get(name, default) for name, default in (
        ('keyword_context_results', {}), ('economic_link_graph', {}),
        ('material_missing_info', {}), ('gharar_speculation_review', {}),
        ('source_identity_conflicts', []), ('contradictions_found', []),
        ('technical_transparency_risk_notes', []))}
    target_review = review.get('official_source_discovery', {})
    if not isinstance(target_review, dict):
        target_review = {}
    audit = list((discovery or {}).get('audit_log', []))
    # Registered official destinations are the explicit owner-reviewed
    # domain fallback. These records reflect bytes opened by this run, not
    # the earlier metadata provider's candidate flags.
    for target, urls in assessment['verified_urls'].items():
        for url in urls:
            audit.append({
                'provider': 'official_project_sources', 'tool_or_method': 'owner_verified_direct_domain',
                'query': (discovery or {}).get('base', ''), 'candidate_url': url,
                'target_type': target, 'status': 'VERIFIED', 'opened': True,
                'identity_checks': target_review.get('targets', {}).get(target, {}).get('evidence', []),
                'final_accept_or_reject_reason': 'exact retrieved source matched the owner-reviewed identity and evidence',
            })
    result['official_source_discovery'] = {**target_review, 'audit_log': audit}
    result['source_discovery_path'] = audit
    result['plugin_source_roles'] = controller['PLUGIN_SOURCE_ROLES']
    result['tech_stop_trigger'] = assessment['tech_stop_trigger']
    for target, field, multiple in (
            ('OFFICIAL_WEBSITE', 'official_website_url', False),
            ('OFFICIAL_DOCS', 'official_docs_urls', True),
            ('OFFICIAL_WHITEPAPER', 'official_whitepaper_url', False),
            ('OFFICIAL_TOKENOMICS_OR_ECONOMICS', 'official_tokenomics_economics_urls', True),
            ('OFFICIAL_GITHUB', 'official_github_url', False),
            ('PRIMARY_EXPLORER', 'primary_explorer_url', False)):
        urls = assessment['verified_urls'].get(target, [])
        result[field] = urls if multiple else next(iter(urls), None)
    result['sub_framework_applied'] = next((f for f in assessment['applicable_frameworks']
        if f in {'STABLECOIN', 'WRAPPED_BRIDGED', 'GOVERNANCE'}), 'NONE')
    return result
