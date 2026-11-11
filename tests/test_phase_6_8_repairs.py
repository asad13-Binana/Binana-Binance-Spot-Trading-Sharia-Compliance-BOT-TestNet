from __future__ import annotations

import io
import json
import logging
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_WORKSPACE = Path.cwd()
_TEMP = tempfile.TemporaryDirectory(prefix='phase-6-8-')
_ROOT = Path(_TEMP.name)
os.environ.setdefault('SHARED_ROOT', str(_ROOT))
os.environ.setdefault('LEGACY_RUNTIME_DIR', str(_ROOT / 'legacy'))
os.environ.setdefault('RUNTIME_DIR', str(_ROOT / 'runtime'))
os.environ.setdefault('AUDIT_LOG', str(_ROOT / 'audit' / 'events.jsonl'))
os.environ.setdefault('TELEGRAM_OWNER_CHAT_ID', '4242')
os.environ.setdefault('TELEGRAM_BOT_TOKEN', '123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ123456')

import _harness  # noqa: E402,F401 - install deterministic bus keys first
from services.common.atomic import atomic_write_json  # noqa: E402
from services.telegram_broker import bot as telegram_bot  # noqa: E402

os.chdir(_WORKSPACE)


class _Public:
    NO_REFERENCE_PRICE = object()

    def __init__(self, filters):
        self.filters = filters

    def exchange_info(self, symbol):
        return {'symbols': [{
            'symbol': symbol, 'status': 'TRADING',
            'isSpotTradingAllowed': True, 'baseAsset': 'ETH', 'quoteAsset': 'USDT',
            'filters': self.filters,
        }]}

    def reference_price(self, _symbol):
        return Decimal('50')


def _core_filters(*, market_step='0', market_min='0', market_max='1000'):
    return [
        {'filterType': 'PRICE_FILTER', 'tickSize': '0.01',
         'minPrice': '0.01', 'maxPrice': '100000'},
        {'filterType': 'LOT_SIZE', 'stepSize': '0.001',
         'minQty': '0.001', 'maxQty': '1000'},
        {'filterType': 'MARKET_LOT_SIZE', 'stepSize': market_step,
         'minQty': market_min, 'maxQty': market_max},
        {'filterType': 'NOTIONAL', 'minNotional': '1', 'maxNotional': '0',
         'applyMinToMarket': False, 'applyMaxToMarket': False},
    ]






