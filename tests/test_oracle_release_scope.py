"""Pure certificate payload tests; no signing, host receipt or account access."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


class OracleReleaseScopeTests(unittest.TestCase):
    def setUp(self):
        path = ROOT / "runtime_owner/deployment/release_authority.py"
        spec = importlib.util.spec_from_file_location("oracle_scope_authority", path)
        self.authority = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.authority)
        self.status = {key: "a" * 64 for key in (
            "code_sha256", "config_sha256", "exchange_epoch_id", "account_key_sha256",
            "deployment_sha256", "dependency_sha256")}
        self.status.update(execution_environment="LIVE", account_scope="offline-live-account",
                           generated_at=100, blockers=[],
                           authenticated_reconciliation={"ok": True, "checked_at": 100},
                           retained_inventory={"ok": True, "checked_at": 100})

    def payload(self, status):
        # Receipt validation is independently covered by owner authority tests.
        # This test isolates payload binding and never issues a certificate.
        with patch.object(self.authority, "validate_receipts"):
            return self.authority.build_payload(status, "offline-image", {}, now=100)

    def test_live_environment_and_account_survive_issuance_payload(self):
        payload = self.payload(self.status)
        self.assertEqual(payload.get("execution_environment"), "LIVE")
        self.assertEqual(payload.get("account_scope"), "offline-live-account")
        self.assertIn("LIVE", payload["scope"])
        self.assertNotIn("Testnet", payload["scope"])
        for key in ("execution_environment", "account_scope"):
            self.assertIn(key, self.authority.BINDINGS)

    def test_missing_or_unknown_environment_cannot_be_inferred(self):
        for value in (None, "", "production", "live"):
            with self.subTest(value=value):
                status = dict(self.status, execution_environment=value)
                with self.assertRaises(ValueError):
                    self.payload(status)
        with self.assertRaises(ValueError):
            self.payload(dict(self.status, account_scope=""))


if __name__ == "__main__":
    unittest.main()
