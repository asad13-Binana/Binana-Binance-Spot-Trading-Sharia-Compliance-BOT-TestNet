from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent))  # make _harness importable in any run mode
_sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _harness  # sets bus keys + release binding; import before service modules

_TEST_SHARED = Path(tempfile.mkdtemp(prefix='v81_safety_import_'))
_OLD_ENV = {key: os.environ.get(key) for key in ('SHARED_ROOT', 'LEGACY_RUNTIME_DIR', 'AUDIT_LOG')}
os.environ['SHARED_ROOT'] = str(_TEST_SHARED)
os.environ['LEGACY_RUNTIME_DIR'] = str(_TEST_SHARED / 'legacy_runtime')
os.environ['AUDIT_LOG'] = str(_TEST_SHARED / 'audit/events.jsonl')
_ORIGINAL_CWD = Path.cwd()

from services.common.audit import audit
from services.telegram_broker import bot

os.chdir(_ORIGINAL_CWD)
for _key, _value in _OLD_ENV.items():
    if _value is None:
        os.environ.pop(_key, None)
    else:
        os.environ[_key] = _value


class _Trader:
    def __init__(self, running=True):
        self._running = running

    def is_running(self):
        return self._running


class _CommandAdapter:
    def __init__(self, *, crash=False):
        self.trader = _Trader(True)
        self.crash = crash
        self.emergency_calls = 0
        self.enabled = []

    def set_enabled(self, value):
        self.enabled.append(bool(value))
        return 'ON' if value else 'OFF'

    def emergency_exit(self, symbol):
        self.emergency_calls += 1
        if self.crash:
            raise SystemExit('simulated process death after exchange action')
        return f'exit {symbol}'


class _SignalAdapter:
    def __init__(self, *, crash=False):
        self.crash = crash
        self.submit_calls = 0

    def submit(self, symbol, reason):
        self.submit_calls += 1
        if self.crash:
            raise SystemExit('simulated process death after accepted submission')
        return True, 'accepted'

    def mirror_positions(self, status):
        return 0




class EventAndInputSafetyTests(unittest.TestCase):



    def test_audit_write_failure_does_not_raise(self):
        # Cross-platform unwritable target: a regular FILE stands where a parent
        # directory is required, so the audit's mkdir raises OSError on any OS
        # (the previous '/proc/...' path is writable as D:\proc\... on Windows).
        with tempfile.TemporaryDirectory() as td:
            blocker = Path(td) / 'blocker'
            blocker.write_text('x')
            target = blocker / 'sub' / 'events.jsonl'
            row = audit('expected-write-failure', path=str(target))
        self.assertIn('audit_write_error', row)





class TelegramFailClosedOrderingTests(unittest.TestCase):
    def test_pause_disarms_execution_sidecar_before_freqtrade(self):
        calls = []
        with mock.patch.object(bot, 'sidecar_command', side_effect=lambda *a, **k: calls.append('sidecar') or {'ok': True}), \
             mock.patch.object(bot, 'ft_call', side_effect=lambda *a, **k: calls.append('freqtrade') or {'ok': True}), \
             mock.patch.object(bot, 'send'):
            bot.route('entries_off', '123')
        self.assertEqual(calls, ['sidecar', 'freqtrade'])

    def test_nan_trade_size_is_rejected_before_confirmation(self):
        message = {'from': {'id': 1}, 'chat': {'id': 1, 'type': 'private'}, 'text': '/setsize nan'}
        with mock.patch.object(bot, 'is_owner', return_value=True), \
             mock.patch.object(bot, '_ask_confirm') as confirm, \
             mock.patch.object(bot, 'send') as send:
            bot.handle_message(message)
        confirm.assert_not_called()
        self.assertIn('finite', send.call_args.args[0])

    def test_nan_profit_lock_is_rejected_before_confirmation(self):
        message = {'from': {'id': 1}, 'chat': {'id': 1, 'type': 'private'}, 'text': '/lockprofit ETHUSDT nan'}
        with mock.patch.object(bot, 'is_owner', return_value=True), \
             mock.patch.object(bot, '_ask_confirm') as confirm, \
             mock.patch.object(bot, 'send') as send:
            bot.handle_message(message)
        confirm.assert_not_called()
        self.assertIn('finite', send.call_args.args[0])


