"""Host deployment regressions; no exchange credentials or orders are used."""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
import subprocess
import unittest

from tests import _harness

ROOT = Path(__file__).resolve().parents[1]


class CleanHostDeploymentTests(unittest.TestCase):



    def test_host_lock_matches_existing_reviewed_crypto_pin(self):
        host = ROOT / "requirements.host.lock"
        self.assertTrue(host.is_file(), "host validation dependency closure missing")
        from scripts.verify_deployment_supply_chain import _logical_requirements
        service = _logical_requirements(ROOT / "requirements.services.lock")
        self.assertEqual(_logical_requirements(host), [
            entry for entry in service if entry.startswith("pycryptodome==")])


    def test_monitor_rendering_has_no_legacy_project_or_backup_root(self):
        source = (ROOT / "deploy/install_monitoring.sh").read_text()
        self.assertIn('s#binana-freqtrade-v101#$INSTANCE_SLUG#g', source)
        self.assertIn('/var/backups/$INSTANCE_SLUG', source)
        self.assertIn('s#/var/lib/binana-freqtrade-v101/shared#$PERSIST#g', source)








if __name__ == "__main__":
    unittest.main()
