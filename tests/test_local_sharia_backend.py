"""Regression tests for the self-hosted Sharia screening backend.

Every test here targets a defect that was actually reached in review, and is
written so that reverting the fix makes it fail. Findings are labelled with
the identifiers from the CODEX independent review of feature head 4ca658f.

These run under the normal release gate and return non-zero on failure. An
earlier pair of ad-hoc scripts printed results and always exited 0, so CI
would have reported success while an expectation failed.
"""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.common.evidence_providers import (
    ACTIVE_PROVIDERS,
    is_registered,
    is_selectable_for_new_result,
    required_record_keys,
)
from services.common.sharia_v19 import (
    SCREENER_HOSTS,
    SCREENER_SITES,
    ResultValidationError,
    _provider_tool_evidence_urls,
    load_controller,
    validate_local_evidence_files,
    validate_result,
)
from services.sharia_retriever.retriever import (
    MAX_REDIRECTS,
    FetchResult,
    RetrievalBlocked,
    Retriever,
    assert_fetchable,
    classify_tier,
    html_to_text,
    pdf_to_text,
)
from services.sharia_retriever.store import EvidenceStore, EvidenceStoreError
from services.sharia_rules.engine import (
    Disposition,
    EvidenceClaim,
    RetrievedDocument,
    evaluate,
    extract_quote,
    is_negated,
)
from services.sharia_screener.approval import (
    SCOPE_CONFIRMATION,
    OwnerDecisionError,
    apply_owner_decision,
)
from services.sharia_screener.local_runner import LocalScreeningRunner
from services.sharia_screener.source_registry import (
    SourceRegistry,
    SourceRegistryError,
)

CONTROLLER_PATH = (ROOT / 'shared/sharia'
                   / 'HALAL_CRYPTO_SPOT_SCREENING_V19_1_PRODUCTION.json')
_RAW, CONTROLLER = load_controller(CONTROLLER_PATH)
ALL_HALAL = {name: 'halal' for name in SCREENER_SITES}


def _doc(text, tier='TIER_1_OFFICIAL', status=200, identity=True,
         url='https://official.example/docs', digest='b' * 64):
    return RetrievedDocument(
        url=url, tier=tier, text=text,
        content_sha256=digest, retrieved_utc='2026-08-09T00:00:00Z',
        http_status=status, identity_match=identity)


def _claim(doc, value, quote):
    return EvidenceClaim(
        value=value, quote=quote, url=doc.url,
        content_sha256=doc.content_sha256)


def _evaluate(text, **over):
    utility_quote = 'settles payments and pays network transaction fees'
    token_quote = 'The token is classified as a payment currency.'
    revenue_quote = 'Project revenue consists solely of transaction fees for network use.'
    official = _doc(
        f'{text} {token_quote} The token {utility_quote} for every transfer. '
        f'{revenue_quote}')
    screener_docs = {}
    screener_evidence = {}
    for index, name in enumerate(sorted(SCREENER_SITES), start=1):
        verdict_quote = f'{name} screening result is halal for this asset.'
        host = min(SCREENER_HOSTS[name])
        doc = _doc(
            verdict_quote, tier='TIER_2_PRIMARY_MARKET', identity=False,
            url=f'https://{host}/result', digest=f'{index:064x}')
        screener_docs[name] = doc
        screener_evidence[name] = _claim(doc, 'halal', verdict_quote)
    args = {
        'documents': [official, *screener_docs.values()],
        'screener_results': ALL_HALAL, 'identity_confirmed': True,
        'token_type': 'PAYMENT', 'utility_quote': utility_quote,
        'revenue_clean': True, 'contradictions': [],
        'expected_screeners': SCREENER_SITES,
        'fact_evidence': {
            'token_type': _claim(official, 'PAYMENT', token_quote),
            'utility': _claim(official, 'real utility', utility_quote),
            'revenue': _claim(official, 'clean', revenue_quote),
        },
        'screener_evidence': screener_evidence,
    }
    args.update(over)
    return evaluate(CONTROLLER, **args)


