"""Offline acceptance gate for the actual final image, not its build stage."""
import ctypes
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import shutil
import sqlite3
import ssl
import tempfile
import unittest
import uuid


class RuntimeSecurityTests(unittest.TestCase):
    def test_patched_sqlite_preserves_fts_and_transaction_recovery(self):
        self.assertGreaterEqual(sqlite3.sqlite_version_info, (3, 54, 1))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'offline.db'
            with sqlite3.connect(path) as writer:
                self.assertEqual(writer.execute('pragma journal_mode=WAL').fetchone()[0], 'wal')
                writer.execute('create table state (id integer primary key, value text)')
                writer.execute("insert into state values (1, 'protected')")
                writer.execute('create virtual table search using fts5(body)')
                writer.executemany('insert into search(rowid, body) values (?, ?)',
                                   [(-1, 'protection plan'), (1, 'protected order')])
                writer.commit()
                self.assertEqual(writer.execute(
                    "select count(*) from search where search match 'protect*'"
                ).fetchone()[0], 2)
                writer.execute("update state set value='uncommitted' where id=1")
                writer.rollback()
            with sqlite3.connect(path) as reader:
                self.assertEqual(reader.execute('select value from state').fetchone()[0], 'protected')
                self.assertEqual(reader.execute('pragma integrity_check').fetchone()[0], 'ok')
                options = {row[0] for row in reader.execute('pragma compile_options')}
                self.assertTrue({'ENABLE_FTS5', 'ENABLE_RTREE', 'ENABLE_SESSION',
                                 'ENABLE_PREUPDATE_HOOK', 'THREADSAFE=1'} <= options)

    def test_python_native_dependencies_and_certificate_store(self):
        for name in ('bz2', 'lzma', 'zlib', '_ctypes', 'dbm.gnu', 'dbm.ndbm',
                     'readline', '_uuid', 'curses', 'curses.panel', '_ssl', '_hashlib'):
            with self.subTest(module=name):
                importlib.import_module(name)
        curses = ctypes.CDLL('libncursesw.so.6')
        curses.curses_version.restype = ctypes.c_char_p
        self.assertTrue(curses.curses_version().decode().startswith('ncurses 6.6.'))
        self.assertEqual(len(uuid.uuid4().bytes), 16)
        self.assertGreater(ssl.create_default_context().cert_store_stats()['x509_ca'], 0)
        # Exercise the native UUID library without interfaces, profiles or sockets.
        output = ctypes.create_string_buffer(16)
        library = ctypes.CDLL('libuuid.so.1')
        library.uuid_generate_random(output)
        self.assertEqual(uuid.UUID(bytes=output.raw).version, 4)

    def test_installers_shells_and_admin_programs_are_absent(self):
        for name in ('pip', 'pip3', 'apt', 'apt-get', 'dpkg', 'gcc', 'cc', 'make',
                     'sh', 'bash', 'perl', 'mount', 'umount', 'infocmp', 'setfacl'):
            with self.subTest(program=name):
                self.assertIsNone(shutil.which(name))
                for directory in ('/bin', '/sbin', '/usr/bin', '/usr/sbin', '/usr/local/bin'):
                    self.assertFalse((Path(directory) / name).exists())
        self.assertIsNone(importlib.util.find_spec('pip'))

    def test_native_source_and_binary_provenance(self):
        manifest = json.loads(Path('/usr/local/share/binana-native-runtime.json').read_text())
        self.assertEqual({item['name']: item['version'] for item in manifest['sources']},
                         {'sqlite': '3.54.1', 'ncurses': '6.6', 'util-linux': '2.42.4'})
        for relative, expected in manifest['libraries'].items():
            path = Path('/usr/local/lib') / relative
            with self.subTest(library=relative):
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), expected)


if __name__ == '__main__':
    unittest.main(verbosity=2)
