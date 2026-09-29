import copy
import json
import unittest
from pathlib import Path

from services.sharia_rules.engine import RetrievedDocument, evaluate_haram_gate, scan_keywords
from services.sharia_rules.v193 import assess, digest, hit_id, material_gap, TARGETS

SHA = '418e7280f0b6a5f4cd9ba3887b8be3099f5fcc4b18bfca66808749720a4dd355'
CONTROLLER = json.loads((Path(__file__).resolve().parents[1] / 'shared/sharia/' /
    'HALAL_CRYPTO_SPOT_SCREENING_V19_3_PRODUCTION.json').read_text(encoding='utf-8'))


def reviewed_case():
    text = 'The token provides access to verified network services and pays transaction fees.'
    doc = RetrievedDocument('https://project.example/docs', 'TIER_1_OFFICIAL', text,
                            'a'*64, '2026-09-26T00:00:00Z', 200, True)
    evidence = [{'url': doc.url, 'quote': text, 'content_sha256': doc.content_sha256}]
    discovery = {'schema_version': 2, 'base': 'XYZ', 'pair': 'XYZ/USDT',
                 'trade_permission': False, 'owner_verified': False,
                 'binance': {'symbol': 'XYZUSDT', 'base_asset': 'XYZ', 'quote_asset': 'USDT',
                             'status': 'TRADING', 'spot_trading_allowed': True},
                 'audit_log': [{
                     'provider': provider, 'target_type': target, 'tool_or_method': 'metadata',
                     'query': 'XYZ', 'candidate_url': None, 'status': 'NOT_EXPOSED_BY_PROVIDER',
                     'identity_checks': [], 'opened': False, 'final_accept_or_reject_reason': 'fixture'}
                     for provider in ('binance', 'coingecko', 'coinmarketcap', 'official_project_sources')
                     for target in TARGETS], 'source_identity_conflicts': []}
    review = {
        'controller_sha256': SHA, 'context_confirmed': True, 'evidence': evidence,
        'discovery_sha256': digest(discovery), 'keyword_context_results': {},
        'economic_link_graph': {'paths': []}, 'material_missing_info': {'items': []},
        'automatic_haram_income': False, 'special_frameworks': {},
        'gharar_speculation_review': {'reviewed': True, 'material_absence_of_lawful_function': False},
        'tech_stop_review': {'reviewed': True, 'triggers': {
            trigger: {'confirmed': False, 'reason': 'No confirmed trigger in the reviewed sources.'}
            for trigger in ('T1', 'T2', 'T3')}},
        'official_source_discovery': {'targets': {target: {
            'material': False, 'status': 'NOT_APPLICABLE',
            'reason': 'Material utility and economics are covered by the current official page.'}
            for target in TARGETS}},
    }
    review['official_source_discovery']['targets']['OFFICIAL_DOCS'] = {
        'material': True, 'status': 'VERIFIED', 'urls': [doc.url], 'evidence': evidence}
    return doc, discovery, review