if __name__ == '__main__':
    unittest.main(verbosity=2)






class ArchivedDeploymentIsolationTests(unittest.TestCase):
    def test_legacy_pull_and_standalone_start_paths_are_fail_closed(self):
        root = Path(__file__).resolve().parents[1]
        for relative in ('freqtrade/deploy/update.sh', 'freqtrade/scripts/start.sh',
                         'runtime_owner/deployment/binana-stage1-start'):
            self.assertFalse((root / relative).exists(), relative)





class SecretScannerRegressionTests(unittest.TestCase):
    def test_sensitive_assignment_is_detected_even_when_variable_name_contains_api_key(self):
        import importlib.util
        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location('release_secret_scan', root / 'tests/secret_scan.py')
        module = importlib.util.module_from_spec(spec)
        assert spec.loader
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as td:
            test_root = Path(td)
            (test_root / '.env').write_text('BINANCE_API_KEY=REALISTIC_NON_PLACEHOLDER_VALUE_123456789\n')
            findings = module.scan(test_root)
            self.assertTrue(any('BINANCE_API_KEY' in item for item in findings))

    def test_blank_and_explicit_placeholders_are_allowed(self):
        import importlib.util
        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location('release_secret_scan_placeholders', root / 'tests/secret_scan.py')
        module = importlib.util.module_from_spec(spec)
        assert spec.loader
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as td:
            test_root = Path(td)
            (test_root / '.env.example').write_text(
                'BINANCE_API_KEY=\nTELEGRAM_BOT_TOKEN=REPLACE_WITH_REAL_TELEGRAM_TOKEN\n'
            )
            self.assertEqual(module.scan(test_root), [])


class DeterministicReleaseManifestTests(unittest.TestCase):
    def test_manifest_and_release_hash_are_stable_for_unchanged_tree(self):
        import subprocess
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'release'
            shutil.copytree(source, root, ignore=shutil.ignore_patterns(
                '.git', '__pycache__', '.pytest_cache', '.ruff_cache',
                'RELEASE_MANIFEST.json', 'RELEASE_SHA256.txt',
            ))
            subprocess.run([_sys.executable, 'scripts/build_manifest.py'], cwd=root, check=True, capture_output=True, text=True)
            first_manifest = (root / 'RELEASE_MANIFEST.json').read_bytes()
            first_hash = (root / 'RELEASE_SHA256.txt').read_bytes()
            time.sleep(0.02)
            subprocess.run([_sys.executable, 'scripts/build_manifest.py'], cwd=root, check=True, capture_output=True, text=True)
            self.assertEqual((root / 'RELEASE_MANIFEST.json').read_bytes(), first_manifest)
            self.assertEqual((root / 'RELEASE_SHA256.txt').read_bytes(), first_hash)
        payload = json.loads(first_manifest)
        self.assertNotIn('generated_at', payload)
        # manifest_format 2: V10.1 replaced interpreter-dependent AST hashes
        # with canonical source/token strategy fingerprints (V101-001).
        self.assertEqual(payload.get('manifest_format'), 2)
        self.assertIn(payload.get('package_mode'), {'testnet', 'live'})
        # V102-REM-009: the release label derives from RELEASE_VERSION
        # metadata plus the package mode — never a hardcoded string.
        release_version = (source / 'RELEASE_VERSION').read_text(encoding='utf-8').strip()
        self.assertEqual(
            payload.get('release'),
            f"{release_version}-{payload['package_mode'].upper()}",
        )
        preservation = payload.get('preservation', {})
        self.assertEqual(preservation.get('strategy_signal_fingerprints'),
                         preservation.get('expected_strategy_signal_fingerprints'))










def _audit_process_worker(path: str, worker: int, count: int) -> None:
    os.environ['AUDIT_LOG_MAX_BYTES'] = '2048'
    os.environ['AUDIT_LOG_BACKUPS'] = '20'
    for index in range(count):
        audit('multiprocess-audit', path=path, details={'worker': worker, 'index': index, 'padding': 'x' * 80})


