import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from freqtrade.binana.cache import CacheManager, CacheError, MAX_OBJECT_BYTES

T = 1_800_000_000

class TestCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.m = CacheManager(self.base/'cache', self.base/'state')
    def tearDown(self):
        self.tmp.cleanup()
    def row_path(self, oid):
        with self.m._connect() as c:
            row = c.execute('select * from objects where object_id=?',(oid,)).fetchone()
        return self.m.root/row['category']/row['filename']
    def test_expired_registered_object_only(self):
        a=self.m.put('market_snapshot', b'old', now=T)
        b=self.m.put('market_snapshot', b'new', now=T+1900)
        unknown=self.m.root/'market_snapshot'/'unregistered.cache';unknown.write_text('retain')
        p=self.m.preview('owner',now=T+2000)
        self.assertEqual(p['candidate_files'],1)
        r=self.m.execute(p['token'],'owner',now=T+2001)
        self.assertEqual(r['deleted_files'],1)
        self.assertTrue(self.row_path(b).exists());self.assertTrue(unknown.exists())
    def test_wrong_owner(self):
        self.m.put('market_snapshot',b'x',now=T)
        p=self.m.preview('owner',now=T+2000)
        with self.assertRaises(CacheError):self.m.execute(p['token'],'other',now=T+2001)
    def test_replay_does_not_delete_twice(self):
        self.m.put('market_snapshot',b'x',now=T)
        p=self.m.preview('owner',now=T+2000)
        first=self.m.execute(p['token'],'owner',now=T+2001)
        second=self.m.execute(p['token'],'owner',now=T+2002)
        self.assertEqual(first['deleted_files'],1);self.assertTrue(second['replay'])
    def test_expired_confirmation(self):
        p=self.m.preview('owner',now=T)
        with self.assertRaises(CacheError):self.m.execute(p['token'],'owner',now=T+121)
    def test_symlink_file_skipped(self):
        oid=self.m.put('market_snapshot',b'x',now=T);p=self.row_path(oid)
        target=self.base/'trades.sqlite';target.write_text('important')
        p.unlink();p.symlink_to(target)
        self.assertEqual(self.m.sweep(now=T+2000)['deleted_files'],0)
        self.assertEqual(target.read_text(),'important')
    def test_hardlink_skipped(self):
        oid=self.m.put('market_snapshot',b'x',now=T);p=self.row_path(oid)
        os.link(p,self.base/'important')
        self.assertEqual(self.m.sweep(now=T+2000)['deleted_files'],0)
    def test_changed_file_skipped(self):
        oid=self.m.put('market_snapshot',b'x',now=T);path=self.row_path(oid)
        p=self.m.preview('owner',now=T+2000);path.write_text('changed')
        r=self.m.execute(p['token'],'owner',now=T+2001)
        self.assertEqual(r['deleted_files'],0);self.assertEqual(r['skipped_changed'],1)
    def test_new_pin_after_preview(self):
        oid=self.m.put('market_snapshot',b'x',now=T)
        p=self.m.preview('owner',now=T+2000);self.m.pin(oid,T+10000)
        self.assertEqual(self.m.execute(p['token'],'owner',now=T+2001)['deleted_files'],0)
    def test_leased_reader_blocks_automatic_cleaner(self):
        oid=self.m.put('market_snapshot',b'payload',now=T)
        with self.m.read(oid) as data:
            self.assertEqual(data,b'payload')
            self.assertTrue(self.m.sweep(now=T+2000)['busy'])
        self.assertEqual(self.m.sweep(now=T+2001)['deleted_files'],1)
    def test_protected_sibling_directories(self):
        for name in ['bitcoin','orders','backtests','registry','database']:
            d=self.base/name;d.mkdir();(d/'x').write_text('persist')
        self.m.sweep(now=T+2000)
        self.assertTrue(all((self.base/n/'x').exists() for n in ['bitcoin','orders','backtests','registry','database']))
    def test_rejects_root_symlink(self):
        target=self.base/'real';target.mkdir();p=self.base/'link';p.symlink_to(target,target_is_directory=True)
        with self.assertRaises(CacheError):CacheManager(p,self.base/'state2')
    def test_rejects_symlink_ancestor(self):
        p=self.base/'link';p.symlink_to(self.base/'cache',target_is_directory=True)
        with self.assertRaises(CacheError):CacheManager(p/'sub',self.base/'state2')
    def test_rejects_overlapping_roots(self):
        with self.assertRaises(CacheError):CacheManager(self.base/'cache',self.base/'cache'/'db')
    def test_rejects_unmarked_nonempty_root(self):
        d=self.base/'valuable';d.mkdir();(d/'trades').write_text('x')
        with self.assertRaises(CacheError):CacheManager(d,self.base/'state3')
    def test_unrecognized_category(self):
        for name in ['../orders','trades','python','../bitcoin']:
            with self.assertRaises(CacheError):self.m.put(name,b'x',now=T)
    def test_size_bound(self):
        with self.assertRaises(CacheError):self.m.put('market_snapshot',b'x'*(MAX_OBJECT_BYTES+1),now=T)
    def test_quota_does_not_evict_unexpired(self):
        with patch('freqtrade.binana.cache.CATEGORIES',{'market_snapshot':(900,5)}):
            a=self.m.put('market_snapshot',b'1234',now=T)
            with self.assertRaises(CacheError):self.m.put('market_snapshot',b'12',now=T)
            self.assertTrue(self.row_path(a).exists())
    def test_preview_is_non_destructive(self):
        a=self.m.put('market_snapshot',b'x',now=T)
        self.m.preview('owner',now=T+2000)
        self.assertTrue(self.row_path(a).exists())
    def test_category_symlink_not_followed(self):
        a=self.m.put('market_snapshot',b'x',now=T)
        old=self.m.root/'market_snapshot';old.rename(self.m.root/'detached')
        old.symlink_to(self.base,target_is_directory=True)
        self.assertEqual(self.m.sweep(now=T+2000)['deleted_files'],0)
    def test_invalid_clock(self):
        for n in [float('nan'),float('inf'),-1,0]:
            with self.assertRaises(CacheError):self.m.preview('owner',now=n)
    def test_pinned_unexpired(self):
        a=self.m.put('market_snapshot',b'x',now=T);self.m.pin(a,T+10000)
        self.assertEqual(self.m.preview('owner',now=T+2000)['candidate_files'],0)
    def test_content_hash_detects_preserved_stat(self):
        a=self.m.put('market_snapshot',b'x',now=T);p=self.row_path(a);s=p.stat()
        p.write_bytes(b'y');os.utime(p,ns=(s.st_atime_ns,s.st_mtime_ns))
        preview=self.m.preview('owner',now=T+2000)
        self.assertEqual(self.m.execute(preview['token'],'owner',now=T+2001)['deleted_files'],0)
    def test_status(self):
        self.m.put('market_snapshot',b'1234',now=T)
        s=self.m.status(now=T+2000)
        self.assertEqual(s['registered_bytes'],4);self.assertTrue(s['persistent_state_never_deleted'])

if __name__=='__main__':unittest.main()
