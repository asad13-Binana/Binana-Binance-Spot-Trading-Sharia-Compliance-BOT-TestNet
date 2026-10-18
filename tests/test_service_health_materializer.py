"""Verify current AWS services receive only the reviewed health-call patches."""
import importlib.util
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    'service_health_materializer', ROOT / 'runtime_owner/deployment/materialize_service_health.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ServiceHealthMaterializerTests(unittest.TestCase):
    def test_output_matches_atomic_helper_and_only_changes_health_call(self):
        with tempfile.TemporaryDirectory() as directory:
            dest = Path(directory) / 'overlay'
            receipt = module.materialize(ROOT, dest)
            self.assertEqual((dest / 'common_atomic.py').read_bytes(),
                             (ROOT / 'services/common/atomic.py').read_bytes())
            for filename, source, replacements in (
                    ('broker_base.py', module.BASE, module.BROKER_PATCHES),
                    ('market_service.py', module.MARKET_BASE, module.MARKET_PATCHES)):
                patched = (dest / filename).read_bytes()
                for old, new in replacements:
                    self.assertEqual(patched.count(new), 1)
                    patched = patched.replace(new, old, 1)
                self.assertEqual(patched, (ROOT / source).read_bytes())
            self.assertEqual((dest / 'manual_registry_service.py').read_bytes(),
                             (ROOT / module.SHARIA_SOURCE).read_bytes())
            self.assertEqual(receipt['base_sharia_sha256'], module.SHARIA_SHA256)
            self.assertEqual(set(receipt['files']), {
                'broker_base.py', 'common_atomic.py', 'market_service.py',
                'manual_registry_service.py'})

    def test_changed_capture_is_rejected_before_output_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / 'source'
            capture = repo / module.BASE
            capture.parent.mkdir(parents=True)
            capture.write_bytes((ROOT / module.BASE).read_bytes() + b'\n# drift\n')
            dest = Path(directory) / 'overlay'
            with self.assertRaisesRegex(ValueError, 'reviewed base'):
                module.materialize(repo, dest)
            self.assertFalse(dest.exists())

    def test_existing_destination_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            dest = Path(directory)
            marker = dest / 'broker_base.py'
            marker.write_bytes(b'existing')
            with self.assertRaises(FileExistsError):
                module.materialize(ROOT, dest)
            self.assertEqual(marker.read_bytes(), b'existing')
