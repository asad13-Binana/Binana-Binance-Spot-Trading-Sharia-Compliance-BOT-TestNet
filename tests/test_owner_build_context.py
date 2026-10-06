import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


MODULE = Path(__file__).resolve().parents[1] / 'runtime_owner/deployment/materialize_context.py'
spec = importlib.util.spec_from_file_location('owner_context', MODULE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class OwnerBuildContext(unittest.TestCase):
    def fixture(self, root, relative='owner/freqtrade/example.py'):
        source = root / 'source'
        path = source / relative
        path.parent.mkdir(parents=True)
        path.write_bytes(b'example = 1\n')
        document = {'files': {relative: {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                                        'bytes': path.stat().st_size}}}
        (source / 'PROVENANCE.json').write_text(json.dumps(document))
        (source / 'deployment').mkdir()
        for name in ['Dockerfile', 'validate_image.py']:
            (source / 'deployment' / name).write_text('placeholder')
        return source, path

    def test_only_manifested_bytes_enter_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, _ = self.fixture(root)
            (source / '.env').write_text('PRIVATE=never-copy')
            (source / 'runtime.sqlite').write_bytes(b'private state')
            result = module.materialize(source, root / 'output')
            self.assertEqual(result['source_files'], 1)
            self.assertFalse((root / 'output/.env').exists())
            self.assertFalse((root / 'output/runtime.sqlite').exists())
            self.assertEqual((root / 'output/owner/freqtrade/example.py').read_bytes(), b'example = 1\n')

    def test_tampered_source_fails_before_creating_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, path = self.fixture(root)
            path.write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'Provenance mismatch'):
                module.materialize(source, root / 'output')
            self.assertFalse((root / 'output').exists())

    def test_existing_destination_is_not_merged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, _ = self.fixture(root)
            (root / 'output').mkdir()
            marker = root / 'output/private'
            marker.write_text('keep')
            with self.assertRaises(FileExistsError):
                module.materialize(source, root / 'output')
            self.assertEqual(marker.read_text(), 'keep')

    def test_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, _ = self.fixture(root)
            document = json.loads((source / 'PROVENANCE.json').read_text())
            document['files']['owner/../../outside'] = next(iter(document['files'].values()))
            (source / 'PROVENANCE.json').write_text(json.dumps(document))
            with self.assertRaisesRegex(ValueError, 'Invalid manifest path'):
                module.materialize(source, root / 'output')
            self.assertFalse((root / 'output').exists())
