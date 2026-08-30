#!/usr/bin/python3
"""Test doubles ONLY inside the disposable clean-host container/chroot.

Docker, systemd, capacity and exchange acknowledgements are simulated. Python,
locks, artifact verification, venvs, permissions, unit rendering and installer
control flow are real. This is NOT evidence of an authenticated deployment.
"""
import json
import os
from pathlib import Path
import subprocess
import sys

assert Path('/.binana-clean-host').is_file(), 'disposable test root required'
name = Path(sys.argv[0]).name
args = sys.argv[1:]
state_path = Path('/run/fake-host.json')
state = json.loads(state_path.read_text())
mode = state.get('failure', '')
services = ['universe', 'sharia-egress-proxy', 'sharia-screener',
            'freqtrade', 'execution-sidecar', 'telegram-broker']


def save():
    state_path.write_text(json.dumps(state))


if name == 'docker':
    if args[0] == 'compose':
        assert args[1:3] == ['--project-name', state['slug']], args
        release = Path(args[args.index('-f') + 1]).parent
        action = args[args.index('-f') + 2:]
        if action[0] in {'config', 'build'}:
            if mode == action[0]:
                sys.exit(41)
        elif action[0] == 'down':
            if mode == 'down':
                sys.exit(44)
            state['active'] = ''
            save()
        elif action[0] == 'up':
            state['active'] = str(release)
            save()
            if mode == 'up':
                state['failure'] = ''  # fail new start, allow cleanup/rollback
                save()
                sys.exit(42)
        elif action[0] == 'ps':
            if state.get('active'):
                if '--services' in action:
                    print('\n'.join(services))
                else:
                    print(action[-1])
        else:
            raise AssertionError(action)
    elif args[0] == 'ps':
        if state.get('other'):
            print('other bitcoin-testnet')
        elif state.get('active') and '-aq' in args:
            print('\n'.join(services))
    elif args[0] == 'inspect':
        if '--format' in args:
            bad = mode == 'health' and state.get('active') != state.get('old')
            print('unhealthy' if bad else 'healthy')
        else:
            print(json.dumps([{'Name': s, 'State': {'Status': 'running',
                  'Health': {'Status': 'healthy'}}, 'Config': {'Labels': {
                  'com.docker.compose.service': s}}} for s in services]))
    elif args[:2] == ['image', 'rm']:
        pass
    else:
        raise AssertionError(args)
elif name == 'systemctl':
    units = [x for x in args[1:] if not x.startswith('-')]
    for unit in units:
        assert unit.startswith(state['slug'] + '-'), (name, args)
    active = set(state.get('units', []))
    if args[0] == 'daemon-reload':
        for unit in Path('/etc/systemd/system').glob(state['slug'] + '-*'):
            text = unit.read_text()
            assert 'binana-freqtrade-v101' not in text, unit
    elif args[0] in {'enable', 'start', 'restart'}:
        if mode == 'monitor' and args[0] == 'restart':
            state['failure'] = ''
            save()
            sys.exit(43)
        active.update(units)
        if args[0] == 'start' and units[0].endswith('monitor-snapshot.service'):
            unit_text = Path('/etc/systemd/system', units[0]).read_text()
            for line in unit_text.splitlines():
                if line.startswith(('ReadWritePaths=', 'ReadOnlyPaths=')):
                    for path in line.split('=', 1)[1].split():
                        if not path.startswith('-'):
                            assert Path(path).exists(), (units[0], path)
            env = dict(os.environ, BINANA_COMPOSE_PROJECT=state['slug'],
                       BINANA_CONTAINER_STATUS_PATH=f"/var/lib/{state['slug']}/shared/runtime/container_status.json")
            subprocess.run([f"/usr/local/libexec/{state['slug']}-monitor-snapshot"],
                           env=env, check=True)
    elif args[0] == 'disable':
        active.difference_update(units)
    elif args[0] == 'is-active':
        sys.exit(0 if all(u in active for u in units) else 3)
    state['units'] = sorted(active)
    save()
elif name == 'awk' and '/proc/meminfo' in args:
    print(16384 if 'MemTotal' in args[0] else 8192)
elif name == 'df':
    print('Filesystem 1024-blocks Used Available Capacity Mounted on')
    print('/fake 200000000 10000000 190000000 5% /')
elif name == 'nproc':
    print(4)
elif name == 'seq':
    print(1)  # one health attempt, without waiting four minutes per fault
elif name == 'sleep':
    import time
    time.sleep(0.15)
elif name == 'curl':
    raise AssertionError('network notifications must not be attempted in the test')
else:
    executable = '/usr/bin/' + name
    os.execv(executable, [executable, *args])
