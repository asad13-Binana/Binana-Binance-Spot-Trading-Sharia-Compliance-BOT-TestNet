"""Infrastructure-only regression gate for Oracle Hardening V2."""
from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from monitoring import control
from monitoring.api import configuration
from tests import _harness


ROOT = Path(__file__).resolve().parents[1]
RELEASE_MODE = (ROOT / "RELEASE_MODE").read_text(encoding="utf-8").strip()
INSTANCE = {
    "testnet": {"slug": "binana-testnet", "bot_user": "binanatn", "monitor_user": "binanatnmon"},
    "live": {"slug": "binana-live", "bot_user": "binanalive", "monitor_user": "binanalivemon"},
}[RELEASE_MODE]
PROTECTED = {
    "freqtrade/user_data/config.json": "64b2959c3300927da27310ebf24e21bbfb57ff2993c25eb5c085efdb5e31cc39",
    "freqtrade/user_data/strategies/IctSmcStrategy.py": "9f6bafc78c8cd0d9b9cbde615ddce89e304ab09738584b88d05bfdf92ff4e830",
    "legacy_core/binance_bot_V4.9.16_ALL_IN_ONE.py": "70b1d67cc0092b5b8db4a68b343cf893641bde1aae580e9ef51e2adec1062459",
    "services/common/evidence_providers.py": "e8c76e1cd75a2847903420b764d4d4669283c1420ebbb0729021b5e565b282cf",
    "services/common/sharia_attestation.py": "55fc7d9b206cf0d4193124bab4d770ae80371dd939054c737c4cfa363c998757",
    "services/common/sharia_v19.py": "768b3f5261d618e0beb50aa7f15de4ab84b4e02ad3a621d9dfebb944175580f5",
    "services/execution_sidecar/core_adapter.py": "647ca0f0e80d2307c98df966f0062830a4616036eff1bf24bdb6927720267da3",
    "services/execution_sidecar/filters.py": "5897ca3ab71c2bda9e096b51aeaf3e9abae40c26048cc615b3ded5bbe6d417a8",
    "services/execution_sidecar/live_evidence.py": "08b467b7e8547efb553f2f88a1d257450887a82a1454b15360d974b0e6fc14c3",
    "services/execution_sidecar/main.py": "967b4cf72ae1fa33563318c2e5243cdb55b4052b5231ef04774035ce585a984f",
    "services/execution_sidecar/order_manager.py": "2ee18c8a7b2ce19ad81c82aa33faa22b54505304fdbd2944bf60a39a76124cd3",
    "services/execution_sidecar/package_mode.py": "4a09dd03e4c23b770d2eaa31fd2ccc6ffe0e8f1c29d2c7b1bc159a1c5db61f22",
    "services/execution_sidecar/protection_modes.py": "fcaf83f7209ff458a8b4c0e14243fc7a4bc5e4166a53c3c44fa9c9dddcc4e541",
    "services/execution_sidecar/reconciler.py": "c3ef1c46abb44001901b9988d94586ff4e33ae928f159385ab004ca4309fa92f",
    "services/execution_sidecar/risk_checks.py": "1664091dfbbcf8fbdcf27363dcb41d85aa4a10eab9849df829bb6d761680d93a",
    "services/execution_sidecar/simulation_adapter.py": "b438c33d8b7e9c1c46432d2a7996aa4070a57cfdc06fb0004f4f01276d311432",
    "services/execution_sidecar/state_store.py": "fd77b9688970978cfedaa53f526ce6fa4f7f00a12efc20f641f793e5cf9bb767",
    "services/execution_sidecar/user_data_stream.py": "85e64a72d8b60e2e91a9a56fa46e69d3a3970221903919f3820b1ef92ee89c83",
    "services/sharia_rules/engine.py": "b17d33bc5a095810ba759554828f6c21e95cffa57a40a62f90464c5a46047e86",
    "services/sharia_screener/approval.py": "f34453c2356cdc40ff23bd532a6dbb8e0a57c3eb0f4a5930442ab0e11cee656e",
    "services/sharia_screener/bridge.py": "930d1a2aaa6d21996277d9dd12022902147f3e434ccb5db79e9c676cad6426c2",
    "services/sharia_screener/evidence_binding.py": "e3782a392e133b1c156dbfef7e592f3f9f0846af86f1273b01643acb8202b645",
    "services/sharia_screener/local_runner.py": "23ad93a18835d548db6d53ab26593b3e7792d590c6ed2b5e2bb89418fe98689e",
    "services/sharia_screener/queue_store.py": "377c83943a6518965b58b5e6f6aa027c173eeaea1590fb8930cffefd6c8b6021",
    "services/sharia_screener/runner.py": "7298f5ebad396b7d45849d3344d7218f6be92e70de930dfe19f92973656d4f66",
    "services/sharia_screener/service.py": "b940f64b03c3bf7f5fc9be5062c2d23cfe19a4736292b9c42d8d3170b6aef914",
    "services/sharia_screener/source_discovery.py": "cf54c90bba66e586da5e161c5b9d1fe5406a9b993018ed72e2b48a53384a8459",
    "services/sharia_screener/source_registry.py": "72de23c10a37c8db27e31cc6bfb58bdfb41b27efb2f2fc57edb43c76f13baffe",
    "services/sharia_screener/verdict_policy.py": "afde5fc2733d6e5a4aea50e436adf3cf55ff83e5d1e32428214f57f8dc6f55e2",
    "services/universe_service/ranker.py": "267b743782453979cbc6fbbf77fa37895dfdec9910b1d190ba6efa898b7c8e2a",
    "services/universe_service/scanner.py": "5691b664168a9b9db0ba7537ba204cb36ca262196b0de22e9cff8429649a4d9d",
    # Explicit owner-authorized transition to the manual operational registry:
    # Research version binding now follows the explicitly supplied v19.3 controller.
    "services/universe_service/sharia_filter.py": "3fed49cca73b8e6646768d144b928a3c588b5cac7a6a750578747a10d33763d0",
    "services/universe_service/snapshot_store.py": "aa4bc05c079cb3d68d782e145dbbf3a5b3cc1255987bdd552b2e9214070d17c6",
    "services/universe_service/validate_sharia.py": "3f512488ecc53f4f259883907843da5218881af3bd579e9dd543c1a486fd8219",
    "shared/sharia/halal_coins.json": "e37edb914877fbd51742a858805f066d35674c267cc1d79ead2c637d7202da16",
    "shared/sharia/HALAL_CRYPTO_SPOT_SCREENING_V19_3_PRODUCTION.json": "418e7280f0b6a5f4cd9ba3887b8be3099f5fcc4b18bfca66808749720a4dd355",
    "shared/sharia/sharia_status.json": "e0d7b34b8411a7829a38618971708da9bda470244fc421fbdebecdae9ff10939",
}

