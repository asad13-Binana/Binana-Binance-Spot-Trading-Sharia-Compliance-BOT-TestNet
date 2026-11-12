"""Release graph permits source validation and artifacts, never deployment."""
from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest
import shlex
import yaml

ROOT = Path(__file__).resolve().parents[1]


class OracleWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workflow = yaml.safe_load((ROOT / '.github/workflows/ci.yml').read_text())
        self.jobs = self.workflow['jobs']

    def test_artifact_cannot_run_before_support_and_both_native_owner_gates(self):
        self.assertEqual(set(self.jobs['artifact']['needs']), {'verify', 'sast', 'quality-evidence', 'host-fixtures', 'dependency-audit', 'owner-build'})
        self.assertEqual(self.jobs['owner-build']['needs'], 'verify')
        entries = self.jobs['owner-build']['strategy']['matrix']['include']
        self.assertEqual({(row['runner'], row['arch']) for row in entries},
                         {('ubuntu-24.04', 'amd64'), ('ubuntu-24.04-arm', 'arm64')})

    def test_ci_has_no_remote_deployment_capability(self):
        self.assertEqual(self.workflow['permissions'], {'contents': 'read'})
        for job in self.jobs.values():
            self.assertNotIn('environment', job)
            for step in job['steps']:
                command = str(step.get('run', ''))
                self.assertNotRegex(command, r'(?m)^\s*(?:ssh|scp|rsync)\s')
                self.assertNotRegex(str(step), r'\$\{\{\s*secrets\.')
                self.assertNotRegex(command, r'docker compose[^\n]*(?: up | restart | down )')

    def test_image_regressions_cannot_authenticate_or_write_the_host(self):
        commands = [step['run'] for step in self.jobs['owner-build']['steps'] if 'run' in step]
        runs = [command for command in commands if 'docker run ' in command]
        self.assertEqual(len(runs), 4)
        for command in runs:
            self.assertIn('--network=none', command)
            self.assertIn('--read-only', command)
            self.assertIn('--cap-drop=ALL', command)
            self.assertNotIn('--mount', command)
            self.assertNotIn(' -v ', command)

    def test_final_image_gate_rejects_high_findings_and_incomplete_scans(self):
        command = next(step['run'] for step in self.jobs['owner-build']['steps']
                       if step.get('name') == 'Scan both final images with no vulnerability exceptions')
        checker = command.split("python - <<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
        for component in ('owner', 'support'):
            for severity, missing, expected in (('MEDIUM', False, 0), ('HIGH', False, 1),
                                                ('CRITICAL', False, 1), ('MEDIUM', True, 1)):
                with self.subTest(component=component, severity=severity, missing=missing):
                    with tempfile.TemporaryDirectory() as directory:
                        for name in ('owner', 'support'):
                            results = [{'Class': 'os-pkgs'}, {'Class': 'lang-pkgs'}]
                            if name == component:
                                results[0]['Vulnerabilities'] = [{'Severity': severity}]
                                if missing:
                                    results[1]['Class'] = 'unexpected-analyzer'
                            (Path(directory) / (name + '.json')).write_text(json.dumps({'Results': results}))
                        result = subprocess.run([sys.executable, '-c', checker], cwd=directory,
                                                capture_output=True, text=True, timeout=10)
                        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)

    def test_explicit_pytest_files_exist_in_this_package(self):
        for job_name, job in self.jobs.items():
            for step in job['steps']:
                command = step.get('run', '')
                if 'pytest' not in command:
                    continue
                for token in shlex.split(command):
                    if token.endswith('.py') and (token.startswith('tests/') or '/tests/' in token):
                        self.assertTrue((ROOT / token).is_file(),
                                        f'{job_name}: missing explicit pytest path {token}')
