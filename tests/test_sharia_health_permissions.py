"""Only derived Sharia health is shared with the separate monitor user."""
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from tests import _harness  # noqa: F401
from services.common.atomic import atomic_write_json
from services.sharia_screener import manual_registry_service as manual
from services.sharia_screener import readiness, service as research


@unittest.skipUnless(os.name == 'posix', 'Linux permission contract')
class ShariaHealthPermissionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in ('manual', 'research'):
            (self.root / name).mkdir()
        previous = os.umask(0o077)
        self.addCleanup(os.umask, previous)

    def publish(self, *, available):
        projector = manual.ManualRegistryProjector.__new__(manual.ManualRegistryProjector)
        projector.health_path = self.root / 'manual/health.json'
        projector.registry_path = self.root / 'registry.json'
        registry = SimpleNamespace(symbols=['BTC'], version='test', sha256='a' * 64)
        projector._write_health(ok=available, registry=registry,
                                error='' if available else 'invalid registry')
        screener = research.ShariaScreenerService.__new__(research.ShariaScreenerService)
        screener.runner = SimpleNamespace(provider='local', available=lambda: (
            available, '' if available else 'backend unavailable'))
        screener.queue = SimpleNamespace(
            counts=lambda: {}, completed_today=lambda: 0,
            cost_today=lambda: 0, last=lambda status: None)
        screener.max_scans_per_day = 100
        screener.urgent_reserve_per_day = 10
        screener.max_scans_per_base_per_day = 1
        screener.max_scans_per_actor_per_day = 10
        screener.min_between_scans = 10
        screener.idle_enabled = False
        screener.source_discovery = None
        with mock.patch.object(research, 'SHARIA_RUNTIME_DIR', self.root / 'research'), \
                mock.patch.object(readiness, 'screening_readiness', return_value={}):
            screener.write_health()

    def test_manual_health_replacements_are_monitor_readable(self):
        for available in (True, False, True):
            self.publish(available=available)
            path = self.root / 'manual/health.json'
            self.assertEqual(json.loads(path.read_text())['ok'], available)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)

    def test_research_health_replacements_are_monitor_readable(self):
        for available in (True, False, True):
            self.publish(available=available)
            path = self.root / 'research/health.json'
            payload = json.loads(path.read_text())
            self.assertEqual(payload['ok'], available)
            self.assertEqual(payload['ready_for_screening'], available)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)

    def test_signed_registry_evidence_reports_and_notifications_stay_private(self):
        for name in ('registry.json', 'status.json', 'evidence.json', 'report.json',
                     'approval.json', 'notification.json'):
            path = self.root / name
            atomic_write_json(path, {'private': 'old'})
            path.chmod(0o640)
            atomic_write_json(path, {'private': 'replacement'})
            self.publish(available=True)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    @unittest.skipUnless(hasattr(os, 'fork') and hasattr(os, 'geteuid') and os.geteuid() == 0,
                         'different-UID check requires a root Linux test container')
    def test_monitor_uid_reads_rewritten_health_but_not_private_registry(self):
        self.root.chmod(0o755)
        for path in (self.root / 'manual', self.root / 'research'):
            os.chown(path, 999, 987)
            path.chmod(0o750)
        private = self.root / 'manual/private-registry.json'

        def run_as(uid, gid, action):
            read_fd, write_fd = os.pipe()
            pid = os.fork()
            if pid == 0:
                os.close(read_fd)
                try:
                    os.setgroups([987])
                    os.setgid(gid)
                    os.setuid(uid)
                    action()
                    result = 'ok'
                except BaseException as exc:
                    result = f'{type(exc).__name__}: {exc}'
                os.write(write_fd, result.encode())
                os.close(write_fd)
                os._exit(0)
            os.close(write_fd)
            result = os.read(read_fd, 4096).decode()
            os.close(read_fd)
            self.assertEqual(os.waitpid(pid, 0)[1], 0)
            self.assertEqual(result, 'ok')

        def publish_as_service():
            atomic_write_json(private, {'signed': 'private'})
            for available in (True, False, True):
                self.publish(available=available)

        def read_as_monitor():
            for name in ('manual', 'research'):
                self.assertTrue(json.loads((self.root / name / 'health.json').read_text())['ok'])
            with self.assertRaises(PermissionError):
                private.read_text()

        run_as(999, 987, publish_as_service)
        run_as(997, 998, read_as_monitor)


if __name__ == '__main__':
    unittest.main()