class ProviderRegistryTests(unittest.TestCase):
    """B1 — the registry must not invent requirements on the legacy provider."""

    def test_legacy_provider_requires_no_extra_record_keys(self):
        # Requiring a top-level 'url' here rejected the real runner's output,
        # which emits source_urls and nests the URL under 'action'.
        self.assertEqual(required_record_keys('openai-responses'), frozenset())

    def test_historical_runner_evidence_shape_remains_verifiable(self):
        evidence = {
            'provider': 'openai-responses',
            'completed_web_search_calls': [{
                'id': 'ws_historical', 'status': 'completed',
                'action_type': 'open_page',
                'source_urls': ['https://official.example/docs'],
            }],
            'url_citations': [],
        }
        _all_urls, evidentiary = _provider_tool_evidence_urls(
            {'tool_evidence': evidence})
        self.assertIn('https://official.example/docs', evidentiary)

    def test_unregistered_provider_is_rejected(self):
        self.assertFalse(is_registered('totally-made-up'))
        with self.assertRaises(ResultValidationError):
            _provider_tool_evidence_urls({'tool_evidence': {
                'provider': 'totally-made-up',
                'completed_web_search_calls': [{
                    'id': 'x', 'status': 'completed', 'action_type': 'open_page',
                    'source_urls': ['https://official.example/docs']}],
                'url_citations': []}})

    def test_new_results_cannot_select_the_weaker_legacy_schema(self):
        # C2 — historical records stay verifiable, but a freshly produced
        # report must not pick the schema without content-digest binding.
        self.assertEqual(ACTIVE_PROVIDERS, frozenset({'local-oracle-v1'}))
        self.assertTrue(is_selectable_for_new_result('local-oracle-v1'))
        self.assertFalse(is_selectable_for_new_result('openai-responses'))

    def test_local_provider_requires_a_content_digest(self):
        base = {'id': 'ret_1', 'status': 'completed', 'action_type': 'open_page',
                'source_urls': ['https://official.example/docs'],
                'url': 'https://official.example/docs',
                'retrieved_utc': '2026-08-09T00:00:00Z', 'http_status': 200,
                'content_sha256': 'a' * 64,
                'content_path': f'sha256/aa/{"a" * 64}.bin',
                'source_tier': 'TIER_1_OFFICIAL'}
        for drop in ('content_sha256', 'content_path', 'http_status', 'source_tier'):
            weakened = {k: v for k, v in base.items() if k != drop}
            with self.subTest(missing=drop), self.assertRaises(
                    ResultValidationError):
                _provider_tool_evidence_urls({'tool_evidence': {
                    'provider': 'local-oracle-v1',
                    'completed_web_search_calls': [weakened],
                    'url_citations': []}})

    def test_local_citation_cannot_add_an_unhashed_evidentiary_url(self):
        record = {
            'id': 'ret_1', 'status': 'completed', 'action_type': 'open_page',
            'source_urls': ['https://official.example/docs'],
            'url': 'https://official.example/docs',
            'retrieved_utc': '2026-08-09T00:00:00Z', 'http_status': 200,
            'content_sha256': 'a' * 64,
            'content_path': f'sha256/aa/{"a" * 64}.bin',
            'source_tier': 'TIER_1_OFFICIAL',
        }
        with self.assertRaises(ResultValidationError):
            _provider_tool_evidence_urls({'tool_evidence': {
                'provider': 'local-oracle-v1',
                'completed_web_search_calls': [record],
                'url_citations': [{
                    'url': 'https://unhashed.example/claim', 'title': 'claim',
                    'start_index': 0, 'end_index': 5,
                }]}})

    def test_local_record_url_must_match_its_hashed_source_url(self):
        record = {
            'id': 'ret_1', 'status': 'completed', 'action_type': 'open_page',
            'source_urls': ['https://different.example/docs'],
            'url': 'https://official.example/docs',
            'retrieved_utc': '2026-08-09T00:00:00Z', 'http_status': 200,
            'content_sha256': 'a' * 64,
            'content_path': f'sha256/aa/{"a" * 64}.bin',
            'source_tier': 'TIER_1_OFFICIAL',
        }
        with self.assertRaises(ResultValidationError):
            _provider_tool_evidence_urls({'tool_evidence': {
                'provider': 'local-oracle-v1',
                'completed_web_search_calls': [record],
                'url_citations': []}})