class TelegramRepairTests(unittest.TestCase):
    def test_callback_acknowledgement_error_redacts_token(self):
        token = '123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ123456'
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger = telegram_bot.log
        old_level = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            with mock.patch.object(telegram_bot, 'TOKEN', token), \
                    mock.patch.object(telegram_bot, 'BASE', f'https://api.telegram.org/bot{token}'), \
                    mock.patch.object(telegram_bot, 'is_owner', return_value=True), \
                    mock.patch.object(telegram_bot.requests, 'post',
                                      side_effect=RuntimeError(
                                          f'failure at https://api.telegram.org/bot{token}/answerCallbackQuery')):
                telegram_bot.handle_callback({
                    'id': 'cb-1', 'from': {'id': 4242},
                    'message': {'chat': {'id': 4242}}, 'data': 'unknown',
                })
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)
        self.assertNotIn(token, stream.getvalue())
        self.assertIn('REDACTED', stream.getvalue())

    def test_owner_orders_uses_signed_sidecar_command_route(self):
        with mock.patch.object(telegram_bot, 'is_owner', return_value=True), \
                mock.patch.object(telegram_bot, 'sidecar_command',
                                  return_value={'ok': True}) as command, \
                mock.patch.object(telegram_bot, 'send'):
            telegram_bot.handle_message({
                'from': {'id': 4242}, 'chat': {'id': 4242}, 'text': '/orders',
            })
        command.assert_called_once_with('orders', wait=True)

    def test_telegram_update_is_claimed_before_dispatch_and_not_replayed(self):
        class StopPolling(BaseException):
            pass

        with tempfile.TemporaryDirectory(prefix='telegram-claim-') as raw:
            runtime = Path(raw)
            offset_path = runtime / 'telegram_offset.json'
            update = {
                'update_id': 41,
                'message': {
                    'from': {'id': 4242}, 'chat': {'id': 4242},
                    'text': '/scan BTC/USDT',
                },
            }
            response = SimpleNamespace(json=mock.Mock(return_value={
                'ok': True, 'result': [update],
            }))
            observed_offsets = []

            def observe_claim_then_stop(_message):
                observed_offsets.append(json.loads(offset_path.read_text(
                    encoding='utf-8'))['offset'])
                raise StopPolling()

            common = (
                mock.patch.object(telegram_bot, 'RUNTIME', runtime),
                mock.patch.object(telegram_bot, 'OFFSET_PATH', offset_path),
                mock.patch.object(telegram_bot, 'TOKEN', 'test-token'),
                mock.patch.object(telegram_bot, 'OWNER', '4242'),
                mock.patch.object(telegram_bot.envelope, 'load_key'),
                mock.patch.object(telegram_bot, 'deliver_sidecar_notifications',
                                  return_value=0),
            )
            with common[0], common[1], common[2], common[3], common[4], common[5], \
                    mock.patch.object(telegram_bot.requests, 'get', return_value=response), \
                    mock.patch.object(telegram_bot, 'handle_message',
                                      side_effect=observe_claim_then_stop):
                with self.assertRaises(StopPolling):
                    telegram_bot.main()

            self.assertEqual(observed_offsets, [42],
                             'offset must be durable before /scan dispatch')

            # A restart at the same update must skip dispatch because offset 42
            # was committed before the first handler began.
            replay_handler = mock.Mock()
            replay_get = mock.Mock(side_effect=[response, StopPolling()])
            common = (
                mock.patch.object(telegram_bot, 'RUNTIME', runtime),
                mock.patch.object(telegram_bot, 'OFFSET_PATH', offset_path),
                mock.patch.object(telegram_bot, 'TOKEN', 'test-token'),
                mock.patch.object(telegram_bot, 'OWNER', '4242'),
                mock.patch.object(telegram_bot.envelope, 'load_key'),
                mock.patch.object(telegram_bot, 'deliver_sidecar_notifications',
                                  return_value=0),
            )
            with common[0], common[1], common[2], common[3], common[4], common[5], \
                    mock.patch.object(telegram_bot.requests, 'get', replay_get), \
                    mock.patch.object(telegram_bot, 'TELEGRAM_ALERT_OUTBOX', runtime / 'alerts'), \
                    mock.patch.object(telegram_bot, 'handle_message', replay_handler):
                with self.assertRaises(StopPolling):
                    telegram_bot.main()
            replay_handler.assert_not_called()

            with mock.patch.object(telegram_bot, '_store_offset',
                                   side_effect=OSError('disk unavailable')):
                with self.assertRaises(OSError):
                    telegram_bot._claim_update(42, 42)


    def test_malformed_alerts_are_quarantined_without_starving_valid_alert(self):
        with tempfile.TemporaryDirectory(prefix='alert-quarantine-') as raw:
            root = Path(raw)
            outbox = root / 'outbox'
            quarantine = root / 'quarantine'
            health = root / 'health.json'
            state = root / 'delivery.json'
            outbox.mkdir()
            for index in range(25):
                (outbox / f'000-invalid-{index:02d}.json').write_text(
                    '{not-json', encoding='utf-8')
            notification_id = 'f' * 32
            atomic_write_json(outbox / f'{notification_id}.json', {
                'notification_id': notification_id,
                'text': 'valid emergency alert',
            })
            delivered = mock.Mock()
            with mock.patch.object(telegram_bot, 'TELEGRAM_ALERT_OUTBOX', outbox), \
                    mock.patch.object(telegram_bot, 'ALERT_DELIVERY_STATE', state), \
                    mock.patch.object(telegram_bot, 'ALERT_QUARANTINE', quarantine), \
                    mock.patch.object(telegram_bot, 'ALERT_OUTBOX_HEALTH', health), \
                    mock.patch.object(telegram_bot, 'OWNER', '4242'), \
                    mock.patch.object(telegram_bot, 'audit'), \
                    mock.patch.object(telegram_bot, 'send', delivered):
                self.assertEqual(telegram_bot.deliver_sidecar_notifications(limit=1), 1)
            delivered.assert_called_once_with('valid emergency alert', '4242')
            self.assertEqual(len(list(quarantine.glob('*.json'))), 25)
            status = json.loads(health.read_text(encoding='utf-8'))
            self.assertTrue(status['ok'])
            self.assertEqual(status['pending_alert_count'], 0)
            self.assertEqual(status['dead_letter_count'], 25)

    def test_corrupt_alert_dedupe_state_pauses_delivery(self):
        with tempfile.TemporaryDirectory(prefix='alert-dedupe-corrupt-') as raw:
            root = Path(raw)
            outbox = root / 'outbox'
            quarantine = root / 'quarantine'
            health = root / 'health.json'
            state = root / 'delivery.json'
            outbox.mkdir()
            state.write_text('{broken', encoding='utf-8')
            notification_id = 'e' * 32
            atomic_write_json(outbox / f'{notification_id}.json', {
                'notification_id': notification_id, 'text': 'do not duplicate',
            })
            delivered = mock.Mock()
            with mock.patch.object(telegram_bot, 'TELEGRAM_ALERT_OUTBOX', outbox), \
                    mock.patch.object(telegram_bot, 'ALERT_DELIVERY_STATE', state), \
                    mock.patch.object(telegram_bot, 'ALERT_QUARANTINE', quarantine), \
                    mock.patch.object(telegram_bot, 'ALERT_OUTBOX_HEALTH', health), \
                    mock.patch.object(telegram_bot, 'audit'), \
                    mock.patch.object(telegram_bot, 'send', delivered):
                self.assertEqual(telegram_bot.deliver_sidecar_notifications(), 0)
            delivered.assert_not_called()
            status = json.loads(health.read_text(encoding='utf-8'))
            self.assertFalse(status['ok'])
            self.assertIn('dedupe state', status['blocked_reason'])

    def test_corrupt_telegram_offset_fails_closed(self):
        with tempfile.TemporaryDirectory(prefix='telegram-offset-corrupt-') as raw:
            path = Path(raw) / 'telegram_offset.json'
            path.write_text('{broken', encoding='utf-8')
            with mock.patch.object(telegram_bot, 'OFFSET_PATH', path):
                with self.assertRaisesRegex(RuntimeError, 'refusing to replay'):
                    telegram_bot._load_offset()

    def test_durable_alert_consumer_retries_and_deduplicates_without_network(self):
        with tempfile.TemporaryDirectory(prefix='alert-outbox-') as raw:
            root = Path(raw)
            outbox = root / 'outbox'
            outbox.mkdir()
            delivery_state = root / 'delivery.json'
            quarantine = root / 'quarantine'
            health = root / 'health.json'
            notification_id = 'a' * 32
            path = outbox / f'{notification_id}.json'
            payload = {'schema': 1, 'notification_id': notification_id,
                       'created_at': 1, 'text': 'critical execution alert',
                       'buttons': None, 'chat_id': 'untrusted-producer-destination'}
            atomic_write_json(path, payload)
            with mock.patch.object(telegram_bot, 'TELEGRAM_ALERT_OUTBOX', outbox), \
                    mock.patch.object(telegram_bot, 'ALERT_DELIVERY_STATE', delivery_state), \
                    mock.patch.object(telegram_bot, 'ALERT_QUARANTINE', quarantine), \
                    mock.patch.object(telegram_bot, 'ALERT_OUTBOX_HEALTH', health), \
                    mock.patch.object(telegram_bot, 'OWNER', '4242'), \
                    mock.patch.object(telegram_bot, 'audit'), \
                    mock.patch.object(telegram_bot, 'send',
                                      side_effect=RuntimeError('mock Telegram offline')):
                self.assertEqual(telegram_bot.deliver_sidecar_notifications(), 0)
            self.assertTrue(path.exists(), 'failed delivery must remain durable')
            failed_health = json.loads(health.read_text(encoding='utf-8'))
            self.assertFalse(failed_health['ok'])
            self.assertIn('delivery failed', failed_health['blocked_reason'])

            delivered = mock.Mock()
            with mock.patch.object(telegram_bot, 'TELEGRAM_ALERT_OUTBOX', outbox), \
                    mock.patch.object(telegram_bot, 'ALERT_DELIVERY_STATE', delivery_state), \
                    mock.patch.object(telegram_bot, 'ALERT_QUARANTINE', quarantine), \
                    mock.patch.object(telegram_bot, 'ALERT_OUTBOX_HEALTH', health), \
                    mock.patch.object(telegram_bot, 'OWNER', '4242'), \
                    mock.patch.object(telegram_bot, 'audit'), \
                    mock.patch.object(telegram_bot, 'send', delivered):
                self.assertEqual(telegram_bot.deliver_sidecar_notifications(), 1)
                self.assertFalse(path.exists())
                # Simulate a leftover duplicate file after the delivered-ID
                # journal committed; it must be removed without another send.
                atomic_write_json(path, payload)
                self.assertEqual(telegram_bot.deliver_sidecar_notifications(), 0)
            delivered.assert_called_once_with('critical execution alert', '4242')



if __name__ == '__main__':
    unittest.main()
