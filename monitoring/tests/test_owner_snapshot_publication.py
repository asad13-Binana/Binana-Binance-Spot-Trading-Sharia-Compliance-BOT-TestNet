"""Privileged publication exposes only a bounded, derived owner status."""
import json
import os
from pathlib import Path

import pytest

from monitoring import snapshot


pytestmark = pytest.mark.skipif(os.name != 'posix', reason='Linux publication contract')


@pytest.fixture(autouse=True)
def ownership_syscall_for_unprivileged_runner(monkeypatch):
    # Production and the root integration run must perform real root chown.
    # Ordinary CI cannot grant uid 0: assert that exact requested identity,
    # then exercise the remaining real filesystem operations as the runner.
    if os.geteuid() != 0:
        real_fchown = os.fchown

        def checked_fchown(descriptor, uid, gid):
            assert uid == 0
            assert gid == os.fstat(descriptor).st_gid
            real_fchown(descriptor, os.geteuid(), gid)

        monkeypatch.setattr(snapshot.os, 'fchown', checked_fchown)


@pytest.fixture
def source(tmp_path):
    root = tmp_path / 'shared'
    path = root / 'freqtrade/runtime/owner_readiness.json'
    path.parent.mkdir(parents=True)
    (root / 'runtime').mkdir()
    payload = {'order_owner': 'FREQTRADE', 'execution_environment': 'TESTNET',
               'generated_at': 1000.0, 'valid_until': 1030.0,
               'release_approved': True, 'blockers': [],
               'account_key_sha256': 'private-account-identity',
               'private_extra': {'secret': 'must-not-be-copied'}}
    path.write_text(json.dumps(payload))
    path.chmod(0o600)
    return root, path, payload


def read_published(root):
    return json.loads((root / 'runtime/owner_monitor_readiness.json').read_text())


def test_publishes_only_allowed_fields_without_relaxing_source(source):
    root, path, payload = source
    original = path.read_bytes()
    snapshot.publish_owner_readiness(root)
    result = read_published(root)
    assert result == {k: payload[k] for k in (
        'order_owner', 'execution_environment', 'generated_at', 'valid_until',
        'release_approved', 'blockers')}
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.read_bytes() == original
    assert (root / 'runtime/owner_monitor_readiness.json').stat().st_mode & 0o777 == 0o640
    assert (root / 'runtime/owner_monitor_readiness.json').stat().st_uid == os.geteuid()


def test_publication_never_renews_the_source_validity(source):
    root, _, _ = source
    snapshot.publish_owner_readiness(root)
    assert read_published(root)['valid_until'] == 1030.0


def test_missing_source_invalidates_previous_good_snapshot(source):
    root, path, _ = source
    snapshot.publish_owner_readiness(root)
    path.unlink()
    snapshot.publish_owner_readiness(root)
    assert read_published(root) == {'available': False, 'reason': 'owner_readiness_unavailable'}


@pytest.mark.parametrize('link_kind', ['symlink', 'hardlink', 'parent_symlink'])
def test_links_are_not_read_or_republished(source, link_kind):
    root, path, _ = source
    original = path.read_bytes()
    path.unlink()
    private = root.parent / 'private.json'
    private.write_bytes(original)
    private.chmod(0o600)
    if link_kind == 'symlink':
        path.symlink_to(private)
    elif link_kind == 'hardlink':
        os.link(private, path)
    else:
        directory = path.parent
        directory.rmdir()
        elsewhere = root.parent / 'elsewhere'
        elsewhere.mkdir()
        (elsewhere / path.name).write_bytes(original)
        directory.symlink_to(elsewhere, target_is_directory=True)
    snapshot.publish_owner_readiness(root)
    assert read_published(root)['available'] is False
    assert private.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('bad', [
    {'execution_environment': 'testnet'}, {'valid_until': False},
    {'generated_at': float('nan')}, {'valid_until': float('inf')},
    {'valid_until': 1000}, {'valid_until': 1060.01},
    {'blockers': 'BLOCKED'}, {'blockers': ['A'] * 101},
    {'blockers': ['A' * 161]}, {'blockers': ['line\nbreak']}, {'blockers': [True]},
])
def test_remaining_type_and_capacity_bounds_fail_closed(source, bad):
    root, path, payload = source
    payload.update(bad)
    path.write_text(json.dumps(payload))
    snapshot.publish_owner_readiness(root)
    assert read_published(root) == {'available': False, 'reason': 'owner_readiness_unavailable'}


