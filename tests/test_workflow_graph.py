from __future__ import annotations
"""GATE-001 / PKG-001 release-gate regression tests.

GATE-001: `deploy-oracle` previously declared only `needs: artifact`. GitHub
runs jobs in parallel and a job waits ONLY for what it lists in `needs`, so
Oracle deployment could begin while the container integration simulation and
the real TA-Lib strategy probe were still running — or after they had FAILED.
Those two jobs exist specifically to convert an offline skip into a hard
failure, so bypassing them defeats the entire container gate. This test pins
the full prerequisite set so it cannot silently regress.

PKG-001: the packaged shell entrypoints must be executable and no packaged
file may be group/world-writable. On POSIX this is asserted against the real
filesystem; the ZIP builder stores the same modes.
"""
import os
import stat
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / '.github' / 'workflows' / 'ci.yml'

REQUIRED_DEPLOY_PREREQUISITES = {
    'artifact', 'integration-simulation', 'strategy-probe-container',
}


def _load_workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding='utf-8'))


class DeployGateGraphTests(unittest.TestCase):
    def setUp(self):
        self.workflow = _load_workflow()
        self.jobs = self.workflow.get('jobs', {})

    def test_mandatory_gate_jobs_exist(self):
        for job in REQUIRED_DEPLOY_PREREQUISITES | {'verify', 'deploy-oracle'}:
            self.assertIn(job, self.jobs, msg=f'workflow job {job!r} is missing')

    def test_deploy_requires_every_mandatory_gate(self):
        needs = self.jobs['deploy-oracle'].get('needs')
        needs = [needs] if isinstance(needs, str) else list(needs or [])
        missing = REQUIRED_DEPLOY_PREREQUISITES - set(needs)
        self.assertFalse(
            missing,
            msg=('deploy-oracle must not start before every mandatory gate '
                 f'completes; missing prerequisites: {sorted(missing)}'))

    def test_container_gates_run_after_verify(self):
        for job in ('integration-simulation', 'strategy-probe-container'):
            needs = self.jobs[job].get('needs')
            needs = [needs] if isinstance(needs, str) else list(needs or [])
            self.assertIn('verify', needs, msg=f'{job} must depend on verify')

    def test_integration_installs_key_dependency_before_env_synthesis(self):
        """The synthesis script imports Crypto before any image is built."""
        steps = self.jobs['integration-simulation']['steps']
        setup = next(i for i, step in enumerate(steps)
                     if str(step.get('uses', '')).startswith('actions/setup-python@'))
        install = next(i for i, step in enumerate(steps)
                       if step.get('name') == 'Install integration env synthesis dependency')
        synthesis = next(i for i, step in enumerate(steps)
                         if step.get('name') == 'Synthesize a no-credential simulation env')
        self.assertLess(setup, install, msg='Python must be configured before dependency install')
        self.assertLess(install, synthesis,
                        msg='pycryptodome must be installed before the synthesis import')
        self.assertEqual(steps[setup].get('with', {}).get('python-version'), '3.12')
        self.assertEqual(steps[install].get('run'),
                         'python -m pip install pycryptodome==3.23.0')

    def test_strategy_probe_job_fails_on_skip(self):
        """The probe job is only meaningful if a SKIP is treated as failure."""
        text = WORKFLOW.read_text(encoding='utf-8')
        self.assertIn('freqtradeorg/freqtrade:2026.6', text)
        self.assertIn("grep -qi 'skipped' probe.log", text)

    def test_deployment_is_never_triggered_without_an_explicit_opt_in(self):
        condition = str(self.jobs['deploy-oracle'].get('if', ''))
        self.assertIn('ORACLE_', condition)
        self.assertIn("github.ref == 'refs/heads/main'", condition)

    def test_artifact_job_rebuilds_ledgers_after_git_commit_stamp(self):
        """ARTIFACT-LEDGER-001: the artifact job writes .git-commit into the
        tree, so it must regenerate the audit ledgers BEFORE rebuilding the
        manifest, then re-verify both. The extracted artifact re-runs
        build_audit_ledgers.py --check (exact tree parity); with the ledger
        rebuild missing, the first push fails in the artifact job with
        'FILE_REVIEW_LEDGER.csv is stale'."""
        steps = self.jobs['artifact']['steps']
        run = next(s['run'] for s in steps
                   if s.get('name') == 'Create deterministic immutable release artifact')
        stamp = run.index('.git-commit')
        ledgers = run.index('python scripts/build_audit_ledgers.py')
        manifest = run.index('python scripts/build_manifest.py')
        verify = run.index('python scripts/verify_manifest.py')
        check = run.index('python scripts/build_audit_ledgers.py --check')
        self.assertLess(stamp, ledgers,
                        msg='.git-commit must exist before the ledger rebuild')
        self.assertLess(ledgers, manifest,
                        msg='audit ledgers must be regenerated BEFORE the manifest')
        self.assertLess(manifest, verify,
                        msg='the rebuilt manifest must be verified in the artifact job')
        self.assertLess(verify, check,
                        msg='ledger parity must be re-checked after the manifest rebuild')
        self.assertLess(check, run.index('tar '),
                        msg='the archive must be created only after the integrity chain is rebuilt')


class ReleaseGateTraversalTests(unittest.TestCase):
    """Generated caches cannot interfere with checks over packaged files."""

    def test_release_gate_prunes_generated_cache_directories(self):
        source = (ROOT / 'deploy/verify_release.sh').read_text(encoding='utf-8')
        self.assertGreaterEqual(source.count("-path './.pytest_cache' -prune"), 3)
        self.assertGreaterEqual(source.count("-path '*/__pycache__' -prune"), 3)


@unittest.skipUnless(os.name == 'posix', 'POSIX file modes only')
class PackagedPermissionTests(unittest.TestCase):
    """PKG-001: shell entrypoints executable, nothing world-writable."""

    def _files(self):
        exclude = {'.git', '__pycache__', '.pytest_cache', '.ruff_cache'}
        return [p for p in ROOT.rglob('*')
                if p.is_file() and not any(part in exclude for part in p.parts)]

    def test_every_shell_script_is_executable(self):
        not_exec = [p.relative_to(ROOT).as_posix() for p in self._files()
                    if p.suffix == '.sh' and not (p.stat().st_mode & stat.S_IXUSR)]
        self.assertEqual(not_exec, [],
                         msg='packaged shell scripts must be executable (0755)')

    def test_no_packaged_file_is_group_or_world_writable(self):
        writable = [p.relative_to(ROOT).as_posix() for p in self._files()
                    if p.stat().st_mode & (stat.S_IWGRP | stat.S_IWOTH)]
        self.assertEqual(writable, [],
                         msg='packaged files must not be group/world-writable')


if __name__ == '__main__':
    unittest.main()