AUTHORIZED_MANUAL_REGISTRY_TRANSITIONS = {
    "services/universe_service/sharia_filter.py": (
        "bc2623d5d0d0468eaf91fb638b97f359036077658e0d9afc4944ff83032a8207",
        PROTECTED["services/universe_service/sharia_filter.py"],
    ),
    "shared/sharia/halal_coins.json": (
        "9f9cf72944eaef6d4d1f779f978a68317f175eaa9978c33d6150527117653877",
        PROTECTED["shared/sharia/halal_coins.json"],
    ),
}


class ProtectedCoreTests(unittest.TestCase):
    def test_every_protected_file_matches_reviewed_baseline(self):
        for relative, expected in PROTECTED.items():
            with self.subTest(relative=relative):
                self.assertEqual(hashlib.sha256((ROOT / relative).read_bytes()).hexdigest(), expected)

    def test_manual_registry_transition_keeps_explicit_before_and_after_hashes(self):
        for relative, (before, after) in AUTHORIZED_MANUAL_REGISTRY_TRANSITIONS.items():
            with self.subTest(relative=relative):
                self.assertRegex(before, r"^[0-9a-f]{64}$")
                self.assertRegex(after, r"^[0-9a-f]{64}$")
                self.assertNotEqual(before, after)

    def test_runtime_pairlist_override_is_absolute_without_editing_base_config(self):
        """Keep the protected base config while fixing Freqtrade 2026.6 I/O."""
        compose = yaml.safe_load(
            (ROOT / 'docker-compose.yml').read_text(encoding='utf-8'))
        raw = compose['services']['freqtrade']['environment'][
            'FREQTRADE__PAIRLISTS']
        self.assertEqual(json.loads(raw), [{
            'method': 'RemotePairList',
            'number_assets': 50,
            'refresh_period': 300,
            'pairlist_url':
                'file:////freqtrade/shared/universe/current_pairlist.json',
            'keep_pairlist_on_failure': False,
            'read_timeout': 10,
        }])


class OracleInstallerStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.setup = (ROOT / "deploy/oracle_setup.sh").read_text(encoding="utf-8")
        cls.installer = (ROOT / "deploy/install_artifact.sh").read_text(encoding="utf-8")
        cls.parser = (ROOT / "deploy/lib/secure_env.sh").read_text(encoding="utf-8")

    def test_official_docker_ce_repository_and_arm64(self):
        for package in ("docker-ce", "docker-ce-cli", "containerd.io", "docker-buildx-plugin", "docker-compose-plugin"):
            self.assertIn(package, self.setup)
        self.assertIn("https://download.docker.com/linux/ubuntu", self.setup)
        self.assertIn("arm64", self.setup)
        self.assertNotIn("apt-get install -y docker.io", self.setup)
        self.assertNotRegex(self.setup, r"curl[^\n]*\|[^\n]*(sh|bash)")

    def test_supported_os_resources_time_swap_and_versions(self):
        for marker in (
            "Ubuntu 24.04", "MIN_PHYSICAL_MEMORY_MIB:-11264",
            "MIN_TOTAL_MEMORY_MIB:-14336", "MIN_FREE_DISK_GIB:-80",
            "SWAP_SIZE_GIB:-4", "chronyc tracking", "docker compose version",
        ):
            self.assertIn(marker, self.setup)
        self.assertIn("clock did not synchronize within 60 seconds", self.setup)
        self.assertIn("exceeds 0.100s", self.setup)
        self.assertIn("is not a valid swap area", self.setup)

    def test_privileged_paths_and_legacy_namespace_fail_closed(self):
        identity = (ROOT / "deploy/instance_identity.sh").read_text(encoding="utf-8")
        for path in (
            f"/etc/{INSTANCE['slug']}",
            f"/opt/{INSTANCE['slug']}",
            f"/var/lib/{INSTANCE['slug']}/shared",
            f"/var/log/{INSTANCE['slug']}/monitor",
            f"/var/lib/{INSTANCE['slug']}/deploy-inbox",
        ):
            self.assertIn(path, identity)
        self.assertIn("privileged path must not be a symlink", self.setup)
        self.assertIn("legacy deployment detected", self.setup)
        self.assertIn("com.docker.compose.project=$legacy_project", self.setup)
        self.assertIn("binance-freqtrade-v101 binana-freqtrade-v101", self.setup)

    def test_secret_file_and_accounts_are_least_privilege(self):
        self.assertIn('chown root:root "$PRIVATE/.env"', self.setup)
        self.assertIn('chmod 0600 "$PRIVATE/.env"', self.setup)
        self.assertNotIn("usermod -aG docker", self.setup)
        self.assertIn('gpasswd -d "$account" docker', self.setup)
        self.assertIn(f"/usr/local/sbin/{INSTANCE['slug']}-deploy", self.setup)

    def test_env_is_parsed_not_sourced(self):
        self.assertIn('secure_env_read "$ENV_FILE" DEPLOY_ENV', self.installer)
        self.assertNotIn('source "$ENV_FILE"', self.installer)
        self.assertNotIn("eval ", self.installer + self.parser)
        self.assertIn("owned by root:root", self.parser)
        self.assertIn("unsupported environment key", self.installer)
        self.assertIn('export "$key=${DEPLOY_ENV[$key]}"', self.installer)

    def test_testnet_env_cannot_request_live(self):
        self.assertIn("testnet package refuses EXECUTION_MODE=live", self.installer)
        self.assertIn("testnet package requires BOT_ENVIRONMENT=TESTNET", self.installer)
        self.assertIn("testnet package requires BINANCE_TESTNET=true", self.installer)

    def test_release_approval_lock_and_rollback_remain(self):
        wrapper = (ROOT / "deploy/binana-deploy-wrapper.sh").read_text(encoding="utf-8")
        self.assertIn("explicitly approved", wrapper)
        self.assertIn("deployment inbox must remain fixed", wrapper)
        self.assertIn("approval file must remain fixed", wrapper)
        self.assertIn("flock -n 9", self.installer)
        self.assertIn("BOT_UID and BOT_GID must match the dedicated $BOT_USER account", self.installer)
        self.assertIn(f'[[ "$APP_ROOT" == /opt/{INSTANCE["slug"]} ]]', self.installer)
        self.assertIn("producer='deploy-installer'", self.installer)
        self.assertIn("sign_envelope(", self.installer)
        self.assertIn('ENVELOPE_RELEASE_HASH="$OLD_RELEASE_HASH"', self.installer)
        self.assertIn("rollback(){", self.installer)
        self.assertIn("verify_manifest.py", self.installer)
        monitor_installer = (ROOT / "deploy/install_monitoring.sh").read_text(encoding="utf-8")
        self.assertIn("monitoring release must be inside the fixed release root", monitor_installer)
        self.assertIn("monitor environment must be a regular non-symlink file", monitor_installer)


