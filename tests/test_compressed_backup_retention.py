"""Compressed daily copies must honor retention without touching repair evidence."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock

SCRIPTS=Path(__file__).resolve().parents[1]/'scripts/recovery'


class CompressedBackupRetention(unittest.TestCase):
    def run_main(self,module,root):
        with patch.dict(sys.modules,{'fcntl':Mock(LOCK_EX=2,LOCK_NB=4)}), patch.object(module.os,'geteuid',return_value=0,create=True), patch.object(module.archive,'ROOT',root), patch.object(module.archive,'unmounted'), patch.dict(module.os.environ,{'BACKUP_RETAIN':'2'}):
            module.main()

    def module(self):
        # The deployed helper imports the adjacent archive utility.
        sys.path.insert(0,str(SCRIPTS))
        try:
            spec=importlib.util.spec_from_file_location('backup_compactor',SCRIPTS/'compact_daily_backups.py')
            module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
            return module
        finally:sys.path.pop(0)

    def archive(self, root, name):
        path=root/(name+'.verified.tar.gz');path.write_bytes(b'verified archive fixture')
        receipt={'archive':str(path),'archive_bytes':path.stat().st_size,
            'archive_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
            'verified':True,'duplicate_removed':True,'files':1}
        (root/(name+'.compression-receipt.json')).write_text(json.dumps(receipt))
        return path

    def test_retains_newest_and_never_deletes_named_repair_evidence(self):
        module=self.module()
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);old=self.archive(root,'20261001T000000Z')
            latest=[self.archive(root,name) for name in ('20261002T000000Z','20261003T000000Z')]
            repair=self.archive(root,'repair-20260101T000000Z')
            self.run_main(module,root)
            self.assertFalse(old.exists())
            self.assertTrue(all(p.exists() for p in [*latest,repair]))
            receipt=json.loads((root/'20261001T000000Z.compression-receipt.json').read_text())
            self.assertTrue(receipt['retention_removed'])

    def test_corrupt_retained_copy_prevents_any_deletion(self):
        module=self.module()
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);old=self.archive(root,'20261001T000000Z')
            self.archive(root,'20261002T000000Z')
            newest=self.archive(root,'20261003T000000Z');newest.write_bytes(b'corrupt')
            with self.assertRaises(RuntimeError):self.run_main(module,root)
            self.assertTrue(old.exists())

    def test_missing_verification_receipt_prevents_deletion(self):
        module=self.module()
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);old=self.archive(root,'20261001T000000Z')
            self.archive(root,'20261002T000000Z');self.archive(root,'20261003T000000Z')
            (root/'20261001T000000Z.compression-receipt.json').unlink()
            with self.assertRaises(RuntimeError):self.run_main(module,root)
            self.assertTrue(old.exists())
