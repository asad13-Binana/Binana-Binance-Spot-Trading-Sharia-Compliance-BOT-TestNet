"""Offline recovery checks. No restore into runtime state, cloud access or secrets."""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3


METADATA = ('RELEASE_VERSION', 'RELEASE_MODE', 'RELEASE_SHA256.txt',
            'RELEASE_MANIFEST.json', '.git-commit')


def file_digest(path):
    value = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def sqlite_snapshot(source: Path, target: Path) -> None:
    if not source.is_file() or source.is_symlink() or target.exists() or target.is_symlink():
        raise ValueError('snapshot requires a regular source and new destination')
    with closing(sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True,
                                 timeout=10)) as source_db:
        with closing(sqlite3.connect(target, timeout=10)) as target_db:
            source_db.backup(target_db)
            target_db.commit()
            # Only the COPY changes journal mode; never change the running DB.
            target_db.execute('PRAGMA journal_mode=DELETE')
            if target_db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                raise ValueError('SQLite backup failed integrity check')
    for suffix in ('-wal', '-shm', '-journal'):
        if Path(str(target) + suffix).exists():
            raise ValueError('SQLite snapshot still requires an auxiliary file')


def release_binding(root: Path, mode: str) -> str:
    for name in METADATA:
        if not (root / name).is_file() or (root / name).is_symlink():
            raise ValueError('missing or unsafe release metadata: ' + name)
    manifest_bytes = (root / 'RELEASE_MANIFEST.json').read_bytes()
    digest = hashlib.sha256(manifest_bytes).hexdigest()
    line = (root / 'RELEASE_SHA256.txt').read_text(encoding='utf-8').strip().split()
    if line != [digest, 'RELEASE_MANIFEST.json']:
        raise ValueError('release manifest SHA-256 binding mismatch')
    manifest = json.loads(manifest_bytes)
    actual_mode = (root / 'RELEASE_MODE').read_text(encoding='utf-8').strip()
    version = (root / 'RELEASE_VERSION').read_text(encoding='utf-8').strip()
    commit = (root / '.git-commit').read_text(encoding='utf-8').strip()
    if actual_mode != mode or manifest.get('package_mode') != mode:
        raise ValueError('backup belongs to a different package mode')
    if not version or manifest.get('release') != version + '-' + mode.upper():
        raise ValueError('release version binding mismatch')
    if not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise ValueError('release commit is missing or invalid')
    for name in ('RELEASE_VERSION', 'RELEASE_MODE'):
        meta = manifest.get('files', {}).get(name, {})
        data = (root / name).read_bytes()
        if meta.get('sha256') != hashlib.sha256(data).hexdigest() or meta.get('size') != len(data):
            raise ValueError('release metadata differs from manifest: ' + name)
    return digest


def validate_backup(root: Path, mode: str) -> str:
    if root.is_symlink() or not root.is_dir():
        raise ValueError('unsafe backup root')
    root = root.resolve()
    files = set()
    for path in root.rglob('*'):
        if path.is_symlink() or not (path.is_dir() or path.is_file()):
            raise ValueError('backup contains a link or special file')
        if path.is_file() and path != root / 'SHA256SUMS':
            files.add(path.relative_to(root).as_posix())
    checked = set()
    for line in (root / 'SHA256SUMS').read_text(encoding='utf-8').splitlines():
        match = re.fullmatch(r'([0-9a-f]{64})  (?:\./)?([^\r\n]+)', line)
        if not match:
            raise ValueError('invalid SHA256SUMS line')
        digest, relative = match.groups()
        path = root / relative
        if ('\\' in relative or Path(relative).is_absolute() or
                '..' in Path(relative).parts or root not in path.resolve().parents):
            raise ValueError('unsafe SHA256SUMS path')
        if relative in checked or relative not in files:
            raise ValueError('duplicate or missing checksum entry')
        if file_digest(path) != digest:
            raise ValueError('backup checksum mismatch: ' + relative)
        checked.add(relative)
    if not files or checked != files:
        raise ValueError('backup checksum inventory is incomplete')
    digest = release_binding(root / 'release', mode)
    databases = list((root / 'sqlite').rglob('*'))
    primary = [p for p in databases if p.is_file() and p.suffix in {'.sqlite', '.db'}]
    if not primary:
        raise ValueError('no primary SQLite state: not a recovery-ready backup')
    for path in databases:
        if path.is_file() and path not in primary:
            raise ValueError('unexpected auxiliary/non-database file in SQLite snapshot')
    for path in primary:
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)) as con:
            if con.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                raise ValueError('invalid SQLite backup')
    return digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('snapshot', 'release', 'validate'))
    parser.add_argument('source', type=Path)
    parser.add_argument('target_or_mode')
    args = parser.parse_args()
    if args.operation == 'snapshot':
        sqlite_snapshot(args.source, Path(args.target_or_mode))
    elif args.operation == 'release':
        print(release_binding(args.source, args.target_or_mode))
    else:
        print(validate_backup(args.source, args.target_or_mode))


if __name__ == '__main__':
    main()
