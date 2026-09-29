import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

class ZeroFillCleanupSourceContract(unittest.TestCase):
    def test_freqtrade_notifies_binana_on_zero_fill_terminal_entry(self):
        src=(ROOT/'freqtrade/freqtradebot.py').read_text()
        needle="if float(order[\"filled\"]) == 0:"
        start=src.index(needle)
        block=src[start:start+700]
        self.assertIn('entry_no_fill', block)

    def test_telegram_suppresses_max_slot_rejection_notices(self):
        src=(ROOT/'binana_tests/fixtures/runtime_stable_panel.py').read_text()
        self.assertIn('NOISY_ADMISSION_REASONS', src)
        self.assertIn('REJECTED_MAX_SLOTS', src)
        marker="elif typ=='TRADE_REJECTED':"
        start=src.index(marker)
        block=src[start:start+700]
        self.assertIn('NOISY_ADMISSION_REASONS', block)

if __name__ == "__main__": unittest.main()
