"""Oracle preparation is bound to a package mode and never enables LIVE."""
import importlib.util
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import tarfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "runtime_owner/deployment"


def load(name):
    spec = importlib.util.spec_from_file_location(name, DEPLOY / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    previous = sys.path[:]
    try:
        sys.path.insert(0, str(DEPLOY))
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = previous
    return module


class OraclePackageModeTests(unittest.TestCase):
    def setUp(self):
        self.builder = load("materialize_oracle_bundle")
        self.preflight = load("oracle_preflight")

    def test_active_policy_template_does_not_read_archived_aws_input(self):
        original = self.builder.source_bytes
        def public_source(root, relative):
            self.assertNotIn("AWS_POLICY", relative)
            return original(root, relative)
        with patch.object(self.builder, "source_bytes", side_effect=public_source):
            config = self.builder.reference_config(ROOT)
        self.assertEqual((config["stake_amount"], config["max_open_trades"]), (250, 4))

    def test_clean_context_does_not_require_retained_image_dockerfile(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            deployment = source / "deployment"
            deployment.mkdir(parents=True)
            (source / "PROVENANCE.json").write_text('{"files":{}}')
            for name in ("validate_image.py", "release_authority.py", "requirements.owner.lock",
                         "requirements.build.lock", "Dockerfile.clean"):
                (deployment / name).write_text("# isolated packaging fixture\n")
            with patch.object(self.builder.clean, "source_files", return_value={}):
                self.builder.clean.clean_context(source, root / "output", b"fixture")
            self.assertTrue((root / "output/Dockerfile.clean").is_file())
            self.assertFalse((root / "output/Dockerfile").exists())

    def test_complete_bundle_binds_repository_profile_and_rejects_mode_swap(self):
        environment = (ROOT / "RELEASE_MODE").read_text().strip()
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as out:
            data = b"Offline packaging fixture, not an executable Freqtrade build.\n"
            item = tarfile.TarInfo("fixture/README.md")
            item.size = len(data)
            out.addfile(item, io.BytesIO(data))
        raw = archive.getvalue()
        with tempfile.TemporaryDirectory() as temporary:
            bundle = Path(temporary) / "bundle"
            with patch.object(self.builder.clean, "UPSTREAM_SHA256", hashlib.sha256(raw).hexdigest()):
                self.builder.materialize(ROOT, bundle, raw)
            manifest = self.preflight.verify_bundle(bundle)
            self.assertEqual(manifest["execution_environment"], environment)
            self.assertEqual((bundle / "services/RELEASE_MODE").read_text().strip(), environment)
            config = json.loads((bundle / "config.example.json").read_text())
            self.assertEqual(config["binana"]["environment"], environment)
            self.assertFalse(config["binana"]["allow_live"])
            self.assertFalse((bundle / "owner/Dockerfile").exists())
            manifest["execution_environment"] = "live" if environment == "testnet" else "testnet"
            (bundle / "BUNDLE_MANIFEST.json").write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "ENVIRONMENT_MISMATCH"):
                self.preflight.verify_bundle(bundle)

    def test_live_preparation_is_stopped_and_uses_separate_account_binding(self):
        with patch.object(self.builder, "package_environment", return_value="live", create=True):
            config = self.builder.reference_config(ROOT)
        self.assertEqual(config["binana"]["environment"], "live")
        self.assertFalse(config["binana"]["allow_live"])
        self.assertEqual(config["binana"]["live_activation"], "")
        self.assertEqual(config["initial_state"], "stopped")
        self.assertFalse(config["exchange"]["binana_testnet"])
        self.assertNotIn("testnet_epoch_id", config["binana"])
        self.assertIn("REPLACE", config["binana"]["account_scope"])
        self.assertIn("REPLACE", config["binana"]["exchange_epoch_id"])
        config["binana"].update(account_scope="offline-live-account", exchange_epoch_id="offline-live-epoch")
        self.preflight.validate_config(config, environment="live")
        for mutation in ({"allow_live": True}, {"live_activation": "I_AUTHORIZE_BINANA_LIVE_SPOT"},
                         {"environment": "testnet"}, {"account_scope": "REPLACE_ACCOUNT"}):
            invalid = json.loads(json.dumps(config))
            invalid["binana"].update(mutation)
            with self.assertRaises(ValueError):
                self.preflight.validate_config(invalid, environment="live")
        with self.assertRaises(ValueError):
            self.preflight.validate_config(config, environment="testnet")

    def test_live_topology_scopes_only_live_credentials_and_separate_identity(self):
        compose = self.builder.topology("a" * 64, environment="live")
        self.assertEqual(compose["name"], "binana-oracle-live")
        owner = compose["services"]["freqtrade"]
        env = owner["environment"]
        self.assertEqual(env["FREQTRADE__INITIAL_STATE"], "stopped")
        self.assertEqual(env["FREQTRADE__EXCHANGE__BINANA_TESTNET"], "false")
        self.assertIn("BINANCE_LIVE_API_KEY", env["FREQTRADE__EXCHANGE__KEY"])
        self.assertNotIn("BINANCE_TESTNET_API_KEY", json.dumps(compose))
        self.assertEqual(owner["ports"], ["127.0.0.1:8091:8080"])
        for name, service in compose["services"].items():
            if name != "freqtrade":
                self.assertNotIn("BINANCE_LIVE_API_KEY", json.dumps(service))
        self.assertNotIn("execution-sidecar", compose["services"])


if __name__ == "__main__":
    unittest.main()
