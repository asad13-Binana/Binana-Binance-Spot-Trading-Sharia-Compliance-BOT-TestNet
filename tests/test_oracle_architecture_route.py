"""An Oracle bootstrap must not silently select the retained sidecar engine."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class OracleArchitectureRouteTests(unittest.TestCase):
    def test_legacy_bootstrap_requires_explicit_selection_before_host_probe(self):
        bash = shutil.which("bash")
        self.assertIsNotNone(bash, "Run this regression in the Linux test image")
        for arguments in ([], ["--unknown"], ["--legacy-sidecar-recovery", "extra"]):
            with self.subTest(arguments=arguments), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                marker = directory / "host-probe"
                probe = directory / "dirname"
                probe.write_text(f"#!/bin/bash\nprintf probe > '{marker}'\nexit 89\n")
                probe.chmod(0o700)
                result = subprocess.run(
                    [bash, str(ROOT / "deploy/oracle_setup.sh"), *arguments],
                    env={**os.environ, "PATH": str(directory)},
                    capture_output=True, text=True, timeout=5,
                )
                self.assertEqual(result.returncode, 64, result.stdout + result.stderr)
                self.assertIn("ORACLE_OWNER_BUNDLE.md", result.stderr)
                self.assertFalse(marker.exists(), "Host probing ran before architecture selection")

    def test_primary_guide_routes_to_versioned_owner_bundle(self):
        guide = (ROOT / "docs/ORACLE_DEPLOYMENT_GUIDE.md").read_text(encoding="utf-8")
        self.assertIn("[ORACLE_OWNER_BUNDLE.md](ORACLE_OWNER_BUNDLE.md)", guide)
        self.assertIn("Freqtrade **2026.9**", guide)
        self.assertNotIn("sudo bash deploy/oracle_setup.sh", guide)
        self.assertNotIn("starts the 6-service stack", guide)


if __name__ == "__main__":
    unittest.main()
