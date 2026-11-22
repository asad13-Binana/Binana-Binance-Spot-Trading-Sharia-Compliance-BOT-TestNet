"""Behavioral packaging checks: no cloud, account, Docker, or network access."""
from __future__ import annotations

import hashlib
import ast
import importlib.util
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from types import SimpleNamespace
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


def fixture_archive():
    """Only packaging needs a tiny archive; real pinned-source build is separate."""
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, raw in {
            "README.md": b"Packaging fixture, not an executable Freqtrade build.\n",
            "freqtrade/__init__.py": b"__version__ = '2026.10'\n",
            "ft_client/__init__.py": b"",
        }.items():
            member = tarfile.TarInfo("freqtrade-fixture/" + name)
            member.size = len(raw)
            archive.addfile(member, io.BytesIO(raw))
    return output.getvalue()


class OracleOwnerBundleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.builder = load("materialize_oracle_bundle")
        cls.preflight = load("oracle_preflight")
        cls.temporary = tempfile.TemporaryDirectory()
        cls.base = Path(cls.temporary.name)
        cls.archive = fixture_archive()
        with patch.object(cls.builder.clean, "UPSTREAM_SHA256",
                          hashlib.sha256(cls.archive).hexdigest()):
            cls.receipt = cls.builder.materialize(ROOT, cls.base / "one", cls.archive)
            cls.builder.materialize(ROOT, cls.base / "two", cls.archive)
        cls.bundle = cls.base / "one"
        cls.compose = json.loads((cls.bundle / "compose.json").read_text())

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_standalone_topology_launches_recovered_owner_only(self):
        services = self.compose["services"]
        self.assertEqual(set(services), {
            "freqtrade", "universe", "sharia-screener", "telegram-broker", "owner-monitor",
            "sharia-research", "sharia-egress-proxy",
        })
        command = services["freqtrade"]["command"]
        self.assertIn("BinanaNfiSpot", command)
        self.assertNotIn("IctSmcStrategy", command)
        self.assertEqual(services["freqtrade"]["entrypoint"], ["/usr/local/bin/freqtrade"])
        self.assertNotIn("execution-sidecar", services)
        self.assertTrue(all("extends" not in row for row in services.values()))
        self.assertEqual(services["freqtrade"]["depends_on"]["universe"]["condition"], "service_healthy")
        self.assertEqual(services["sharia-research"]["profiles"], ["research"])
        self.assertEqual(services["sharia-egress-proxy"]["profiles"], ["research"])

    def test_package_contains_complete_support_import_graph_without_order_engine(self):
        missing = self.builder.missing_local_imports(self.bundle / "services/services")
        self.assertEqual(missing, [])
        self.assertTrue((self.bundle / "services/runtime_stable_panel.py").is_file())
        self.assertTrue((self.bundle / "services/services/market_context/l2_book.py").is_file())
        self.assertTrue((self.bundle / "services/services/sharia_screener/manual_registry.py").is_file())
        self.assertFalse((self.bundle / "services/services/execution_sidecar").exists())
        self.assertFalse((self.bundle / "services/legacy_core").exists())
        paths = [p.relative_to(self.bundle).as_posix() for p in self.bundle.rglob("*")]
        self.assertFalse(any(p.endswith((".sqlite", ".env", ".pem")) for p in paths))

    def test_config_preserves_strategy_policy_and_stops_on_fresh_host(self):
        config = json.loads((self.bundle / "config.example.json").read_text())
        self.assertEqual(config["strategy"], "BinanaNfiSpot")
        self.assertEqual(config["initial_state"], "stopped")
        self.assertEqual(config["timeframe"], "5m")
        self.assertEqual((config["stake_amount"], config["max_open_trades"]), (250, 4))
        self.assertEqual(config["binana"]["flow_policy"]["normal_buy_ratio"], .52)
        self.assertFalse(config["binana"]["allow_live"])
        self.assertTrue(config["exchange"]["binana_testnet"])
        self.assertNotIn("binana-20260910-owner-cutover-1", json.dumps(config))
        with self.assertRaisesRegex(ValueError, "EPOCH"):
            self.preflight.validate_config(config)
        config["binana"]["testnet_epoch_id"] = "owner-reviewed-epoch-20261021"
        self.preflight.validate_config(config)
        for mutate in (
            lambda c: c.update(initial_state="running"),
            lambda c: c["binana"].update(environment="live"),
            lambda c: c["exchange"].update(binana_testnet=False),
            lambda c: c["exchange"].update(ccxt_config={"urls": {"api": "https://api.binance.com"}}),
            lambda c: c.update(strategy="IctSmcStrategy"),
        ):
            invalid = json.loads(json.dumps(config))
            mutate(invalid)
            with self.assertRaises(ValueError):
                self.preflight.validate_config(invalid)

    def test_manifest_detects_missing_changed_and_extra_file(self):
        self.preflight.verify_bundle(self.bundle)
        target = self.bundle / "config.example.json"
        original = target.read_bytes()
        try:
            target.write_bytes(original + b" ")
            with self.assertRaisesRegex(ValueError, "BUNDLE"):
                self.preflight.verify_bundle(self.bundle)
        finally:
            target.write_bytes(original)
        target = self.bundle / "private.env"
        try:
            target.write_text("EXAMPLE_PRIVATE=not-a-key\n")
            with self.assertRaisesRegex(ValueError, "BUNDLE"):
                self.preflight.verify_bundle(self.bundle)
        finally:
            target.unlink()
        self.preflight.verify_bundle(self.bundle)

    def test_bundle_is_reproducible_across_destinations(self):
        def content(path):
            return {p.relative_to(path).as_posix(): p.read_bytes()
                    for p in path.rglob("*") if p.is_file()}
        self.assertEqual(content(self.base / "one"), content(self.base / "two"))

    def test_owner_secret_and_privilege_boundaries(self):
        services = self.compose["services"]
        for name, service in services.items():
            self.assertEqual(service["cap_drop"], ["ALL"])
            self.assertTrue(service["read_only"])
            self.assertIn("no-new-privileges:true", service["security_opt"])
            self.assertNotIn("/var/run/docker.sock", json.dumps(service))
            if name != "freqtrade":
                self.assertNotIn("FREQTRADE__EXCHANGE__SECRET", service["environment"])
                self.assertNotIn("BINANCE_API_SECRET", service["environment"])
        self.assertEqual(services["owner-monitor"]["network_mode"], "none")
        self.assertEqual(services["sharia-screener"]["network_mode"], "none")
        self.assertNotIn("/home/ubuntu/", json.dumps(self.compose))
        self.assertLessEqual(round(sum(row["cpus"] for row in services.values()), 6), 2.0)
        self.assertEqual(services["sharia-research"]["networks"], ["sharia-egress"])
        self.assertTrue(self.compose["networks"]["sharia-egress"]["internal"])

    def test_monitor_can_write_only_derived_runtime_outputs(self):
        monitor = self.compose['services']['owner-monitor']
        state = self.builder.required('BINANA_STATE_DIR')
        writable = [row['source'] for row in monitor['volumes'] if not row['read_only']]
        self.assertEqual(writable, [state + '/runtime'])
        root = next(row for row in monitor['volumes'] if row['target'] == '/app/shared')
        self.assertTrue(root['read_only'])

    def test_approved_all_eligible_universe_is_not_recapped_at_120(self):
        self.assertEqual(self.compose["services"]["universe"]["environment"]["UNIVERSE_LIMIT"],
                         "${UNIVERSE_LIMIT:-0}")
        values = self.preflight.parse_environment(self.bundle / "oracle.env.example")
        self.assertEqual(values["UNIVERSE_LIMIT"], "0")

    def test_no_source_symlink_or_existing_destination_is_accepted(self):
        with self.assertRaises(FileExistsError):
            self.builder.materialize(ROOT, self.bundle, self.archive)

    def test_missing_private_setup_is_not_reported_as_accepted(self):
        with self.assertRaisesRegex(ValueError, "PRIVATE_ENVIRONMENT_REQUIRED"):
            self.preflight.validate_private_inputs(self.base / "absent-private.env")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_dir, state_dir, release_dir = [root / name for name in ("config", "state", "release")]
            for path in (config_dir, state_dir, release_dir):
                path.mkdir()
            config = self.builder.reference_config(ROOT)
            config["binana"]["testnet_epoch_id"] = "offline-fixture-epoch-no-account"
            (config_dir / "config.json").write_text(json.dumps(config))
            (config_dir / "release-authority.ed25519.pub").write_bytes(b"\x01" * 32)
            for name in ("freqtrade/logs", "sharia", "universe", "runtime"):
                (state_dir / name).mkdir(parents=True, exist_ok=True)
            (state_dir / "sharia/halal_coins.json").write_text('{"symbols":[]}')
            values = self.preflight.parse_environment(self.bundle / "oracle.env.example")
            for name, value in values.items():
                if not value:
                    values[name] = "offline-test-placeholder-" + "0" * 40
            values.update(BINANA_CONFIG_DIR=str(config_dir), BINANA_STATE_DIR=str(state_dir),
                          BINANA_RELEASE_DIR=str(release_dir))
            env = root / "runtime.env"
            env.write_text("".join(name + "=" + value + "\n" for name, value in values.items()))
            env.chmod(0o600)
            status = self.preflight.validate_private_inputs(env)
            self.assertTrue(status["configuration_valid"])
            self.assertFalse(status["host_accepted"])
            self.assertFalse(status["trading_authorized"])
            self.assertFalse(status["certificate_present"])
            (release_dir / "issuer.ed25519").write_bytes(b"\x02" * 32)
            with self.assertRaisesRegex(ValueError, "ISSUER"):
                self.preflight.validate_private_inputs(env)

    def test_bad_archive_is_rejected_before_output_creation(self):
        output = self.base / "bad-archive"
        with self.assertRaisesRegex(ValueError, "archive hash mismatch"):
            self.builder.materialize(ROOT, output, b"not an upstream archive")
        self.assertFalse(output.exists())

    def test_research_unavailable_is_visible_and_never_queues(self):
        guard = load("oracle_panel")
        queued, messages = [], []
        request = guard.guarded_action(lambda: queued.append("request"),
                                       lambda: False, messages.append)
        with self.assertRaises(guard.ResearchUnavailable):
            request()
        self.assertEqual(queued, [])
        self.assertIn("No request or decision was queued", messages[0])
        self.assertFalse(guard.research_ready(
            {"ok": True, "ready_for_screening": True, "ts": 10}, enabled=True, now=200))
        health = {"ok": True, "ready_for_screening": True, "ts": 100}
        self.assertFalse(guard.research_ready(health, enabled=False, now=101))
        guard.guarded_action(lambda: queued.append("request"),
                             lambda: guard.research_ready(health, enabled=True, now=101),
                             messages.append)()
        self.assertEqual(queued, ["request"])

    def test_current_market_and_universe_sources_are_used(self):
        scanner = (self.bundle / "services/services/universe_service/scanner.py").read_text()
        self.assertIn("0 <= LIMIT <= 200", scanner)
        self.assertIn("{'BTC', 'ETH', 'BNB', 'SOL', 'XRP'}", scanner)
        self.assertIn("None if LIMIT == 0 else LIMIT", scanner)
        market = (self.bundle / "services/services/market_context/service.py").read_text()
        self.assertIn("group_readable=True", market)
        broker = (self.bundle / "services/services/telegram_broker/bot.py").read_text(encoding="utf-8")
        self.assertIn("def registry_owner_command(", broker)
        self.assertIn("def _sidecar_read(", broker)
        self.assertIn("atomic_write_json(ALERT_OUTBOX_HEALTH, payload, group_readable=True)", broker)
        for relative in ('services/telegram_broker/bot.py', 'services/sharia_screener/service.py',
                         'services/universe_service/snapshot_store.py'):
            self.assertEqual((self.bundle / 'services' / relative).read_bytes(),
                             (ROOT / relative).read_bytes())
        strategy = ROOT / "runtime_owner/owner/binana-strategies/BinanaNfiSpot.py"
        self.assertEqual((self.bundle / "owner/owner/binana-strategies/BinanaNfiSpot.py").read_bytes(),
                         strategy.read_bytes())

    def test_generated_scanner_binds_current_validated_listing_age_cache(self):
        from services.universe_service.listing_age import ListingAgeCache
        scanner = self.bundle / "services/services/universe_service/scanner.py"
        tree = ast.parse(scanner.read_bytes())
        bindings = [node for node in tree.body if
                    isinstance(node, ast.ClassDef) and node.name == 'ListingAgeCache' or
                    isinstance(node, ast.ImportFrom) and
                    node.module == 'services.universe_service.listing_age']
        namespace = {'Path': Path, 'BinancePublic': object}
        exec(compile(ast.Module(body=bindings, type_ignores=[]), str(scanner), 'exec'), namespace)
        self.assertIs(namespace['ListingAgeCache'], ListingAgeCache)
        self.assertEqual((self.bundle / 'services/services/universe_service/listing_age.py').read_bytes(),
                         (ROOT / 'services/universe_service/listing_age.py').read_bytes())

    def test_generated_snapshot_store_preserves_all_eligible_boundaries(self):
        spec = importlib.util.spec_from_file_location('bundle_snapshot_store',
            self.bundle / 'services/services/universe_service/snapshot_store.py')
        snapshots = importlib.util.module_from_spec(spec)
        with patch.object(sys, 'dont_write_bytecode', True):
            spec.loader.exec_module(snapshots)
        for count in (0, 1, 117, 120, 121, 376):
            with self.subTest(count=count):
                rows = [{'rank': i + 1, 'pair': f'ASSET{i}/USDT',
                         'base': f'ASSET{i}', 'symbol': f'ASSET{i}USDT'} for i in range(count)]
                self.assertEqual(snapshots._validate_publication(rows,
                    {'limit_mode': 'all_eligible', 'limit': count}, 900), count)

    def test_bundle_contains_portable_host_maintenance_outside_images(self):
        root = self.bundle / "maintenance"
        for relative in ("scripts/recovery/issue_candidate_release.py",
                         "scripts/recovery/install_release_renewal.py",
                         "scripts/recovery/release_host.py",
                         "scripts/recovery/deploy_candidate_owner.py",
                         "runtime_owner/deployment/release_authority.py",
                         "requirements.renewal.txt", "requirements.renewal.lock",
                         "release-host.oracle.example.json", "README.md"):
            self.assertTrue((root / relative).is_file(), relative)
        profile = json.loads((root / "release-host.oracle.example.json").read_text())
        self.assertEqual(profile["project"], self.compose["name"])
        self.assertIn(profile["service"], self.compose["services"])
        self.assertFalse((self.bundle / "owner/maintenance").exists())
        self.assertFalse((self.bundle / "services/maintenance").exists())

    def test_generated_owner_mounts_pass_host_renewal_validation(self):
        spec = importlib.util.spec_from_file_location(
            "bundle_release_host", ROOT / "scripts/recovery/release_host.py")
        host = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(host)
        context = object.__new__(host.HostContext)
        context.project, context.service = self.compose["name"], "freqtrade"
        context.paths = {"config": self.base / "config/config.json",
                         "shared": self.base / "shared", "release": self.base / "release",
                         "issuer": self.base / "authority/issuer.ed25519"}
        replacements = {self.builder.required("BINANA_STATE_DIR"): context.paths["shared"],
                        self.builder.required("BINANA_CONFIG_DIR"): context.paths["config"].parent,
                        self.builder.required("BINANA_RELEASE_DIR"): context.paths["release"]}
        mounts = []
        for row in self.compose["services"]["freqtrade"]["volumes"]:
            for prefix, source in replacements.items():
                if row["source"].startswith(prefix):
                    suffix = row["source"][len(prefix):].lstrip("/")
                    source = source / suffix if suffix else source
                    break
            mounts.append({"Type": "bind", "Source": str(source),
                           "Destination": row["target"], "RW": not row["read_only"]})
        container = {"Id": "fixture-owner", "State": {"Running": True},
                     "Config": {"Labels": {"com.docker.compose.project": context.project,
                                           "com.docker.compose.service": context.service}},
                     "Mounts": mounts}
        context.deployment = SimpleNamespace(run=lambda command, **kwargs:
            b"fixture-owner\n" if command[1] == "ps" else json.dumps([container]).encode())
        with patch.object(host, "checked_path", side_effect=Path):
            self.assertEqual(context.inspect()["Id"], "fixture-owner")


if __name__ == "__main__":
    unittest.main()
