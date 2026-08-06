from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tests._harness  # noqa: F401  (install deterministic test keys)
from services.common.config_bounds import ConfigError
from services.sharia_screener import service as service_mod
from services.sharia_screener.queue_store import QueueStore


class ShariaQuotaHardeningTests(unittest.TestCase):
    @staticmethod
    def _finish(queue: QueueStore, request_id: str, base: str, actor: str,
                *, failed: bool = False) -> None:
        assert queue.enqueue(request_id, base, f'{base}/USDT', 'manual', actor)
        queue.mark_running(request_id)
        if failed:
            queue.mark_failed(request_id, 'offline-test failure')
        else:
            queue.mark_done(request_id, 'GREEN')

    @staticmethod
    def _service(queue: QueueStore, *, daily: int, reserve: int,
                 per_base: int = 20, per_actor: int = 20,
                 spacing: int = 1):
        service = object.__new__(service_mod.ShariaScreenerService)
        service.queue = queue
        service.max_scans_per_day = daily
        service.urgent_reserve_per_day = reserve
        service.max_scans_per_base_per_day = per_base
        service.max_scans_per_actor_per_day = per_actor
        service.min_between_scans = spacing
        service._last_scan_at = 0.0
        return service

    def test_urgent_requests_obey_total_ceiling_and_durable_spacing(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'queue.sqlite'
            queue = QueueStore(path)
            self._finish(queue, 'done-1', 'AAA', 'actor-a')
            self._finish(queue, 'done-2', 'BBB', 'actor-b', failed=True)
            self.assertTrue(queue.enqueue(
                'manual-pending', 'CCC', 'CCC/USDT', 'manual', 'owner'))

            service = self._service(queue, daily=2, reserve=1)
            with mock.patch.object(
                    service_mod.time, 'time',
                    return_value=queue.last_activity_at() + 10_000):
                self.assertIsNone(service._throttled_next())

            reopened = QueueStore(path)
            self.assertEqual(reopened.completed_today(), 2)
            spacing_service = self._service(reopened, daily=3, reserve=1, spacing=120)
            self.assertIsNone(spacing_service._throttled_next())
            with mock.patch.object(
                    service_mod.time, 'time',
                    return_value=reopened.last_activity_at() + 121):
                selected = spacing_service._throttled_next()
            self.assertEqual(selected['request_id'], 'manual-pending')

    def test_bulk_and_idle_stop_before_reserve_but_urgent_can_use_it(self):
        with tempfile.TemporaryDirectory() as td:
            queue = QueueStore(Path(td) / 'queue.sqlite')
            for index, base in enumerate(('AAA', 'BBB', 'CCC'), 1):
                self._finish(queue, f'done-{index}', base, f'actor-{index}')
            self.assertTrue(queue.enqueue(
                'bulk-pending', 'DDD', 'DDD/USDT', 'bulk', 'installer'))
            service = self._service(queue, daily=4, reserve=1)
            now = queue.last_activity_at() + 10_000
            with mock.patch.object(service_mod.time, 'time', return_value=now):
                self.assertIsNone(service._throttled_next())

            self.assertTrue(queue.enqueue(
                'signal-pending', 'EEE', 'EEE/USDT', 'signal', 'execution-sidecar'))
            with mock.patch.object(service_mod.time, 'time', return_value=now):
                selected = service._throttled_next()
            self.assertEqual(selected['request_id'], 'signal-pending')

    def test_per_base_and_actor_caps_skip_blocked_head_of_line_requests(self):
        with tempfile.TemporaryDirectory() as td:
            queue = QueueStore(Path(td) / 'base.sqlite')
            self._finish(queue, 'eth-1', 'ETH', 'first')
            self._finish(queue, 'eth-2', 'ETH', 'second')
            self.assertTrue(queue.enqueue(
                'blocked-base', 'ETH', 'ETH/USDT', 'signal', 'third'))
            self.assertTrue(queue.enqueue(
                'eligible-base', 'ADA', 'ADA/USDT', 'manual', 'fourth'))
            service = self._service(queue, daily=20, reserve=2, per_base=2)
            with mock.patch.object(
                    service_mod.time, 'time',
                    return_value=queue.last_activity_at() + 10_000):
                selected = service._throttled_next()
            self.assertEqual(selected['request_id'], 'eligible-base')
            self.assertEqual(queue.completed_today(base='eth'), 2)

        with tempfile.TemporaryDirectory() as td:
            queue = QueueStore(Path(td) / 'actor.sqlite')
            self._finish(queue, 'actor-1', 'AAA', ' Owner ')
            self._finish(queue, 'actor-2', 'BBB', 'owner')
            self.assertTrue(queue.enqueue(
                'blocked-actor', 'CCC', 'CCC/USDT', 'signal', 'OWNER'))
            self.assertTrue(queue.enqueue(
                'eligible-actor', 'DDD', 'DDD/USDT', 'manual', 'another'))
            service = self._service(queue, daily=20, reserve=2, per_actor=2)
            with mock.patch.object(
                    service_mod.time, 'time',
                    return_value=queue.last_activity_at() + 10_000):
                selected = service._throttled_next()
            self.assertEqual(selected['request_id'], 'eligible-actor')
            self.assertEqual(queue.completed_today(requested_by=' OWNER '), 2)

    def test_zero_unlimited_and_relational_env_bypasses_are_rejected(self):
        knobs = (
            'SHARIA_MAX_SCANS_PER_DAY',
            'SHARIA_MIN_SECONDS_BETWEEN_SCANS',
            'SHARIA_URGENT_RESERVE_PER_DAY',
            'SHARIA_MAX_SCANS_PER_BASE_PER_DAY',
            'SHARIA_MAX_SCANS_PER_ACTOR_PER_DAY',
        )
        for name in knobs:
            with self.subTest(zero=name), mock.patch.dict(
                    os.environ, {name: '0'}, clear=True):
                with self.assertRaises(ConfigError):
                    service_mod._quota_settings()

        too_large = {
            'SHARIA_MAX_SCANS_PER_DAY': '1001',
            'SHARIA_MIN_SECONDS_BETWEEN_SCANS': '86401',
            'SHARIA_URGENT_RESERVE_PER_DAY': '201',
            'SHARIA_MAX_SCANS_PER_BASE_PER_DAY': '25',
            'SHARIA_MAX_SCANS_PER_ACTOR_PER_DAY': '201',
        }
        for name, value in too_large.items():
            with self.subTest(too_large=name), mock.patch.dict(
                    os.environ, {name: value}, clear=True):
                with self.assertRaises(ConfigError):
                    service_mod._quota_settings()

        with mock.patch.dict(os.environ, {
                'SHARIA_MAX_SCANS_PER_DAY': '2',
                'SHARIA_URGENT_RESERVE_PER_DAY': '3'}, clear=True):
            with self.assertRaises(ConfigError):
                service_mod._quota_settings()

        with mock.patch.dict(os.environ, {
                'SHARIA_MAX_SCANS_PER_DAY': '10',
                'SHARIA_MIN_SECONDS_BETWEEN_SCANS': '2',
                'SHARIA_URGENT_RESERVE_PER_DAY': '2',
                'SHARIA_MAX_SCANS_PER_BASE_PER_DAY': '3',
                'SHARIA_MAX_SCANS_PER_ACTOR_PER_DAY': '5'}, clear=True):
            self.assertEqual(service_mod._quota_settings(), {
                'daily': 10, 'min_between': 2, 'urgent_reserve': 2,
                'per_base': 3, 'per_actor': 5,
            })


if __name__ == '__main__':
    unittest.main()
