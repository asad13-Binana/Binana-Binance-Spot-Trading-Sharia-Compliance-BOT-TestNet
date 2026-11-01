from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAIRS = (
    ("requirements.renewal.txt", "requirements.renewal.lock"),
    ("requirements.services.txt", "requirements.services.lock"),
    ("monitoring/requirements-monitoring.txt", "monitoring/requirements-monitoring.lock"),
)
HASH = "a" * 64


class DependencyConsistencyTests(unittest.TestCase):
    def test_shipped_direct_pins_match_locked_versions(self):
        # Independent observation: recompiling a direct pin must not downgrade it.
        for source, lock in PAIRS:
            locked = dict(re.findall(r"^([A-Za-z0-9_.-]+)==([^\s\\]+)",
                                     (ROOT / lock).read_text(encoding="utf-8"), re.M))
            for name, version in re.findall(r"^([A-Za-z0-9_.-]+)==([^\s\\]+)",
                                          (ROOT / source).read_text(encoding="utf-8"), re.M):
                with self.subTest(source=source, package=name):
                    self.assertEqual(version, locked.get(name), "direct source/lock mismatch")

    def run_checker(self, source="example==2.0\n", lock=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            if lock is None:
                lock = "example==2.0 \\\n    --hash=sha256:" + HASH + "\n"
            for source_path, lock_path in PAIRS:
                (root / source_path).parent.mkdir(parents=True, exist_ok=True)
                (root / source_path).write_text(source, encoding="utf-8")
                (root / lock_path).write_text(lock, encoding="utf-8")
            return subprocess.run(
                [sys.executable, "-B", str(ROOT / "scripts/verify_dependency_consistency.py"),
                 "--root", str(root)],
                capture_output=True, text=True, encoding="utf-8", timeout=30,
            )

    def test_matching_hash_locked_requirements_pass(self):
        result = self.run_checker()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_shipped_closures_are_accepted_by_release_gate(self):
        result = subprocess.run(
            [sys.executable, "-B", str(ROOT / "scripts/verify_dependency_consistency.py")],
            capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_conditional_transitive_pins_are_validated_without_host_filtering(self):
        for marker in ('python_version < "3.11"', 'sys_platform == "win32"'):
            with self.subTest(marker=marker):
                lock = ("example==2.0 \\\n    --hash=sha256:" + HASH + "\n"
                        "conditional==1.0 ; " + marker + " \\\n    --hash=sha256:" + HASH + "\n")
                result = self.run_checker(lock=lock)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_conditional_only_lock_cannot_cover_unconditional_direct_pin(self):
        result = self.run_checker(lock='example==2.0 ; sys_platform == "win32" \\\n    --hash=sha256:' + HASH + '\n')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("MISSING", result.stdout)

    def test_complementary_python_lock_variants_are_valid(self):
        lock = ('example==2.0 \\\n    --hash=sha256:' + HASH + '\n'
                'conditional==1.0 ; python_version < "3.11" \\\n    --hash=sha256:' + HASH + '\n'
                'conditional==2.0 ; python_version >= "3.11" \\\n    --hash=sha256:' + HASH + '\n')
        result = self.run_checker(lock=lock)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_conditional_transitive_pin_still_requires_hash(self):
        lock = ('example==2.0 \\\n    --hash=sha256:' + HASH + '\n'
                'conditional==1.0 ; python_version < "3.11"\n')
        result = self.run_checker(lock=lock)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("missing hashes", result.stdout)

    def test_unsupported_marker_fails_closed(self):
        result = self.run_checker(lock='example==2.0 ; invalid_marker \\\n    --hash=sha256:' + HASH + '\n')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)

    def test_conditional_direct_sources_require_explicit_checker_support(self):
        result = self.run_checker(source='example==2.0 ; sys_platform == "win32"\n')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)

    def test_lock_downgrade_is_rejected_with_package_diagnostic(self):
        result = self.run_checker(source="example==3.0\n")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("example", result.stdout)
        self.assertIn("3.0", result.stdout)
        self.assertIn("2.0", result.stdout)

    def test_missing_direct_requirement_is_rejected(self):
        result = self.run_checker(source="missing==1.0\n")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("missing", result.stdout)

    def test_unhashed_dependency_is_rejected(self):
        result = self.run_checker(lock="example==2.0\n")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("hash", result.stdout.lower())

    def test_conflicting_duplicate_pin_is_rejected(self):
        result = self.run_checker(source="example==2.0\nexample==3.0\n")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("duplicate", result.stdout.lower())

    def test_normalized_names_and_extras_match_distribution(self):
        result = self.run_checker(
            source="Example_Package[extra]==2.0\n",
            lock="example-package==2.0 \\\n    --hash=sha256:" + HASH + "\n",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_unsupported_unpinned_requirement_fails_closed(self):
        result = self.run_checker(source="example>=2.0\n")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("pin", result.stdout.lower())


if __name__ == "__main__":
    unittest.main()
