from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _harness

from services.common import envelope
from services.common.sharia_v19 import (
    ResultValidationError, V19_CONTROLLER_FILENAME, validate_result,
)
from services.execution_sidecar.package_mode import enforce_sharia_gate_mode
from services.sharia_screener.queue_store import QueueStore
from services.sharia_screener.runner import ScreeningUnavailable, enforce_live_screening_policy
from services.universe_service.sharia_filter import ShariaFilter


ROOT = Path(__file__).resolve().parents[1]
CONTROLLER = ROOT / 'shared/sharia' / V19_CONTROLLER_FILENAME


def _copy_controller(directory: Path) -> None:
    shutil.copyfile(CONTROLLER, directory / V19_CONTROLLER_FILENAME)


class ProjectionAndBridgeRepairTests(unittest.TestCase):
    def test_missing_controller_and_unsigned_green_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            status = root / 'sharia_status.json'
            status.write_text(json.dumps(_harness.v19_status([('ETH', 'GREEN')])))
            with self.assertRaisesRegex(ValueError, 'missing or invalid'):
                ShariaFilter(status)
            _copy_controller(root)
            self.assertFalse(ShariaFilter(status).decision('ETH').allowed)

    def test_bridge_revalidates_and_drops_plaintext_rows(self):
        from services.sharia_screener import bridge
        with tempfile.TemporaryDirectory() as td:
            sharia = Path(td) / 'sharia'; sharia.mkdir()
            _copy_controller(sharia)
            status = sharia / 'sharia_status.json'
            status.write_text(json.dumps(_harness.v19_status([('ADA', 'GREEN')])))
            reports, results = sharia / 'reports', sharia / 'results'
            with mock.patch.multiple(
                    bridge, SHARIA_FILE=status,
                    SHARIA_CONTROLLER_FILE=sharia / V19_CONTROLLER_FILENAME,
                    SHARIA_REPORTS_DIR=reports, SHARIA_RESULTS_DIR=results,
                    LEGACY_HALAL_FILE=sharia / 'halal_coins.json',
                    audit=mock.Mock()):
                out = bridge.write_screening_outcome(
                    'repair-invalid', 'ETH', 'ETH/USDT',
                    {'ticker': 'ETH', 'final_code': 'GREEN'}, validated=True)
                self.assertFalse(out['payload']['validated'])
                self.assertEqual(out['payload']['final_code'], 'NO_TRADE_INFO')
                raw = json.loads(status.read_text())
                self.assertNotIn('ADA', {r['symbol'] for r in raw['records']})
                self.assertFalse(ShariaFilter(status).decision('ETH').allowed)

                good = bridge.write_screening_outcome(
                    'repair-valid', 'ETH', 'ETH/USDT',
                    _harness.green_report('ETH'), validated=True)
                self.assertTrue(good['payload']['validated'])
                self.assertTrue(ShariaFilter(status).decision('ETH').allowed)

    def test_request_or_result_hmac_holder_cannot_forge_result_attestation(self):
        from services.execution_sidecar import order_manager as om
        with tempfile.TemporaryDirectory() as td:
            results = Path(td) / 'results'; results.mkdir()
            forged_payload = {
                'request_id': 'signal-forged', 'base': 'ETH', 'pair': 'ETH/USDT',
                'final_code': 'GREEN', 'validated': True,
            }
            forged = envelope.sign_envelope(
                producer='sharia-screener', purpose=envelope.BUS_SHARIA_RESULT,
                payload=forged_payload, ttl_seconds=300)
            (results / 'result_signal-forged.json').write_text(json.dumps(forged))
            manager = object.__new__(om.OrderManager)
            with mock.patch.object(om, 'SHARIA_RESULTS_DIR', results):
                state, detail = manager._fresh_screening_result('signal-forged', 'ETH')
            self.assertEqual(state, 'rejected')
            self.assertIn('unattested', detail)


