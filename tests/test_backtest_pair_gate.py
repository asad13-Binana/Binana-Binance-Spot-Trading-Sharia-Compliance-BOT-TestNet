from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from services.common.backtest_pairs import (
    DEFAULT_STABLECOINS,
    EXCLUDED_BASES,
    LEVERAGED_SUFFIXES,
    BacktestPairError,
    load_pairs,
)
from services.universe_service import scanner


class BacktestPairGateTests(unittest.TestCase):
    def _write(self, payload) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / 'halal_coins.json'
        path.write_text(json.dumps(payload), encoding='utf-8')
        return path

    @staticmethod
    def _registry(symbols):
        today = datetime.now(timezone.utc).date()
        return {
            'schema_version': 1,
            'version': 'test-1',
            'last_reviewed': today.isoformat() if symbols else None,
            'next_review': (today + timedelta(days=30)).isoformat() if symbols else None,
            'symbols': symbols,
        }

    def test_only_manual_exact_symbols_become_pairs(self):
        path = self._write(self._registry(['ETHUSDT', 'SOLUSDT']))
        self.assertEqual(load_pairs(path), ['ETH/USDT', 'SOL/USDT'])

    def test_missing_malformed_empty_and_duplicate_registry_blocks(self):
        cases = [
            ({}, 'schema_version'),
            (self._registry([]), 'registry is empty'),
            (self._registry(['ethusdt']), 'uppercase'),
            (self._registry(['ETHUSDT', 'ETHUSDT']), 'duplicate'),
            (self._registry(['SOLUSDT', 'ETHUSDT']), 'must be sorted'),
        ]
        for payload, message in cases:
            with self.subTest(payload=payload), self.assertRaisesRegex(
                    BacktestPairError, message):
                load_pairs(self._write(payload))

    def test_existing_universe_policy_remains_an_additional_gate(self):
        payload = self._registry([
            'BNBUSDT', 'BTCUSDT', 'ETHUPUSDT', 'ETHUSDT', 'USDCUSDT'])
        self.assertEqual(load_pairs(self._write(payload)), ['ETH/USDT'])
        with self.assertRaisesRegex(BacktestPairError, 'universe policy'):
            load_pairs(self._write(self._registry(['BTCUSDT', 'USDCUSDT'])))

    def test_policy_constants_match_the_protected_universe_policy(self):
        self.assertEqual(EXCLUDED_BASES, frozenset(scanner.EXCLUDED))
        self.assertEqual(LEVERAGED_SUFFIXES, scanner.LEV_SUFFIX)
        with mock.patch.dict(os.environ, {'STABLECOINS': DEFAULT_STABLECOINS}):
            expected = frozenset(DEFAULT_STABLECOINS.split(','))
            self.assertEqual(expected, frozenset(scanner.STABLES))

    def test_shell_scripts_use_arrays_and_the_manual_registry(self):
        root = Path(__file__).resolve().parents[1]
        for relative in (
            'freqtrade/scripts/backtest.sh',
            'freqtrade/scripts/download_data.sh',
        ):
            text = (root / relative).read_text(encoding='utf-8')
            self.assertIn('services/common/backtest_pairs.py', text)
            self.assertIn('--pairs "${PAIRS[@]}"', text)
            self.assertNotIn('halal_list.json', text)


if __name__ == '__main__':
    unittest.main()
