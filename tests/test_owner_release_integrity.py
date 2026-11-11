"""Release integrity must bind the current owner, without obsolete engines."""
from pathlib import Path
import hashlib
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class OwnerReleaseIntegrityTests(unittest.TestCase):
    def test_manifest_build_and_verify_work_without_the_retired_engines(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for relative in ("scripts", "services", "runtime_owner/owner",
                             "shared/sharia"):
                shutil.copytree(ROOT / relative, root / relative)
            for relative in ("RELEASE_MODE", "RELEASE_VERSION"):
                shutil.copy2(ROOT / relative, root / relative)
            for name in ("build_manifest.py", "verify_manifest.py"):
                result = subprocess.run([sys.executable, "-B", str(root / "scripts" / name)],
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            strategy = root / "runtime_owner/owner/binana-strategies/BinanaNfiSpot.py"
            strategy.write_bytes(strategy.read_bytes() + b"\n# unreviewed change\n")
            result = subprocess.run([sys.executable, "-B", str(root / "scripts/build_manifest.py")],
                                    capture_output=True, text=True, timeout=30)
            self.assertNotEqual(result.returncode, 0, "Rebuilding cannot approve a changed strategy")
