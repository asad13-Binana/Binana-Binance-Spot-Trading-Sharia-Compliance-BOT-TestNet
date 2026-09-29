"""Conservative, namespace-owned disposable cache with preview/confirm cleanup.

Only this module's completed, registered cache objects are deletable. Unknown
files, symlinks, hardlinks, modified files, live leases and persistent state are
never deletion candidates. No path comes from Telegram input. Linux/POSIX only.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import time
from typing import Iterator

CATEGORIES = {
    'market_snapshot': (15 * 60, 32 * 1024 * 1024),
    'http_response': (60 * 60, 64 * 1024 * 1024),
    'temporary_download': (24 * 60 * 60, 128 * 1024 * 1024),
    'test_artifact_cache': (7 * 24 * 60 * 60, 32 * 1024 * 1024),
}
OBJECT_RE = re.compile(r'[0-9a-f]{32}\.cache\Z')
TOKEN_RE = re.compile(r'[0-9a-f]{32}\Z')
MAX_OBJECT_BYTES = 16 * 1024 * 1024
MAX_CATALOG_OBJECTS = 5000
MAX_BATCH_FILES = 100
MAX_BATCH_BYTES = 32 * 1024 * 1024
PLAN_TTL = 120


class CacheError(RuntimeError):
    pass


def _real_directory(path: Path) -> Path:
    path = path.absolute()
    # Reject symlink ancestors, including one passed as the root itself.
    for p in reversed((path, *path.parents)):
        try:
            s = p.lstat()
        except FileNotFoundError:
            p.mkdir(mode=0o700)
            s = p.lstat()
        if not stat.S_ISDIR(s.st_mode) or stat.S_ISLNK(s.st_mode):
            raise CacheError('cache path must have real directory ancestors')
    return path


def _clock(value: float | None) -> float:
    result = time.time() if value is None else float(value)
    if not math.isfinite(result) or result <= 0:
        raise CacheError('invalid clock')
    return result


class CacheManager:
    """One dedicated cache root plus a separate persistent catalog directory."""
    def __init__(self, root: str | Path, catalog_dir: str | Path):
        self.root = _real_directory(Path(root))
        self.catalog_dir = _real_directory(Path(catalog_dir))
        if (self.root == self.catalog_dir or self.root in self.catalog_dir.parents
                or self.catalog_dir in self.root.parents):
            raise CacheError('cache and catalog must be disjoint directories')
        self.db = self.catalog_dir / 'disposable_cache.sqlite'
        if self.db.is_symlink():
            raise CacheError('catalog symlink refused')
        with self._lock() as _:
            marker = self.root / 'BINANA_CACHE_NAMESPACE'
            expected = b'BINANA_DISPOSABLE_CACHE_V1\n'
            if marker.exists() or marker.is_symlink():
                if marker.is_symlink() or marker.read_bytes() != expected:
                    raise CacheError('cache namespace marker mismatch')
            else:
                if any(self.root.iterdir()):
                    raise CacheError('refuse to adopt a nonempty unmarked cache root')
                fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                try:
                    os.write(fd, expected)
                    os.fsync(fd)
                finally:
                    os.close(fd)
            for category in CATEGORIES:
                _real_directory(self.root / category)
            with self._connect() as con:
                con.executescript('''
                CREATE TABLE IF NOT EXISTS objects(
                    object_id TEXT PRIMARY KEY, category TEXT NOT NULL,
                    filename TEXT NOT NULL, dev INTEGER NOT NULL, inode INTEGER NOT NULL,
                    size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, sha256 TEXT NOT NULL,
                    created REAL NOT NULL, expires REAL NOT NULL, pin_until REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS plans(
                    token TEXT PRIMARY KEY, owner TEXT NOT NULL, created REAL NOT NULL,
                    expires REAL NOT NULL, payload TEXT NOT NULL, result TEXT
                );
                ''')

    @contextlib.contextmanager
    def _connect(self):
        if self.db.is_symlink():
            raise CacheError('catalog symlink refused')
        con = sqlite3.connect(self.db, timeout=2)
        con.row_factory = sqlite3.Row
        try:
            con.execute('PRAGMA synchronous=FULL')
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    @contextlib.contextmanager
    def _lock(self, shared=False, blocking=True):
        path = self.catalog_dir / 'cache.lock'
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            s = os.fstat(fd)
            if not stat.S_ISREG(s.st_mode) or s.st_nlink != 1:
                raise CacheError('invalid lock file')
            flags = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
            if not blocking:
                flags |= fcntl.LOCK_NB
            fcntl.flock(fd, flags)
            yield fd
        finally:
            os.close(fd)

    @contextlib.contextmanager
    def _category_fd(self, category: str):
        if category not in CATEGORIES:
            raise CacheError('category not registered')
        root_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fd = os.open(category, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
            try:
                yield fd
            finally:
                os.close(fd)
        finally:
            os.close(root_fd)

    def put(self, category: str, data: bytes, *, now: float | None = None) -> str:
        """Publish a completed disposable object; producers must use this API."""
        now = _clock(now)
        if category not in CATEGORIES or not isinstance(data, bytes):
            raise CacheError('unsupported category or content type')
        if len(data) > MAX_OBJECT_BYTES:
            raise CacheError('object exceeds bounded cache size')
        ttl, quota = CATEGORIES[category]
        with self._lock(), self._connect() as con, self._category_fd(category) as dirfd:
            used = con.execute('SELECT COALESCE(SUM(size),0) FROM objects WHERE category=?', (category,)).fetchone()[0]
            count = con.execute('SELECT COUNT(*) FROM objects').fetchone()[0]
            if used + len(data) > quota or count >= MAX_CATALOG_OBJECTS:
                raise CacheError('cache quota reached; required state must not be evicted')
            oid = secrets.token_hex(16)
            filename = oid + '.cache'
            fd = os.open(filename, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=dirfd)
            try:
                view = memoryview(data)
                while view:
                    written = os.write(fd, view)
                    if written <= 0:
                        raise CacheError('incomplete cache write')
                    view = view[written:]
                os.fsync(fd)
                s = os.fstat(fd)
                con.execute('INSERT INTO objects VALUES(?,?,?,?,?,?,?,?,?,?,?)', (
                    oid, category, filename, s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns,
                    hashlib.sha256(data).hexdigest(), now, now + ttl, 0))
                os.fsync(dirfd)
                con.commit()
            except BaseException:
                # This process exclusively created this name under the cache lock.
                os.unlink(filename, dir_fd=dirfd)
                raise
            finally:
                os.close(fd)
        return oid

    def pin(self, object_id: str, until: float) -> None:
        until = _clock(until)
        with self._lock(), self._connect() as con:
            if con.execute('UPDATE objects SET pin_until=? WHERE object_id=?', (until, object_id)).rowcount != 1:
                raise CacheError('unknown cache object')

    @staticmethod
    def _identity(s: os.stat_result) -> tuple:
        return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns

    def _verified(self, dirfd: int, row: dict, *, check_hash=True) -> bool:
        filename = str(row['filename'])
        if not OBJECT_RE.fullmatch(filename):
            return False
        try:
            s = os.stat(filename, dir_fd=dirfd, follow_symlinks=False)
            expected = (row['dev'], row['inode'], row['size'], row['mtime_ns'])
            if (not stat.S_ISREG(s.st_mode) or s.st_nlink != 1
                    or self._identity(s) != expected or s.st_size > MAX_OBJECT_BYTES):
                return False
            if not check_hash:
                return True
            fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dirfd)
            try:
                if self._identity(os.fstat(fd)) != expected:
                    return False
                digest = hashlib.sha256()
                while chunk := os.read(fd, 65536):
                    digest.update(chunk)
                return hmac.compare_digest(digest.hexdigest(), row['sha256'])
            finally:
                os.close(fd)
        except OSError:
            return False

    @contextlib.contextmanager
    def read(self, object_id: str) -> Iterator[bytes]:
        """Hold a read lease until the consumer exits the context."""
        with self._lock(shared=True), self._connect() as con:
            row = con.execute('SELECT * FROM objects WHERE object_id=?', (object_id,)).fetchone()
            if row is None:
                raise CacheError('unknown cache object')
            row = dict(row)
            with self._category_fd(row['category']) as dirfd:
                if not self._verified(dirfd, row):
                    raise CacheError('cache identity/content changed')
                fd = os.open(row['filename'], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dirfd)
                try:
                    with os.fdopen(fd, 'rb', closefd=False) as f:
                        yield f.read(MAX_OBJECT_BYTES + 1)
                finally:
                    os.close(fd)

    def _preview(self, con, owner: str, now: float) -> dict:
        rows = con.execute('SELECT * FROM objects WHERE expires<=? AND pin_until<=? ORDER BY expires,object_id LIMIT ?',
                           (now, now, MAX_CATALOG_OBJECTS)).fetchall()
        candidates, skipped, total = [], 0, 0
        for item in rows:
            row = dict(item)
            try:
                with self._category_fd(row['category']) as fd:
                    valid = self._verified(fd, row, check_hash=False)
            except (OSError, CacheError):
                valid = False
            if not valid:
                skipped += 1
                continue
            if len(candidates) >= MAX_BATCH_FILES or total + row['size'] > MAX_BATCH_BYTES:
                break
            candidates.append(row)
            total += row['size']
        token = secrets.token_hex(16)
        con.execute('DELETE FROM plans WHERE expires<?', (now - 3600,))
        con.execute('DELETE FROM plans WHERE token IN (SELECT token FROM plans ORDER BY created DESC LIMIT -1 OFFSET 499)')
        con.execute('INSERT INTO plans VALUES(?,?,?,?,?,NULL)',
                    (token, str(owner), now, now + PLAN_TTL, json.dumps(candidates, sort_keys=True)))
        return {'token': token, 'candidate_files': len(candidates), 'candidate_bytes': total,
                'skipped_unsafe': skipped, 'expires_at': now + PLAN_TTL,
                'scope': 'registered expired BINANA disposable cache only'}

    def preview(self, owner: str, *, now: float | None = None) -> dict:
        now = _clock(now)
        if not str(owner):
            raise CacheError('owner required')
        with self._lock(), self._connect() as con:
            return self._preview(con, str(owner), now)

    def _execute(self, con, token: str, owner: str, now: float) -> dict:
        plan = con.execute('SELECT * FROM plans WHERE token=?', (token,)).fetchone()
        if plan is None or not hmac.compare_digest(str(plan['owner']), str(owner)):
            raise CacheError('unknown or unauthorized cleanup plan')
        if plan['result'] is not None:
            return dict(json.loads(plan['result']), replay=True)
        if now > plan['expires'] or now < plan['created']:
            raise CacheError('cleanup confirmation expired')
        result = {'deleted_files': 0, 'deleted_bytes': 0, 'skipped_changed': 0, 'errors': 0,
                  'scope': 'registered expired BINANA disposable cache only'}
        for approved in json.loads(plan['payload']):
            item = con.execute('SELECT * FROM objects WHERE object_id=?', (approved['object_id'],)).fetchone()
            if item is None:
                result['skipped_changed'] += 1
                continue
            current = dict(item)
            if current != approved or current['expires'] > now or current['pin_until'] > now:
                result['skipped_changed'] += 1
                continue
            try:
                with self._category_fd(current['category']) as fd:
                    if not self._verified(fd, current):
                        result['skipped_changed'] += 1
                        continue
                    os.unlink(current['filename'], dir_fd=fd)
                    os.fsync(fd)
                    con.execute('DELETE FROM objects WHERE object_id=?', (current['object_id'],))
                    result['deleted_files'] += 1
                    result['deleted_bytes'] += current['size']
            except (OSError, CacheError):
                result['errors'] += 1
        con.execute('UPDATE plans SET result=? WHERE token=?', (json.dumps(result, sort_keys=True), token))
        return result

    def execute(self, token: str, owner: str, *, now: float | None = None) -> dict:
        now = _clock(now)
        if not TOKEN_RE.fullmatch(str(token)):
            raise CacheError('invalid cleanup token')
        with self._lock(), self._connect() as con:
            return self._execute(con, token, str(owner), now)

    def sweep(self, *, now: float | None = None) -> dict:
        """Automatic expiry uses the identical conservative deletion contract."""
        now = _clock(now)
        try:
            with self._lock(blocking=False), self._connect() as con:
                plan = self._preview(con, 'automatic-retention', now)
                return self._execute(con, plan['token'], 'automatic-retention', now)
        except BlockingIOError:
            return {'deleted_files': 0, 'deleted_bytes': 0, 'busy': True}

    def status(self, *, now: float | None = None) -> dict:
        now = _clock(now)
        with self._lock(shared=True), self._connect() as con:
            rows = [dict(row) for row in con.execute(
                'SELECT category,COUNT(*) AS files,COALESCE(SUM(size),0) AS bytes FROM objects GROUP BY category')]
            expired = con.execute('SELECT COUNT(*) FROM objects WHERE expires<=? AND pin_until<=?', (now, now)).fetchone()[0]
            total = sum(r['bytes'] for r in rows)
            return {'registered_bytes': total, 'registered_files': sum(r['files'] for r in rows),
                    'expired_unpinned_files': expired, 'categories': rows,
                    'max_registered_bytes': sum(q for _, q in CATEGORIES.values()),
                    'persistent_state_never_deleted': True}
