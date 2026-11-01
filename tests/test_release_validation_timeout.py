"""Renewal validation has bounded diagnostics and cleans only its own container."""
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/recovery"))
import issue_candidate_release as issuer
import release_host


class ValidationTimeoutTests(unittest.TestCase):
    def test_hanging_validator_is_removed_by_unique_identity_and_logs_survive(self):
        calls = []
        def run(command, **kwargs):
            calls.append((command, kwargs))
            if command[1] == "run":
                raise subprocess.TimeoutExpired(command, kwargs["timeout"],
                                                output=b"last progress", stderr=b"blocked validator")
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        with tempfile.TemporaryDirectory() as temporary, patch.object(issuer.subprocess, "run", side_effect=run):
            evidence = Path(temporary)
            with self.assertRaises(subprocess.TimeoutExpired):
                issuer.refresh_offline(evidence, "sha256:" + "b" * 64)
            self.assertEqual(len(calls), 2)
            command = calls[0][0]
            name = command[command.index("--name") + 1]
            self.assertRegex(name, r"^binana-release-check-[a-f0-9]{32}$")
            self.assertEqual(calls[1][0], ["docker", "rm", "--force", name])
            self.assertLessEqual(calls[1][1]["timeout"], 30)
            self.assertEqual(next(evidence.rglob("1.stdout")).read_bytes(), b"last progress")
            self.assertEqual(next(evidence.rglob("1.stderr")).read_bytes(), b"blocked validator")
            self.assertFalse((evidence / "offline.json").exists())

    def test_timeout_cleanup_failure_is_explicit_and_cannot_publish_success(self):
        def run(command, **kwargs):
            if command[1] == "run":
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            return SimpleNamespace(returncode=1, stdout=b"", stderr=b"fixture engine unavailable")
        with tempfile.TemporaryDirectory() as temporary, patch.object(issuer.subprocess, "run", side_effect=run):
            with self.assertRaisesRegex(RuntimeError, "VALIDATION_CONTAINER_CLEANUP_FAILED"):
                issuer.refresh_offline(Path(temporary), "sha256:" + "b" * 64)
            self.assertFalse((Path(temporary) / "offline.json").exists())

    def test_owner_inspection_operations_have_short_timeout_budget(self):
        context = object.__new__(release_host.HostContext)
        context.project, context.service = "fixture", "freqtrade"
        calls = []
        def run(command, **kwargs):
            calls.append(kwargs)
            return b""  # No owner is safe failure, before reading any host path.
        context.deployment = SimpleNamespace(run=run)
        with self.assertRaisesRegex(RuntimeError, "ONE_OWNER"):
            context.inspect()
        self.assertEqual(calls, [{"timeout": 30}])


if __name__ == "__main__":
    unittest.main()
