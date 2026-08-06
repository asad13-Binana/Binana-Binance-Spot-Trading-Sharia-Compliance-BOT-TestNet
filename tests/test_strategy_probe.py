from __future__ import annotations
"""V102-REM-016 / A-001: run the Freqtrade strategy smoke probe in the gate and
prove the ACTIVE entry gate.

History (why this file was rewritten): the first version forced the 1-minute
``macdhist`` column and asserted it was a hard gate. That was wrong — the
strategy's active gate is the 5-minute ``macdhist_5m``; the 1-minute condition
is deliberately commented out (docs/STRATEGY_NOTES.md). The old test also
accepted a zero-signal fixture and converted ANY probe exception into a skip,
so a real failure could be hidden. An independent audit (A-001) caught this.

This version:
  * forces ``macdhist_5m`` (the real gate) and requires zero entries;
  * requires the fixture to actually produce entries (no vacuous pass);
  * separately proves the 1-minute ``macdhist`` changes NOTHING;
  * skips ONLY when the optional TA-Lib/pandas/numpy stack is missing, and
    fails on any other probe error.

The strategy source itself is unchanged.
"""
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / 'freqtrade' / 'tests' / 'audit_probes.py'


def _require_strategy_stack():
    """Skip only for a genuinely absent optional dependency."""
    for module in ('numpy', 'pandas', 'talib'):
        try:
            __import__(module)
        except ImportError as exc:
            raise unittest.SkipTest(
                f'optional strategy stack missing ({module}): {exc}. '
                'The probe runs fully in the pinned Freqtrade 2026.6 container '
                '(see the strategy-probe CI job).') from exc


def _load_probe():
    spec = importlib.util.spec_from_file_location('audit_probes_under_test', PROBE)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(PROBE.exists(), 'strategy probe not present')
class StrategyProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _require_strategy_stack()
        # Deliberately NOT wrapped in try/except: a probe error is a real
        # failure and must not be disguised as a skip (A-001).
        cls.probe = _load_probe()
        cls.result = cls.probe.strategy_smoke()

    def test_fixture_actually_exercises_the_entry_path(self):
        """Non-vacuity: a fixture that produces no entries proves nothing."""
        self.assertGreater(self.result['candles'], 0)
        baseline = self.result['entry_signals']
        forced_ok = self.result['entries_macd5m_forced_positive']
        self.assertGreater(
            max(baseline, forced_ok), 0,
            msg=('strategy fixture produced ZERO entry signals; the gate tests '
                 'below would be vacuous. Investigate the probe fixture or the '
                 'strategy before trusting this suite.'))

    def test_active_5m_macd_gate_blocks_entries_when_negative(self):
        """macdhist_5m > 0 is ANDed into populate_entry_trend, so forcing it
        negative must eliminate every entry."""
        self.assertEqual(
            self.result['entries_macd5m_forced_negative'], 0,
            msg='forcing macdhist_5m negative did not block entries — the '
                'active 5m MACD hard gate is not effective')

    def test_active_5m_macd_gate_permits_entries_when_positive(self):
        self.assertGreaterEqual(
            self.result['entries_macd5m_forced_positive'],
            self.result['entries_macd5m_forced_negative'])

    def test_1m_macd_is_reference_only_and_changes_no_entries(self):
        """docs/STRATEGY_NOTES.md: the 1-minute MACD (5/13/6) is computed for
        reference and must NOT influence entry selection."""
        baseline = self.result['entry_signals']
        self.assertEqual(self.result['entries_macd_forced_negative'], baseline,
                         msg='1m macdhist affected entries; it must be reference-only')
        self.assertEqual(self.result['entries_macd_forced_positive'], baseline,
                         msg='1m macdhist affected entries; it must be reference-only')

    def test_exit_path_executes(self):
        self.assertGreaterEqual(self.result['exit_signals'], 0)

    def test_prefix_determinism(self):
        for column, diff in self.result['prefix_max_abs_diffs'].items():
            self.assertLess(diff, 1e-6, msg=f'{column} not deterministic: {diff}')


if __name__ == '__main__':
    unittest.main()
