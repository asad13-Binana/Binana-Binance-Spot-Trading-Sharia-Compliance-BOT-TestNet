"""Historical rollout must never select AWS implicitly on a fresh host."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/recovery'))
import deploy_candidate_owner as deploy


class RecoveryRolloutProfileTests(unittest.TestCase):
    def test_even_historical_profile_cannot_activate_a_retired_rollout(self):
        with patch.object(sys, 'argv', self.arguments() + ['--host-profile', 'aws-historical']), \
                patch.object(deploy, 'inspect', side_effect=AssertionError('HOST_ACCESSED')):
            try:
                deploy.main()
            except SystemExit as error:
                self.assertEqual(error.code, 64)
            else:
                self.fail('The retired launcher must refuse before host operations')

    def test_unbound_generic_helper_cannot_run_docker(self):
        with patch.object(deploy, 'HOST_CONTEXT', None), \
                patch.object(deploy.subprocess, 'run', side_effect=AssertionError('PROCESS_STARTED')):
            with self.assertRaisesRegex(RuntimeError, 'HOST_PROFILE_REQUIRED'):
                deploy.run(['docker', 'inspect', 'anything'])

    def arguments(self):
        return ['deploy_candidate_owner.py', '--image', 'sha256:' + 'b' * 64,
                '--tag', 'fixture:offline', '--evidence', '/fixture/evidence']

    def test_missing_profile_fails_before_any_docker_or_host_operation(self):
        with patch.object(sys, 'argv', self.arguments()), \
                patch.object(deploy, 'inspect', side_effect=AssertionError('HOST_ACCESSED')) as inspect:
            with self.assertRaises(SystemExit) as caught:
                deploy.main()
            self.assertEqual(caught.exception.code, 64)
            inspect.assert_not_called()

    def test_oracle_profile_cannot_activate_historical_aws_rollout(self):
        with patch.object(sys, 'argv', self.arguments() + ['--host-profile', 'oracle']), \
                patch.object(deploy, 'inspect', side_effect=AssertionError('HOST_ACCESSED')) as inspect:
            with self.assertRaises(SystemExit) as caught:
                deploy.main()
            self.assertEqual(caught.exception.code, 64)
            inspect.assert_not_called()


if __name__ == '__main__':
    unittest.main()