class SemanticAndModeRepairTests(unittest.TestCase):
    def test_vacuous_green_and_unresolved_escalation_are_rejected(self):
        report = _harness.green_report('ETH')
        report['green_proof_card'] = {f'invented_{i}': True for i in range(20)}
        report['sources_opened'] = ['not-a-url']
        report['shariah_screener_check'] = {}
        report['whitepaper_parsing'] = {}
        report['keyword_scan_results'] = {}
        with self.assertRaises(ResultValidationError):
            validate_result(report, expected_base='ETH')

        report = _harness.green_report('ETH')
        report['shariah_screener_check'] = {
            name: {} for name in report['shariah_screener_check']}
        with self.assertRaises(ResultValidationError):
            validate_result(report, expected_base='ETH')

        report = _harness.green_report('ETH')
        category = next(iter(report['keyword_scan_results']))
        report['keyword_scan_results'][category] = {'hits': 1, 'quotes': [{}]}
        with self.assertRaises(ResultValidationError):
            validate_result(report, expected_base='ETH')

        report = _harness.green_report('ETH')
        report['human_escalation_required'] = True
        report['contradictions_found'] = [
            {'material': True, 'resolved': False, 'detail': 'credible split'}]
        report['contradiction_resolution'] = 'unresolved material contradiction'
        with self.assertRaises(ResultValidationError):
            validate_result(report, expected_base='ETH')

    def test_self_declared_tier1_without_provider_url_evidence_is_rejected(self):
        from services.sharia_screener.runner import ScreeningRunner
        report = _harness.green_report('ETH')
        report['sources_opened'] = [{
            'url': 'https://evil.example/fake-whitepaper',
            'tier': 'TIER_1_OFFICIAL', 'opened': True, 'identity_match': True,
            'quote': 'This self declared source claims useful network transaction services.',
        }]
        report['tool_evidence'] = ScreeningRunner._extract_tool_evidence({
            'output': [{
                'type': 'web_search_call', 'id': 'ws_no_sources',
                'status': 'completed',
                'action': {'type': 'search', 'sources': []},
            }],
        })
        with self.assertRaisesRegex(ResultValidationError, 'citation/open-page evidence'):
            validate_result(report, expected_base='ETH')

    def test_runner_overwrites_model_provenance_with_provider_citation(self):
        from services.sharia_screener import runner as runner_mod
        with tempfile.TemporaryDirectory() as td:
            mode = Path(td) / 'RELEASE_MODE'
            mode.write_text('testnet', encoding='utf-8')
            model_report = _harness.green_report('ETH')
            model_report['tool_evidence'] = {
                'provider': 'model-self-declared',
                'completed_web_search_calls': [], 'url_citations': [],
            }
            output_text = json.dumps(model_report)
            response_data = {
                'output': [{
                    'type': 'web_search_call', 'id': 'ws_provider_1',
                    'status': 'completed', 'action': {
                        'type': 'search', 'sources': [{
                            'type': 'url',
                            'url': 'https://example.org/whitepaper/',
                        }],
                    },
                }, {
                    'type': 'message', 'content': [{
                        'type': 'output_text', 'text': output_text,
                        'annotations': [{
                            'type': 'url_citation',
                            'url': 'https://EXAMPLE.org/whitepaper#utility',
                            'title': 'Official whitepaper',
                            'start_index': 0, 'end_index': len(output_text),
                        }],
                    }],
                }],
                'usage': {'input_tokens': 10, 'output_tokens': 20},
            }
            fake_response = mock.Mock(status_code=200, text='')
            fake_response.json.return_value = response_data
            env = {
                'SHARIA_OPENAI_BASE': 'https://api.openai.com/v1',
                'SHARIA_ALLOWED_OPENAI_HOSTS': 'api.openai.com',
                'SHARIA_MODEL': 'offline-fixture-model',
            }
            with mock.patch.dict('os.environ', env), \
                    mock.patch.object(runner_mod, 'PACKAGE_MODE_FILE', mode), \
                    mock.patch.object(runner_mod.requests, 'post',
                                      return_value=fake_response) as post:
                runner = runner_mod.ScreeningRunner(b'controller')
                runner.api_key = 'offline-fixture-not-a-key'
                report, meta = runner.run('ETH', 'ETH/USDT')
            self.assertEqual(report['tool_evidence']['provider'], 'openai-responses')
            self.assertEqual(meta['completed_web_search_calls'], 1)
            self.assertEqual(meta['url_citations'], 1)
            self.assertEqual(validate_result(report, expected_base='ETH'), report)
            request_body = post.call_args.kwargs['json']
            self.assertEqual(request_body['include'], ['web_search_call.action.sources'])
            self.assertFalse(post.call_args.kwargs['allow_redirects'])

    def test_live_package_rejects_cached_gate(self):
        with self.assertRaisesRegex(SystemExit, 'requires SHARIA_SIGNAL_GATE_MODE=fresh'):
            enforce_sharia_gate_mode('live', 'cached')
        self.assertEqual(enforce_sharia_gate_mode('live', 'fresh'), 'fresh')
        self.assertEqual(enforce_sharia_gate_mode('testnet', 'cached'), 'cached')

    def test_direct_order_manager_construction_cannot_enable_cached_live_gate(self):
        from services.execution_sidecar import order_manager as om
        with mock.patch.object(om, 'load_package_mode', return_value='live'), \
                mock.patch.dict('os.environ', {'SHARIA_SIGNAL_GATE_MODE': 'cached'}):
            with self.assertRaisesRegex(
                    SystemExit, 'requires SHARIA_SIGNAL_GATE_MODE=fresh'):
                om.OrderManager(None, None, None, None, None, {})

    def test_live_host_and_model_are_immutable_policy_not_env_authority(self):
        from services.sharia_screener import runner as runner_mod
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            mode = root / 'RELEASE_MODE'; mode.write_text('live')
            policy = root / 'VALIDATION_STATUS.json'
            policy.write_text(json.dumps({'sharia_live_policy': {
                'approved': True, 'base_url': 'https://api.openai.com/v1',
                'model': 'approved-model-exact'}}))
            good = {
                'SHARIA_OPENAI_BASE': 'https://api.openai.com/v1',
                'SHARIA_MODEL': 'approved-model-exact',
                'SHARIA_ALLOWED_OPENAI_HOSTS': 'api.openai.com',
            }
            with mock.patch.dict('os.environ', good):
                enforce_live_screening_policy(
                    package_mode_path=mode, policy_path=policy, execution_mode='live')
                with mock.patch.object(runner_mod, 'PACKAGE_MODE_FILE', mode), \
                        mock.patch.object(runner_mod, 'LIVE_POLICY_FILE', policy), \
                        mock.patch.dict('os.environ', {'EXECUTION_MODE': 'live'}):
                    self.assertEqual(
                        runner_mod.ScreeningRunner(b'controller').model,
                        'approved-model-exact')
                with mock.patch.dict('os.environ', {'SHARIA_MODEL': 'substituted-model'}):
                    with self.assertRaises(ScreeningUnavailable):
                        enforce_live_screening_policy(
                            package_mode_path=mode, policy_path=policy,
                            execution_mode='live')
                with mock.patch.dict('os.environ', {
                        'SHARIA_ALLOWED_OPENAI_HOSTS': 'api.openai.com,evil.example'}):
                    with self.assertRaises(ScreeningUnavailable):
                        enforce_live_screening_policy(
                            package_mode_path=mode, policy_path=policy,
                            execution_mode='live')
            mode.write_text('testnet')
            with mock.patch.dict('os.environ', {
                    'SHARIA_OPENAI_BASE': 'https://sandbox.example/v1',
                    'SHARIA_MODEL': 'simulation-model',
                    'SHARIA_ALLOWED_OPENAI_HOSTS': 'sandbox.example'}):
                enforce_live_screening_policy(
                    package_mode_path=mode, policy_path=policy,
                    execution_mode='testnet')

    def test_live_package_simulation_cannot_redirect_screening_credentials(self):
        from services.sharia_screener import runner as runner_mod
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            mode = root / 'RELEASE_MODE'
            mode.write_text('live', encoding='utf-8')
            policy = root / 'VALIDATION_STATUS.json'

            official = {
                'EXECUTION_MODE': 'simulation',
                'SHARIA_OPENAI_BASE': 'https://api.openai.com/v1',
                'SHARIA_ALLOWED_OPENAI_HOSTS': 'api.openai.com',
                'SHARIA_MODEL': 'simulation-model',
            }
            with mock.patch.object(runner_mod, 'PACKAGE_MODE_FILE', mode), \
                    mock.patch.object(runner_mod, 'LIVE_POLICY_FILE', policy), \
                    mock.patch.dict('os.environ', official, clear=True):
                instance = runner_mod.ScreeningRunner(b'controller')
                self.assertEqual(instance.base_url, 'https://api.openai.com/v1')

            redirected = dict(official)
            redirected.update({
                'SHARIA_OPENAI_BASE': 'https://evil.example/v1',
                'SHARIA_ALLOWED_OPENAI_HOSTS': 'evil.example',
            })
            with mock.patch.object(runner_mod, 'PACKAGE_MODE_FILE', mode), \
                    mock.patch.object(runner_mod, 'LIVE_POLICY_FILE', policy), \
                    mock.patch.dict('os.environ', redirected, clear=True):
                with self.assertRaisesRegex(
                        ScreeningUnavailable, 'approved/default endpoint'):
                    runner_mod.ScreeningRunner(b'controller')

            widened = dict(official)
            widened['SHARIA_ALLOWED_OPENAI_HOSTS'] = 'api.openai.com,evil.example'
            with mock.patch.object(runner_mod, 'PACKAGE_MODE_FILE', mode), \
                    mock.patch.object(runner_mod, 'LIVE_POLICY_FILE', policy), \
                    mock.patch.dict('os.environ', widened, clear=True):
                with self.assertRaisesRegex(
                        ScreeningUnavailable, 'single-host endpoint'):
                    runner_mod.ScreeningRunner(b'controller')

            policy.write_text(json.dumps({'sharia_live_policy': {
                'approved': False,
                'base_url': 'https://candidate.example/v1',
                'model': 'candidate-model',
            }}), encoding='utf-8')
            candidate = dict(official)
            candidate.update({
                'SHARIA_OPENAI_BASE': 'https://candidate.example/v1',
                'SHARIA_ALLOWED_OPENAI_HOSTS': 'candidate.example',
            })
            with mock.patch.object(runner_mod, 'PACKAGE_MODE_FILE', mode), \
                    mock.patch.object(runner_mod, 'LIVE_POLICY_FILE', policy), \
                    mock.patch.dict('os.environ', candidate, clear=True):
                with self.assertRaisesRegex(
                        ScreeningUnavailable, 'approved/default endpoint'):
                    runner_mod.ScreeningRunner(b'controller')

            policy.write_text(json.dumps({'sharia_live_policy': {
                'approved': True,
                'base_url': 'https://approved.example/v1',
                'model': 'production-approved-model',
            }}), encoding='utf-8')
            approved_host = dict(official)
            approved_host.update({
                'SHARIA_OPENAI_BASE': 'https://approved.example/v1',
                'SHARIA_ALLOWED_OPENAI_HOSTS': 'approved.example',
                'SHARIA_MODEL': 'simulation-only-model',
            })
            with mock.patch.object(runner_mod, 'PACKAGE_MODE_FILE', mode), \
                    mock.patch.object(runner_mod, 'LIVE_POLICY_FILE', policy), \
                    mock.patch.dict('os.environ', approved_host, clear=True):
                instance = runner_mod.ScreeningRunner(b'controller')
                self.assertEqual(instance.base_url, 'https://approved.example/v1')
                self.assertEqual(instance.model, 'simulation-only-model')

    def test_screening_policy_obeys_package_and_execution_modes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            mode = root / 'RELEASE_MODE'
            absent_policy = root / 'VALIDATION_STATUS.json'

            mode.write_text('live')
            enforce_live_screening_policy(
                package_mode_path=mode, policy_path=absent_policy,
                execution_mode='simulation')
            with self.assertRaisesRegex(
                    ScreeningUnavailable, 'policy is absent'):
                enforce_live_screening_policy(
                    package_mode_path=mode, policy_path=absent_policy,
                    execution_mode='live')
            with self.assertRaisesRegex(
                    ScreeningUnavailable, 'does not permit'):
                enforce_live_screening_policy(
                    package_mode_path=mode, policy_path=absent_policy,
                    execution_mode='testnet')

            mode.write_text('testnet')
            for execution in ('simulation', 'testnet'):
                enforce_live_screening_policy(
                    package_mode_path=mode, policy_path=absent_policy,
                    execution_mode=execution)
            with self.assertRaisesRegex(
                    ScreeningUnavailable, 'does not permit'):
                enforce_live_screening_policy(
                    package_mode_path=mode, policy_path=absent_policy,
                    execution_mode='live')


