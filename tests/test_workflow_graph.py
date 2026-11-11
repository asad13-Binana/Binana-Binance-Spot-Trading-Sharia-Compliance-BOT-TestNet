"""Release graph permits source validation and artifacts, never deployment."""
from pathlib import Path
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
        self.assertEqual(len(runs), 3)
        for command in runs:
            self.assertIn('--network=none', command)
            self.assertIn('--read-only', command)
            self.assertIn('--cap-drop=ALL', command)
            self.assertNotIn('--mount', command)
            self.assertNotIn(' -v ', command)

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
