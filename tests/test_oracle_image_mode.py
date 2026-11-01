"""Image admission selects verified package fixtures without changing runtime mode."""
import hashlib
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class OracleImageModeTests(unittest.TestCase):
    def setUp(self):
        path = ROOT / "runtime_owner/deployment/validate_image.py"
        spec = importlib.util.spec_from_file_location("oracle_image_mode_validator", path)
        self.validator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.validator)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.profile = self.root / "freqtrade/binana/package_profile.py"
        self.profile.parent.mkdir(parents=True)

    def fixture(self, mode):
        raw = ("PACKAGE_ENVIRONMENT = " + repr(mode) + "\n").encode()
        self.profile.write_bytes(raw)
        return {"files": {"owner/freqtrade/binana/package_profile.py": {
            "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}}}

    def configure(self, manifest):
        self.assertTrue(hasattr(self.validator, "configure_test_environment"),
                        "image validator must supply the verified package mode before discovery")
        return self.validator.configure_test_environment(self.root, manifest)

    def test_both_verified_packages_select_their_own_test_expectation(self):
        for mode in ("testnet", "live"):
            with self.subTest(mode=mode), patch.dict(os.environ, {}, clear=True):
                manifest = self.fixture(mode)
                before = self.profile.read_bytes()
                self.assertEqual(self.configure(manifest), mode)
                self.assertEqual(os.environ["BINANA_TEST_EXPECTED_MODE"], mode)
                self.assertEqual(self.profile.read_bytes(), before)
                self.assertEqual(set(os.environ), {"BINANA_TEST_EXPECTED_MODE"})

    def test_matching_external_expectation_is_accepted(self):
        for mode in ("testnet", "live"):
            with self.subTest(mode=mode), patch.dict(os.environ, {"BINANA_TEST_EXPECTED_MODE": mode}, clear=True):
                self.assertEqual(self.configure(self.fixture(mode)), mode)

    def test_foreign_external_expectation_fails_without_rewriting_it(self):
        for mode in ("testnet", "live"):
            other = "live" if mode == "testnet" else "testnet"
            with self.subTest(mode=mode), patch.dict(os.environ, {"BINANA_TEST_EXPECTED_MODE": other}, clear=True):
                with self.assertRaisesRegex(RuntimeError, "TEST_EXPECTED_MODE_MISMATCH"):
                    self.configure(self.fixture(mode))
                self.assertEqual(os.environ["BINANA_TEST_EXPECTED_MODE"], other)

    def test_unknown_and_unverified_profiles_are_rejected(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "INVALID_PACKAGE_ENVIRONMENT"):
                self.configure(self.fixture("production"))
            with self.assertRaisesRegex(RuntimeError, "VERIFIED_PACKAGE_PROFILE_REQUIRED"):
                self.configure({"files": {}})
            manifest = self.fixture("live")
            self.profile.write_text("PACKAGE_ENVIRONMENT = 'testnet'\n")
            with self.assertRaisesRegex(RuntimeError, "PACKAGE_PROFILE_PROVENANCE_MISMATCH"):
                self.configure(manifest)
            self.assertNotIn("BINANA_TEST_EXPECTED_MODE", os.environ)


if __name__ == "__main__":
    unittest.main()
