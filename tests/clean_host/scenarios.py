#!/usr/bin/python3
"""Exercise the actual approved-artifact wrapper in a disposable Ubuntu root."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import threading
import time

assert os.geteuid() == 0 and Path('/.binana-clean-host').is_file()
SOURCE = Path(sys.argv[1]).resolve()
MODE = (SOURCE / 'RELEASE_MODE').read_text().strip()
SLUG = 'binana-' + MODE
BOT = 'binanatn' if MODE == 'testnet' else 'binanalive'
MONITOR = 'binanatnmon' if MODE == 'testnet' else 'binanalivemon'
APP = Path('/opt') / SLUG
PRIVATE = Path('/etc') / SLUG
PERSIST = Path('/var/lib') / SLUG / 'shared'
INBOX = PERSIST.parent / 'deploy-inbox'
STATE = Path('/run/fake-host.json')
ENV = dict(os.environ, PATH='/test-bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
           PYTHONDONTWRITEBYTECODE='1')


def run(args, **kwargs):
    return subprocess.run([str(a) for a in args], env=ENV, check=True, **kwargs)


def state(**changes):
    value = json.loads(STATE.read_text())
    value.update(changes)
    STATE.write_text(json.dumps(value))
    return value


def artifact(label):
    target = Path('/build') / label
    shutil.copytree(SOURCE, target, ignore=shutil.ignore_patterns(
        '.git', '__pycache__', '.pytest_cache', '.ruff_cache', '.hypothesis'))
    (target / 'CLEAN_HOST_TEST_LABEL.txt').write_text(label + '\n')
    sha = hashlib.sha1(label.encode(), usedforsecurity=False).hexdigest()
    (target / '.git-commit').write_text(sha + '\n')
    for script in target.rglob('*.sh'):
        script.chmod(0o755)
    run(['/usr/bin/python3', target / 'scripts/build_audit_ledgers.py'])
    run(['/usr/bin/python3', target / 'scripts/build_manifest.py'])
    prefix = 'testnet' if MODE == 'testnet' else 'live-trading'
    archive = INBOX / f'binance-bot-{prefix}-{sha}.tar.gz'
    with tarfile.open(archive, 'w:gz') as bundle:
        bundle.add(target, arcname='binance-bot-' + prefix)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    checksum = Path(str(archive) + '.sha256')
    checksum.write_text(digest + '  ' + archive.name + '\n')
    for path in (archive, checksum):
        path.chmod(0o600)
    return archive, checksum, digest


def deploy(bundle, expect_ok):
    archive, checksum, digest = bundle
    run(['bash', SOURCE / 'deploy/binana-approve-release.sh', digest])
    result = subprocess.run(['bash', str(SOURCE / 'deploy/binana-deploy-wrapper.sh'),
                             str(archive), str(checksum)], env=ENV,
                            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            timeout=600)
    print(result.stdout, flush=True)
    assert (result.returncode == 0) == expect_ok, result.returncode
    return result


def responder():
    """Fake sidecar verifies signed controls and checks bot-readable ownership."""
    sys.path.insert(0, str(SOURCE))
    from services.common.envelope import verify_envelope, BUS_COMMAND
    while not stop.is_set():
        for path in (PERSIST / 'commands/inbox').glob('*.json'):
            if path.name in acknowledged:
                continue
            assert path.stat().st_uid == uid, path
            envelope = json.loads(path.read_text())
            os.environ['ENVELOPE_RELEASE_HASH'] = envelope['release_hash']
            verify_envelope(envelope, purpose=BUS_COMMAND, expected_producers={'deploy-installer'})
            payload = envelope['payload']
            assert payload['command'] in {'entries', 'reconcile'}
            result = PERSIST / 'runtime' / ('command_result_' + payload['command_id'] + '.json')
            result.write_text(json.dumps({'ok': True}))
            acknowledged.add(path.name)
        time.sleep(0.02)


assert not APP.exists(), 'fresh test root required'
assert shutil.which('python') is None, 'must not inherit python-is-python3'
result = subprocess.run(['/usr/bin/python3', '-I', '-c', 'import Crypto'], capture_output=True)
assert result.returncode != 0, 'must not inherit global Crypto'
for directory in (APP / 'releases', PRIVATE, INBOX, Path('/build'), Path('/test-bin'),
                  Path('/etc/systemd/system'), Path('/usr/local/libexec')):
    directory.mkdir(parents=True, exist_ok=True)
PRIVATE.chmod(0o700)
for account in (BOT, MONITOR):
    run(['useradd', '--system', '--user-group', '--shell', '/usr/sbin/nologin', account])
uid = int(subprocess.check_output(['id', '-u', BOT]))
gid = int(subprocess.check_output(['id', '-g', BOT]))
Path('/etc/default/' + SLUG + '-deploy').write_text('BINANA_DEPLOY_UID=0\n')
STATE.write_text(json.dumps({'slug': SLUG, 'failure': '', 'units': []}))
for command in ('docker', 'systemctl', 'awk', 'df', 'seq', 'sleep', 'nproc', 'curl'):
    target = Path('/test-bin') / command
    shutil.copyfile(SOURCE / 'tests/clean_host/fake_host.py', target)
    target.chmod(0o755)
# snapshot deliberately uses this absolute path, never a PATH-selected helper.
Path('/usr/bin/docker').symlink_to('/test-bin/docker')
Path('/var/run/docker.sock').touch()
values = {}
for line in (SOURCE / '.env.example').read_text().splitlines():
    if line and not line.startswith('#') and '=' in line:
        key, value = line.split('=', 1)
        values[key] = value
values.update(BOT_PRODUCT='BINANA', BOT_ENVIRONMENT=MODE.upper(),
              BOT_INSTANCE_ID='BINANA-' + ('TN' if MODE == 'testnet' else 'LIVE') + '-CI-01',
              BOT_UID=str(uid), BOT_GID=str(gid), SHARED_HOST_PATH=str(PERSIST),
              EXECUTION_MODE='simulation', AUTO_CONFIRM='false',
              TELEGRAM_BOT_TOKEN='REPLACE_TEST_ONLY', TELEGRAM_OWNER_CHAT_ID='1',
              DEPLOYMENT_PROFILE=('single-bot-testnet-experiment' if MODE == 'testnet' else 'four-bot-oracle'))
for key in ('COMMAND_HMAC_KEY', 'SIGNAL_HMAC_KEY', 'SHARIA_HMAC_KEY',
            'SHARIA_APPROVAL_HMAC_KEY', 'SHARIA_RESULT_HMAC_KEY'):
    values[key] = 'test-only-' * 8
os.environ['COMMAND_HMAC_KEY'] = values['COMMAND_HMAC_KEY']
# Seed schema-only data has no eligible GREEN records; a public key is not
# needed until a real signed record is verified. No approvals are fabricated.
env_file = PRIVATE / '.env'
env_file.write_text(''.join(f'{k}={v}\n' for k, v in values.items()))
env_file.chmod(0o600)
shutil.copyfile(SOURCE / 'deploy/offhost-backup.env.example', PRIVATE / 'offhost-backup.env')
(PRIVATE / 'offhost-backup.env').chmod(0o600)
first = artifact('first')
state(failure='up')
deploy(first, False)
assert not (APP / 'current').exists()
assert json.loads((PERSIST / 'runtime/deployment_status.json').read_text())['ok'] is False
assert not state()['units']
print('FIRST_INSTALL_FAILURE_CLEANUP=PASS', flush=True)
state(failure='')
time.sleep(1.1)
deploy(first, True)
old = str((APP / 'current').resolve())
assert json.loads((PERSIST / 'runtime/deployment_status.json').read_text())['ok'] is True
for name in ('sharia_status.json', 'source_registry.json', 'halal_coins.json'):
    assert (PERSIST / 'sharia' / name).stat().st_uid == uid
assert (PERSIST / 'universe').stat().st_uid == uid
assert Path('/var/backups', SLUG).is_dir()
if MODE == 'live':
    # Preserve the release's disabled-by-default LIVE monitoring policy.
    monitor_env = PRIVATE / 'live-monitor.env'
    assert 'MONITOR_ENABLED=false\n' in monitor_env.read_text()
    assert SLUG + '-monitor-live.service' not in state()['units']
    assert not (PERSIST / 'runtime/container_status.json').exists()
    print('LIVE_MONITOR_DEFAULT_DISABLED=PASS', flush=True)
    # Explicit opt-in is confined to this disposable test root, while execution
    # remains simulation. It exercises optional monitoring and its rollback.
    monitor_env.write_text(monitor_env.read_text().replace(
        'MONITOR_ENABLED=false\n', 'MONITOR_ENABLED=true\n'))
    release_hash = (Path(old) / 'RELEASE_SHA256.txt').read_text().split()[0]
    run(['bash', Path(old) / 'deploy/install_monitoring.sh', old, MODE, release_hash])
snapshot = json.loads((PERSIST / 'runtime/container_status.json').read_text())
assert len(snapshot['containers']) == 6
assert 'sharia/sharia_status.json' in snapshot['readonly_sources_published']
print('FIRST_INSTALL_RETRY_AND_MONITOR_RENDERING=PASS', flush=True)
acknowledged = set()
stop = threading.Event()
thread = threading.Thread(target=responder, daemon=True)
thread.start()
second = artifact('second')
for fault in ('health', 'monitor'):
    state(failure=fault, old=old)
    time.sleep(1.1)
    deploy(second, False)
    assert str((APP / 'current').resolve()) == old
    outcome = json.loads((PERSIST / 'runtime/deployment_status.json').read_text())
    assert outcome['status'] == 'ROLLED_BACK_OLD_HEALTHY', outcome
    print(f'UPGRADE_{fault.upper()}_ROLLBACK=PASS', flush=True)
state(failure='')
time.sleep(1.1)
deploy(second, True)
assert str((APP / 'current').resolve()) != old
assert Path(old).is_dir(), 'last successful rollback release must survive failed-attempt pruning'
assert len(acknowledged) == 6
if MODE == 'testnet':
    state(other=True)
    deploy(second, False)
    state(other=False)
stop.set()
thread.join(timeout=3)
assert subprocess.run(['/usr/bin/python3', '-I', '-c', 'import Crypto'], capture_output=True).returncode != 0
assert shutil.which('python') is None
print('CLEAN_HOST_INSTALLER_SCENARIOS=PASS; Docker/systemd/account events simulated; no orders or credentials', flush=True)
