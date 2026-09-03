from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tests import _harness  # noqa: F401 - installs deterministic attestation keys
from services.sharia_screener.manual_registry import (
    MANUAL_PROJECTION_MODE,
    ManualRegistryError,
    build_manual_bootstrap_status,
    load_manual_registry,
    write_manual_bootstrap_status,
)
from services.sharia_screener.manual_registry_service import ManualRegistryProjector
from services.universe_service.sharia_gate import ManualRegistryFilter, load_sharia_gate
from services.universe_service.sharia_filter import ShariaFilter


ROOT = Path(__file__).resolve().parents[1]


class ManualRegistryContractTests(unittest.TestCase):
    def _payload(self, symbols: list[str]) -> dict:
        today = datetime.now(timezone.utc).date()
        return {
            'schema_version': 1,
            'version': 'test-1',
            'last_reviewed': today.isoformat() if symbols else None,
            'next_review': (today + timedelta(days=30)).isoformat() if symbols else None,
            'symbols': symbols,
        }

    def _write(self, directory: Path, payload: dict) -> Path:
        path = directory / 'halal_coins.json'
        path.write_text(json.dumps(payload, indent=2) + '\n', encoding='utf-8')
        return path

    def test_shipped_owner_registry_is_current_sorted_and_can_build_deny_all_bootstrap(self):
        registry_path = ROOT / 'shared/sharia/halal_coins.json'
        status_path = ROOT / 'shared/sharia/sharia_status.json'
        registry = load_manual_registry(registry_path)
        bootstrap = build_manual_bootstrap_status(registry)
        self.assertEqual(len(registry.symbols), 244)
        self.assertEqual(registry.symbols, tuple(sorted(set(registry.symbols))))
        self.assertEqual(bootstrap['projection_mode'], MANUAL_PROJECTION_MODE)
        self.assertEqual(bootstrap['registry_sha256'], registry.sha256)
        self.assertEqual(bootstrap['registry_version'], registry.version)
        self.assertIs(bootstrap['projection_complete'], False)
        self.assertEqual(bootstrap['records'], [])
        # The repository seed remains the historical deny-all V19 projection;
        # the installer writes the hash-bound manual bootstrap transactionally.
        self.assertEqual(ShariaFilter(status_path).current_halal_symbols(), [])

    def test_malformed_duplicate_unsorted_and_expired_registry_fail_closed(self):
        today = datetime.now(timezone.utc).date()
        cases = [
            ({}, 'schema_version'),
            (self._payload(['ethusdt']), 'uppercase'),
            (self._payload(['ETHUSDT', 'ETHUSDT']), 'duplicate'),
            (self._payload(['SOLUSDT', 'ETHUSDT']), 'must be sorted'),
            ({
                'schema_version': 1,
                'version': 'test-1',
                'last_reviewed': (today - timedelta(days=40)).isoformat(),
                'next_review': (today - timedelta(days=1)).isoformat(),
                'symbols': ['ETHUSDT'],
            }, 'expired'),
        ]
        for payload, message in cases:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as td:
                path = self._write(Path(td), payload)
                with self.assertRaisesRegex(ManualRegistryError, message):
                    load_manual_registry(path)

    def test_bootstrap_is_hash_bound_and_never_approves_a_coin(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            registry_path = self._write(root, self._payload(['ETHUSDT']))
            registry = load_manual_registry(registry_path)
            document = build_manual_bootstrap_status(registry)
            self.assertEqual(document['registry_sha256'], registry.sha256)
            self.assertEqual(document['records'], [])
            status_path = root / 'sharia_status.json'
            write_manual_bootstrap_status(registry_path, status_path)
            gate = load_sharia_gate(status_path)
            self.assertFalse(gate.decision('ETH').allowed)


class ManualRegistryProjectionTests(unittest.TestCase):
    def _payload(self, symbols: list[str], version: str = 'test-1') -> dict:
        today = datetime.now(timezone.utc).date()
        return {
            'schema_version': 1,
            'version': version,
            'last_reviewed': today.isoformat(),
            'next_review': (today + timedelta(days=30)).isoformat(),
            'symbols': symbols,
        }

    def test_projector_signs_exact_list_and_both_gates_fail_closed_on_change(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            registry_path = root / 'halal_coins.json'
            status_path = root / 'sharia_status.json'
            registry_path.write_text(
                json.dumps(self._payload(['ETHUSDT', 'SOLUSDT'])), encoding='utf-8')
            projector = ManualRegistryProjector(
                registry_path=registry_path,
                status_path=status_path,
                reports_dir=root / 'reports',
                runtime_dir=root / 'runtime',
                alert_outbox=root / 'alerts',
            )
            with mock.patch(
                    'services.sharia_screener.manual_registry_service.audit'):
                self.assertTrue(projector.sync(force=True))
            gate = load_sharia_gate(status_path)
            self.assertTrue(gate.decision('ETH').allowed)
            self.assertTrue(gate.decision('SOLUSDT').allowed)
            self.assertFalse(gate.decision('DOGE').allowed)
            self.assertEqual(gate.current_halal_symbols(), ['ETH', 'SOL'])

            registry_path.write_text(
                json.dumps(self._payload(['DOGEUSDT', 'ETHUSDT', 'SOLUSDT'],
                                         version='test-2')),
                encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'changed after projection'):
                load_sharia_gate(status_path)

            with mock.patch(
                    'services.sharia_screener.manual_registry_service.audit'):
                self.assertTrue(projector.sync())
            updated = load_sharia_gate(status_path)
            self.assertTrue(updated.decision('DOGE').allowed)

    def test_invalid_runtime_update_writes_deny_all_status(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            registry_path = root / 'halal_coins.json'
            status_path = root / 'sharia_status.json'
            registry_path.write_text('{"schema_version":1,"version":"bad",'
                                     '"symbols":["ethusdt"]}', encoding='utf-8')
            projector = ManualRegistryProjector(
                registry_path=registry_path,
                status_path=status_path,
                reports_dir=root / 'reports',
                runtime_dir=root / 'runtime',
                alert_outbox=root / 'alerts',
            )
            with mock.patch(
                    'services.sharia_screener.manual_registry_service.audit'):
                self.assertFalse(projector.sync(force=True))
            payload = json.loads(status_path.read_text(encoding='utf-8'))
            self.assertIs(payload['registry_valid'], False)
            self.assertEqual(payload['records'], [])
            with self.assertRaisesRegex(ValueError, 'invalid; fail closed'):
                ManualRegistryFilter(status_path)

    def test_usdt_base_is_not_normalized_to_an_empty_symbol(self):
        self.assertEqual(ManualRegistryFilter._normalize_symbol('USDT'), 'USDT')
        self.assertEqual(ManualRegistryFilter._normalize_symbol('USDTUSDT'), 'USDT')
        self.assertEqual(ManualRegistryFilter._normalize_symbol('USDT/USDT'), 'USDT')


if __name__ == '__main__':
    unittest.main()
