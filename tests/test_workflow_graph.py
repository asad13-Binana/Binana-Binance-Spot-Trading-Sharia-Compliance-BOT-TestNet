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
        self.assertEqual(set(self.jobs['artifact']['needs']), {
            'verify', 'sast', 'quality-evidence', 'host-fixtures', 'dependency-audit',
            'owner-build', 'strategy-probe-container', 'integration-simulation',
        })
        for key in ('if', 'continue-on-error'):
            self.assertNotIn(key, self.jobs['artifact'])
        self.assertEqual(self.jobs['owner-build']['needs'], 'verify')
        entries = self.jobs['owner-build']['strategy']['matrix']['include']
        self.assertEqual({(row['runner'], row['arch']) for row in entries},
                         {('ubuntu-24.04', 'amd64'), ('ubuntu-24.04-arm', 'arm64')})

    def test_required_checks_run_on_pull_requests(self):
        events = self.workflow.get("on", self.workflow.get(True))
        self.assertIn("pull_request", events)
        self.assertFalse(events["pull_request"])
        verify = self.jobs["verify"]
        matrix = verify["strategy"]["matrix"]
        self.assertEqual(set(matrix), {"python-version"})
        self.assertCountEqual(matrix["python-version"], ["3.10", "3.11", "3.12", "3.13", "3.14"])
        interpreters = [step["with"]["python-version"] for step in verify["steps"]
                        if step.get("uses", "").startswith("actions/setup-python@")]
        self.assertEqual(interpreters, ["${{ matrix.python-version }}"])
        for name in ("verify", "strategy-probe-container", "integration-simulation"):
            with self.subTest(job=name):
                job = self.jobs[name]
                for key in ("name", "if", "continue-on-error", "needs"):
                    self.assertNotIn(key, job)
                for step in job["steps"]:
                    self.assertNotIn("if", step)
                    self.assertNotIn("continue-on-error", step)

    def test_required_checks_execute_the_packaged_fixtures(self):
        def commands(name):
            self.assertIn(name, self.jobs)
            return [step["run"].strip() for step in self.jobs[name]["steps"] if "run" in step]

        verify = commands("verify")
        self.assertIn("bash deploy/verify_release.sh", verify)
        self.assertIn("python -m pip check", verify)
        owner_build, owner_run = commands("strategy-probe-container")
        support_build, support_run = commands("integration-simulation")
        for build, component in ((owner_build, "owner"), (support_build, "services")):
            materialize, docker_build = [shlex.split(line) for line in build.splitlines()]
            self.assertEqual(materialize, ["python", "runtime_owner/deployment/materialize_oracle_bundle.py",
                                           "$RUNNER_TEMP/bundle"])
            self.assertEqual(docker_build[:2], ["docker", "build"])
            self.assertEqual(docker_build[-1], "$RUNNER_TEMP/bundle/" + component)
        self.assertIn('"$RUNNER_TEMP/bundle/owner/Dockerfile.clean"', owner_build)
        probe = shlex.split(owner_run.replace("\\\n", " "))
        self.assertEqual(probe[:2], ["docker", "run"])
        for flag in ("--network=none", "--read-only", "--cap-drop=ALL", "--pull=never"):
            self.assertIn(flag, probe)
        self.assertEqual(probe[-7:], ["--entrypoint", "python", "binana-strategy-ci", "-B", "-",
                                      "<", "tests/ci/owner_strategy_probe.py"])
        self.assertEqual(shlex.split(support_run), ["python", "tests/ci/support_integration.py", "host",
                                                    "$(cat RELEASE_MODE)", "--image", "binana-integration-ci"])
        for path in ("tests/ci/owner_strategy_probe.py", "tests/ci/support_integration.py"):
            self.assertTrue((ROOT / path).is_file())

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
