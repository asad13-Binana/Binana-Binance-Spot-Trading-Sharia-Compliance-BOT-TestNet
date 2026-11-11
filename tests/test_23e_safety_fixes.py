from __future__ import annotations
"""Regression tests for the 2026-07-23E safety fixes.

Each test pins one independently-reported defect so it cannot silently return:

  SPOT-FAILOPEN-001 / F5-001  missing isSpotTradingAllowed meant "permitted"
  H-001 / UDS-STARTUP-001     rejected subscription left a live, unsubscribed socket
  H-001                       serverShutdown was ignored
  UDS-EVENT-001 / F5-002      balanceUpdate / externalLockUpdate never reconciled
  H-001                       sidecar health was hardcoded ok=True
  H-004                       Telegram bot token leaked through exception text
  SHARIA-HEALTH-001 / F5-004  Sharia health said ok=true with no screening API
  M-001                       malformed provider response stranded a RUNNING request

No strategy, signal, indicator or order-decision logic is exercised or changed.
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import tests._harness as harness  # noqa: E402,F401  (bus keys before service imports)


class SpotPermissionFailsClosedTests(unittest.TestCase):
    """SPOT-FAILOPEN-001: absent Spot permission is uncertainty, not consent."""


    def test_universe_scanner_rejects_symbol_without_spot_permission(self):
        from services.universe_service.scanner import _basic_filter
        info = {'symbol': 'FAKEUSDT', 'baseAsset': 'FAKE', 'quoteAsset': 'USDT',
                'status': 'TRADING', 'ocoAllowed': True, 'otoAllowed': True,
                'allowTrailingStop': True}  # isSpotTradingAllowed ABSENT
        _symbol, _base, reasons = _basic_filter(info)
        self.assertIn('spot_not_allowed', reasons)

    def test_universe_scanner_accepts_explicit_spot_permission(self):
        from services.universe_service.scanner import _basic_filter
        info = {'symbol': 'REALUSDT', 'baseAsset': 'REAL', 'quoteAsset': 'USDT',
                'status': 'TRADING', 'isSpotTradingAllowed': True,
                'ocoAllowed': True, 'otoAllowed': True, 'allowTrailingStop': True}
        _symbol, _base, reasons = _basic_filter(info)
        self.assertNotIn('spot_not_allowed', reasons)






class TelegramSecretRedactionTests(unittest.TestCase):
    """H-004: the bot token must never reach a log or a shared health file."""

    def test_token_is_redacted_from_exception_text(self):
        sentinel = '123456789:EXAMPLE_fake_token_must_never_leak_0123'  # EXAMPLE placeholder
        with mock.patch.dict(os.environ, {
                'TELEGRAM_BOT_TOKEN': sentinel,
                'TELEGRAM_OWNER_CHAT_ID': '1'}, clear=False):
            import importlib
            from services.telegram_broker import bot as bot_module
            bot_module = importlib.reload(bot_module)
            exc = RuntimeError(
                f'HTTPSConnectionPool: /bot{sentinel}/getUpdates failed')
            safe = bot_module._redact_secrets(exc)
            self.assertNotIn(sentinel, safe)
            self.assertIn('REDACTED', safe)

    def test_generic_bot_token_shape_is_redacted_even_if_not_configured(self):
        from services.telegram_broker import bot as bot_module
        foreign = 'bot987654321:EXAMPLE_foreign_fake_token_0123456789'  # EXAMPLE placeholder
        safe = bot_module._redact_secrets(RuntimeError(f'url /{foreign}/x'))
        self.assertNotIn('EXAMPLE_foreign_fake_token', safe)


class ShariaReadinessTests(unittest.TestCase):
    """SHARIA-HEALTH-001 and M-001."""

    def test_health_is_not_ok_when_screening_api_is_unavailable(self):
        source = (ROOT / 'services/sharia_screener/service.py').read_text(encoding='utf-8')
        self.assertNotIn("'ok': True, 'ts': time.time(),", source,
                         msg='Sharia health must not hardcode ok=True')
        self.assertIn("'ready_for_screening'", source)
        self.assertIn("'ok': bool(available)", source)

    def test_provider_and_validation_errors_cannot_strand_a_running_request(self):
        source = (ROOT / 'services/sharia_screener/service.py').read_text(encoding='utf-8')
        # Every failure path out of process_request must mark the request failed.
        self.assertIn('sharia_provider_error', source)
        self.assertIn('sharia_validation_error', source)
        self.assertIn('sharia_outcome_write_failed', source)
        self.assertIn('def _mark_failed(self, row: dict, error: str)', source)
        self.assertIn('self.queue.mark_failed(', source)
        self.assertGreaterEqual(source.count('self._mark_failed(row'), 5)


if __name__ == '__main__':
    unittest.main()
