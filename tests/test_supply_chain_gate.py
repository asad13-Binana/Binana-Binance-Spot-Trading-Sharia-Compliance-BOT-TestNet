from pathlib import Path
import tempfile
import unittest
from scripts.verify_deployment_supply_chain import deployment_supply_chain_errors

ROOT = Path(__file__).resolve().parents[1]


class DeploymentSupplyChainGateTests(unittest.TestCase):
    def fixture(self, root):
        for relative in ('runtime_owner/deployment/Dockerfile.clean', 'runtime_owner/deployment/oracle_services.Dockerfile'):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('FROM python:3.14-slim@sha256:' + 'a' * 64 + '\nRUN pip install --require-hashes -r lock\n')
        for relative in ('requirements.services.lock', 'requirements.host.lock', 'monitoring/requirements-monitoring.lock',
                         'runtime_owner/deployment/requirements.owner.lock', 'runtime_owner/deployment/requirements.build.lock'):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('package==1.2 --hash=sha256:' + 'b' * 64 + '\n')
        (root / 'deploy').mkdir()
        (root / 'deploy/install_monitoring.sh').write_text('pip install --require-hashes -r lock\n')

    def test_complete_oracle_source_pin_contract_passes_without_root_dockerfiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root)
            self.assertEqual(deployment_supply_chain_errors(root), [])

    def test_mutable_image_and_unhashed_requirement_fail_independently(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root)
            (root / 'runtime_owner/deployment/Dockerfile.clean').write_text('FROM python:3.14-slim\nRUN pip install --require-hashes -r lock\n')
            (root / 'requirements.services.lock').write_text('requests==1.0\n')
            errors = deployment_supply_chain_errors(root)
            self.assertEqual(len(errors), 2)
            self.assertTrue(any('base image' in error for error in errors))
            self.assertTrue(any('without sha256 hashes' in error for error in errors))

    def test_valid_hash_does_not_hide_a_malformed_second_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root)
            lock = root / 'requirements.services.lock'
            lock.write_text(lock.read_text().strip() + ' --hash=sha256:broken\n')
            self.assertTrue(any('malformed' in error for error in deployment_supply_chain_errors(root)))

    def test_missing_and_empty_locks_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root)
            (root / 'requirements.services.lock').unlink()
            (root / 'requirements.host.lock').write_text('')
            self.assertEqual(len(deployment_supply_chain_errors(root)), 2)

    def test_current_oracle_tree_passes(self):
        self.assertEqual(deployment_supply_chain_errors(ROOT), [])
