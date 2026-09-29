"""Executable recovery checks against real temporary SQLite/WAL files."""
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest
import yaml

from scripts.backup_integrity import release_binding, sqlite_snapshot, validate_backup

ROOT = Path(__file__).resolve().parents[1]


def checksums(root):
    rows = [hashlib.sha256(p.read_bytes()).hexdigest() + '  ./' + p.relative_to(root).as_posix()
            for p in sorted(root.rglob('*')) if p.is_file() and p.name != 'SHA256SUMS']
    (root / 'SHA256SUMS').write_text('\n'.join(rows) + '\n', encoding='utf-8')


@pytest.fixture
def recovery(tmp_path):
    root = tmp_path / '20260830T010203Z'
    release = root / 'release'
    release.mkdir(parents=True)
    (root / 'sqlite').mkdir()
    (root / 'state').mkdir()
    for name in ('RELEASE_VERSION', 'RELEASE_MODE', 'RELEASE_MANIFEST.json', 'RELEASE_SHA256.txt'):
        (release / name).write_bytes((ROOT / name).read_bytes())
    (release / '.git-commit').write_text('a' * 40 + '\n')
    with sqlite3.connect(root / 'sqlite/state.sqlite') as con:
        con.execute('CREATE TABLE safety(reason TEXT)')
        con.execute("INSERT INTO safety VALUES('paused')")
    con.close()
    checksums(root)
    return root, (ROOT / 'RELEASE_MODE').read_text().strip()


def test_online_wal_copy_is_standalone_and_does_not_change_source(tmp_path):
    source, target = tmp_path / 'source.db', tmp_path / 'target.db'
    con = sqlite3.connect(source)
    try:
        con.execute('PRAGMA journal_mode=WAL')
        con.execute('CREATE TABLE safety(value TEXT)')
        con.execute("INSERT INTO safety VALUES('pause survives')")
        con.commit()
        sqlite_snapshot(source, target)
        assert con.execute('PRAGMA journal_mode').fetchone() == ('wal',)
        assert not Path(str(target) + '-wal').exists()
        assert not Path(str(target) + '-shm').exists()
        check = sqlite3.connect(target)
        try:
            assert check.execute('SELECT value FROM safety').fetchone() == ('pause survives',)
            assert check.execute('PRAGMA journal_mode').fetchone() == ('delete',)
        finally:
            check.close()
        with pytest.raises(ValueError):
            sqlite_snapshot(source, target)
    finally:
        con.close()


def test_backup_release_binding_and_exact_inventory(recovery):
    root, mode = recovery
    assert len(validate_backup(root, mode)) == 64
    with pytest.raises(ValueError, match='different package'):
        validate_backup(root, 'live' if mode == 'testnet' else 'testnet')
    (root / 'state/unmanifested.json').write_text('{}')
    with pytest.raises(ValueError, match='incomplete'):
        validate_backup(root, mode)


@pytest.mark.parametrize('field', ['RELEASE_MODE', 'RELEASE_VERSION', '.git-commit',
                                   'RELEASE_SHA256.txt', 'RELEASE_MANIFEST.json'])
def test_missing_release_is_not_recovery_ready(recovery, field):
    root, mode = recovery
    (root / 'release' / field).unlink()
    checksums(root)
    with pytest.raises(ValueError, match='metadata'):
        validate_backup(root, mode)


def test_manifest_tamper_and_commit_tamper(recovery):
    root, mode = recovery
    (root / 'release/.git-commit').write_text('not-a-commit')
    with pytest.raises(ValueError, match='commit'):
        release_binding(root / 'release', mode)
    (root / 'release/RELEASE_MANIFEST.json').write_text('{}')
    with pytest.raises(ValueError, match='binding'):
        release_binding(root / 'release', mode)


@pytest.mark.parametrize('name', ['state.sqlite-wal', 'state.sqlite-shm', 'extra.txt'])
def test_auxiliary_files_are_rejected_not_opened_as_databases(recovery, name):
    root, mode = recovery
    (root / 'sqlite' / name).write_bytes(b'not a standalone database')
    checksums(root)
    with pytest.raises(ValueError, match='auxiliary'):
        validate_backup(root, mode)


def test_no_database_no_vacuous_pass(recovery):
    root, mode = recovery
    (root / 'sqlite/state.sqlite').unlink()
    checksums(root)
    with pytest.raises(ValueError, match='no primary'):
        validate_backup(root, mode)


def test_checksum_duplicates_traversal_and_corruption(recovery):
    root, mode = recovery
    manifest = root / 'SHA256SUMS'
    original = manifest.read_text()
    manifest.write_text(original + original)
    with pytest.raises(ValueError, match='duplicate'):
        validate_backup(root, mode)
    manifest.write_text('a' * 64 + '  ../outside\n')
    with pytest.raises(ValueError, match='unsafe'):
        validate_backup(root, mode)
    manifest.write_text(original)
    (root / 'sqlite/state.sqlite').write_bytes(b'corrupt')
    with pytest.raises(ValueError, match='checksum'):
        validate_backup(root, mode)


def test_tmpfs_is_one_valid_mount():
    compose = yaml.safe_load((ROOT / 'docker-compose.yml').read_text())
    for service in compose['services'].values():
        for mount in service.get('tmpfs', []):
            assert mount.startswith('/'), mount
            assert mount != 'mode=1777'


def test_backup_timestamp_discovery_and_logging_do_not_skip_safety():
    for file in ('backup_state.sh', 'offhost_backup.sh'):
        text = (ROOT / 'deploy' / file).read_text()
        assert '20??????T??????Z' in text
        assert '20????????T??????Z' not in text
    guard = (ROOT / 'deploy/disk_guard.sh').read_text()
    assert guard.index('sign_envelope(') < guard.index('disk_status.json')
    assert '0 <= time.time() - path.stat().st_mtime <= 30' in guard
    assert 'user.CRITICAL' not in guard


def test_host_backup_excludes_rotating_sidecar_backup_snapshots():
    script = (ROOT / 'deploy/backup_state.sh').read_text(encoding='utf-8')
    assert '-path "$PERSIST/runtime/db_backups"' in script
    assert '-path "$PERSIST/runtime/db_backups/*"' in script
    assert '-prune -o' in script
