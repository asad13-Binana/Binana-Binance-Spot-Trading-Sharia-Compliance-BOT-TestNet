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
        report = _harness.green_report('ETH')
        report['sources_opened'] = [{
            'url': 'https://evil.example/fake-whitepaper',
            'tier': 'TIER_1_OFFICIAL', 'opened': True, 'identity_match': True,
            'quote': 'This self declared source claims useful network transaction services.',
        }]
        report['tool_evidence'] = {
            'provider': 'openai-responses',
            'completed_web_search_calls': [{
                'id': 'ws_no_sources', 'status': 'completed',
                'action_type': 'search', 'source_urls': [],
            }],
            'url_citations': [],
        }
        with self.assertRaisesRegex(ResultValidationError, 'citation/open-page evidence'):
            validate_result(report, expected_base='ETH')

    def test_external_model_runner_is_removed_from_runtime(self):
        from services.sharia_screener import runner as runner_mod
        source = Path(runner_mod.__file__).read_text(encoding='utf-8')
        self.assertFalse(hasattr(runner_mod, 'ScreeningRunner'))
        for forbidden in (
                'SHARIA_OPENAI_API_KEY', 'SHARIA_OPENAI_BASE',
                'api.openai.com', 'requests.post'):
            self.assertNotIn(forbidden, source)

    def test_all_packages_require_signed_cached_gate(self):
        with self.assertRaisesRegex(SystemExit, 'requires.*cached'):
            enforce_sharia_gate_mode('live', 'fresh')
        self.assertEqual(enforce_sharia_gate_mode('live', 'cached'), 'cached')
        self.assertEqual(enforce_sharia_gate_mode('testnet', 'cached'), 'cached')

    def test_direct_order_manager_construction_cannot_enable_fresh_live_gate(self):
        from services.execution_sidecar import order_manager as om
        with mock.patch.object(om, 'load_package_mode', return_value='live'), \
                mock.patch.dict('os.environ', {'SHARIA_SIGNAL_GATE_MODE': 'fresh'}):
            with self.assertRaisesRegex(
                    SystemExit, 'requires.*cached'):
                om.OrderManager(None, None, None, None, None, {})

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
            entries = {
                base: {
                    'symbol': base + 'USDT', 'status': 'TRADING',
                    'baseAsset': base, 'quoteAsset': 'USDT',
                    'isSpotTradingAllowed': True,
                } for base in ('BNB', 'ETH', 'ADA')
            }
            svc._spot_usdt_bases = lambda: entries
            svc.source_discovery = mock.Mock()
            svc.source_discovery.ensure.return_value = {
                'status': 'VERIFIED_CANDIDATE', 'cache_hit': False}
            with mock.patch.object(paths_mod, 'SHARIA_FILE', status), \
                    mock.patch.object(service_mod, 'UNIVERSE_CURRENT', universe), \
                    mock.patch.object(service_mod, 'audit', mock.Mock()):
                svc.idle_enqueue()
            queued = {row['base'] for row in svc.queue.recent(10)}
            self.assertEqual(queued, {'ETH', 'ADA'})
            svc.source_discovery.write_universe_index.assert_called_once_with(entries)
            svc.source_discovery.ensure.assert_called_once_with(
                'BNB', 'BNB/USDT', entries['BNB'])

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