class EvidenceStoreTests(unittest.TestCase):
    def test_exact_bytes_are_content_addressed_and_reverified(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EvidenceStore(directory)
            digest, relative = store.put(b'exact response bytes')
            self.assertTrue(store.verify(digest, relative))
            self.assertEqual(store.read(digest, relative), b'exact response bytes')

            target = Path(directory) / relative
            target.write_bytes(b'tampered')
            self.assertFalse(store.verify(digest, relative))
            with self.assertRaises(EvidenceStoreError):
                store.read(digest, relative)

    def test_evidence_path_cannot_escape_store(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EvidenceStore(directory)
            with self.assertRaises(EvidenceStoreError):
                store.read('a' * 64, '../../outside.bin')

    def test_report_validation_rehashes_stored_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EvidenceStore(directory)
            digest, relative = store.put(b'official source bytes')
            report = {'tool_evidence': {
                'provider': 'local-oracle-v1',
                'completed_web_search_calls': [{
                    'id': 'ret_1', 'status': 'completed',
                    'action_type': 'open_page',
                    'source_urls': ['https://official.example/docs'],
                    'url': 'https://official.example/docs',
                    'retrieved_utc': '2026-08-09T00:00:00Z',
                    'http_status': 200, 'content_sha256': digest,
                    'content_path': relative,
                    'source_tier': 'TIER_1_OFFICIAL',
                }], 'url_citations': []}}
            validate_local_evidence_files(report, directory)
            (Path(directory) / relative).write_bytes(b'changed')
            with self.assertRaises(ResultValidationError):
                validate_local_evidence_files(report, directory)


class LocalRunnerIntegrationTests(unittest.TestCase):
    @staticmethod
    def _registry_payload():
        utility = 'The token settles payments and pays network transaction fees.'
        token = 'The token is classified as a payment currency.'
        revenue = 'Project revenue consists solely of transaction fees for network use.'
        official_url = 'https://official.example/docs'
        sources = [{'url': official_url, 'identity_match': True}]
        claims = {
            'token_type': {'value': 'PAYMENT', 'quote': token, 'url': official_url},
            'utility': {'value': 'real utility', 'quote': utility, 'url': official_url},
            'revenue': {'value': 'clean', 'quote': revenue, 'url': official_url},
        }
        screeners = {}
        bodies = {official_url: f'{token} {utility} {revenue}'}
        for name in sorted(SCREENER_SITES):
            host = min(SCREENER_HOSTS[name])
            url = f'https://{host}/asset/example'
            quote = f'{name} screening result is halal for this asset.'
            sources.append({'url': url, 'identity_match': False})
            screeners[name] = {'value': 'halal', 'quote': quote, 'url': url}
            bodies[url] = quote
        return ({'schema_version': 1, 'assets': {'XYZ': {
            'official_hosts': ['official.example'], 'sources': sources,
            'claims': claims, 'screeners': screeners,
        }}}, bodies)

    def test_complete_local_case_produces_review_not_trade_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry_payload, bodies = self._registry_payload()
            registry = root / 'source_registry.json'
            registry.write_text(json.dumps(registry_payload), encoding='utf-8')
            store = EvidenceStore(root / 'evidence')

            class _Retriever:
                def fetch(self, url, *, official_hosts=None, identity_match=False):
                    body = bodies[url].encode()
                    digest, relative = store.put(body)
                    return FetchResult(
                        url=url, http_status=200, content_sha256=digest,
                        content_path=relative, text=bodies[url],
                        retrieved_utc='2026-08-09T00:00:00Z',
                        tier=('TIER_1_OFFICIAL' if identity_match
                              else 'TIER_3_SECONDARY'),
                        identity_match=identity_match)

            runner = LocalScreeningRunner(
                CONTROLLER, registry_path=registry,
                evidence_root=root / 'evidence', retriever=_Retriever())
            report, meta = runner.run('XYZ', 'XYZ/USDT')
            self.assertEqual(meta['backend'], 'local-oracle-v1')
            self.assertEqual(report['tool_evidence']['provider'], 'local-oracle-v1')
            self.assertEqual(
                report['local_review']['disposition'], Disposition.PROPOSE_GREEN)
            self.assertEqual(report['final_code'], 'NO_TRADE_INFO')
            self.assertTrue(report['human_escalation_required'])
            self.assertTrue(report['local_review']['promotable'])
            validate_local_evidence_files(report, root / 'evidence')

    def test_seven_haram_screener_results_can_never_propose_green(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry_payload, bodies = self._registry_payload()
            for name, claim in registry_payload['assets']['XYZ']['screeners'].items():
                quote = f'{name} screening result is haram for this asset.'
                claim['value'] = 'haram'
                claim['quote'] = quote
                bodies[claim['url']] = quote
            registry = root / 'source_registry.json'
            registry.write_text(json.dumps(registry_payload), encoding='utf-8')
            store = EvidenceStore(root / 'evidence')

            class _Retriever:
                def fetch(self, url, *, official_hosts=None, identity_match=False):
                    body = bodies[url].encode()
                    digest, relative = store.put(body)
                    return FetchResult(
                        url=url, http_status=200, content_sha256=digest,
                        content_path=relative, text=bodies[url],
                        retrieved_utc='2026-08-09T00:00:00Z',
                        tier=('TIER_1_OFFICIAL' if identity_match
                              else 'TIER_3_SECONDARY'),
                        identity_match=identity_match)

            report, _meta = LocalScreeningRunner(
                CONTROLLER, registry_path=registry,
                evidence_root=root / 'evidence', retriever=_Retriever()).run(
                    'XYZ', 'XYZ/USDT')
            self.assertNotEqual(
                report['local_review']['disposition'], Disposition.PROPOSE_GREEN)
            self.assertFalse(report['local_review']['promotable'])
            self.assertFalse(report['local_review']['green_checks'][
                'shariah_screener_check_completed'])

    def test_halal_value_bound_to_negative_quote_can_never_propose_green(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry_payload, bodies = self._registry_payload()
            for claim in registry_payload['assets']['XYZ']['screeners'].values():
                quote = 'This asset is not Shariah compliant for spot holding.'
                claim['value'] = 'halal'
                claim['quote'] = quote
                bodies[claim['url']] = quote
            registry = root / 'source_registry.json'
            registry.write_text(json.dumps(registry_payload), encoding='utf-8')
            store = EvidenceStore(root / 'evidence')

            class _Retriever:
                def fetch(self, url, *, official_hosts=None, identity_match=False):
                    body = bodies[url].encode()
                    digest, relative = store.put(body)
                    return FetchResult(
                        url=url, http_status=200, content_sha256=digest,
                        content_path=relative, text=bodies[url],
                        retrieved_utc='2026-08-09T00:00:00Z',
                        tier=('TIER_1_OFFICIAL' if identity_match
                              else 'TIER_3_SECONDARY'),
                        identity_match=identity_match)

            report, _meta = LocalScreeningRunner(
                CONTROLLER, registry_path=registry,
                evidence_root=root / 'evidence', retriever=_Retriever()).run(
                    'XYZ', 'XYZ/USDT')
            self.assertNotEqual(
                report['local_review']['disposition'], Disposition.PROPOSE_GREEN)
            self.assertFalse(report['local_review']['promotable'])
            self.assertFalse(report['local_review']['green_checks'][
                'shariah_screener_check_completed'])

    def test_owner_approval_is_bound_to_exact_report_and_evidence_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry_payload, bodies = self._registry_payload()
            registry = root / 'source_registry.json'
            registry.write_text(json.dumps(registry_payload), encoding='utf-8')
            store = EvidenceStore(root / 'evidence')

            class _Retriever:
                def fetch(self, url, *, official_hosts=None, identity_match=False):
                    body = bodies[url].encode()
                    digest, relative = store.put(body)
                    return FetchResult(
                        url=url, http_status=200, content_sha256=digest,
                        content_path=relative, text=bodies[url],
                        retrieved_utc='2026-08-09T00:00:00Z',
                        tier=('TIER_1_OFFICIAL' if identity_match
                              else 'TIER_3_SECONDARY'),
                        identity_match=identity_match)

            report, _meta = LocalScreeningRunner(
                CONTROLLER, registry_path=registry,
                evidence_root=root / 'evidence', retriever=_Retriever()).run(
                    'XYZ', 'XYZ/USDT')
            reports = root / 'reports'
            reports.mkdir()
            report_name = 'XYZ_manual-XYZ-test.json'
            report_path = reports / report_name
            report_path.write_text(
                json.dumps(report, sort_keys=True), encoding='utf-8')
            report_sha = hashlib.sha256(report_path.read_bytes()).hexdigest()
            payload = {
                'decision_id': 'a' * 32,
                'action': 'APPROVE',
                'base': 'XYZ',
                'pair': 'XYZ/USDT',
                'proposal_request_id': 'manual-XYZ-test',
                'report_file': report_name,
                'report_sha256': report_sha,
                'decided_at': '2026-08-09T00:00:00+00:00',
            }
            approved, request_id = apply_owner_decision(
                payload, reports_root=reports,
                evidence_root=root / 'evidence')
            self.assertEqual(request_id, 'decision-' + 'a' * 32)
            self.assertEqual(approved['final_code'], 'GREEN')
            self.assertFalse(approved['human_escalation_required'])
            self.assertEqual(
                approved['owner_approval']['proposal_report_sha256'], report_sha)
            validate_result(approved, expected_base='XYZ')

            payload['proposal_request_id'] = 'different-request'
            with self.assertRaisesRegex(
                    OwnerDecisionError, 'proposal_request_id'):
                apply_owner_decision(
                    payload, reports_root=reports,
                    evidence_root=root / 'evidence')
            payload['proposal_request_id'] = 'manual-XYZ-test'
            payload['report_sha256'] = 'b' * 64
            with self.assertRaises(OwnerDecisionError):
                apply_owner_decision(
                    payload, reports_root=reports,
                    evidence_root=root / 'evidence')

    def test_scope_disclaimer_needs_explicit_hash_bound_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry_payload, bodies = self._registry_payload()
            official = 'https://official.example/docs'
            bodies[official] += (
                ' No fixed or guaranteed return is offered to any participant.')
            registry = root / 'source_registry.json'
            registry.write_text(json.dumps(registry_payload), encoding='utf-8')
            store = EvidenceStore(root / 'evidence')

            class _Retriever:
                def fetch(self, url, *, official_hosts=None, identity_match=False):
                    body = bodies[url].encode()
                    digest, relative = store.put(body)
                    return FetchResult(
                        url=url, http_status=200, content_sha256=digest,
                        content_path=relative, text=bodies[url],
                        retrieved_utc='2026-08-09T00:00:00Z',
                        tier=('TIER_1_OFFICIAL' if identity_match
                              else 'TIER_3_SECONDARY'),
                        identity_match=identity_match)

            report, _meta = LocalScreeningRunner(
                CONTROLLER, registry_path=registry,
                evidence_root=root / 'evidence', retriever=_Retriever()).run(
                    'XYZ', 'XYZ/USDT')
            self.assertEqual(
                report['local_review']['disposition'], Disposition.ESCALATE)
            self.assertTrue(report['local_review']['scope_review_only'])
            self.assertTrue(report['local_review']['promotable'])
            reports = root / 'reports'
            reports.mkdir()
            report_path = reports / 'XYZ_scope-test.json'
            report_path.write_text(json.dumps(report, sort_keys=True), encoding='utf-8')
            payload = {
                'decision_id': 'c' * 32,
                'action': 'APPROVE', 'base': 'XYZ', 'pair': 'XYZ/USDT',
                'proposal_request_id': 'scope-test',
                'report_file': report_path.name,
                'report_sha256': hashlib.sha256(report_path.read_bytes()).hexdigest(),
                'decided_at': '2026-08-09T00:00:00+00:00',
            }
            with self.assertRaises(OwnerDecisionError):
                apply_owner_decision(
                    payload, reports_root=reports,
                    evidence_root=root / 'evidence')
            payload['scope_confirmation'] = SCOPE_CONFIRMATION
            approved, _request_id = apply_owner_decision(
                payload, reports_root=reports,
                evidence_root=root / 'evidence')
            self.assertEqual(approved['final_code'], 'GREEN')

    def test_missing_asset_fails_closed_without_external_api(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = root / 'source_registry.json'
            registry.write_text(
                json.dumps({'schema_version': 1, 'assets': {}}), encoding='utf-8')
            runner = LocalScreeningRunner(
                CONTROLLER, registry_path=registry,
                evidence_root=root / 'evidence')
            report, meta = runner.run('XYZ', 'XYZ/USDT')
            self.assertEqual(report['final_code'], 'NO_TRADE_INFO')
            self.assertEqual(meta['backend'], 'local-oracle-v1')
            self.assertNotIn('openai', json.dumps(report).lower())

    def test_screener_registry_identity_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            payload, _bodies = self._registry_payload()
            payload['assets']['XYZ']['screeners']['musaffa']['url'] = (
                'https://cryptoummah.com/asset/example')
            path = Path(directory) / 'registry.json'
            path.write_text(json.dumps(payload), encoding='utf-8')
            with self.assertRaises(SourceRegistryError):
                SourceRegistry(path).asset('XYZ')

    def test_overly_broad_official_host_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            payload, _bodies = self._registry_payload()
            payload['assets']['XYZ']['official_hosts'] = ['com']
            path = Path(directory) / 'registry.json'
            path.write_text(json.dumps(payload), encoding='utf-8')
            with self.assertRaisesRegex(SourceRegistryError, 'overly broad'):
                SourceRegistry(path).asset('XYZ')

    def test_duplicate_normalized_screener_name_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            payload, _bodies = self._registry_payload()
            original = payload['assets']['XYZ']['screeners']['halalscreener']
            payload['assets']['XYZ']['screeners']['halal-screener'] = dict(original)
            path = Path(directory) / 'registry.json'
            path.write_text(json.dumps(payload), encoding='utf-8')
            with self.assertRaisesRegex(SourceRegistryError, 'duplicate normalized'):
                SourceRegistry(path).asset('XYZ')

    def test_active_service_constructs_only_the_local_runner(self):
        source = (ROOT / 'services/sharia_screener/service.py').read_text(
            encoding='utf-8')
        self.assertIn('LocalScreeningRunner(', source)
        self.assertNotIn('from services.sharia_screener.runner import ScreeningRunner',
                         source)
        self.assertNotIn('SHARIA_OPENAI_', source)

    def test_transient_owner_decision_failure_remains_for_retry(self):
        from services.sharia_screener import service as service_mod

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inbox = root / 'inbox'
            processed = root / 'processed'
            inbox.mkdir()
            decision = inbox / 'decision_test.json'
            decision.write_text('{}', encoding='utf-8')
            service = object.__new__(service_mod.ShariaScreenerService)
            with mock.patch.object(service_mod, 'SHARIA_DECISION_INBOX', inbox), \
                    mock.patch.object(
                        service_mod, 'SHARIA_DECISION_PROCESSED', processed), \
                    mock.patch.object(
                        service_mod.envelope, 'read_verified_file',
                        return_value={'decision_id': 'a' * 32}), \
                    mock.patch.object(
                        service_mod, 'apply_owner_decision',
                        side_effect=OSError('disk temporarily unavailable')), \
                    mock.patch.object(service_mod, 'audit'):
                service.ingest_owner_decisions()
            self.assertTrue(decision.exists())
            self.assertFalse(any(processed.glob('*.json')))


class NegationTests(unittest.TestCase):
    """C4 — a disclaimer must not erase a real disclosure."""

    def test_later_genuine_disclosure_is_not_hidden_by_an_earlier_denial(self):
        finding = _evaluate(
            'No guaranteed return is offered in the legacy plan. '
            'The active plan promises a guaranteed return to every holder '
            'from treasury revenue for as long as tokens remain staked.')
        self.assertNotEqual(finding.disposition, Disposition.PROPOSE_GREEN)
        self.assertTrue([h for h in finding.hits if not h.negated])

    def test_double_negative_asserts_rather_than_denies(self):
        # "does not prohibit X" permits X.
        finding = _evaluate(
            'The protocol does not prohibit or prevent a guaranteed return '
            'to stakers and pays it from treasury revenue continuously.')
        self.assertNotEqual(finding.disposition, Disposition.PROPOSE_GREEN)
        self.assertTrue([h for h in finding.hits if not h.negated])

    def test_plain_disclaimer_is_still_recognised(self):
        text = 'No fixed or guaranteed return is offered to any participant.'
        self.assertTrue(is_negated(text, text.index('guaranteed return')))

    def test_polarity_reversing_verb_defeats_the_cue(self):
        text = 'The protocol does not prohibit a guaranteed return.'
        self.assertFalse(is_negated(text, text.index('guaranteed return')))

    def test_unrelated_negation_in_same_sentence_cannot_clear_disclosure(self):
        for text in (
                'No fee applies, but holders receive a guaranteed return from treasury revenue.',
                'The protocol does not charge users and provides a guaranteed return to holders.'):
            with self.subTest(text=text):
                self.assertNotEqual(_evaluate(text).disposition,
                                    Disposition.PROPOSE_GREEN)

    def test_unless_and_except_disclosures_cannot_clear_green(self):
        for text in (
                'No guaranteed return exists except for early depositors.',
                'No guaranteed return is promised unless the treasury exceeds its target.'):
            with self.subTest(text=text):
                self.assertNotEqual(_evaluate(text).disposition,
                                    Disposition.PROPOSE_GREEN)

    def test_even_plain_disclaimer_requires_owner_scope_review(self):
        finding = _evaluate(
            'No fixed or guaranteed return is offered to any participant.')
        self.assertEqual(finding.disposition, Disposition.ESCALATE)


class QuoteBindingTests(unittest.TestCase):
    """H1 — length must not be borrowed from unrelated prose."""

    def test_short_sentence_is_not_inflated_by_the_next_sentence(self):
        text = ('We offer lending. This following sentence discusses branding '
                'colours, community events, software documentation, user '
                'interfaces, and unrelated website navigation details only.')
        quote = extract_quote(text, text.index('lending'),
                              text.index('lending') + len('lending'))
        self.assertEqual(quote, 'We offer lending.')
        self.assertLess(len(quote.split()), 15)

    def test_short_adverse_lead_escalates_instead_of_being_dropped(self):
        finding = _evaluate('We offer lending.')
        self.assertEqual(finding.disposition, Disposition.ESCALATE)


class EvidenceDerivedFactTests(unittest.TestCase):
    """C1 — a caller boolean is not Sharia evidence."""

    def test_invalid_token_type_is_not_a_classification(self):
        finding = _evaluate('This project publishes a website.',
                            token_type='BANANA')
        self.assertNotEqual(finding.disposition, Disposition.PROPOSE_GREEN)
        self.assertFalse(finding.green_checks['token_type_classified'])

    def test_blank_screener_values_do_not_complete_the_check(self):
        finding = _evaluate('This project publishes a website.',
                            screener_results={n: '' for n in SCREENER_SITES})
        self.assertNotEqual(finding.disposition, Disposition.PROPOSE_GREEN)
        self.assertFalse(
            finding.green_checks['shariah_screener_check_completed'])

    def test_utility_quote_must_appear_in_a_retrieved_tier1_document(self):
        finding = _evaluate(
            'This project publishes a website.',
            documents=[_doc('This project publishes a website.')],
            utility_quote='a claim that appears in no retrieved document')
        self.assertNotEqual(finding.disposition, Disposition.PROPOSE_GREEN)
        self.assertFalse(finding.green_checks['real_utility_official_quote'])

    def test_bare_positive_facts_cannot_produce_a_green_proposal(self):
        finding = _evaluate(
            'This project publishes a website.',
            fact_evidence={}, screener_evidence={})
        self.assertNotEqual(finding.disposition, Disposition.PROPOSE_GREEN)
        self.assertFalse(finding.green_checks['token_type_classified'])
        self.assertFalse(finding.green_checks['revenue_clean_or_non_material'])
        self.assertFalse(
            finding.green_checks['shariah_screener_check_completed'])

    def test_revenue_boolean_must_be_bound_to_retrieved_evidence(self):
        finding = _evaluate(
            'This project publishes a website.',
            fact_evidence={'token_type': None, 'utility': None,
                           'revenue': None})
        self.assertNotEqual(finding.disposition, Disposition.PROPOSE_GREEN)
        self.assertFalse(finding.green_checks['revenue_clean_or_non_material'])

    def test_unattested_document_cannot_support_positive_claims(self):
        finding = _evaluate(
            'This project publishes a website.',
            documents=[_doc('apparently valid text', digest='not-a-digest')])
        self.assertEqual(finding.disposition, Disposition.AUTO_NO_TRADE_INFO)

    def test_screener_name_must_match_its_real_host(self):
        finding = _evaluate('This project publishes a website.')
        forged = dict(finding.green_checks)
        self.assertTrue(forged['shariah_screener_check_completed'])

        # Rebuild the normal inputs, then bind Musaffa's verdict to a different
        # screener host. The text and digest are valid, but the identity is not.
        utility_quote = 'settles payments and pays network transaction fees'
        token_quote = 'The token is classified as a payment currency.'
        revenue_quote = 'Project revenue consists solely of transaction fees for network use.'
        official = _doc(
            f'{token_quote} The token {utility_quote} for every transfer. '
            f'{revenue_quote}')
        docs = [official]
        claims = {}
        for index, name in enumerate(sorted(SCREENER_SITES), start=1):
            host = min(SCREENER_HOSTS[name])
            if name == 'musaffa':
                host = 'cryptoummah.com'
            quote = f'{name} screening result is halal for this asset.'
            doc = _doc(quote, tier='TIER_3_SECONDARY', identity=False,
                       url=f'https://{host}/result', digest=f'{index:064x}')
            docs.append(doc)
            claims[name] = _claim(doc, 'halal', quote)
        result = evaluate(
            CONTROLLER, documents=docs, screener_results=ALL_HALAL,
            identity_confirmed=True, token_type='PAYMENT',
            utility_quote=utility_quote, revenue_clean=True,
            contradictions=[], expected_screeners=SCREENER_SITES,
            fact_evidence={
                'token_type': _claim(official, 'PAYMENT', token_quote),
                'utility': _claim(official, 'real utility', utility_quote),
                'revenue': _claim(official, 'clean', revenue_quote),
            }, screener_evidence=claims)
        self.assertFalse(
            result.green_checks['shariah_screener_check_completed'])


class RetrieverSecurityTests(unittest.TestCase):
    """C3 — the fetcher must not become an SSRF primitive."""

    def test_non_public_destinations_are_refused(self):
        for url in ('https://127.0.0.1/',
                    'https://169.254.169.254/latest/meta-data/',
                    'https://10.0.0.5/', 'https://192.168.1.1/',
                    'https://[::1]/'):
            with self.subTest(url=url), self.assertRaises(RetrievalBlocked):
                assert_fetchable(url)

    def test_model_inference_hosts_are_refused(self):
        for url in ('https://api.openai.com/v1/responses',
                    'https://openrouter.ai/api',
                    'https://sub.api.anthropic.com/x'):
            with self.subTest(url=url), self.assertRaises(RetrievalBlocked):
                assert_fetchable(url)

    def test_plaintext_and_credentialed_urls_are_refused(self):
        for url in ('http://example.com/', 'https://user:pw@example.com/',
                    'https://example.com:8443/'):
            with self.subTest(url=url), self.assertRaises(RetrievalBlocked):
                assert_fetchable(url)

    def test_redirects_are_validated_before_the_next_request_is_made(self):
        # The blocked hop must never be requested. With allow_redirects=True
        # the library completed the chain first and the check came too late.
        requested = []

        class _Resp:
            def __init__(self, status, location=None):
                self.status_code = status
                self.headers = {'Location': location} if location else {}
                self.encoding = 'utf-8'
                self.history = []

            def close(self):
                pass

            def iter_content(self, _n):
                yield b'body'

        class _Session:
            def get(self, url, **kwargs):
                requested.append(url)
                self.last = kwargs
                if len(requested) == 1:
                    return _Resp(302, 'https://169.254.169.254/latest/')
                return _Resp(200)

        session = _Session()
        with mock.patch('services.sharia_retriever.retriever._addresses_for',
                        lambda h: [__import__('ipaddress').ip_address(
                            '169.254.169.254' if h == '169.254.169.254'
                            else '93.184.216.34')]):
            result = Retriever(session=session, verify_peer=False).fetch('https://example.com/')
        self.assertFalse(result.ok)
        self.assertNotIn('https://169.254.169.254/latest', requested)
        self.assertIs(session.last.get('allow_redirects'), False)

    def test_redirect_cap_is_enforced(self):
        self.assertEqual(MAX_REDIRECTS, 5)

    def test_cross_origin_public_redirect_cannot_launder_tier_one(self):
        requested = []

        class _Resp:
            status_code = 302
            encoding = 'utf-8'

            def __init__(self):
                self.headers = {
                    'Location': 'https://public-attacker.example/payload'}

            def close(self):
                pass

            def iter_content(self, _n):
                yield b''

        class _Session:
            def get(self, url, **_kwargs):
                requested.append(url)
                return _Resp()

        with mock.patch('services.sharia_retriever.retriever._addresses_for',
                        lambda _h: [__import__('ipaddress').ip_address(
                            '93.184.216.34')]):
            # verify_peer=False: the injected session has no real socket to
            # inspect. Peer verification is exercised separately.
            result = Retriever(session=_Session(), verify_peer=False).fetch(
                'https://official.example/docs',
                official_hosts={'official.example'}, identity_match=True)
        self.assertFalse(result.ok)
        self.assertEqual(requested, ['https://official.example/docs'])
        self.assertIn('cross-origin redirect', result.error)


class HtmlExtractionTests(unittest.TestCase):
    """H2 — void tags must not suppress the whole document."""

    def test_ordinary_head_with_meta_and_link_keeps_the_body(self):
        html = ("<html><head><meta charset='utf-8'><link rel='stylesheet'>"
                "<title>T</title></head><body><p>VISIBLE BODY</p></body></html>")
        self.assertEqual(html_to_text(html), 'VISIBLE BODY')

    def test_script_and_style_content_is_still_dropped(self):
        html = ('<html><body><style>a{color:red}</style>'
                '<p>Validator rewards are variable.</p>'
                '<script>var x=1;</script></body></html>')
        self.assertEqual(html_to_text(html), 'Validator rewards are variable.')

    def test_stray_end_tag_does_not_strand_the_extractor(self):
        html = '<html><body></script><p>STILL VISIBLE</p></body></html>'
        self.assertIn('STILL VISIBLE', html_to_text(html))


class PdfExtractionTests(unittest.TestCase):
    def test_pdf_text_is_bounded_and_normalized(self):
        pages = [mock.Mock(), mock.Mock()]
        pages[0].extract_text.return_value = 'Official token utility\nstatement.'
        pages[1].extract_text.return_value = 'Revenue comes from service fees.'
        reader = mock.Mock(is_encrypted=False, pages=pages)
        with mock.patch(
                'services.sharia_retriever.retriever.PdfReader',
                return_value=reader):
            self.assertEqual(
                pdf_to_text(b'%PDF-fixture'),
                'Official token utility statement. Revenue comes from service fees.')

    def test_encrypted_and_image_only_pdfs_fail_closed(self):
        encrypted = mock.Mock(is_encrypted=True, pages=[mock.Mock()])
        with mock.patch(
                'services.sharia_retriever.retriever.PdfReader',
                return_value=encrypted), self.assertRaises(RetrievalBlocked):
            pdf_to_text(b'%PDF-encrypted')
        blank_page = mock.Mock()
        blank_page.extract_text.return_value = ''
        blank = mock.Mock(is_encrypted=False, pages=[blank_page])
        with mock.patch(
                'services.sharia_retriever.retriever.PdfReader',
                return_value=blank), self.assertRaises(RetrievalBlocked):
            pdf_to_text(b'%PDF-image-only')


class TierClassificationTests(unittest.TestCase):
    def test_official_subdomain_is_tier_one(self):
        self.assertEqual(
            classify_tier('https://docs.web3.foundation/x', {'web3.foundation'}),
            'TIER_1_OFFICIAL')

    def test_suffix_spoofing_is_not_official(self):
        self.assertEqual(
            classify_tier('https://evil-solana.com/x', {'solana.com'}),
            'TIER_3_SECONDARY')


class EngineAuthorityTests(unittest.TestCase):
    """The engine must never be able to authorise a trade."""

    def test_no_disposition_is_a_runtime_tradeable_code(self):
        from services.common.sharia_v19 import TRADE_ELIGIBLE_CODES
        dispositions = {Disposition.AUTO_HARAM, Disposition.AUTO_NO_TRADE_INFO,
                        Disposition.PROPOSE_GREEN, Disposition.ESCALATE}
        self.assertFalse(dispositions & TRADE_ELIGIBLE_CODES)

    def test_clean_document_only_ever_proposes(self):
        finding = _evaluate(
            'The network is secured by proof of stake. Validator rewards are '
            'variable and derive from network inflation together with '
            'transaction fee revenue collected by the protocol.')
        self.assertEqual(finding.disposition, Disposition.PROPOSE_GREEN)
        self.assertNotIn(finding.disposition, {'GREEN', 'GREEN_AVOID_OPTIONAL'})

    def test_nothing_retrieved_fails_closed(self):
        self.assertEqual(_evaluate('', documents=[]).disposition,
                         Disposition.AUTO_NO_TRADE_INFO)

    def test_failed_fetch_fails_closed(self):
        finding = _evaluate('x', documents=[_doc('body', status=503)])
        self.assertEqual(finding.disposition, Disposition.AUTO_NO_TRADE_INFO)


if __name__ == '__main__':
    unittest.main()
