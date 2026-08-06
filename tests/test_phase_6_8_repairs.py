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
from services.execution_sidecar.core_adapter import CoreAdapter  # noqa: E402
from services.execution_sidecar.filters import (  # noqa: E402
    FilterDataUnavailable, FilterViolation, SpotFilterValidator,
)
from services.execution_sidecar import main as sidecar_main  # noqa: E402
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


class FilterRepairTests(unittest.TestCase):
    def test_missing_core_filter_metadata_fails_closed(self):
        validator = SpotFilterValidator(public_client=_Public([]), max_age_seconds=300)
        with self.assertRaisesRegex(FilterDataUnavailable, 'PRICE_FILTER'):
            validator.validate_replacement(
                'ETHUSDT', 'order',
                {'side': 'SELL', 'type': 'STOP_LOSS_LIMIT', 'quantity': '1',
                 'price': '49.00', 'stopPrice': '49.50'})

    def test_missing_required_filter_field_fails_closed(self):
        filters = _core_filters()
        filters[1].pop('stepSize')
        validator = SpotFilterValidator(public_client=_Public(filters), max_age_seconds=300)
        with self.assertRaisesRegex(FilterDataUnavailable, 'LOT_SIZE.stepSize'):
            validator.validate_replacement(
                'ETHUSDT', 'order',
                {'side': 'SELL', 'type': 'LIMIT', 'quantity': '1', 'price': '50.00'})

    def test_market_lot_size_zero_step_still_enforces_minimum(self):
        validator = SpotFilterValidator(
            public_client=_Public(_core_filters(market_step='0', market_min='5')),
            max_age_seconds=300)
        with self.assertRaisesRegex(FilterViolation, 'MARKET_LOT_SIZE.minQty'):
            validator.validate_replacement(
                'ETHUSDT', 'order',
                {'side': 'SELL', 'type': 'MARKET', 'quantity': '1'})

    def test_market_lot_and_checked_filter_list_are_exact(self):
        validator = SpotFilterValidator(
            public_client=_Public(_core_filters(market_step='5', market_min='5')),
            max_age_seconds=300)
        with self.assertRaisesRegex(FilterViolation, 'MARKET_LOT_SIZE.stepSize'):
            validator.validate_replacement(
                'ETHUSDT', 'order',
                {'side': 'SELL', 'type': 'MARKET', 'quantity': '6'})
        validator = SpotFilterValidator(
            public_client=_Public(_core_filters(market_step='0', market_min='5')),
            max_age_seconds=300)
        summary = validator.validate_replacement(
            'ETHUSDT', 'order',
            {'side': 'SELL', 'type': 'MARKET', 'quantity': '5'})
        self.assertEqual(summary['filters_checked'], ['LOT_SIZE', 'MARKET_LOT_SIZE'])

    def test_limit_summary_lists_only_evaluated_filters(self):
        validator = SpotFilterValidator(
            public_client=_Public(_core_filters()), max_age_seconds=300)
        summary = validator.validate_replacement(
            'ETHUSDT', 'order',
            {'side': 'SELL', 'type': 'LIMIT', 'quantity': '1', 'price': '50.00'})
        self.assertEqual(
            summary['filters_checked'], ['PRICE_FILTER', 'LOT_SIZE', 'NOTIONAL'])


