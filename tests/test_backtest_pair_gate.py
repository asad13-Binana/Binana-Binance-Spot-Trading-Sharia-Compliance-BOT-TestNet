from __future__ import annotations

import json
import os
import tempfile
import unittest
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

    def test_only_generated_exact_symbols_become_pairs(self):
        path = self._write({'symbols': ['SOLUSDT', 'ETHUSDT']})
        self.assertEqual(load_pairs(path), ['ETH/USDT', 'SOL/USDT'])

    def test_missing_malformed_empty_duplicate_and_policy_violations_block(self):
        cases = [
            ({}, 'symbols array'),
            ({'symbols': []}, 'no current V19.1'),
            ({'symbols': ['ethusdt']}, 'uppercase'),
            ({'symbols': ['ETHUSDT', 'ETHUSDT']}, 'duplicate'),
            ({'symbols': ['BTCUSDT']}, 'universe policy'),
            ({'symbols': ['USDCUSDT']}, 'universe policy'),
            ({'symbols': ['ETHUPUSDT']}, 'universe policy'),
        ]
        for payload, message in cases:
            with self.subTest(payload=payload), self.assertRaisesRegex(
                    BacktestPairError, message):
                load_pairs(self._write(payload))

    def test_policy_constants_match_the_protected_universe_policy(self):
        self.assertEqual(EXCLUDED_BASES, frozenset(scanner.EXCLUDED))
        self.assertEqual(LEVERAGED_SUFFIXES, scanner.LEV_SUFFIX)
        with mock.patch.dict(os.environ, {'STABLECOINS': DEFAULT_STABLECOINS}):
            expected = frozenset(DEFAULT_STABLECOINS.split(','))
            self.assertEqual(expected, frozenset(scanner.STABLES))

    def test_shell_scripts_use_arrays_and_not_deprecated_halal_list(self):
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
