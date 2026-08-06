from __future__ import annotations
"""V102-REM-001 runtime package-mode interlock tests (audit ISSUE 1).

The testnet package must be RUNTIME-incapable of live execution: no .env
edit, direct ``docker compose up``, bypassed installer, or restored
deployment may drive a testnet package down the live path. The mode comes
from the shipped read-only RELEASE_MODE file, never from an environment
variable, and is enforced in ``live_interlock`` before any authenticated
Binance client, stream, reconciliation, order, or live-evidence code runs.

The live package's behavior is unchanged: simulation default, existing
signed-evidence promotion gates (pinned by the existing V8.1/V10.1
regression suites), and rejection of cross-mode EXECUTION_MODE=testnet.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import tests._harness as harness  # noqa: E402,F401  (bus keys before service imports)
from services.execution_sidecar import package_mode  # noqa: E402
from services.execution_sidecar import main as sidecar_main  # noqa: E402
from services.execution_sidecar.core_adapter import legacy  # noqa: E402
from services.execution_sidecar.package_mode import (  # noqa: E402
    enforce_package_mode, load_package_mode,
)


def _mode_file(tmp: str, content: str) -> Path:
    path = Path(tmp) / 'RELEASE_MODE'
    path.write_text(content, encoding='utf-8')
    return path


class StateStub:
    """Minimal StateStore stand-in for live_interlock's non-live paths."""

    def __init__(self):
        self.data = {}
        self.entries_calls = []
        self.saved = False

    def set_entries(self, enabled, reason=None):
        self.entries_calls.append((enabled, reason))

    def save(self):
        self.saved = True


