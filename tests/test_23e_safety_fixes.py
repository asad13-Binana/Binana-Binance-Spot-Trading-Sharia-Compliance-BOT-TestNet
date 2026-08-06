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

    def test_all_four_consumers_default_to_false(self):
        consumers = {
            'services/common/binance_public.py': "isSpotTradingAllowed', False",
            'services/execution_sidecar/filters.py': "entry.get('isSpotTradingAllowed') is not True",
            'services/sharia_screener/service.py': "isSpotTradingAllowed', False",
            'services/universe_service/scanner.py': "isSpotTradingAllowed', False",
        }
        for rel, fail_closed_check in consumers.items():
            text = (ROOT / rel).read_text(encoding='utf-8')
            self.assertNotIn("isSpotTradingAllowed', True", text,
                             msg=f'{rel} still treats missing Spot permission as allowed')
            self.assertIn(fail_closed_check, text,
                          msg=f'{rel} must require explicit Spot permission')

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


class UserDataStreamTests(unittest.TestCase):
    """H-001 / UDS-EVENT-001 / UDS-STARTUP-001."""

    def _stream(self):
        from services.execution_sidecar import user_data_stream as uds
        obj = uds.ModernUserDataStream.__new__(uds.ModernUserDataStream)
        obj._ws = mock.Mock()
        obj._subscribed = True
        obj._connected = True
        obj._last_error = None
        obj._write_health = lambda: None
        obj.on_order_update = None
        obj.on_list_update = None
        obj.on_resync = mock.Mock()
        return obj

    def test_balance_update_triggers_reconciliation(self):
        stream = self._stream()
        stream._dispatch_event({'e': 'balanceUpdate'})
        stream.on_resync.assert_called_once()

    def test_external_lock_update_triggers_reconciliation(self):
        stream = self._stream()
        stream._dispatch_event({'e': 'externalLockUpdate'})
        stream.on_resync.assert_called_once()

    def test_outbound_account_position_still_reconciles(self):
        stream = self._stream()
        stream._dispatch_event({'e': 'outboundAccountPosition'})
        stream.on_resync.assert_called_once()

    def test_server_shutdown_closes_the_socket(self):
        stream = self._stream()
        stream._dispatch_event({'e': 'serverShutdown'})
        stream._ws.close.assert_called_once()
        self.assertFalse(stream._subscribed)
        self.assertEqual(stream._last_error, 'server_shutdown')

    def test_event_stream_terminated_closes_the_socket(self):
        stream = self._stream()
        stream._dispatch_event({'e': 'eventStreamTerminated'})
        stream._ws.close.assert_called_once()

    def test_rejected_subscription_closes_the_socket(self):
        """A non-200 acknowledgement must not leave a connected-but-
        unsubscribed socket alive with no reconnect."""
        import json as _json
        stream = self._stream()
        stream._request_id = 'req-1'
        stream._ready = mock.Mock()
        stream._backoff = 1.0
        stream._last_message_at = 0.0
        stream._last_event_at = 0.0
        stream._startup_error = None
        from services.execution_sidecar import user_data_stream as uds
        uds.ModernUserDataStream._on_message(stream, None,
                                       _json.dumps({'id': 'req-1', 'status': 401}))
        self.assertFalse(stream._subscribed)
        self.assertEqual(stream._last_error, 'subscription_rejected')
        stream._ws.close.assert_called_once()


class SidecarHealthReflectsStreamTests(unittest.TestCase):
    """H-001: health must not be green while the stream is unusable."""

    def test_simulation_needs_no_exchange_stream(self):
        from services.common.models import ExecutionMode
        from services.execution_sidecar.main import user_stream_state
        ok, detail = user_stream_state(ExecutionMode.SIMULATION)
        self.assertTrue(ok)
        self.assertFalse(detail['required'])

    def test_live_requires_connected_subscribed_and_fresh(self):
        import time as _time
        from services.common.models import ExecutionMode
        from services.execution_sidecar import main as sidecar_main
        cases = [
            ({'connected': True, 'subscribed': True, 'ts': _time.time()}, True),
            ({'connected': True, 'subscribed': False, 'ts': _time.time()}, False),
            ({'connected': False, 'subscribed': True, 'ts': _time.time()}, False),
            ({'connected': True, 'subscribed': True, 'ts': _time.time() - 99999}, False),
            ({}, False),
        ]
        for health, expected in cases:
            with self.subTest(health=health), \
                    mock.patch.object(sidecar_main, 'read_json', return_value=health):
                ok, _detail = sidecar_main.user_stream_state(ExecutionMode.LIVE)
                self.assertEqual(ok, expected)


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