@unittest.skipUnless(_harness.posix_bash(), "POSIX bash unavailable")
class HostileEnvironmentParserTests(unittest.TestCase):
    def test_hostile_values_are_literal_and_execute_nothing(self):
        bash = _harness.posix_bash()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            env_file = root / "bot.env"
            marker = root / "PWNED"
            payloads = [
                f"$(touch {marker})", f"`touch {marker}`", "; touch PWNED", "| touch PWNED",
                "&& touch PWNED", "|| touch PWNED", ">PWNED", "*", "$HOME", "${USER}",
                "$(printf $(id))",
            ]
            env_file.write_text("\n".join(f"K{i}={value}" for i, value in enumerate(payloads)) + "\n", encoding="utf-8")
            command = (
                f"source '{ROOT.as_posix()}/deploy/lib/secure_env.sh'; "
                "declare -A values=(); exec {fd}<\"$1\"; _secure_env_parse_fd \"$fd\" values test; "
                "for key in \"${!values[@]}\"; do printf '%s=%s\\n' \"$key\" \"${values[$key]}\"; done"
            )
            if os.name == "posix":
                os.chmod(env_file, 0o600)
            proc = subprocess.run([bash, "-c", command, "parser", str(env_file)], text=True, capture_output=True, timeout=10)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertFalse(marker.exists())
            for payload in payloads:
                self.assertIn(payload, proc.stdout)


class ComposeHardeningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))

    def test_unique_namespace_and_no_public_ports(self):
        self.assertEqual(self.compose["name"], INSTANCE["slug"])
        for name, service in self.compose["services"].items():
            with self.subTest(service=name):
                self.assertNotIn("ports", service)

    def test_every_service_has_resource_and_privilege_bounds(self):
        for name, service in self.compose["services"].items():
            with self.subTest(service=name):
                self.assertIn("mem_limit", service)
                self.assertIn("pids_limit", service)
                self.assertIn("ALL", service.get("cap_drop", []))
                self.assertIn("no-new-privileges:true", service.get("security_opt", []))
                logging = service.get("logging", {})
                self.assertEqual(logging.get("options", {}).get("max-size"), "10m")
                self.assertEqual(logging.get("options", {}).get("max-file"), "3")

    def test_monitor_is_not_a_container_and_no_docker_socket_is_mounted(self):
        source = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertNotIn("/var/run/docker.sock", source)
        self.assertNotIn("botmon", source)


