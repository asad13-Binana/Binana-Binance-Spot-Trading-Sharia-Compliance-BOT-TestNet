from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class OracleCapacityContractTests(unittest.TestCase):
    def test_setup_and_install_enforce_the_same_cpu_floor(self):
        setup = (ROOT / 'deploy/oracle_setup.sh').read_text(encoding='utf-8')
        install = (ROOT / 'deploy/install_artifact.sh').read_text(
            encoding='utf-8')
        for text in (setup, install):
            self.assertIn('MIN_CPU_COUNT=${MIN_CPU_COUNT:-2}', text)
            self.assertIn('cpu_count=$(nproc)', text)
        self.assertLess(setup.index('cpu_count=$(nproc)'), setup.index('apt-get'))
        self.assertLess(
            install.index('cpu_count=$(nproc)'),
            install.index('EXPECTED=$(awk'))

    def test_arm64_is_default_and_amd64_requires_explicit_review(self):
        setup = (ROOT / 'deploy/oracle_setup.sh').read_text(encoding='utf-8')
        self.assertIn('ALLOW_REVIEWED_AMD64=${ALLOW_REVIEWED_AMD64:-false}', setup)
        self.assertIn('architecture" == arm64', setup)
        self.assertIn(
            'architecture" == amd64 && "$ALLOW_REVIEWED_AMD64" == true',
            setup)

    def test_example_and_primary_guides_share_one_capacity_target(self):
        env = (ROOT / '.env.example').read_text(encoding='utf-8')
        github = (ROOT / 'docs/GITHUB_ORACLE_DEPLOYMENT.md').read_text(
            encoding='utf-8')
        guide = (ROOT / 'docs/ORACLE_DEPLOYMENT_GUIDE.md').read_text(
            encoding='utf-8')
        self.assertIn('MIN_CPU_COUNT=2', env)
        for text in (env, github, guide):
            self.assertIn('12 GiB', text)
            self.assertIn('80 GiB', text)
        self.assertNotIn('1 OCPU, 6 GiB', github + guide)


if __name__ == '__main__':
    unittest.main()