class EnforcePackageModeTests(unittest.TestCase):
    def test_testnet_package_permits_testnet_and_simulation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _mode_file(tmp, 'testnet\n')
            self.assertEqual(enforce_package_mode('testnet', path), 'testnet')
            self.assertEqual(enforce_package_mode('simulation', path), 'testnet')

    def test_testnet_package_always_rejects_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _mode_file(tmp, 'testnet')
            with self.assertRaises(SystemExit) as ctx:
                enforce_package_mode('live', path)
            self.assertIn('LIVE BLOCKED', str(ctx.exception))
            self.assertIn('TESTNET package', str(ctx.exception))

    def test_live_package_permits_live_and_simulation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _mode_file(tmp, 'live')
            self.assertEqual(enforce_package_mode('simulation', path), 'live')
            self.assertEqual(enforce_package_mode('live', path), 'live')

    def test_live_package_rejects_cross_mode_testnet(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _mode_file(tmp, 'live')
            with self.assertRaises(SystemExit) as ctx:
                enforce_package_mode('testnet', path)
            self.assertIn('PACKAGE MODE BLOCKED', str(ctx.exception))

    def test_missing_release_mode_blocks_startup(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / 'RELEASE_MODE'
            with self.assertRaises(SystemExit) as ctx:
                load_package_mode(missing)
            self.assertIn('unreadable', str(ctx.exception))

    def test_invalid_release_mode_blocks_startup(self):
        for content in ('production', '', 'LIVEISH', 'testnet live'):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as tmp:
                path = _mode_file(tmp, content)
                with self.assertRaises(SystemExit):
                    enforce_package_mode('simulation', path)

    def test_mode_is_read_from_file_not_environment(self):
        """RELEASE_MODE in the environment must be ignored entirely."""
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
                os.environ, {'RELEASE_MODE': 'live'}, clear=False):
            path = _mode_file(tmp, 'testnet')
            with self.assertRaises(SystemExit):
                enforce_package_mode('live', path)


class LiveInterlockPackageModeTests(unittest.TestCase):
    """The interlock runs inside live_interlock BEFORE any authenticated
    code path (adapters are only constructed later in main())."""

    def setUp(self):
        self._cfg_testnet = legacy.CFG.TESTNET
        self._fortress_testnet = legacy.FORTRESS_CFG.TESTNET

    def tearDown(self):
        legacy.CFG.TESTNET = self._cfg_testnet
        legacy.FORTRESS_CFG.TESTNET = self._fortress_testnet

    def _patched_mode(self, tmp, content):
        return mock.patch.object(package_mode, 'PACKAGE_MODE_FILE',
                                 _mode_file(tmp, content))

    def test_testnet_package_blocks_live_even_with_full_promotion_env(self):
        """Valid-looking live promotion material cannot override the lock."""
        with tempfile.TemporaryDirectory() as tmp, self._patched_mode(tmp, 'testnet'), \
                mock.patch.dict(os.environ, {
                    'EXECUTION_MODE': 'live',
                    'BINANCE_TESTNET': 'false',
                    'BINANCE_API_KEY': 'k', 'BINANCE_API_SECRET': 's',
                    'SIDECAR_RELEASE_HASH': 'a' * 64,
                    'AUTO_CONFIRM': 'false'}, clear=False):
            state = StateStub()
            with self.assertRaises(SystemExit) as ctx:
                sidecar_main.live_interlock(state)
            self.assertIn('LIVE BLOCKED', str(ctx.exception))
            self.assertIn('TESTNET package', str(ctx.exception))
            # Blocked before any state mutation or adapter work.
            self.assertEqual(state.entries_calls, [])
            self.assertFalse(state.saved)

    def test_testnet_package_runs_testnet_and_forces_testnet_endpoints(self):
        """Plan test 10: even BINANCE_TESTNET=false in .env cannot point a
        testnet package at production endpoints."""
        with tempfile.TemporaryDirectory() as tmp, self._patched_mode(tmp, 'testnet'), \
                mock.patch.dict(os.environ, {
                    'EXECUTION_MODE': 'testnet',
                    'BINANCE_TESTNET': 'false'}, clear=False):
            state = StateStub()
            mode = sidecar_main.live_interlock(state)
            self.assertEqual(mode.value, 'testnet')
            self.assertEqual(os.environ['BINANCE_TESTNET'], 'true')
            self.assertTrue(legacy.CFG.TESTNET)
            self.assertTrue(legacy.FORTRESS_CFG.TESTNET)

    def test_testnet_package_allows_simulation(self):
        with tempfile.TemporaryDirectory() as tmp, self._patched_mode(tmp, 'testnet'), \
                mock.patch.dict(os.environ, {'EXECUTION_MODE': 'simulation'},
                                clear=False):
            state = StateStub()
            mode = sidecar_main.live_interlock(state)
            self.assertEqual(mode.value, 'simulation')
            self.assertTrue(state.data.get('simulation'))
            self.assertEqual(os.environ['BINANCE_TESTNET'], 'true')

    def test_missing_release_mode_blocks_every_execution_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / 'RELEASE_MODE'
            with mock.patch.object(package_mode, 'PACKAGE_MODE_FILE', missing):
                for execution in ('simulation', 'testnet', 'live'):
                    with self.subTest(execution=execution), mock.patch.dict(
                            os.environ, {'EXECUTION_MODE': execution}, clear=False):
                        with self.assertRaises(SystemExit):
                            sidecar_main.live_interlock(StateStub())

    def test_invalid_execution_mode_still_reports_clean_error(self):
        with tempfile.TemporaryDirectory() as tmp, self._patched_mode(tmp, 'testnet'), \
                mock.patch.dict(os.environ, {'EXECUTION_MODE': 'bogus'}, clear=False):
            with self.assertRaises(SystemExit) as ctx:
                sidecar_main.live_interlock(StateStub())
            self.assertIn('Invalid EXECUTION_MODE', str(ctx.exception))


class ShippedPackageConsistencyTests(unittest.TestCase):
    """The tree this suite runs in must itself be internally consistent."""

    def test_release_mode_is_valid_and_baked_into_docker_image(self):
        mode = (ROOT / 'RELEASE_MODE').read_text(encoding='utf-8').strip()
        self.assertIn(mode, ('live', 'testnet'))
        dockerfile = (ROOT / 'Dockerfile.services').read_text(encoding='utf-8')
        self.assertIn('COPY RELEASE_MODE /app/RELEASE_MODE', dockerfile)
        self.assertIn('chmod 0444 /app/RELEASE_MODE', dockerfile)

    def test_live_evidence_strategy_is_shipped_into_the_image(self):
        # V102-REM-011 (deep-audit CRITICAL): the sidecar's live-evidence gate
        # fingerprints /app/freqtrade/user_data/strategies/IctSmcStrategy.py, so
        # the image MUST contain the strategy or every live promotion fails.
        dockerfile = (ROOT / 'Dockerfile.services').read_text(encoding='utf-8')
        self.assertIn('freqtrade/user_data/strategies', dockerfile,
                      msg='services image must ship the strategy for the live gate')
        # The path the live gate computes must match what the Dockerfile ships.
        main_src = (ROOT / 'services/execution_sidecar/main.py').read_text(encoding='utf-8')
        self.assertIn("freqtrade/user_data/strategies/IctSmcStrategy.py", main_src)
        self.assertTrue((ROOT / 'freqtrade/user_data/strategies/IctSmcStrategy.py').is_file())

    def test_env_example_default_is_permitted_and_safe(self):
        mode = (ROOT / 'RELEASE_MODE').read_text(encoding='utf-8').strip()
        env_text = (ROOT / '.env.example').read_text(encoding='utf-8')
        default = next(line.split('=', 1)[1].strip()
                       for line in env_text.splitlines()
                       if line.startswith('EXECUTION_MODE='))
        self.assertIn(default, package_mode.ALLOWED_EXECUTION_MODES[mode])
        if mode == 'live':
            # Plan test 8: the live package still defaults to simulation.
            self.assertEqual(default, 'simulation')
        else:
            self.assertEqual(default, 'testnet')

    def test_package_mode_file_resolves_to_repo_root(self):
        self.assertEqual(package_mode.PACKAGE_MODE_FILE,
                         ROOT / 'RELEASE_MODE')


class ImportOutsideTheImageTests(unittest.TestCase):
    """CI-SAFE-001: importing the sidecar from a source checkout must not
    require the deployment image's /app volume.

    core_adapter defaulted SHARED_ROOT to '/app/shared' and ran mkdir()+chdir()
    at import time. Inside the image that is correct. On a GitHub Actions
    runner /app does not exist and an unprivileged user cannot create a
    directory at the filesystem root, so importing
    services.execution_sidecar.main raised PermissionError: '/app' and every
    matrix job failed before a single test ran.

    Runs in a SUBPROCESS with the overrides stripped: this process has already
    imported the module and other test modules set SHARED_ROOT, so an
    in-process assertion would prove nothing. Asserts on the resolved path
    rather than waiting for mkdir to fail, so it also catches the regression on
    Windows, where /app resolves to a creatable C:\\app.
    """

    def _import_clean(self, extra_env=None):
        import subprocess
        env = {k: v for k, v in os.environ.items()
               if k not in ('SHARED_ROOT', 'LEGACY_RUNTIME_DIR', 'AUDIT_LOG')}
        env['PYTHONPATH'] = str(ROOT)
        env.update(extra_env or {})
        return subprocess.run(
            [sys.executable, '-c',
             'import services.execution_sidecar.main;'
             'from services.execution_sidecar.core_adapter import LEGACY_RUNTIME;'
             'print(LEGACY_RUNTIME)'],
            cwd=str(ROOT), env=env, capture_output=True, text=True)

    def test_sidecar_imports_without_the_app_volume(self):
        proc = self._import_clean()
        self.assertEqual(
            proc.returncode, 0,
            msg='importing the sidecar outside the deployment image must not '
                'fail:\n' + (proc.stdout + proc.stderr).strip())
        runtime = proc.stdout.strip().replace('\\', '/')
        self.assertFalse(
            runtime.startswith('/app') or runtime[1:].startswith(':/app'),
            msg='the legacy runtime must not be placed under /app outside the '
                f'deployment image; got {runtime!r}')

    def test_explicit_shared_root_still_wins(self):
        # docker-compose sets SHARED_ROOT for every service; that must always win.
        with tempfile.TemporaryDirectory() as tmp:
            proc = self._import_clean({'SHARED_ROOT': tmp})
            self.assertEqual(proc.returncode, 0,
                             msg=(proc.stdout + proc.stderr).strip())
            self.assertEqual(
                Path(proc.stdout.strip()), Path(tmp) / 'legacy_runtime',
                msg='an explicit SHARED_ROOT must override the fallback')


if __name__ == '__main__':
    unittest.main()