class AutonomousDiscoveryAndRetryRepairTests(unittest.TestCase):
    def test_idle_discovery_seeds_from_binance_before_sharia(self):
        from services.common import paths as paths_mod
        from services.sharia_screener import service as service_mod
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            status = root / 'sharia_status.json'
            _copy_controller(root)
            status.write_text(json.dumps(_harness.v19_status([('BNB', 'NO_TRADE_INFO')])))
            universe = root / 'current_pairlist.json'
            universe.write_text(json.dumps({'pairs': []}))
            svc = object.__new__(service_mod.ShariaScreenerService)
            svc.idle_enabled = True; svc._next_idle_at = 0.0; svc.idle_cycle_seconds = 300
            svc.queue = QueueStore(root / 'queue.sqlite')
            svc._spot_usdt_bases = lambda: {'BNB': {}, 'ETH': {}, 'ADA': {}}
            with mock.patch.object(paths_mod, 'SHARIA_FILE', status), \
                    mock.patch.object(service_mod, 'UNIVERSE_CURRENT', universe), \
                    mock.patch.object(service_mod, 'audit', mock.Mock()):
                svc.idle_enqueue()
            queued = {row['base'] for row in svc.queue.recent(10)}
            self.assertEqual(queued, {'ETH', 'ADA'})

    def test_idle_failure_requeues_same_request_with_bounded_backoff(self):
        with tempfile.TemporaryDirectory() as td:
            queue = QueueStore(Path(td) / 'queue.sqlite')
            self.assertTrue(queue.enqueue('idle-ETH-today', 'ETH', 'ETH/USDT', 'idle', 'test'))
            queue.mark_running('idle-ETH-today')
            self.assertTrue(queue.mark_failed(
                'idle-ETH-today', 'network', retry_base_seconds=5,
                retry_max_seconds=8, max_attempts=2))
            pending = queue.recent(1)[0]
            self.assertEqual(pending['status'], 'QUEUED')
            delay = (datetime.fromisoformat(pending['next_attempt_at']) -
                     datetime.now(timezone.utc)).total_seconds()
            self.assertGreater(delay, 0)
            self.assertLessEqual(delay, 8)
            con = sqlite3.connect(queue.db_path)
            try:
                con.execute("UPDATE screening_requests SET next_attempt_at=? WHERE request_id=?",
                            ('2000-01-01T00:00:00+00:00', 'idle-ETH-today'))
                con.commit()
            finally:
                con.close()
            self.assertEqual(queue.next_request()['request_id'], 'idle-ETH-today')
            queue.mark_running('idle-ETH-today')
            self.assertFalse(queue.mark_failed(
                'idle-ETH-today', 'network again', retry_base_seconds=5,
                retry_max_seconds=8, max_attempts=2))
            self.assertEqual(queue.recent(1)[0]['status'], 'FAILED')


if __name__ == '__main__':
    unittest.main(verbosity=2)