class V193Policy(unittest.TestCase):
    def test_optional_and_gharar_flags_cannot_silently_coerce_non_booleans(self):
        for invalid in ('true', 'false', 0, 1, None):
            doc, discovery, review = reviewed_case()
            review['optional_features'] = {'present': invalid}
            self.assertTrue(self.assess(review, doc, discovery)['issues'])
            review.pop('optional_features')
            review['gharar_speculation_review']['material_absence_of_lawful_function'] = invalid
            self.assertTrue(self.assess(review, doc, discovery)['issues'])

    def test_official_regulator_can_confirm_stop_without_project_identity(self):
        doc, discovery, review = reviewed_case()
        regulator = type(doc)(**{**doc.__dict__, 'url': 'https://ofac.treasury.gov/recent-actions/example',
            'tier': 'TIER_3_SECONDARY', 'identity_match': False, 'content_sha256': 'b'*64,
            'text': 'The named issuer is subject to the sanctions described in this official notice.'})
        item = {'confirmed': True, 'reason': 'Owner confirmed the exact issuer identity.',
                'evidence': [{'url': regulator.url, 'quote': regulator.text, 'content_sha256': regulator.content_sha256}]}
        review['tech_stop_review']['triggers']['T1'] = item
        result = assess(review, documents=[doc, regulator], hits=[], token_type='UTILITY',
                        discovery=discovery, controller_sha256=SHA)
        self.assertEqual(result['tech_stop_trigger'], 'T1')
        regulator = type(regulator)(**{**regulator.__dict__, 'url': 'https://treasury.gov.attacker.example/notice'})
        item['evidence'][0]['url'] = regulator.url
        result = assess(review, documents=[doc, regulator], hits=[], token_type='UTILITY',
                        discovery=discovery, controller_sha256=SHA)
        self.assertEqual(result['tech_stop_trigger'], 'NONE')
        self.assertTrue(result['issues'])

    def test_material_evidence_rejects_trimmed_negation_and_ambiguous_occurrences(self):
        from services.sharia_rules.v193 import evidence_bound
        from services.sharia_screener.evidence_binding import bind_reviewed_block
        doc, _, _ = reviewed_case()
        text = 'It is false that the reserve backing and redemption terms are independently verified.'
        doc = type(doc)(**{**doc.__dict__, 'text': text})
        item = {'url': doc.url, 'content_sha256': doc.content_sha256,
                'quote': 'the reserve backing and redemption terms are independently verified'}
        self.assertFalse(evidence_bound([item], [doc]))
        item['quote'] = text
        self.assertTrue(evidence_bound([item], [doc]))
        doc = type(doc)(**{**doc.__dict__, 'text': text + '\n' + text})
        self.assertFalse(evidence_bound([item], [doc]))
        item.update(bind_reviewed_block(doc.text, text, quote_start=0))
        self.assertTrue(evidence_bound([item], [doc]))
        item['context'] = 'tampered'
        self.assertFalse(evidence_bound([item], [doc]))

    def test_material_current_activity_requires_graph_coverage(self):
        doc, discovery, review = reviewed_case()
        doc = type(doc)(**{**doc.__dict__, 'text': 'Our protocol earns lending interest from borrowers and distributes the resulting income to token holders.'})
        evidence = [{'url': doc.url, 'quote': doc.text, 'content_sha256': doc.content_sha256}]
        review['evidence'] = evidence
        review['official_source_discovery']['targets']['OFFICIAL_DOCS']['evidence'] = evidence
        hits, clean = scan_keywords(CONTROLLER, [doc])
        self.assertTrue(hits)
        for hit in hits + clean:
            review['keyword_context_results'][hit_id(hit)] = {
                'context': 'PROJECT_REVENUE_ACTIVITY', 'material': True, 'current': True,
                'reason': 'Current project revenue requires tracing to the ordinary holder.',
                'evidence': [{'url': hit.url, 'quote': hit.quote, 'content_sha256': hit.content_sha256}]}
        result = assess(review, documents=[doc], hits=hits + clean, token_type='UTILITY',
                        discovery=discovery, controller_sha256=SHA)
        self.assertFalse(result['checks']['no_proven_prohibited_economic_link'])
        self.assertIn('current material activity lacks an economic path assessment', result['issues'])

    def test_secondary_mention_can_be_resolved_with_official_economics(self):
        doc, discovery, review = reviewed_case()
        secondary = type(doc)(**{**doc.__dict__, 'url': 'https://screener.example/review',
            'tier': 'TIER_2_PRIMARY_MARKET', 'identity_match': False, 'content_sha256': 'b'*64,
            'text': 'This article discusses third party lending with interest in an unrelated protocol.'})
        hits, clean = scan_keywords(CONTROLLER, [secondary])
        self.assertTrue(hits)
        for hit in hits + clean:
            review['keyword_context_results'][hit_id(hit)] = {
                'context': 'THIRD_PARTY_PROTOCOL', 'material': False, 'current': True,
                'reason': 'Official economics identify network service fees only.',
                'resolution_evidence': review['evidence'],
                'evidence': [{'url': hit.url, 'quote': hit.quote, 'content_sha256': hit.content_sha256}]}
        result = assess(review, documents=[doc, secondary], hits=hits + clean, token_type='UTILITY',
                        discovery=discovery, controller_sha256=SHA)
        self.assertTrue(all(result['checks'].values()), result)
        self.assertEqual(result['issues'], [])

    def test_tech_stop_requires_review_and_confirmed_evidence_blocks_green(self):
        doc, discovery, review = reviewed_case()
        missing = copy.deepcopy(review)
        missing.pop('tech_stop_review')
        self.assertTrue(self.assess(missing, doc, discovery)['issues'])
        review['tech_stop_review']['triggers']['T2'].update(
            confirmed=True, reason='Official confirmation reviewed', evidence=review['evidence'])
        result = self.assess(review, doc, discovery)
        self.assertEqual(result['tech_stop_trigger'], 'T2')
        self.assertTrue(result['issues'])

    def test_stablecoin_reserve_interest_requires_avoid_optional_outcome(self):
        from services.sharia_rules.special_frameworks import PILLARS
        doc, discovery, review = reviewed_case()
        framework = {'complete': True, 'passed': True, 'evidence': review['evidence'],
                     'pillars': {name: {'reviewed': True, 'evidence': review['evidence']}
                                 for name in PILLARS['STABLECOIN']},
                     'facts': {'reserve_model': 'FULLY_BACKED', 'audited': True,
                               'issuer_earns_interest': True, 'holder_receives_interest': False,
                               'redemption_one_to_one': True, 'redemption_restricted': False}}
        review['special_frameworks']['STABLECOIN'] = framework
        result = self.assess(review, doc, discovery, token_type='STABLECOIN')
        self.assertTrue(result['avoid_optional'])
        self.assertTrue(result['checks']['applicable_special_frameworks_completed'])
        framework['facts']['holder_receives_interest'] = True
        self.assertFalse(self.assess(review, doc, discovery, token_type='STABLECOIN')['checks'][
            'applicable_special_frameworks_completed'])

    def test_variable_network_rebase_is_permitted_but_lending_rebase_is_not(self):
        from services.sharia_rules.special_frameworks import PILLARS
        doc, discovery, review = reviewed_case()
        framework = {'complete': True, 'passed': True, 'evidence': review['evidence'],
                     'pillars': {name: {'reviewed': True, 'evidence': review['evidence']}
                                 for name in PILLARS['WRAPPED_BRIDGED']},
                     'facts': {'underlying_halal': True, 'custody_verified': True,
                               'redemption_one_to_one': True, 'redemption_restricted': False,
                               'automatic_yield': True, 'yield_source': 'VARIABLE_POS_ONLY'}}
        review['special_frameworks']['WRAPPED_BRIDGED'] = framework
        result = self.assess(review, doc, discovery, token_type='WRAPPED_BRIDGED')
        self.assertTrue(all(result['checks'].values()), result)
        self.assertEqual(result['issues'], [])
        framework['facts']['yield_source'] = 'LENDING_INTEREST'
        result = self.assess(review, doc, discovery, token_type='WRAPPED_BRIDGED')
        self.assertFalse(result['checks']['applicable_special_frameworks_completed'])
        self.assertTrue(result['issues'])

    def assess(self, review, doc, discovery, **kw):
        return assess(review, documents=[doc], hits=[], token_type=kw.get('token_type','UTILITY'),
                      discovery=discovery, controller_sha256=SHA)

    def test_keyword_alone_cannot_prove_narrative_or_spot_relevance(self):
        doc, _, _ = reviewed_case()
        doc = type(doc)(**{**doc.__dict__, 'text': 'Our platform offers lending with interest '
            'to customers who may elect to use this optional third party service at their own discretion.'})
        leads, _ = scan_keywords(CONTROLLER, [doc])
        self.assertTrue(leads)
        conditions, _, _ = evaluate_haram_gate(CONTROLLER, leads)
        self.assertTrue(conditions['C2_VERBATIM_EVIDENCE'])
        for key in ('C1_CONFIRMED_NARRATIVE', 'C3_ACTIVE_AND_MATERIAL',
                    'C4_ECONOMIC_LINK', 'C5_SPOT_RELEVANCE'):
            self.assertFalse(conditions[key], key)

    def test_material_gap_requires_verdict_impact_and_current_connection(self):
        self.assertFalse(material_gap({'could_change_verdict': False,
                                      'direct_holder_connection': True}))
        self.assertFalse(material_gap({'could_change_verdict': True,
                                      'direct_holder_connection': False,
                                      'current_active_connection': False}))
        self.assertTrue(material_gap({'could_change_verdict': True,
                                     'current_active_connection': True}))

    def test_missing_whitepaper_does_not_block_complete_material_evidence(self):
        doc, discovery, review = reviewed_case()
        result = self.assess(review, doc, discovery)
        self.assertTrue(all(result['checks'].values()), result)
        self.assertEqual(result['issues'], [])

    def test_stale_discovery_or_unopened_destination_cannot_be_verified(self):
        doc, discovery, review = reviewed_case()
        changed = {**discovery, 'different_identity': True}
        self.assertFalse(self.assess(review, doc, changed)['checks']['material_source_discovery_completed'])
        review['official_source_discovery']['targets']['OFFICIAL_DOCS']['urls'] = ['https://other.example/']
        self.assertFalse(self.assess(review, doc, discovery)['checks']['material_source_discovery_completed'])

    def test_hybrid_requires_explicit_applicable_frameworks(self):
        doc, discovery, review = reviewed_case()
        self.assertFalse(self.assess(review, doc, discovery, token_type='HYBRID')['checks'][
            'applicable_special_frameworks_completed'])

    def test_forged_boolean_review_does_not_pass(self):
        doc, discovery, review = reviewed_case()
        review['evidence'][0]['quote'] = 'Invented economic evidence absent from the source.'
        self.assertFalse(any(self.assess(review, doc, discovery)['checks'].values()))

    def test_incomplete_or_nonfinite_economic_path_cannot_disappear(self):
        for percentage in ('20', 'NaN', '-1', '101'):
            doc, discovery, review = reviewed_case()
            review['economic_link_graph']['paths'] = [{
                'narrative': 'N2', 'active': True, 'spot_relevance': True,
                'confirmed_narrative': True, 'revenue_percentage': percentage,
                'nodes': {}, 'proof': []}]
            result = self.assess(review, doc, discovery)
            self.assertFalse(result['checks']['no_proven_prohibited_economic_link'])
            self.assertTrue(result['issues'])

    def test_provider_order_cannot_be_forged_by_rehashing_review(self):
        doc, discovery, review = reviewed_case()
        discovery['audit_log'].reverse()
        review['discovery_sha256'] = digest(discovery)
        self.assertFalse(self.assess(review, doc, discovery)['checks'][
            'material_source_discovery_completed'])

    def test_material_resolution_requires_actual_evidence(self):
        doc, discovery, review = reviewed_case()
        review['material_missing_info']['items'] = [{
            'item': 'automatic income source', 'reason': 'owner assertion', 'resolved': True,
            'could_change_verdict': True, 'direct_holder_connection': True,
            'current_active_connection': True}]
        self.assertFalse(self.assess(review, doc, discovery)['checks'][
            'material_source_discovery_completed'])

    def test_unknown_material_path_percentage_cannot_be_treated_as_zero(self):
        for token_type in ('PAYMENT', 'PAYMENT_CURRENCY', 'UTILITY', 'GOVERNANCE'):
            for percentage in (None, '', 'unknown'):
                with self.subTest(token_type=token_type, percentage=percentage):
                    doc, discovery, review = reviewed_case()
                    path = {
                        'status': 'UNRESOLVED', 'narrative': 'N2', 'active': True,
                        'spot_relevance': True, 'confirmed_narrative': True,
                        'material': True, 'earmarked_token_support': False,
                        'unresolved_material': False, 'reason': 'Recipient under review',
                        'evidence': review['evidence'],
                    }
                    if percentage is not None:
                        path['revenue_percentage'] = percentage
                    review['economic_link_graph']['paths'] = [path]
                    result = self.assess(review, doc, discovery, token_type=token_type)
                    self.assertTrue(result['issues'])
                    self.assertFalse(result['checks']['no_proven_prohibited_economic_link'])

    def test_material_percentage_cannot_be_overridden_by_false_flag(self):
        doc, discovery, review = reviewed_case()
        review['economic_link_graph']['paths'] = [{
            'status': 'UNRESOLVED', 'narrative': 'N2', 'active': True, 'spot_relevance': True,
            'confirmed_narrative': True, 'revenue_percentage': '10', 'material': False,
            'earmarked_token_support': False, 'unresolved_material': False,
            'reason': 'economic recipient not yet established', 'evidence': review['evidence']}]
        self.assertTrue(self.assess(review, doc, discovery)['issues'])

    def test_payment_labels_share_materiality_and_missing_context_cannot_prove_haram(self):
        from services.sharia_rules.v193 import NODES
        doc, discovery, review = reviewed_case()
        doc = type(doc)(**{**doc.__dict__, 'text': 'Our protocol collects lending interest from current borrowers and distributes the full resulting income directly to ordinary spot token holders.'})
        evidence = [{'url': doc.url, 'quote': doc.text, 'content_sha256': doc.content_sha256}]
        review['evidence'] = evidence
        review['official_source_discovery']['targets']['OFFICIAL_DOCS']['evidence'] = evidence
        path = {'status': 'PROVEN', 'narrative': 'N2', 'active': True,
                'spot_relevance': True, 'confirmed_narrative': True,
                'revenue_percentage': '10', 'material': False,
                'earmarked_token_support': False, 'unresolved_material': False,
                'nodes': {node: evidence for node in NODES}, 'proof': evidence}
        review['economic_link_graph']['paths'] = [path]
        for token_type in ('PAYMENT', 'PAYMENT_CURRENCY', 'UTILITY'):
            result = self.assess(review, doc, discovery, token_type=token_type)
            self.assertEqual(len(result['proven_paths']), 1)
        hits, clean = scan_keywords(CONTROLLER, [doc])
        self.assertTrue(hits)
        result = assess(review, documents=[doc], hits=hits + clean, token_type='UTILITY',
                        discovery=discovery, controller_sha256=SHA)
        self.assertFalse(result['checks']['material_keyword_hits_context_classified'])
        self.assertTrue(result['issues'])

    def test_underlying_asset_and_equity_cannot_skip_their_frameworks(self):
        for token_type in ('TOKENIZED_ASSET', 'EQUITY_SECURITY', 'NFT'):
            doc, discovery, review = reviewed_case()
            self.assertFalse(self.assess(review, doc, discovery, token_type=token_type)['checks'][
                'applicable_special_frameworks_completed'])


if __name__ == '__main__':
    unittest.main()
