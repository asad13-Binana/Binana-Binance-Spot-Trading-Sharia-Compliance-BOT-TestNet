"""Historical rollout must never select AWS implicitly on a fresh host."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/recovery'))
import deploy_candidate_owner as deploy


class RecoveryRolloutProfileTests(unittest.TestCase):
    def arguments(self):
        return ['deploy_candidate_owner.py', '--image', 'sha256:' + 'b' * 64,
                '--tag', 'fixture:offline', '--evidence', '/fixture/evidence']

    def test_missing_profile_fails_before_any_docker_or_host_operation(self):
        with patch.object(sys, 'argv', self.arguments()), \
                patch.object(deploy, 'inspect', side_effect=AssertionError('HOST_ACCESSED')) as inspect:
            with self.assertRaises(SystemExit) as caught:
                deploy.main()
            self.assertEqual(caught.exception.code, 2)
            inspect.assert_not_called()

    def test_oracle_profile_cannot_activate_historical_aws_rollout(self):
        with patch.object(sys, 'argv', self.arguments() + ['--host-profile', 'oracle']), \
                patch.object(deploy, 'inspect', side_effect=AssertionError('HOST_ACCESSED')) as inspect:
            with self.assertRaises(SystemExit) as caught:
                deploy.main()
            self.assertEqual(caught.exception.code, 2)
            inspect.assert_not_called()


if __name__ == '__main__':
    unittest.main()
