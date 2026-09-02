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
    def test_compose_helper_accepts_readonly_identity(self):
        bash = _harness.posix_bash()
        if not bash:
            self.skipTest("POSIX bash unavailable")
        source = (ROOT / "deploy/install_artifact.sh").read_text()
        helper = re.search(r"(?ms)^compose_for\(\)\{.*?^\}", source).group()
        script = (
            'set -euo pipefail\nreadonly COMPOSE_PROJECT_NAME=binana-testnet\nENV_FILE=/private/env\n'
            'docker(){ printf "%s\\n" "$@"; }\n' + helper +
            '\ncompose_for /release reviewed-tag config -q\n'
        )
        run = subprocess.run([bash, "-c", script], capture_output=True,
                             text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("binana-testnet", run.stdout)

    def test_installer_does_not_require_bare_python(self):
        source = (ROOT / "deploy/install_artifact.sh").read_text()
        self.assertNotRegex(source, r"(?m)^(?:PYTHONPATH=\S+ )?\s*python ")

    def test_host_lock_matches_existing_reviewed_crypto_pin(self):
        host = ROOT / "requirements.host.lock"
        self.assertTrue(host.is_file(), "host validation dependency closure missing")
        from scripts.verify_deployment_supply_chain import _logical_requirements
        service = _logical_requirements(ROOT / "requirements.services.lock")
        self.assertEqual(_logical_requirements(host), [
            entry for entry in service if entry.startswith("pycryptodome==")])

    def test_host_dependency_bootstrap_precedes_sharia_validation(self):
        source = (ROOT / "deploy/install_artifact.sh").read_text()
        self.assertLess(source.index('prepare_host_python "$NEW"'),
                        source.index("-m services.universe_service.validate_sharia"))

    def test_monitor_rendering_has_no_legacy_project_or_backup_root(self):
        source = (ROOT / "deploy/install_monitoring.sh").read_text()
        self.assertIn('s#binana-freqtrade-v101#$INSTANCE_SLUG#g', source)
        self.assertIn('/var/backups/$INSTANCE_SLUG', source)
        self.assertIn('s#/var/lib/binana-freqtrade-v101/shared#$PERSIST#g', source)

    def test_transaction_catches_early_start_and_write_failure(self):
        source = (ROOT / "deploy/install_artifact.sh").read_text()
        self.assertIn("trap 'rollback' ERR", source)
        self.assertIn('[[ ${#COMMAND_IDS[@]} -eq 2 ]]', source)

    def test_ci_runs_the_manual_projector_without_a_network(self):
        import yaml
        compose = yaml.safe_load((ROOT / 'docker-compose.yml').read_text())
        self.assertEqual(compose['services']['sharia-screener']['network_mode'], 'none')
        workflow = yaml.safe_load((ROOT / '.github/workflows/ci.yml').read_text())
        step = next(step for step in workflow['jobs']['integration-simulation']['steps']
                    if step.get('name') ==
                    'Prove the manual Sharia registry projector is networkless')
        script = step['run']
        self.assertIn('up -d sharia-screener', script)
        self.assertIn('HostConfig.NetworkMode', script)
        self.assertIn('len(status[\'records\']) == len(registry[\'symbols\']) == 244',
                      script)

    def test_all_shared_bind_sources_are_precreated(self):
        source = (ROOT / "deploy/install_artifact.sh").read_text()
        self.assertIn('"$PERSIST/universe"', source)
        self.assertIn('"$PERSIST/signals/inbox"', source)
        self.assertIn('install -m 0640 -o "$BOT_UID" -g "$BOT_GID"', source)

    def test_freqtrade_uid_wrapper_is_built_before_release_switch(self):
        compose = (ROOT / 'docker-compose.yml').read_text(encoding='utf-8')
        installer = (ROOT / 'deploy/install_artifact.sh').read_text(encoding='utf-8')
        wrapper = (ROOT / 'Dockerfile.freqtrade').read_text(encoding='utf-8')
        self.assertIn('dockerfile: Dockerfile.freqtrade', compose)
        self.assertIn('BOT_UID: "${BOT_UID:-1000}"', compose)
        self.assertIn('BOT_GID: "${BOT_GID:-1000}"', compose)
        self.assertIn('user: "${BOT_UID:-1000}:${BOT_GID:-1000}"', compose)
        self.assertIn(
            'compose_for "$NEW" "$NEW_TAG" build universe freqtrade', installer)
        self.assertRegex(
            wrapper.splitlines()[0],
            r'^FROM freqtradeorg/freqtrade:2026\.6@sha256:[0-9a-f]{64}$')
        self.assertIn('ARG BOT_UID=1000', wrapper)
        self.assertIn('ARG BOT_GID=1000', wrapper)
        self.assertTrue(wrapper.rstrip().endswith('USER ftuser'))

    def test_manual_registry_migration_is_transactional_and_preserves_owner_data(self):
        source = (ROOT / 'deploy/install_artifact.sh').read_text(encoding='utf-8')
        self.assertIn("legacy_empty=(isinstance(value,dict)", source)
        self.assertIn('SHARIA_REGISTRY_INSTALL=preserve', source)
        self.assertIn('backup_manual_sharia_state', source)
        self.assertIn('restore_manual_sharia_state', source)
        self.assertIn('bootstrap-status', source)
        self.assertLess(source.index('compose_for "$OLD" "$OLD_TAG" down'),
                        source.index('\napply_manual_sharia_state\n'))

    def test_readonly_core_matches_prior_release(self):
        expected = {
            "freqtrade/user_data/strategies/IctSmcStrategy.py": "9f6bafc78c8cd0d9b9cbde615ddce89e304ab09738584b88d05bfdf92ff4e830",
            "legacy_core/binance_bot_V4.9.16_ALL_IN_ONE.py": "70b1d67cc0092b5b8db4a68b343cf893641bde1aae580e9ef51e2adec1062459",
            "services/common/sharia_v19.py": "5eb9fd5338d80fcaf0d39bb3f4935a75b57dd91136c72a83a7551b659b04d865",
            "shared/sharia/HALAL_CRYPTO_SPOT_SCREENING_V19_1_PRODUCTION.json": "07106bb8bfc1924d8d0c6f61ced4e0c51c2ac2054988423f42c1fd67f3b2ba78",
        }
        for path, digest in expected.items():
            self.assertEqual(hashlib.sha256((ROOT / path).read_bytes()).hexdigest(), digest)


if __name__ == "__main__":
    unittest.main()
