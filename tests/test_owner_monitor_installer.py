"""Execute the real monitoring installer against disposable host adapters."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == 'posix' and getattr(os, 'geteuid', lambda: -1)() == 0,
                     'the real privileged installer requires a root Linux test container')
class OwnerMonitorInstallerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.host = Path(temporary.name)
        self.mode = (ROOT / 'RELEASE_MODE').read_text().strip()
        self.prefix = 'binana-' + self.mode
        self.app = self.host / 'opt' / self.prefix
        self.private = self.host / 'etc' / self.prefix
        self.units = self.host / 'etc/systemd/system'
        self.release = self.app / 'releases/reviewed'
        self.digest = 'a' * 64
        self.bin = self.host / 'bin'
        for path in (self.private, self.units, self.bin, self.host / 'run',
                     self.app / 'monitoring-venvs' / self.digest):
            path.mkdir(parents=True, exist_ok=True)
        (self.app / 'monitoring-venvs' / self.digest / '.complete').touch()
        shutil.copytree(ROOT / 'monitoring', self.release / 'monitoring')
        # Rebase template paths too, so the real sed rules still match their
        # generic source paths and can be checked against this disposable host.
        for unit in (self.release / 'monitoring/systemd').iterdir():
            template = unit.read_text()
            for prefix in ('/opt/', '/etc/', '/var/lib/', '/var/log/',
                           '/usr/local/libexec'):
                template = template.replace(prefix, str(self.host) + prefix)
            unit.write_text(template)
        (self.release / 'RELEASE_MODE').write_text(self.mode + '\n')
        deploy = self.release / 'deploy'
        deploy.mkdir()
        # Rebase host filesystem roots, retaining the actual installer logic,
        # fixed instance guards, unit rendering, and activation commands.
        for filename in ('install_monitoring.sh', 'instance_identity.sh'):
            source = (ROOT / 'deploy' / filename).read_text()
            for prefix in ('/opt/', '/etc/', '/var/lib/', '/var/log/',
                           '/var/backups/', '/usr/local/libexec', '/run/'):
                source = source.replace(prefix, str(self.host) + prefix)
            (deploy / filename).write_text(source)
        self.installer = deploy / 'install_monitoring.sh'
        self.env_file = self.private / (self.mode + '-monitor.env')
        self.state_path = self.host / 'systemctl.json'
        self.state_path.write_text(json.dumps({'calls': [], 'enabled': [], 'active': []}))
        self.command('install', '''import os,sys
args=[]; remaining=iter(sys.argv[1:])
for arg in remaining:
    if arg in ("-o","-g"):
        next(remaining)
    else:
        args.append(arg)
os.execv("/usr/bin/install",["install",*args])
''')
        for name in ('id', 'usermod', 'chown'):
            self.command(name, 'raise SystemExit(0)\n')
        self.command('systemctl', '''import json,os,sys
from pathlib import Path
path=Path(os.environ["INSTALLER_SYSTEMCTL_STATE"])
state=json.loads(path.read_text()); args=sys.argv[1:]
state["calls"].append(args)
units=[arg for arg in args[1:] if not arg.startswith("-")]
enabled=set(state["enabled"]); active=set(state["active"])
if args[0]=="enable":
    enabled.update(units)
    if "--now" in args: active.update(units)
elif args[0]=="disable":
    enabled.difference_update(units)
    if "--now" in args: active.difference_update(units)
elif args[0]=="stop": active.difference_update(units)
elif args[0] in ("start","restart"): active.update(units)
state.update(enabled=sorted(enabled),active=sorted(active)); path.write_text(json.dumps(state))
raise SystemExit(1 if args[0]=="is-active" and not set(units).issubset(active) else 0)
''')
        self.environment = dict(os.environ,
            PATH=str(self.bin) + os.pathsep + os.environ['PATH'],
            INSTALLER_SYSTEMCTL_STATE=str(self.state_path),
            MONITOR_EXECUTION_OWNER='FREQTRADE')
        adapters = self.host / 'host-adapters.sh'
        adapters.write_text(''.join(
            name + '(){ "$INSTALLER_TEST_PYTHON" "$INSTALLER_TEST_BIN/' + name + '" "$@"; }\n'
            for name in ('id', 'usermod', 'chown', 'install', 'systemctl')))
        self.environment.update(BASH_ENV=str(adapters),
                                INSTALLER_TEST_PYTHON=sys.executable,
                                INSTALLER_TEST_BIN=str(self.bin))
        self.owner_timer = self.prefix + '-owner-monitor-snapshot.timer'
        self.owner_service = self.prefix + '-owner-monitor-snapshot.service'

    def command(self, name, body):
        path = self.bin / name
        path.write_text('#!' + sys.executable + '\n' + body)
        path.chmod(0o755)

    def configure(self, owner=None, *, enabled=True):
        values = ['MONITOR_TOKEN=' + 'b' * 64,
                  'MONITOR_PORT=' + ('8090' if self.mode == 'testnet' else '8092'),
                  'MONITOR_ENABLED=' + str(enabled).lower(),
                  'TELEGRAM_REPORTS_ENABLED=false']
        if owner is not None:
            values.append('MONITOR_EXECUTION_OWNER=' + owner)
        self.env_file.write_text('\n'.join(values) + '\n')
        self.env_file.chmod(0o640)

    def install(self, *, expected=0):
        result = subprocess.run(
            ['/bin/bash', str(self.installer), str(self.release), self.mode, self.digest],
            env=self.environment, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return json.loads(self.state_path.read_text())

    def test_explicit_owner_installs_scoped_units_and_publishes_before_api_restart(self):
        self.configure('FREQTRADE')
        original = self.env_file.read_bytes()
        state = self.install()
        service = (self.units / self.owner_service).read_text()
        timer = (self.units / self.owner_timer).read_text()
        self.assertIn('Unit=' + self.owner_service + '\n', timer)
        self.assertIn('OnUnitActiveSec=5s\n', timer)
        self.assertNotIn('Unit=binana-owner-', timer)
        self.assertIn('/usr/local/libexec/' + self.prefix + '-monitor-snapshot --owner-readiness-only', service)
        self.assertIn('InaccessiblePaths=-/var/run/docker.sock', service)
        shared = self.host / 'var/lib' / self.prefix / 'shared'
        self.assertIn('ReadOnlyPaths=' + str(shared / 'freqtrade/runtime') + '\n', service)
        self.assertIn('ReadWritePaths=' + str(shared / 'runtime') + '\n', service)
        self.assertNotIn('binana-freqtrade-v101', service)
        self.assertEqual((self.units / self.owner_service).stat().st_mode & 0o777, 0o644)
        self.assertEqual((self.units / self.owner_timer).stat().st_mode & 0o777, 0o644)
        self.assertIn(self.owner_timer, state['enabled'])
        self.assertIn(self.owner_timer, state['active'])
        self.assertLess(state['calls'].index(['start', self.owner_service]),
                        state['calls'].index(['restart', self.prefix + '-monitor-' + self.mode + '.service']))
        self.assertEqual(self.env_file.read_bytes(), original)

    def test_default_and_explicit_sidecar_never_enable_owner_timer_from_process_env(self):
        for owner in (None, 'SIDECAR'):
            with self.subTest(owner=owner):
                self.configure(owner)
                state = self.install()
                self.assertNotIn(self.owner_timer, state['enabled'])
                self.assertNotIn(self.owner_timer, state['active'])
                self.assertNotIn(self.owner_service, state['active'])

    def test_reinstall_and_architecture_or_monitor_switch_stop_stale_owner_units(self):
        self.configure('FREQTRADE')
        state = self.install()
        self.assertIn(self.owner_timer, state['enabled'])
        self.install()
        self.configure('SIDECAR')
        state = self.install()
        self.assertNotIn(self.owner_timer, state['enabled'])
        self.assertNotIn(self.owner_timer, state['active'])
        self.assertNotIn(self.owner_service, state['active'])
        self.configure('FREQTRADE')
        self.assertIn(self.owner_timer, self.install()['enabled'])
        self.configure('FREQTRADE', enabled=False)
        state = self.install()
        self.assertNotIn(self.owner_timer, state['enabled'])
        self.assertNotIn(self.owner_timer, state['active'])
        self.assertNotIn(self.owner_service, state['active'])

    def test_explicit_owner_values_match_monitor_case_and_quote_handling(self):
        for owner in ('freqtrade', '"FREQTRADE"', "'freqtrade'"):
            with self.subTest(owner=owner):
                self.configure(owner)
                self.assertIn(self.owner_timer, self.install()['enabled'])

    def test_invalid_owner_selection_is_rejected_without_shell_evaluation(self):
        marker = self.host / 'must-not-be-created'
        self.configure('$(touch ' + str(marker) + ')')
        state = self.install(expected=1)
        self.assertFalse(marker.exists())
        self.assertNotIn(self.owner_timer, state['enabled'])


if __name__ == '__main__':
    unittest.main()
