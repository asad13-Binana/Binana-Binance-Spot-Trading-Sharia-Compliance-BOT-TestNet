import hashlib
import importlib.util
import io
from pathlib import Path
import sys
import tarfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1] / 'runtime_owner/deployment'
spec = importlib.util.spec_from_file_location('materialize_context', ROOT / 'materialize_context.py')
context = importlib.util.module_from_spec(spec)
spec.loader.exec_module(context)
with patch.dict(sys.modules, {'materialize_context': context}):
    spec = importlib.util.spec_from_file_location('clean_context', ROOT / 'materialize_clean_context.py')
    clean = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(clean)


def archive(extra=None):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w:gz') as handle:
        rows = [('source/LICENSE', b'license'), ('source/freqtrade/main.py', b'code'),
                ('source/private.env', b'never include'), ('source/tests/private.sqlite', b'never include')]
        for name, content in rows:
            member = tarfile.TarInfo(name)
            member.size = len(content)
            handle.addfile(member, io.BytesIO(content))
        member = tarfile.TarInfo('source/ft_client/LICENSE')
        member.type = tarfile.SYMTYPE
        member.linkname = '../LICENSE'
        handle.addfile(member)
        if extra:
            handle.addfile(extra)
    return stream.getvalue()


class CleanOwnerSource(unittest.TestCase):
    def test_tampered_download_is_rejected_before_extraction(self):
        with self.assertRaisesRegex(ValueError, 'hash mismatch'):
            clean.source_files(archive())

    def test_only_runtime_source_and_regular_license_bytes_are_materialized(self):
        raw = archive()
        with patch.object(clean, 'UPSTREAM_SHA256', hashlib.sha256(raw).hexdigest()):
            files = clean.source_files(raw)
        self.assertEqual(set(files), {'LICENSE', 'freqtrade/main.py', 'ft_client/LICENSE'})
        self.assertEqual(files['ft_client/LICENSE'], b'license')

    def test_traversal_is_rejected_even_outside_allowlist(self):
        raw = archive(tarfile.TarInfo('source/../../outside'))
        with patch.object(clean, 'UPSTREAM_SHA256', hashlib.sha256(raw).hexdigest()):
            with self.assertRaisesRegex(ValueError, 'Unsafe'):
                clean.source_files(raw)

    def test_arbitrary_links_cannot_enter_source_tree(self):
        member = tarfile.TarInfo('source/freqtrade/escape.py')
        member.type = tarfile.SYMTYPE
        member.linkname = '/private/env'
        raw = archive(member)
        with patch.object(clean, 'UPSTREAM_SHA256', hashlib.sha256(raw).hexdigest()):
            with self.assertRaisesRegex(ValueError, 'Non-regular'):
                clean.source_files(raw)


if __name__ == '__main__':
    unittest.main()