class MonitorHardeningTests(unittest.TestCase):
    def test_default_loopback_and_port(self):
        with mock.patch.dict(os.environ, {"MONITOR_TOKEN": "x" * 40}, clear=True):
            cfg = configuration.Config()
        self.assertEqual(cfg.bind_host, "127.0.0.1")
        self.assertEqual(cfg.port, 8090)

    def test_configurable_occupied_port_is_rejected(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        try:
            port = listener.getsockname()[1]
            listener.listen(1)
            env = {"MONITOR_TOKEN": "x" * 40, "MONITOR_PORT": str(port)}
            with mock.patch.dict(os.environ, env, clear=True):
                configuration.reload()
                ok, reason = control.monitor_port_available()
            self.assertFalse(ok)
            self.assertIn("occupied", reason)
        finally:
            listener.close()
            configuration.reload()

    def test_systemd_monitor_has_no_sudo_or_docker_socket(self):
        for unit in (ROOT / "monitoring/systemd").glob("binana-monitor-*.service"):
            text = unit.read_text(encoding="utf-8")
            if "snapshot" in unit.name:
                continue
            self.assertIn("User=botmon", text)
            self.assertNotIn("sudo", text)
            self.assertNotIn("docker.sock", text)


class OperationalScriptTests(unittest.TestCase):
    def test_diagnostics_use_ten_https_requests_and_percentiles(self):
        text = (ROOT / "deploy/oracle_validate.sh").read_text(encoding="utf-8")
        self.assertIn("seq 1 10", text)
        for marker in ("time_namelookup", "time_connect", "time_appconnect", "time_starttransfer", "time_total", "median", "p95"):
            self.assertIn(marker, text)
        self.assertNotRegex(text, r"(?m)^\s*ping\s")

    def test_backup_uses_python_sqlite_backup_and_restore_integrity(self):
        backup = (ROOT / "deploy/backup_state.sh").read_text(encoding="utf-8")
        restore = (ROOT / "deploy/restore_validate.sh").read_text(encoding="utf-8")
        helper = (ROOT / "scripts/backup_integrity.py").read_text(encoding="utf-8")
        self.assertIn("backup_integrity.py", backup)
        self.assertIn("backup_integrity.py", restore)
        self.assertIn("source_db.backup(target_db)", helper)
        self.assertNotIn(".backup '", backup)
        self.assertIn("--no-links --no-devices --no-specials", backup)
        self.assertIn("backup contains a link or special file", restore)
        self.assertIn("unsafe SHA256SUMS path", helper)
        self.assertIn("PRAGMA integrity_check", helper)
        self.assertNotIn(".env", backup)

    def test_ssh_validation_precedes_reload(self):
        text = (ROOT / "deploy/harden_ssh.sh").read_text(encoding="utf-8")
        self.assertLess(text.index("sshd -t"), text.index("systemctl reload ssh"))

    def test_critical_disk_pressure_queues_authenticated_entry_pause(self):
        guard = (ROOT / "deploy/disk_guard.sh").read_text(encoding="utf-8")
        unit = (ROOT / "monitoring/systemd/binana-disk-guard.service").read_text(encoding="utf-8")
        self.assertIn("status\" == critical", guard)
        self.assertIn("sign_envelope(", guard)
        self.assertIn('"command": "entries"', guard)
        self.assertIn('"args": {"enabled": False}', guard)
        self.assertIn("commands/inbox", unit)

    def test_ci_overrides_oracle_shared_path_with_checked_out_fixture(self):
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        self.assertIn("lines = setk(lines, 'SHARED_HOST_PATH', './shared')", workflow)


if __name__ == "__main__":
    unittest.main()
