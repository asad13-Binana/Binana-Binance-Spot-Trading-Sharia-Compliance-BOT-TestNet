from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class OracleCapacityContractTests(unittest.TestCase):


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
