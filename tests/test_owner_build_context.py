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


class RetainedOwnerBuild(unittest.TestCase):
    def fixture(self, root):
        import shutil
        script = root / 'build_verified_owner.py'
        shutil.copyfile(MODULE.with_name('build_verified_owner.py'), script)
        retained = {'freqtrade': {'reference': 'retained:base', 'image_id': 'sha256:' + 'a' * 64}}
        runtime = {'freqtrade': {'reference': 'current:clean', 'image_id': 'sha256:' + 'b' * 64}}
        (root / 'retained-owner-base.json').write_text(json.dumps(retained))
        (root / 'local-images-20261007.json').write_text(json.dumps(runtime))
        return script, retained

    def run_builder(self, script, verify, build, tag='candidate:test'):
        import runpy
        import sys
        from types import ModuleType
        from unittest.mock import patch
        verifier = ModuleType('verify_local_images')
        verifier.verify = verify
        with patch.dict(sys.modules, {'verify_local_images': verifier}), \
                patch.object(sys, 'argv', [str(script), str(script.parent / 'context'), '--tag', tag]), \
                patch('subprocess.run', build):
            runpy.run_path(str(script), run_name='__main__')

    def test_current_runtime_map_cannot_change_retained_build_base(self):
        from unittest.mock import Mock, call
        with tempfile.TemporaryDirectory() as tmp:
            script, retained = self.fixture(Path(tmp))
            verify, build = Mock(), Mock()
            self.run_builder(script, verify, build)
            self.assertEqual(verify.call_args_list, [call(retained), call(retained)])
            self.assertIn('BASE_IMAGE=retained:base', build.call_args.args[0])
            self.assertIn('--pull=false', build.call_args.args[0])

    def test_missing_retained_image_prevents_build(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as tmp:
            script, _ = self.fixture(Path(tmp))
            build = Mock()
            with self.assertRaisesRegex(RuntimeError, 'missing retained image'):
                self.run_builder(script, Mock(side_effect=RuntimeError('missing retained image')), build)
            build.assert_not_called()

    def test_base_identity_change_during_build_is_rejected(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as tmp:
            script, _ = self.fixture(Path(tmp))
            build = Mock()
            with self.assertRaisesRegex(RuntimeError, 'retagged retained image'):
                self.run_builder(script, Mock(side_effect=[None, RuntimeError('retagged retained image')]), build)
            build.assert_called_once()
