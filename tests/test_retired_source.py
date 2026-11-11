"""Historical lookup is read-only, hash-bound, and never an engine restore."""
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/resolve_retired_source.py"


class RetiredSourceTests(unittest.TestCase):
    def invoke(self, path, digest):
        return subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "--root", str(ROOT),
             "--path", path, "--expected-sha256", digest],
            capture_output=True, timeout=20,
        )

    def test_historical_filter_bytes_are_readable_without_restoring_a_file(self):
        path = "services/execution_sidecar/filters.py"
        before = (ROOT / path).exists()
        result = self.invoke(path, "5897ca3ab71c2bda9e096b51aeaf3e9abae40c26048cc615b3ded5bbe6d417a8")
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        import hashlib
        self.assertEqual(hashlib.sha256(result.stdout).hexdigest(),
                         "5897ca3ab71c2bda9e096b51aeaf3e9abae40c26048cc615b3ded5bbe6d417a8")
        self.assertEqual((ROOT / path).exists(), before)

    def test_wrong_hash_never_releases_historical_bytes(self):
        result = self.invoke("services/execution_sidecar/filters.py", "0" * 64)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")

    def test_path_traversal_never_releases_bytes(self):
        result = self.invoke("../secrets", "0" * 64)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")

    def test_unregistered_path_never_releases_bytes(self):
        result = self.invoke("runtime_owner/PROVENANCE.json", "0" * 64)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")
