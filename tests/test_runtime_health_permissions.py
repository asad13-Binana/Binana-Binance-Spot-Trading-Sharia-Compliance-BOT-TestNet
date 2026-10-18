"""Actual health writers remain readable by the separate monitor Unix user."""
from __future__ import annotations

from contextlib import ExitStack
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
from services.market_context.service import MarketContextService
from services.telegram_broker import bot


@unittest.skipUnless(os.name == 'posix', 'POSIX file permissions require Linux')
class RuntimeHealthPermissionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.runtime = self.root / 'runtime'
        self.runtime.mkdir()
        previous = os.umask(0o077)
        self.addCleanup(os.umask, previous)
        self.token = '123456789:' + ('EXAMPLE_' * 5)

    def poll(self, *, fail=False):
        if fail:
            result = RuntimeError('failed https://api.telegram.org/bot' + self.token)
            responses = [result, result, KeyboardInterrupt()]
        else:
            responses = [SimpleNamespace(json=lambda uid=uid: {
                'ok': True, 'result': [{'update_id': uid}],
            }) for uid in (11, 12)] + [KeyboardInterrupt()]
        with ExitStack() as stack:
            for name, value in (
                ('RUNTIME', self.runtime),
                ('OFFSET_PATH', self.runtime / 'telegram_offset.json'),
                ('TOKEN', self.token), ('OWNER', '4242'),
                ('BASE', 'https://api.telegram.org/bot' + self.token),
            ):
                stack.enter_context(mock.patch.object(bot, name, value))
            stack.enter_context(mock.patch.object(bot.envelope, 'load_key'))
            stack.enter_context(mock.patch.object(bot, 'audit'))
            for name in ('deliver_sidecar_notifications', 'deliver_signal_notifications',
                         '_maybe_restore_testnet_entries'):
                stack.enter_context(mock.patch.object(bot, name, return_value=0, create=True))
            stack.enter_context(mock.patch.object(bot.requests, 'get', side_effect=responses))
            stack.enter_context(mock.patch.object(bot.time, 'sleep'))
            with self.assertRaises(KeyboardInterrupt):
                bot.main()

    def market(self, ready=True):
        collector = MarketContextService.__new__(MarketContextService)
        collector.snapshot_path = self.root / 'market_context/current.json'
        collector.health_path = self.runtime / 'market_context/health.json'
        collector.analytics = SimpleNamespace(snapshot=lambda: {
            'symbol_count': 1, 'fresh_symbol_count': int(ready), 'statistics': {},
            'advisory_only': True, 'spot_only': True, 'can_trade': False,
        })
        collector.stream = SimpleNamespace(status=lambda: {'subscription_ready': ready})
        collector.mode = (Path(__file__).resolve().parents[1] / 'RELEASE_MODE').read_text().strip()
        collector._universe_snapshot_hash = 'a' * 64
        collector._last_universe_error = None
        return collector

    def test_successful_telegram_polls_replace_health_with_group_readable_files(self):
        health = self.runtime / 'telegram_health.json'
        for _ in range(3):
            self.poll()
            self.assertTrue(json.loads(health.read_text())['ok'])
            self.assertEqual(stat.S_IMODE(health.stat().st_mode), 0o640)

    def test_failed_telegram_polls_preserve_redaction_and_monitor_access(self):
        health = self.runtime / 'telegram_health.json'
        with self.assertLogs(bot.log, level='WARNING') as logs:
            for _ in range(3):
                self.poll(fail=True)
                text = health.read_text()
                payload = json.loads(text)
                self.assertFalse(payload['ok'])
                self.assertEqual(payload['error_type'], 'RuntimeError')
                self.assertNotIn(self.token, text)
                self.assertIn('***REDACTED***', payload['error'])
                self.assertEqual(stat.S_IMODE(health.stat().st_mode), 0o640)
        self.assertNotIn(self.token, '\n'.join(logs.output))

    def test_fresh_and_degraded_public_market_snapshots_remain_group_readable(self):
        for ready in (True, False, True):
            collector = self.market(ready)
            collector._publish()
            for path in (collector.snapshot_path, collector.health_path):
                payload = json.loads(path.read_text())
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)
                self.assertTrue(payload['advisory_only'])
                self.assertFalse(payload['can_trade'])
            self.assertEqual(json.loads(collector.health_path.read_text())['ok'], ready)

    def test_offset_and_other_private_state_keep_owner_only_permissions(self):
        for name in ('telegram_offset.json', 'telegram_alert_delivery.json',
                     'telegram_signal_notification_state.json', 'config.json',
                     'private-alert.json'):
            path = self.runtime / name
            atomic_write_json(path, {'old': True})
            path.chmod(0o640)
            if name == 'telegram_offset.json':
                with mock.patch.object(bot, 'OFFSET_PATH', path):
                    bot._store_offset(42)
            else:
                atomic_write_json(path, {'private': True})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    @unittest.skipUnless(hasattr(os, 'fork') and getattr(os, 'geteuid', lambda: -1)() == 0,
                         'different-UID access requires a root Linux test container')
    def test_monitor_user_reads_republished_health_but_cannot_read_offset(self):
        self.root.chmod(0o755)
        for directory in (self.runtime, self.root / 'market_context',
                          self.runtime / 'market_context'):
            directory.mkdir(exist_ok=True)
            os.chown(directory, 999, 987)
            directory.chmod(0o750)

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
            _, status = os.waitpid(pid, 0)
            self.assertEqual(status, 0)
            self.assertEqual(result, 'ok')

        def publish():
            self.poll()
            with self.assertLogs(bot.log, level='WARNING'):
                self.poll(fail=True)
            for ready in (True, False, True):
                self.market(ready)._publish()

        paths = [self.runtime / 'telegram_health.json',
                 self.root / 'market_context/current.json',
                 self.runtime / 'market_context/health.json']

        def read_as_monitor():
            for path in paths:
                json.loads(path.read_text())
            with self.assertRaises(PermissionError):
                (self.runtime / 'telegram_offset.json').read_text()

        run_as(999, 987, publish)
        run_as(997, 998, read_as_monitor)
        for path in paths:
            self.assertEqual((path.stat().st_uid, path.stat().st_gid), (999, 987))


if __name__ == '__main__':
    unittest.main()
