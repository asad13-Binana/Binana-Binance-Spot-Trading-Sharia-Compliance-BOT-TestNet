"""Health publication stays monitor-readable without exposing private state."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from tests import _harness  # noqa: F401
from services.common import atomic
from services.telegram_broker import bot


@unittest.skipUnless(os.name == 'posix', 'POSIX file permissions require Linux')
class TelegramHealthPermissionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.health = self.root / 'telegram_alert_outbox_health.json'
        self.outbox = self.root / 'outbox'
        self.quarantine = self.root / 'quarantine'
        self.outbox.mkdir()
        self.quarantine.mkdir()
        for name, value in [('ALERT_OUTBOX_HEALTH', self.health),
                            ('TELEGRAM_ALERT_OUTBOX', self.outbox),
                            ('ALERT_QUARANTINE', self.quarantine)]:
            patcher = mock.patch.object(bot, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_health_replacements_remain_group_readable_under_private_umask(self):
        atomic.atomic_write_json(self.health, {'old': True})
        previous_umask = os.umask(0o077)
        try:
            for _ in range(3):
                expected = bot._alert_outbox_health()
                self.assertEqual(stat.S_IMODE(self.health.stat().st_mode), 0o640)
                self.assertEqual(json.loads(self.health.read_text()), expected)
        finally:
            os.umask(previous_umask)

    def test_private_writes_remain_owner_only_even_after_broader_old_permissions(self):
        for name in ('telegram_alert_delivery.json', 'telegram_offset.json',
                     'telegram_signal_notification_state.json', 'config.json',
                     'outbox/private-alert.json'):
            with self.subTest(name=name):
                path = self.root / name
                atomic.atomic_write_json(path, {'private': 'value'})
                path.chmod(0o640)
                atomic.atomic_write_json(path, {'private': 'updated'})
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_health_mode_is_set_on_temporary_inode_before_publication(self):
        atomic.atomic_write_json(self.health, {'old': True})
        modes = []
        replace = atomic.os.replace

        def inspect_before_replace(source, destination):
            modes.append(stat.S_IMODE(Path(source).stat().st_mode))
            self.assertEqual(json.loads(self.health.read_text()), {'old': True})
            return replace(source, destination)

        with mock.patch.object(atomic.os, 'replace', side_effect=inspect_before_replace):
            bot._alert_outbox_health()
        self.assertEqual(modes, [0o640])

    def test_permission_failure_keeps_existing_snapshot_and_cleans_temporary_file(self):
        atomic.atomic_write_json(self.health, {'old': True})
        with mock.patch.object(atomic.os, 'fchmod', side_effect=PermissionError('denied')):
            with self.assertRaises(PermissionError):
                bot._alert_outbox_health()
        self.assertEqual(json.loads(self.health.read_text()), {'old': True})
        self.assertEqual(stat.S_IMODE(self.health.stat().st_mode), 0o600)
        self.assertEqual(list(self.root.glob(self.health.name + '.*.tmp')), [])

    @unittest.skipUnless(hasattr(os, 'fork') and getattr(os, 'geteuid', lambda: -1)() == 0,
                         'different-UID access check requires a root Linux test container')
    def test_monitor_group_can_read_rewritten_health_but_not_private_files(self):
        self.root.chmod(0o755)
        shared = self.root / 'shared'
        shared.mkdir()
        os.chown(shared, 999, 987)
        shared.chmod(0o750)
        health = shared / self.health.name
        private = [shared / 'telegram_alert_delivery.json', shared / 'config.json']
        for directory in (self.outbox, self.quarantine):
            os.chown(directory, 999, 987)
            directory.chmod(0o700)

        def run_as(uid, gid, groups, action):
            read_fd, write_fd = os.pipe()
            pid = os.fork()
            if pid == 0:
                os.close(read_fd)
                try:
                    os.setgroups(groups)
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
            _, status = os.waitpid(pid, 0)
            self.assertEqual(status, 0)
            self.assertEqual(result, 'ok')

        def broker_publish():
            os.umask(0o077)
            for path in private + [self.outbox / 'private-alert.json']:
                atomic.atomic_write_json(path, {'private': 'value'})
            for _ in range(3):
                bot._alert_outbox_health()

        def monitor_read():
            self.assertEqual(json.loads(health.read_text())['pending_alert_count'], 1)
            for path in private + [self.outbox / 'private-alert.json']:
                try:
                    path.read_text()
                except PermissionError:
                    continue
                raise AssertionError(f'monitor could read private file: {path.name}')

        with mock.patch.object(bot, 'ALERT_OUTBOX_HEALTH', health):
            run_as(999, 987, [987], broker_publish)
            run_as(997, 998, [987], monitor_read)
        self.assertEqual((health.stat().st_uid, health.stat().st_gid), (999, 987))
        self.assertEqual(stat.S_IMODE(health.stat().st_mode), 0o640)


if __name__ == '__main__':
    unittest.main()
