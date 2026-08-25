import unittest
from pathlib import Path

from scripts.verify_github_action_pins import verify_repository, workflow_pin_errors

ROOT = Path(__file__).resolve().parents[1]


class GitHubActionPinTests(unittest.TestCase):
    def test_repository_uses_only_verified_immutable_action_pins(self) -> None:
        errors, files, references = verify_repository(ROOT)
        self.assertGreaterEqual(files, 2)
        self.assertGreater(references, 0)
        self.assertEqual(errors, [])

    def test_regression_rejects_the_invalid_setup_python_sha(self) -> None:
        errors, references = workflow_pin_errors(
            "uses: actions/setup-python@0b93645e9fea731f9e45d8d6c3b548447c85004",
            "bad.yml",
        )
        self.assertEqual(references, 1)
        self.assertEqual(len(errors), 1)
        self.assertIn("40-character commit SHA", errors[0])

    def test_moving_tag_is_rejected(self) -> None:
        errors, _ = workflow_pin_errors("uses: actions/setup-python@v5", "bad.yml")
        self.assertEqual(len(errors), 1)
        self.assertIn("40-character commit SHA", errors[0])

    def test_unreviewed_external_action_is_rejected(self) -> None:
        errors, _ = workflow_pin_errors(
            "uses: example/unknown-action@0123456789012345678901234567890123456789",
            "bad.yml",
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("not in the verified allowlist", errors[0])

    def test_mutable_docker_action_is_rejected(self) -> None:
        errors, _ = workflow_pin_errors("uses: docker://alpine:latest", "bad.yml")
        self.assertEqual(len(errors), 1)
        self.assertIn("not pinned by sha256 digest", errors[0])


if __name__ == "__main__":
    unittest.main()
