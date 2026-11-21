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
import json
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






class ShippedPackageConsistencyTests(unittest.TestCase):
    """The tree this suite runs in must itself be internally consistent."""






    def test_default_environment_exposes_only_manual_sharia_contract(self):
        example = (ROOT / '.env.example').read_text(encoding='utf-8')
        self.assertIn('LEGACY_HALAL_FILE=', example)
        self.assertIn('SHARIA_SIGNAL_GATE_MODE=cached', example)
        for inactive_key in (
                'SHARIA_SOURCE_REGISTRY=',
                'SHARIA_EVIDENCE_DIR=',
                'SHARIA_AUTO_SOURCE_DISCOVERY_ENABLED=',
                'SHARIA_IDLE_SCAN_ENABLED=',
                'SHARIA_MAX_SCANS_PER_DAY=',
                'SHARIA_MIN_SECONDS_BETWEEN_SCANS=',
                'SHARIA_RESCAN_INTERVAL_DAYS=',
        ):
            self.assertNotIn(inactive_key, example)

    def test_release_metadata_is_consistent_and_disarmed(self):
        mode = (ROOT / 'RELEASE_MODE').read_text(encoding='utf-8').strip()
        version = (ROOT / 'RELEASE_VERSION').read_text(encoding='utf-8').strip()
        validation = json.loads(
            (ROOT / 'VALIDATION_STATUS.json').read_text(encoding='utf-8'))
        manifest = json.loads(
            (ROOT / 'RELEASE_MANIFEST.json').read_text(encoding='utf-8'))

        self.assertEqual(validation['release'], version)
        self.assertTrue(version.endswith(validation['revision']))
        self.assertEqual(validation['package_mode'], mode)
        self.assertEqual(validation['package_interlock']['release_mode'], mode)
        self.assertEqual(
            validation['default_execution_mode'],
            'simulation')
        self.assertIs(validation['production_live_certified'], False)
        self.assertIs(validation['core_strategy_changed'], False)
        self.assertEqual(manifest['release'], f'{version}-{mode.upper()}')
        self.assertIs(manifest['live_certified'], False)




if __name__ == '__main__':
    unittest.main()
