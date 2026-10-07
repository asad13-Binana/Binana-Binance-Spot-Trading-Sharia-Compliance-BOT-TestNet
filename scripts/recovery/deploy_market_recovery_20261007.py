"""Install hash-checked collector/startup files; recreate only BINANA universe."""
import datetime
import hashlib
import json
from pathlib import Path
import os
import tempfile
import time
import subprocess
import sys

HOST = Path('/home/ubuntu/binana-runtime-fixes')
SERVICES = ('freqtrade', 'telegram-broker', 'execution-sidecar', 'sharia-screener', 'sharia-research', 'sharia-egress-proxy')


def command(args):
    return subprocess.check_output(args, text=True, timeout=180)


def identity(service):
    result = json.loads(command(['docker', 'inspect', f'binana-testnet-{service}-1']))[0]
    return {'id': result['Id'], 'image': result['Image'], 'started': result['State']['StartedAt']}


def atomic_write(target, data, mode):
    for parent in [target.parent, *target.parent.parents]:
        if parent.is_symlink():
            raise ValueError('Symlink parent directory')
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=target.name + '.', dir=target.parent)
    try:
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
        Path(name).replace(target)
    finally:
        Path(name).unlink(missing_ok=True)


def wait_universe_healthy():
    deadline = time.monotonic() + 140
    while time.monotonic() < deadline:
        state = json.loads(command(['docker', 'inspect', 'binana-testnet-universe-1']))[0]['State']
        if state['Status'] == 'running' and state.get('Health', {}).get('Status') == 'healthy':
            return
        if state['Status'] in {'exited', 'dead'}:
            raise ValueError('Universe stopped after deployment')
        time.sleep(3)
    raise ValueError('Universe did not become healthy')


def main():
    payload = json.loads(Path(sys.argv[1]).read_text())
    before = {name: identity(name) for name in SERVICES}
    backup = Path('/var/backups/binana-testnet') / datetime.datetime.now(datetime.timezone.utc).strftime('market-recovery-%Y%m%dT%H%M%SZ')
    backup.mkdir(parents=True, exist_ok=False, mode=0o700)
    import base64
    targets = {}
    for relative, item in payload.items():
        # Exact allowed destinations only. This bundle contains no credentials.
        if relative not in {
            'market-recovery-20261007/analytics.py', 'market-recovery-20261007/service.py',
            'market-recovery-20261007/spot_stream.py', 'compose.market-recovery-20261007.json',
            'compose.running-images-20261007.json', 'local-images-20261007.json',
            'verify_local_images.py', 'binana-stage1-start',
        }:
            raise ValueError('Unexpected deployment target')
        data = base64.b64decode(item['base64'], validate=True)
        if hashlib.sha256(data).hexdigest() != item['sha256']:
            raise ValueError('Payload hash mismatch')
        target = Path('/usr/local/sbin/binana-stage1-start') if relative == 'binana-stage1-start' else HOST / relative
        targets[target] = data
    if len(targets) != 8:
        raise ValueError('Incomplete deployment bundle')
    for target in targets:
        if any(part.is_symlink() for part in [target, *target.parents]):
            raise ValueError('Symlink destination or ancestor')
    # Reject drift from the live sources used to prepare this patch.
    expected = {
        '/home/ubuntu/binana-runtime-fixes/market_context/analytics.py': '1d0a891ed70d360bf1305f6a781d32afad145922fb1cf997e2d7ffb7c7f8767b',
        '/home/ubuntu/binana-runtime-fixes/market_context/spot_stream.py': '43e00ea7e8a12a28cceb56136f527eb321b61a73fe13053c60fabfc9b472be05',
        '/usr/local/sbin/binana-stage1-start': '0258d37975ca8d188d89f764ef427e669101eded2652afe311df9a06d810c7d6',
    }
    for path, digest in expected.items():
        if hashlib.sha256(Path(path).read_bytes()).hexdigest() != digest:
            raise ValueError('Deployment source drift: ' + path)
    live_sources = {
        'analytics.py': '1d0a891ed70d360bf1305f6a781d32afad145922fb1cf997e2d7ffb7c7f8767b',
        'service.py': '0ba25107c84caec129aeac1a67773485e84a6f694c8910bdd3752ca241a84008',
        'spot_stream.py': '43e00ea7e8a12a28cceb56136f527eb321b61a73fe13053c60fabfc9b472be05',
        'l2_book.py': '882a9ae6848227728fcf3a08f94280f26ba0c2d68a08e5af7ab56aefb8134d35',
        'l2_collector.py': '103a4da5186cc36454d79982289f075176b2ec8b1b3d59baafe9c9c7af60f36d',
    }
    for name, digest in live_sources.items():
        raw = subprocess.check_output(['docker', 'exec', 'binana-testnet-universe-1', 'cat', '/app/services/market_context/' + name], timeout=30)
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError('Deployed collector source drift: ' + name)
    originals = {str(p): p.read_bytes() if p.exists() else None for p in targets}
    for index, (path, data) in enumerate(originals.items()):
        if data is not None:
            (backup / str(index)).write_bytes(data)
    (backup / 'index.json').write_text(json.dumps(list(originals), indent=2))
    base = ['docker', 'compose', '-p', 'binana-testnet', '--env-file', '/etc/binana-testnet/.env']
    for name in ['/opt/binana-testnet/current/docker-compose.yml', str(HOST / 'compose.runtime-fixes.yml'), str(HOST / 'compose.freqtrade-owner.yml'), str(HOST / 'compose.sharia-v193.yml'), str(HOST / 'compose.running-images-20261007.json')]:
        base += ['-f', name]
    updated = base + ['-f', str(HOST / 'compose.market-recovery-20261007.json')]
    # Compose uses this non-secret release tag in the existing base file.
    os.environ['RELEASE_TAG'] = 'v101-4dd60e295d145e62'
    started = False
    try:
        for target, data in targets.items():
            atomic_write(target, data, 0o755 if target.name == 'binana-stage1-start' else 0o644)
        command(['python3', str(HOST / 'verify_local_images.py'), str(HOST / 'local-images-20261007.json'), str(HOST / 'compose.running-images-20261007.json')])
        config = json.loads(command(updated + ['config', '--format', 'json']))
        universe = config['services']['universe']
        if float(universe['cpus']) != 0.75 or int(universe['mem_limit']) != 536870912:
            raise ValueError('Incorrect collector resource limit')
        started = True
        command(updated + ['up', '-d', '--no-deps', '--no-build', '--pull', 'never', 'universe'])
        wait_universe_healthy()
        after = {name: identity(name) for name in SERVICES}
        if before != after:
            raise ValueError('Another BINANA service changed during deployment')
        report = {'backup': str(backup), 'other_services_unchanged': True, 'universe': identity('universe'),
                  'files': {str(p): hashlib.sha256(data).hexdigest() for p, data in targets.items()},
                  'market_freshness_acceptance': 'pending observation'}
        (backup / 'receipt.json').write_text(json.dumps(report, indent=2))
        print(json.dumps(report))
    except BaseException:
        for path, data in originals.items():
            target = Path(path)
            if data is None:
                target.unlink(missing_ok=True)
            else:
                atomic_write(target, data, 0o755 if target.name == 'binana-stage1-start' else 0o644)
        if started:
            command(base + ['up', '-d', '--no-deps', '--no-build', '--pull', 'never', 'universe'])
            wait_universe_healthy()
        raise


if __name__ == '__main__':
    main()
