"""Run the deployment rollback functions against disposable systemd state."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == 'posix' and shutil.which('bash'), 'requires Bash')
class MonitoringRollbackTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.host = Path(temporary.name)
        self.bin = self.host / 'bin'
        self.bin.mkdir()
        self.app = self.host / 'app'
        self.app.mkdir()
        self.mode = (ROOT / 'RELEASE_MODE').read_text().strip()
        self.prefix = 'binana-' + self.mode
        self.owner_timer = self.prefix + '-owner-monitor-snapshot.timer'
        self.owner_service = self.prefix + '-owner-monitor-snapshot.service'
        self.monitor_service = self.prefix + '-monitor-' + self.mode + '.service'
        self.unrelated = 'unrelated-project-monitor.service'
        self.link = self.app / 'monitoring-current'
        self.link.symlink_to(self.host / 'failed-venv')
        self.state_path = self.host / 'systemd-state.json'
        self.write_state()
        self.command('sudo', 'import os,sys\nos.execvp(sys.argv[1],sys.argv[1:])\n')
        self.command('systemctl', '''import json,os,sys
from pathlib import Path
path=Path(os.environ['ROLLBACK_SYSTEMD_STATE'])
state=json.loads(path.read_text()); args=sys.argv[1:]
state['calls'].append(args)
units=[arg for arg in args[1:] if not arg.startswith('-')]
enabled=set(state['enabled']); active=set(state['active']); result=0
if args[0]=='disable':
    for unit in units:
        if unit not in state['stuck_enabled']: enabled.discard(unit)
        if '--now' in args and unit not in state['stuck_active']: active.discard(unit)
    result=int(bool(set(units) & (set(state['stuck_active']) | set(state['stuck_enabled']))))
elif args[0]=='is-active': result=0 if set(units).issubset(active) else 3
elif args[0]=='is-enabled':
    value=state['unit_file_states'].get(units[0], 'enabled' if set(units).issubset(enabled) else 'disabled')
    print(value)
    result=0 if value in ('enabled','static') else 4 if value=='not-found' else 1
elif args[0]!='daemon-reload': raise SystemExit('unexpected systemctl command: '+repr(args))
state.update(enabled=sorted(enabled),active=sorted(active)); path.write_text(json.dumps(state))
raise SystemExit(result)
''')
        self.environment = dict(os.environ,
            PATH=str(self.bin) + os.pathsep + os.environ['PATH'],
            ROLLBACK_SYSTEMD_STATE=str(self.state_path),
            ROLLBACK_PREFIX=self.prefix)
        # Execute the actual function bodies, not a reimplementation. Host and
        # release validation preceding these functions belongs to other tests;
        # only the external systemctl/sudo boundary is replaced here.
        source = (ROOT / 'deploy/install_artifact.sh').read_text()
        functions = dict(re.findall(r'(?ms)^([a-z_]+)\(\)\{\n(.*?)^\}', source))
        names = ('as_root', 'install_monitoring_for', 'disable_monitoring_units',
                 'disable_monitoring_after_failed_first_install', 'restore_monitoring')
        self.functions = '\n'.join(name + '(){\n' + functions[name] + '}\n'
                                   for name in names if name in functions)

    def command(self, name, body):
        path = self.bin / name
        path.write_text('#!' + sys.executable + '\n' + body)
        path.chmod(0o755)

    def write_state(self, *, stuck_active=(), stuck_enabled=(), unit_file_states=None):
        units = [self.owner_timer, self.owner_service, self.monitor_service, self.unrelated]
        self.state_path.write_text(json.dumps({
            'calls': [], 'enabled': units, 'active': units,
            'stuck_active': list(stuck_active), 'stuck_enabled': list(stuck_enabled),
            'unit_file_states': unit_file_states or {},
        }))

    def old_release(self):
        old = self.host / 'pre-owner-publisher-release'
        (old / 'deploy').mkdir(parents=True)
        script = old / 'deploy/install_monitoring.sh'
        script.write_text('#!' + sys.executable + '''
import json,os
from pathlib import Path
path=Path(os.environ['ROLLBACK_SYSTEMD_STATE']); state=json.loads(path.read_text())
state['previous_installer_observed']={name:list(state[name]) for name in ('active','enabled')}
# A pre-feature installer restores only its legacy API, knowing nothing about
# owner-readiness units introduced by the failed release.
unit=os.environ['ROLLBACK_PREFIX']+'-monitor-'+os.environ['INSTANCE_MODE']+'.service'
state['active']=sorted(set(state['active']) | {unit})
state['enabled']=sorted(set(state['enabled']) | {unit})
path.write_text(json.dumps(state))
''')
        script.chmod(0o755)
        return old

    def restore(self, old=None):
        environment = dict(self.environment,
            APP_ROOT=str(self.app), SYSTEMD_PREFIX=self.prefix, INSTANCE_MODE=self.mode,
            OLD=str(old) if old else '', OLD_MODE=self.mode, OLD_RELEASE_HASH='a' * 64,
            stop_result='0')
        script = 'set -Eeuo pipefail\n' + self.functions
        script += '\nif restore_monitoring; then exit 0; else exit 1; fi\n'
        result = subprocess.run(['bash', '-c', script], env=environment,
                                text=True, capture_output=True, timeout=20)
        return result, json.loads(self.state_path.read_text())

    def assert_owner_inert(self, state):
        for unit in (self.owner_timer, self.owner_service):
            self.assertNotIn(unit, state['active'])
            self.assertNotIn(unit, state['enabled'])
        self.assertIn(self.unrelated, state['active'])
        self.assertIn(self.unrelated, state['enabled'])

    def test_failed_first_install_disables_owner_units_and_removes_current_link(self):
        result, state = self.restore()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_owner_inert(state)
        self.assertFalse(self.link.is_symlink())

    def test_pre_feature_rollback_quiesces_owner_units_before_previous_installer(self):
        result, state = self.restore(self.old_release())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_owner_inert(state)
        before = state['previous_installer_observed']
        for unit in (self.owner_timer, self.owner_service):
            self.assertNotIn(unit, before['active'])
            self.assertNotIn(unit, before['enabled'])
        self.assertIn(self.monitor_service, state['active'])

    def test_stuck_active_owner_unit_makes_first_install_rollback_fail(self):
        self.write_state(stuck_active=[self.owner_service])
        result, state = self.restore()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(self.owner_service, state['active'])
        self.assertTrue(self.link.is_symlink())

    def test_stuck_active_owner_unit_prevents_previous_installer_execution(self):
        self.write_state(stuck_active=[self.owner_timer])
        result, state = self.restore(self.old_release())
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('previous_installer_observed', state)

    def test_inactive_but_still_enabled_owner_timer_is_not_reported_as_clean(self):
        self.write_state(stuck_enabled=[self.owner_timer])
        result, state = self.restore()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(self.owner_timer, state['active'])
        self.assertIn(self.owner_timer, state['enabled'])

    def test_static_oneshot_and_absent_units_do_not_block_verified_cleanup(self):
        self.write_state(unit_file_states={
            self.owner_service: 'static',
            self.prefix + '-api-readiness.service': 'not-found',
        })
        result, state = self.restore()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_owner_inert(state)
        self.assertFalse(self.link.is_symlink())

    def test_unreadable_enablement_state_does_not_pass_rollback(self):
        self.write_state(unit_file_states={self.owner_timer: ''})
        result, state = self.restore(self.old_release())
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('previous_installer_observed', state)


if __name__ == '__main__':
    unittest.main()