class OrdersVisibilityTests(unittest.TestCase):
    @staticmethod
    def _adapter(client):
        broker = SimpleNamespace(c=client, _sync_weight=mock.Mock())
        adapter = CoreAdapter.__new__(CoreAdapter)
        adapter.trader = SimpleNamespace(
            is_running=lambda: True, broker=broker,
        )
        return adapter

    def test_open_orders_requires_both_exchange_endpoints(self):
        client = SimpleNamespace(
            get_open_orders=lambda: [{'symbol': 'ETHUSDT', 'orderId': 1}],
            _get=mock.Mock(side_effect=TimeoutError('mock list timeout')),
        )
        with mock.patch('services.execution_sidecar.core_adapter.audit'):
            result = self._adapter(client).open_orders_snapshot()
        self.assertFalse(result['ok'])
        self.assertNotIn('open_orders', result)

    def test_open_orders_returns_safelisted_orders_and_lists(self):
        class Client:
            def get_open_orders(self):
                return [{'symbol': 'ETHUSDT', 'orderId': 1, 'status': 'NEW',
                         'secret': 'must-not-leak'}]

            def _get(self, endpoint, signed, data):
                self.args = (endpoint, signed, data)
                return [{'symbol': 'ETHUSDT', 'orderListId': 2,
                         'listOrderStatus': 'EXECUTING',
                         'orders': [{'symbol': 'ETHUSDT', 'orderId': 1}]}]

        client = Client()
        with mock.patch('services.execution_sidecar.core_adapter.audit'):
            result = self._adapter(client).open_orders_snapshot()
        self.assertTrue(result['ok'])
        self.assertEqual(result['open_order_count'], 1)
        self.assertEqual(result['open_order_list_count'], 1)
        self.assertNotIn('secret', result['open_orders'][0])
        self.assertEqual(client.args, ('openOrderList', True, {}))

    def test_orders_command_accepts_signed_envelope_and_persists_result(self):
        class State:
            data = {'simulation': False}

            def claim_command(self, _cid, _cmd):
                return True

            def record_command(self, _cid, _cmd, result):
                self.result = json.loads(result)

        adapter = SimpleNamespace(open_orders_snapshot=mock.Mock(return_value={
            'ok': True, 'open_orders': [], 'open_order_lists': [],
        }))
        state = State()
        with tempfile.TemporaryDirectory(prefix='orders-command-') as raw:
            runtime = Path(raw)
            command = runtime / 'orders-1.json'
            command.write_text(json.dumps(
                _harness.sign_command('orders', command_id='orders-1')),
                encoding='utf-8')
            with mock.patch.object(sidecar_main, 'RUNTIME', runtime), \
                    mock.patch.object(sidecar_main, 'audit'):
                sidecar_main.process_command(adapter, state, SimpleNamespace(), command)
            result = json.loads((runtime / 'command_result_orders-1.json').read_text(
                encoding='utf-8'))
        self.assertTrue(result['ok'])
        self.assertEqual(result['command'], 'orders')
        adapter.open_orders_snapshot.assert_called_once_with()


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
                    mock.patch.object(telegram_bot, 'handle_message', replay_handler):
                with self.assertRaises(StopPolling):
                    telegram_bot.main()
            replay_handler.assert_not_called()

            with mock.patch.object(telegram_bot, '_store_offset',
                                   side_effect=OSError('disk unavailable')):
                with self.assertRaises(OSError):
                    telegram_bot._claim_update(42, 42)

    def test_sidecar_alert_outbox_retries_and_deduplicates_without_network(self):
        with tempfile.TemporaryDirectory(prefix='alert-outbox-') as raw:
            root = Path(raw)
            outbox = root / 'outbox'
            outbox.mkdir()
            delivery_state = root / 'delivery.json'
            with mock.patch.object(sidecar_main, 'TELEGRAM_ALERT_OUTBOX', outbox), \
                    mock.patch.object(sidecar_main, 'audit') as audit_call:
                notification_id = sidecar_main.notify('critical execution alert')
            self.assertEqual(audit_call.call_args.args, ('sidecar_notification',))
            path = outbox / f'{notification_id}.json'
            payload = json.loads(path.read_text(encoding='utf-8'))
            with mock.patch.object(telegram_bot, 'TELEGRAM_ALERT_OUTBOX', outbox), \
                    mock.patch.object(telegram_bot, 'ALERT_DELIVERY_STATE', delivery_state), \
                    mock.patch.object(telegram_bot, 'OWNER', '4242'), \
                    mock.patch.object(telegram_bot, 'audit'), \
                    mock.patch.object(telegram_bot, 'send',
                                      side_effect=RuntimeError('mock Telegram offline')):
                self.assertEqual(telegram_bot.deliver_sidecar_notifications(), 0)
            self.assertTrue(path.exists(), 'failed delivery must remain durable')

            delivered = mock.Mock()
            with mock.patch.object(telegram_bot, 'TELEGRAM_ALERT_OUTBOX', outbox), \
                    mock.patch.object(telegram_bot, 'ALERT_DELIVERY_STATE', delivery_state), \
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
