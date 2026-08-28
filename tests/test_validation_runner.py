from __future__ import annotations

import unittest
from pathlib import Path

from scripts.run_validation_venv import (
    VALIDATION_STATUSES,
    classify_returncode,
)


class ValidationRunnerTests(unittest.TestCase):
    def test_status_vocabulary_is_explicit_and_complete(self):
        self.assertEqual(VALIDATION_STATUSES, {
            'PASS', 'TOOLING_UNAVAILABLE', 'COLLECTION_FAILED',
            'TEST_FAILED', 'TIMEOUT',
        })

    def test_stage_failures_are_not_misreported_as_test_failures(self):
        self.assertEqual(classify_returncode('tooling', 1),
                         'TOOLING_UNAVAILABLE')
        self.assertEqual(classify_returncode('collection', 1),
                         'COLLECTION_FAILED')
        self.assertEqual(classify_returncode('tests', 1), 'TEST_FAILED')
        self.assertEqual(classify_returncode('tests', 0), 'PASS')

    def test_runner_uses_only_disposable_venv_and_hash_locked_runtime_files(self):
        root = Path(__file__).resolve().parents[1]
        text = (root / 'scripts/run_validation_venv.py').read_text(
            encoding='utf-8')
        self.assertIn("TemporaryDirectory(prefix='binana-validation-')", text)
        self.assertIn("'--require-hashes'", text)
        self.assertIn("'requirements.services.lock'", text)
        self.assertIn("'monitoring/requirements-monitoring.lock'", text)
        self.assertNotIn('sudo', text)


if __name__ == '__main__':
    unittest.main()
