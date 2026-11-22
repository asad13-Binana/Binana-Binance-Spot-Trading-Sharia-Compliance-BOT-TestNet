import hashlib
import ast
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
    def test_support_context_includes_the_hash_locked_installer(self):
        import sys
        previous = sys.path[:]
        try:
            sys.path.insert(0, str(MODULE.parent))
            spec = importlib.util.spec_from_file_location(
                'oracle_build_toolchain', MODULE.parent / 'materialize_oracle_bundle.py')
            builder = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(builder)
            sources = builder.support_sources(MODULE.parents[2])
        finally:
            sys.path[:] = previous
        self.assertEqual(sources['requirements.build.lock'],
                         (MODULE.parent / 'requirements.build.lock').read_bytes())

    def test_context_does_not_export_retained_aws_dockerfile(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, _ = self.fixture(root)
            output = root / 'output'
            module.materialize(source, output)
            self.assertFalse((output / 'Dockerfile').exists(),
                             'Raw patch export must not select a retained AWS image')
            self.assertTrue((output / 'validate_image.py').is_file())

    def test_fresh_source_exports_the_custom_exchange_used_by_the_resolver(self):
        path = MODULE.parents[1] / 'owner/freqtrade/exchange/__init__.py'
        self.assertTrue(path.is_file(), 'clean build would lose the deployed BinanaBinance export')
        tree = ast.parse(path.read_text())
        names = {alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
                 and node.module == 'freqtrade.exchange.binance' for alias in node.names}
        self.assertIn('BinanaBinance', names)

    def fixture(self, root, relative='owner/freqtrade/example.py'):
        source = root / 'source'
        path = source / relative
        path.parent.mkdir(parents=True)
        path.write_bytes(b'example = 1\n')
        document = {'files': {relative: {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                                        'bytes': path.stat().st_size}}}
        (source / 'PROVENANCE.json').write_text(json.dumps(document))
        (source / 'deployment').mkdir()
        for name in ['Dockerfile', 'validate_image.py','release_authority.py']:
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

    def test_runtime_file_omitted_from_manifest_is_rejected_before_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, _ = self.fixture(root)
            (source / 'owner/freqtrade/required_patch.py').write_bytes(b'PATCHED = True\n')
            with self.assertRaisesRegex(ValueError, 'Manifest file set mismatch'):
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
