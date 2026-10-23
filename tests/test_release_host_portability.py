"""Portable host issuance and transactional installer checks; all I/O is disposable."""
import base64
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts/recovery"))
import issue_candidate_release as issuer
import install_release_renewal as installer


@unittest.skipUnless(os.name == "posix" and os.geteuid() == 0, "requires isolated Linux root fixture")
class PortableReleaseTests(unittest.TestCase):
    def setUp(self):
        for attribute in ("CONFIG", "SHARED", "DB", "RELEASE", "ISSUER", "OWNER", "EVIDENCE_ROOT", "HOST_CONTEXT"):
            guard = patch.object(issuer.deploy, attribute, getattr(issuer.deploy, attribute))
            guard.start()
            self.addCleanup(guard.stop)
        previous_umask = os.umask(0o077)
        os.umask(previous_umask)
        self.addCleanup(os.umask, previous_umask)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name in ("config", "shared/freqtrade/runtime", "release", "authority", "evidence", "units"):
            (self.root / name).mkdir(parents=True, mode=0o700, exist_ok=True)
        self.config = self.root / "config/config.json"
        self.config.write_text(json.dumps({"dry_run": False, "trading_mode": "spot",
            "binana": {"environment": "testnet"}, "exchange": {"binana_testnet": True}}))
        self.key = Ed25519PrivateKey.generate()
        self.key_path = self.root / "authority/issuer.ed25519"
        self.key_path.write_bytes(self.key.private_bytes(serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw, serialization.NoEncryption()))
        self.key_path.chmod(0o600)
        (self.config.parent / "release-authority.ed25519.pub").write_bytes(
            self.key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))
        self.profile = self.root / "host.json"
        self.profile_data = {"schema": "binana-release-host-v1", "project": "binana-oracle-testnet",
            "service": "freqtrade", "config": str(self.config), "shared": str(self.root / "shared"),
            "release": str(self.root / "release"), "issuer": str(self.key_path),
            "evidence_root": str(self.root / "evidence")}
        self.write_profile()
        self.evidence = self.root / "evidence/acceptance"
        self.evidence.mkdir(mode=0o700)
        self.image = "sha256:" + "b" * 64
        stamp = time.time()
        self.status = {k: "a" * 64 for k in issuer.deploy.authority.BINDINGS}
        self.status.update(generated_at=stamp, blockers=["release_evidence_unavailable"],
            authenticated_reconciliation={"ok": True, "checked_at": stamp, "open_orders": 0, "open_lists": 0},
            retained_inventory={"ok": True, "checked_at": stamp})
        self.readiness = self.root / "shared/freqtrade/runtime/owner_readiness.json"
        self.readiness.write_text(json.dumps(self.status))
        self.container = {"Id": "c" * 64, "Image": self.image,
            "State": {"Running": True, "StartedAt": "2026-01-01T00:00:00Z", "Health": {"Status": "healthy"}},
            "HostConfig": {"ReadonlyRootfs": True, "Init": True, "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges:true"]},
            "Config": {"Env": ["FIXTURE_ONLY=true"], "Cmd": ["trade"], "Entrypoint": ["freqtrade"], "User": "999:987",
                "Labels": {"com.docker.compose.project": "binana-oracle-testnet", "com.docker.compose.service": "freqtrade"}},
            "Mounts": [{"Type": "bind", "Source": str(self.root / src), "Destination": dst, "RW": rw}
                for src, dst, rw in (("config", "/freqtrade/binana-config", False),
                    ("release", "/freqtrade/binana-release", False), ("shared", "/freqtrade/shared", False),
                    ("shared/freqtrade", "/freqtrade/user_data", True))]}
        self.record = {"image": self.image, "environment_sha256":
            hashlib.sha256(json.dumps(sorted(self.container["Config"]["Env"])).encode()).hexdigest(),
            "security_opt": self.container["HostConfig"]["SecurityOpt"],
            "mounts": sorted(self.container["Mounts"], key=lambda m: m["Destination"]),
            "command": ["trade"], "entrypoint": ["freqtrade"], "user": "999:987"}
        (self.root / "release/deployment.json").write_text(json.dumps(self.record))
        config_sha = hashlib.sha256(self.config.read_bytes()).hexdigest()
        for name, slots in (("lifecycle", None), ("one_slot", 1), ("four_slot", 4)):
            phases = ["entry", "restart", "exit", "closed_restart"]
            if slots is None:
                phases.insert(2, "restart_again")
            row = {"image": self.image, "passed": True, "source_config_sha256": config_sha,
                "account_key_sha256": self.status["account_key_sha256"], "slot_soak": slots,
                "soak_seconds_observed": 601, "live_writable_mounts": False, "source_preserved": True,
                "open_exchange_orders": 0, "open_exchange_lists": 0,
                "phases": [{"phase": p, "passed": True} for p in phases]}
            (self.evidence / (name + ".json")).write_text(json.dumps(row))
        (self.evidence / "restore.json").write_text(json.dumps(
            {"image": self.image, "passed": True, "logical_equality": True}))
        self.offline = {"image": self.image, "passed": True, "completed_at": stamp,
            "runs": [{"successful": True, "skipped": [], "tests": 252} for _ in range(10)]}
        (self.evidence / "offline.json").write_text(json.dumps(self.offline))
        self.rollout = self.evidence / "rollout.json"
        self.rollout.write_text(json.dumps({"image": self.image, "passed": True,
            "source_config_sha256": config_sha, "read_only": True, "init": True,
            "rollback_verified": True, "accounting_preserved": True}))
        self.args = ["--host", str(self.profile), "--evidence", str(self.evidence), "--rollout", str(self.rollout)]
        self.commands = []

    def write_profile(self):
        self.profile.write_text(json.dumps(self.profile_data))
        self.profile.chmod(0o600)

    def docker(self, args, **kwargs):
        self.commands.append(args)
        if args[:2] == ["docker", "ps"]:
            return SimpleNamespace(returncode=0, stdout=(self.container["Id"] + "\n").encode(), stderr=b"")
        if args[:2] == ["docker", "inspect"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps([self.container]).encode(), stderr=b"")
        if args[:2] == ["docker", "exec"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps(
                {"state": "stopped", "dry_run": False, "trading_mode": "spot"}).encode(), stderr=b"")
        raise AssertionError("Unexpected external command: " + repr(args[:2]))

    def invoke(self, renew=False):
        with patch("subprocess.run", side_effect=self.docker), \
                patch.object(issuer, "refresh_offline", return_value=self.offline):
            with patch.object(sys, "argv", ["issue"] + self.args + (["--renew", "--force-renewal"] if renew else [])):
                return issuer.main()

    def test_oracle_first_issuance_and_renewal_preserve_bindings(self):
        self.assertEqual(self.invoke(), 0)
        dest = self.root / "release/certificate.json"
        first = json.loads(dest.read_bytes())
        self.assertEqual(self.invoke(renew=True), 0)
        renewed = json.loads(dest.read_bytes())
        for key in issuer.deploy.authority.BINDINGS:
            self.assertEqual(first["payload"][key], renewed["payload"][key])
        public = self.key.public_key()
        public.verify(base64.b64decode(renewed["signature"]),
            json.dumps(renewed["payload"], sort_keys=True, separators=(",", ":")).encode())
        self.assertTrue(any("label=com.docker.compose.project=binana-oracle-testnet" in cmd for cmd in self.commands))
        self.assertFalse(any(cmd[1] in {"start", "restart", "stop"} for cmd in self.commands))
        self.assertNotIn("/home/ubuntu/", repr(self.commands))
        self.assertTrue(all("POST" not in part for cmd in self.commands for part in cmd))

    def test_failed_renewal_keeps_previous_certificate(self):
        self.invoke()
        dest = self.root / "release/certificate.json"
        old = dest.read_bytes()
        status = {**self.status, "dependency_sha256": "d" * 64}
        self.readiness.write_text(json.dumps(status))
        with self.assertRaisesRegex(RuntimeError, "BINDING_DRIFT"):
            self.invoke(renew=True)
        self.assertEqual(dest.read_bytes(), old)

    def test_duplicate_owner_is_rejected(self):
        def duplicate(args, **kwargs):
            if args[:2] == ["docker", "ps"]:
                return SimpleNamespace(returncode=0, stdout=b"one\ntwo\n", stderr=b"")
            return self.docker(args, **kwargs)
        with patch("subprocess.run", side_effect=duplicate), self.assertRaisesRegex(RuntimeError, "ONE_OWNER"):
            with patch.object(sys, "argv", ["issue"] + self.args):
                issuer.main()
        self.assertFalse((self.root / "release/certificate.json").exists())

    def test_wrong_compose_identity_is_rejected(self):
        self.container["Config"]["Labels"]["com.docker.compose.project"] = "some-other-project"
        with self.assertRaisesRegex(RuntimeError, "OWNER_IDENTITY"):
            self.invoke()

    def test_host_mount_drift_is_rejected(self):
        self.container["Mounts"][0]["Source"] = str(self.root / "authority")
        with self.assertRaisesRegex(RuntimeError, "ISSUER_RUNTIME_MOUNT|HOST_MOUNT"):
            self.invoke()

    def test_issuer_cannot_be_under_any_runtime_mount(self):
        self.container["Mounts"].append({"Type": "bind", "Source": str(self.root / "authority"),
            "Destination": "/unexpected", "RW": False})
        with self.assertRaisesRegex(RuntimeError, "ISSUER_RUNTIME_MOUNT"):
            self.invoke()

    def test_private_profile_refuses_group_writable_or_symlinked_input(self):
        self.profile.chmod(0o660)
        with self.assertRaisesRegex(RuntimeError, "TRUSTED_HOST_PATH"):
            self.invoke()
        self.profile.chmod(0o600)
        other = self.root / "linked.json"
        other.symlink_to(self.profile)
        self.args[1] = str(other)
        with self.assertRaisesRegex(RuntimeError, "SYMLINK"):
            self.invoke()

    def test_live_config_cannot_receive_testnet_certificate(self):
        data = json.loads(self.config.read_text())
        data["binana"]["environment"] = "live"
        self.config.write_text(json.dumps(data))
        with self.assertRaisesRegex(RuntimeError, "CONFIGURATION_CHANGED"):
            self.invoke()

    def test_wrong_signing_key_does_not_replace_previous_certificate(self):
        self.invoke()
        dest = self.root / "release/certificate.json"
        previous = dest.read_bytes()
        self.key_path.write_bytes(Ed25519PrivateKey.generate().private_bytes(serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw, serialization.NoEncryption()))
        with self.assertRaisesRegex(RuntimeError, "ISSUER_VERIFIER"):
            self.invoke(renew=True)
        self.assertEqual(dest.read_bytes(), previous)

    def test_missing_host_profile_is_required_without_aws_fallback(self):
        with self.assertRaises(SystemExit):
            with patch.object(sys, "argv", ["issue"] + self.args[2:]):
                issuer.main()
        self.assertEqual(self.commands, [])

    def install(self, *, fail_enable=False, old=False, absent=False):
        self.destination = self.root / "tools"
        self.unit_dir = self.root / "units"
        self.timer = "binana-oracle-testnet-release-renewal.timer"
        service = self.timer.replace(".timer", ".service")
        self.enabled = {self.timer} if old else set()
        self.active = {self.timer} if old else set()
        if old:
            self.destination.mkdir()
            (self.destination / "host.json").write_bytes(b"old profile")
            (self.unit_dir / self.timer).write_bytes(b"old timer")
            (self.unit_dir / service).write_bytes(b"old service")
        state = {"failed": False}
        def run(args, **kwargs):
            self.commands.append(args)
            op = args[1]
            unit = args[-1]
            if absent and op != "daemon-reload" and not (self.unit_dir / unit).exists():
                values = {"is-active": (4, b"unknown\n"), "is-enabled": (1, b"not-found\n")}
                code, output = values.get(op, (5, b""))
                return SimpleNamespace(returncode=code, stdout=output, stderr=b"Unit not found")
            code, output = 0, b""
            if op == "is-enabled":
                code, output = (0, b"enabled\n") if unit in self.enabled else (1, b"disabled\n")
            elif op == "is-active":
                code, output = (0, b"active\n") if unit in self.active else (3, b"inactive\n")
            elif op == "stop":
                self.active.discard(unit)
            elif op == "disable":
                self.enabled.discard(unit)
                if "--now" in args:
                    self.active.discard(unit)
            elif op == "enable":
                self.enabled.add(unit)
                if "--now" in args:
                    self.active.add(unit)
                if fail_enable and not state["failed"]:
                    state["failed"] = True
                    return SimpleNamespace(returncode=1, stdout=b"", stderr=b"fixture enable failure")
            elif op == "start":
                if unit.endswith(".timer"):
                    self.active.add(unit)
            elif op != "daemon-reload":
                raise AssertionError(args)
            return SimpleNamespace(returncode=code, stdout=output, stderr=b"")
        with patch.object(installer, "DESTINATION", self.destination, create=True), \
                patch.object(installer, "UNIT_DIR", self.unit_dir, create=True), patch("subprocess.run", side_effect=run):
            with patch.object(sys, "argv", ["install"] + self.args):
                installer.main()

    def test_installer_renders_oracle_paths_and_full_validation_timeout(self):
        self.install()
        service = (self.unit_dir / self.timer.replace(".timer", ".service")).read_text()
        self.assertIn(str(self.root / "release"), service)
        self.assertIn("--host " + str(self.destination / "host.json"), service)
        self.assertIn("TimeoutStartSec=2400", service)
        self.assertNotIn("/home/ubuntu", service)
        self.assertNotIn("/var/backups/binana-testnet", service)
        self.assertIn(self.timer, self.enabled & self.active)
        self.assertTrue((self.destination / "scripts/recovery/release_host.py").is_file())
        self.assertTrue((self.destination / "scripts/recovery/deploy_candidate_owner.py").is_file())

    def test_failed_first_install_cleans_own_timer_and_units(self):
        with self.assertRaisesRegex(RuntimeError, "COMMAND_FAILED"):
            self.install(fail_enable=True)
        self.assertEqual(self.enabled, set())
        self.assertEqual(self.active, set())
        self.assertFalse((self.unit_dir / self.timer).exists())
        self.assertFalse((self.destination / "host.json").exists())

    def test_failed_upgrade_restores_old_tooling_and_timer_state(self):
        with self.assertRaisesRegex(RuntimeError, "COMMAND_FAILED"):
            self.install(fail_enable=True, old=True)
        self.assertEqual(self.enabled, {self.timer})
        self.assertEqual(self.active, {self.timer})
        self.assertEqual((self.unit_dir / self.timer).read_bytes(), b"old timer")
        self.assertEqual((self.destination / "host.json").read_bytes(), b"old profile")


    def test_new_install_handles_absent_units(self):
        self.install(absent=True)
        self.assertIn(self.timer, self.enabled & self.active)

    def test_symlink_mount_cannot_expose_issuer_even_if_recorded(self):
        alias = self.root / "issuer-alias"
        alias.symlink_to(self.root / "authority", target_is_directory=True)
        self.container["Mounts"].append({"Type": "bind", "Source": str(alias),
            "Destination": "/unexpected", "RW": False})
        self.record["mounts"] = sorted(self.container["Mounts"], key=lambda m: m["Destination"])
        (self.root / "release/deployment.json").write_text(json.dumps(self.record))
        with self.assertRaisesRegex(RuntimeError, "SYMLINK|ISSUER_RUNTIME_MOUNT"):
            self.invoke()
