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
PROTECTED = {'services/common/evidence_providers.py': 'e8c76e1cd75a2847903420b764d4d4669283c1420ebbb0729021b5e565b282cf', 'services/common/sharia_attestation.py': '55fc7d9b206cf0d4193124bab4d770ae80371dd939054c737c4cfa363c998757', 'services/common/sharia_v19.py': '768b3f5261d618e0beb50aa7f15de4ab84b4e02ad3a621d9dfebb944175580f5', 'services/sharia_rules/engine.py': 'b17d33bc5a095810ba759554828f6c21e95cffa57a40a62f90464c5a46047e86', 'services/sharia_screener/approval.py': 'f34453c2356cdc40ff23bd532a6dbb8e0a57c3eb0f4a5930442ab0e11cee656e', 'services/sharia_screener/bridge.py': '3cc237e29f2892967d562b9f0272e79e9bb8f2e491d532a5a4fa8056f38dcd49', 'services/sharia_screener/evidence_binding.py': 'e3782a392e133b1c156dbfef7e592f3f9f0846af86f1273b01643acb8202b645', 'services/sharia_screener/local_runner.py': '23ad93a18835d548db6d53ab26593b3e7792d590c6ed2b5e2bb89418fe98689e', 'services/sharia_screener/queue_store.py': '44c63c34b0407dbeb319636259a425d19ec3542c4ca81e5e01cdccea2b302c6d', 'services/sharia_screener/runner.py': '7298f5ebad396b7d45849d3344d7218f6be92e70de930dfe19f92973656d4f66', 'services/sharia_screener/service.py': '3c837f393bbf18158ed366021107cbefb476c5791ad1971909c018f688d50f4a', 'services/sharia_screener/source_discovery.py': 'cf54c90bba66e586da5e161c5b9d1fe5406a9b993018ed72e2b48a53384a8459', 'services/sharia_screener/source_registry.py': '72de23c10a37c8db27e31cc6bfb58bdfb41b27efb2f2fc57edb43c76f13baffe', 'services/sharia_screener/verdict_policy.py': 'afde5fc2733d6e5a4aea50e436adf3cf55ff83e5d1e32428214f57f8dc6f55e2', 'services/universe_service/ranker.py': '267b743782453979cbc6fbbf77fa37895dfdec9910b1d190ba6efa898b7c8e2a', 'services/universe_service/scanner.py': '4b6e80fe5b2777adfff3d42b7a77883b804e6c56d8b2cc74be3fafc2778a1028', 'services/universe_service/sharia_filter.py': '3fed49cca73b8e6646768d144b928a3c588b5cac7a6a750578747a10d33763d0', 'services/universe_service/snapshot_store.py': '70b2cc407a014c069fa902d90da955b2b62a33407a370269b46fe33672a6b264', 'services/universe_service/validate_sharia.py': '3f512488ecc53f4f259883907843da5218881af3bd579e9dd543c1a486fd8219', 'shared/sharia/halal_coins.json': 'e37edb914877fbd51742a858805f066d35674c267cc1d79ead2c637d7202da16', 'shared/sharia/HALAL_CRYPTO_SPOT_SCREENING_V19_3_PRODUCTION.json': '418e7280f0b6a5f4cd9ba3887b8be3099f5fcc4b18bfca66808749720a4dd355', 'shared/sharia/sharia_status.json': 'e0d7b34b8411a7829a38618971708da9bda470244fc421fbdebecdae9ff10939', 'runtime_owner/owner/binana-strategies/BinanaNfiSpot.py': '3b66d4a64280b37ecc321cab939f86fd89ee00cdd888fa38ec5ff1e4757974cf', 'runtime_owner/owner/binana-strategies/binana_vendor/NostalgiaForInfinityX8.py': 'fcf9f622a3ad797bd143561d1017f3930220f22c793905af929ff1d33bbde686'}

# BEGIN REVIEWED LOCAL RECOVERY TRANSITIONS
# Audit 2026-10-24: local repairs, regression evidence and independent review.
AUDITED_RECOVERY_TRANSITIONS = {'services/sharia_screener/bridge.py': {'before': '930d1a2aaa6d21996277d9dd12022902147f3e434ccb5db79e9c676cad6426c2', 'after': '3cc237e29f2892967d562b9f0272e79e9bb8f2e491d532a5a4fa8056f38dcd49', 'finding': 'ST-004'}, 'services/sharia_screener/queue_store.py': {'before': '377c83943a6518965b58b5e6f6aa027c173eeaea1590fb8930cffefd6c8b6021', 'after': '44c63c34b0407dbeb319636259a425d19ec3542c4ca81e5e01cdccea2b302c6d', 'finding': 'ST-004'}, 'services/sharia_screener/service.py': {'before': '2b3e94ac3a990b19915e85e89b5b74a6714ac2c5d01bc605e3bc34e8422d4dcc', 'after': '3c837f393bbf18158ed366021107cbefb476c5791ad1971909c018f688d50f4a', 'finding': 'ST-001, ST-003, ST-005'}, 'services/universe_service/scanner.py': {'before': '5691b664168a9b9db0ba7537ba204cb36ca262196b0de22e9cff8429649a4d9d', 'after': '4b6e80fe5b2777adfff3d42b7a77883b804e6c56d8b2cc74be3fafc2778a1028', 'finding': 'UNI-001'}, 'services/universe_service/snapshot_store.py': {'before': 'aa4bc05c079cb3d68d782e145dbbf3a5b3cc1255987bdd552b2e9214070d17c6', 'after': '70b2cc407a014c069fa902d90da955b2b62a33407a370269b46fe33672a6b264', 'finding': 'UNI-002; approved LIVE owner service parity 2026-10-29'}}
# END REVIEWED LOCAL RECOVERY TRANSITIONS

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
    def test_sharia_health_permissions_remain_in_reviewed_recovery_source(self):
        # The earlier permission-only inverse hash is historical evidence.
        # Durable failure/correlation repairs now intentionally change this file.
        raw = (ROOT / 'services/sharia_screener/service.py').read_bytes()
        revised = b"else {'enabled': False, 'trade_permission_from_discovery': False}),\n        }, group_readable=True)"
        self.assertEqual(raw.count(revised), 1)
        self.assertEqual(hashlib.sha256(raw).hexdigest(), PROTECTED['services/sharia_screener/service.py'])

    def test_recovery_transitions_keep_explicit_reviewed_before_and_after_hashes(self):
        for relative, transition in AUDITED_RECOVERY_TRANSITIONS.items():
            with self.subTest(relative=relative):
                self.assertRegex(transition['before'], r'^[0-9a-f]{64}$')
                self.assertNotEqual(transition['before'], transition['after'])
                self.assertEqual(PROTECTED[relative], transition['after'])
                self.assertTrue(transition['finding'])

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

    def test_ci_builds_an_independent_bundle_without_a_host_environment(self):
        workflow = (ROOT / '.github/workflows/ci.yml').read_text(encoding='utf-8')
        self.assertIn('materialize_oracle_bundle.py /tmp/oracle-bundle', workflow)
        self.assertIn('compose.json config --no-interpolate --quiet', workflow)
        self.assertNotIn('--env-file .env.ci', workflow)


if __name__ == "__main__":
    unittest.main()