def test_source_owned_by_an_unrelated_user_is_rejected(source):
    if os.geteuid() != 0:
        pytest.skip('owner-identity check requires the root helper test container')
    root, path, _ = source
    os.chown(path, 12345, path.stat().st_gid)
    snapshot.publish_owner_readiness(root)
    assert read_published(root)['available'] is False


def test_output_symlink_is_replaced_without_following_it(source):
    root, _, _ = source
    private = root.parent / 'private-output.json'
    private.write_text('private')
    target = root / 'runtime/owner_monitor_readiness.json'
    target.symlink_to(private)
    snapshot.publish_owner_readiness(root)
    assert not target.is_symlink()
    assert private.read_text() == 'private'


def test_output_parent_symlink_cannot_redirect_publication(source):
    root, _, _ = source
    output = root / 'runtime'
    output.rmdir()
    elsewhere = root.parent / 'outside'
    elsewhere.mkdir()
    output.symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(OSError):
        snapshot.publish_owner_readiness(root)
    assert list(elsewhere.iterdir()) == []


def test_readiness_only_cli_never_collects_docker_or_changes_source_permissions(monkeypatch):
    calls = []
    monkeypatch.setattr(snapshot, 'publish_owner_readiness', lambda root: calls.append(root))

    def forbidden(*args, **kwargs):
        pytest.fail('readiness-only mode invoked another privileged operation')

    monkeypatch.setattr(snapshot, 'publish_monitor_permissions', forbidden)
    monkeypatch.setattr(snapshot, 'collect', forbidden)
    monkeypatch.setattr(snapshot, 'atomic_write', forbidden)
    assert snapshot.main(['--owner-readiness-only']) == 0
    assert calls == [snapshot.SHARED_ROOT]


@pytest.mark.parametrize('present', ['none', 'source', 'previous_derived'])
def test_default_cli_only_publishes_readiness_for_an_existing_owner_artifact(
        tmp_path, monkeypatch, present):
    root = tmp_path / 'shared'
    (root / 'runtime').mkdir(parents=True)
    if present != 'none':
        path = root / ('freqtrade/runtime/owner_readiness.json' if present == 'source'
                       else 'runtime/owner_monitor_readiness.json')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{}')
    calls = []
    monkeypatch.setattr(snapshot, 'SHARED_ROOT', root)
    monkeypatch.setattr(snapshot, 'DEFAULT_OUTPUT', root / 'runtime/container_status.json')
    monkeypatch.setattr(snapshot, 'publish_monitor_permissions', lambda: [])
    monkeypatch.setattr(snapshot, 'publish_owner_readiness', lambda root: calls.append('owner'))

    def collect():
        calls.append('docker')
        return {}

    monkeypatch.setattr(snapshot, 'collect', collect)
    monkeypatch.setattr(snapshot, 'atomic_write', lambda *args: None)
    assert snapshot.main([]) == 0
    assert calls == (['docker'] if present == 'none' else ['owner', 'docker'])


@pytest.mark.parametrize('bad', [
    {'release_approved': 'true'}, {'generated_at': True},
    {'blockers': ['https://private/token']}, {'order_owner': 'SIDECAR'},
    {'valid_until': 9999999}, {'private_extra': 'x' * 140000},
])
def test_invalid_or_oversized_source_is_not_published_as_valid(source, bad):
    root, path, payload = source
    payload.update(bad)
    path.write_text(json.dumps(payload))
    snapshot.publish_owner_readiness(root)
    assert read_published(root)['available'] is False
