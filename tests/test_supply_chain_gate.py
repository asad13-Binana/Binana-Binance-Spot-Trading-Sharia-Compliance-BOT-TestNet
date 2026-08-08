from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.verify_deployment_supply_chain import deployment_supply_chain_errors

ROOT = Path(__file__).resolve().parents[1]
DIGEST = 'a' * 64
HASH = 'b' * 64


def _write_tree(root: Path, *, pinned: bool) -> None:
    image_suffix = f'@sha256:{DIGEST}' if pinned else ''
    hash_suffix = f' --hash=sha256:{HASH}' if pinned else ''
    require_hashes = ' --require-hashes' if pinned else ''
    (root / 'monitoring').mkdir()
    (root / 'deploy').mkdir()
    (root / 'Dockerfile.services').write_text(
        f'FROM python:3.12-slim{image_suffix}\n'
        f'RUN pip install{require_hashes} -r requirements.services.lock\n')
    (root / 'docker-compose.yml').write_text(
        'services:\n  freqtrade:\n'
        f'    image: freqtradeorg/freqtrade:2026.6{image_suffix}\n')
    (root / 'requirements.services.lock').write_text(
        f'requests==2.34.2{hash_suffix}\n')
    (root / 'monitoring/requirements-monitoring.lock').write_text(
        f'fastapi==0.139.2{hash_suffix}\n')
    (root / 'deploy/install_monitoring.sh').write_text(
        f'python -m pip install{require_hashes} -r lock\n')


class DeploymentSupplyChainGateTests(unittest.TestCase):
    def test_complete_digest_and_hash_pinning_passes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_tree(root, pinned=True)
            self.assertEqual(deployment_supply_chain_errors(root), [])

    def test_mutable_inputs_fail_with_all_reasons(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_tree(root, pinned=False)
            errors = deployment_supply_chain_errors(root)
        self.assertEqual(len(errors), 6)
        self.assertTrue(any('base image' in error for error in errors))
        self.assertTrue(any('Freqtrade' in error for error in errors))
        self.assertEqual(sum('without sha256 hashes' in error for error in errors), 2)
        self.assertEqual(sum('--require-hashes' in error for error in errors), 2)

    def test_malformed_hash_is_rejected_even_when_a_valid_hash_is_present(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_tree(root, pinned=True)
            lock = root / 'requirements.services.lock'
            lock.write_text(
                lock.read_text()[:-1] + ' --hash=sha256:not-a-digest\n')
            errors = deployment_supply_chain_errors(root)
        self.assertTrue(any('malformed' in error for error in errors))

    def test_current_tree_is_blocked_only_by_unpinned_container_images(self):
        self.assertEqual(
            deployment_supply_chain_errors(ROOT),
            [
                'Dockerfile.services base image is not pinned by sha256 digest',
                'Freqtrade runtime image is not pinned by sha256 digest',
            ],
        )

    def test_oracle_installer_runs_gate_before_activation(self):
        installer = (ROOT / 'deploy/install_artifact.sh').read_text(encoding='utf-8')
        gate = 'python "$NEW/scripts/verify_deployment_supply_chain.py"'
        self.assertIn(gate, installer)
        self.assertLess(installer.index(gate), installer.index('RELEASE_HASH='))


if __name__ == '__main__':
    unittest.main()