class CrossProcessAuditLockTests(unittest.TestCase):
    def test_concurrent_container_style_writers_leave_only_valid_json_lines(self):
        import multiprocessing
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / 'events.jsonl')
            ctx = multiprocessing.get_context('spawn')
            # Windows can transiently refuse handle duplication under load
            # (WinError 5) during spawn; retry the spawn a few times so a real
            # cross-process run happens on the audit host as it does on Linux.
            procs = None
            for attempt in range(5):
                try:
                    procs = [ctx.Process(target=_audit_process_worker, args=(path, n, 40)) for n in range(4)]
                    for proc in procs:
                        proc.start()
                    break
                except PermissionError:
                    for proc in procs or []:
                        try:
                            proc.terminate()
                        except Exception:
                            pass
                    procs = None
                    time.sleep(1.0)
            self.assertIsNotNone(procs, 'the OS refused to spawn worker processes after retries')
            for proc in procs:
                proc.join(15)
                self.assertEqual(proc.exitcode, 0)
            files = sorted(Path(td).glob('events.jsonl*'))
            data_files = [p for p in files if not p.name.endswith('.lock')]
            self.assertTrue(data_files)
            rows = []
            for file in data_files:
                for line in file.read_text(encoding='utf-8').splitlines():
                    rows.append(json.loads(line))
            self.assertTrue(rows)
            self.assertTrue(all(row.get('event') == 'multiprocess-audit' for row in rows))
            self.assertTrue((Path(path + '.lock')).exists())


class UniversePublicationOrderingTests(unittest.TestCase):
    def test_historical_snapshot_exists_before_current_pointer_is_published(self):
        from services.universe_service.snapshot_store import store
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            result = store(root, [{'pair': 'ETH/USDT', 'rank': 1}], {'limit': 50}, 900)
            current = json.loads((root / 'current_pairlist.json').read_text())
            history = root / 'snapshots' / current['snapshot_file']
            self.assertTrue(history.is_file())
            self.assertEqual(json.loads(history.read_text())['snapshot_hash'], result['snapshot_hash'])
            self.assertIn(result['snapshot_hash'][:12], history.name)
            self.assertIn('.', history.name)




class UniverseRuntimeConfigurationTests(unittest.TestCase):
    def test_rate_limit_hostile_refresh_and_oversized_universe_are_rejected(self):
        from services.universe_service import scanner
        with mock.patch.object(scanner, 'REFRESH', 0), mock.patch.object(scanner, 'LIMIT', 51):
            with self.assertRaisesRegex(ValueError, 'UNIVERSE_LIMIT.*UNIVERSE_REFRESH_SECONDS'):
                scanner.validate_runtime_settings()

    def test_nonfinite_timeout_is_rejected(self):
        from services.universe_service import scanner
        with mock.patch.object(scanner, 'TIMEOUT', float('nan')):
            with self.assertRaisesRegex(ValueError, 'HTTP_TIMEOUT_SECONDS'):
                scanner.validate_runtime_settings()






class PackagedArtifactCIParityTests(unittest.TestCase):
    def test_ci_uses_deterministic_tar_and_retests_extracted_artifact(self):
        root = Path(__file__).resolve().parents[1]
        workflow = (root / '.github/workflows/ci.yml').read_text(encoding='utf-8')
        self.assertIn("--sort=name --mtime='UTC 2020-01-01'", workflow)
        self.assertIn('gzip -n -9', workflow)
        self.assertIn('Verify freshly extracted release artifact', workflow)
        self.assertIn('sha256sum -c oracle-bundle.tar.gz.sha256', workflow)
        self.assertIn('cmp /tmp/oracle-bundle/BUNDLE_MANIFEST.json /tmp/oracle-extracted/oracle-bundle/BUNDLE_MANIFEST.json', workflow)
        self.assertIn('python /tmp/oracle-extracted/oracle-bundle/oracle_preflight.py --bundle /tmp/oracle-extracted/oracle-bundle', workflow)
