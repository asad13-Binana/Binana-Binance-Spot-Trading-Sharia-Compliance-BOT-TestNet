"""Telegram flood control must delay API calls without replaying commands."""
from unittest import mock

import pytest

from tests import _harness  # noqa: F401
from services.telegram_broker import bot


class Response:
    def __init__(self, code, payload):
        self.status_code = code
        self.payload = payload

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise bot.requests.HTTPError('HTTP error')


def test_429_blocks_all_telegram_calls_until_retry_after_elapsed():
    attempts = []
    responses = iter([
        Response(429, {'ok': False, 'error_code': 429,
                       'parameters': {'retry_after': 120}}),
        Response(200, {'ok': True, 'result': {'message_id': 1}}),
    ])

    def post(*args, **kwargs):
        attempts.append(args[0])
        return next(responses)

    with mock.patch.object(bot, 'TOKEN', 'mock-token'), \
            mock.patch.object(bot, '_TELEGRAM_RETRY_AT', 0, create=True), \
            mock.patch.object(bot.requests, 'post', side_effect=post), \
            mock.patch.object(bot.time, 'monotonic', return_value=100) as now:
        with pytest.raises(Exception):
            bot.send('first')
        with pytest.raises(Exception):
            bot.send('second')
        assert len(attempts) == 1
        assert bot._telegram_retry_delay() == 120
        now.return_value = 220
        bot.send('after cooldown')
        assert len(attempts) == 2


def test_telegram_ok_false_cannot_be_recorded_as_success():
    with mock.patch.object(bot, 'TOKEN', 'mock-token'), \
            mock.patch.object(bot, '_TELEGRAM_RETRY_AT', 0, create=True), \
            mock.patch.object(bot.requests, 'post', return_value=Response(
                200, {'ok': False, 'error_code': 500, 'description': 'rejected'})), \
            pytest.raises(RuntimeError):
        bot.send('must remain pending')


@pytest.mark.parametrize('value, expected', [
    (True, 3), (None, 3), ('120', 3), (-1, 3), (0, 3), (1_000_000, 86_400),
])
def test_retry_after_is_type_checked_and_bounded(value, expected):
    with mock.patch.object(bot, '_TELEGRAM_RETRY_AT', 0), \
            mock.patch.object(bot.time, 'monotonic', return_value=100):
        with pytest.raises(RuntimeError):
            bot._telegram_check_payload({
                'ok': False, 'error_code': 429, 'parameters': {'retry_after': value}})
        assert bot._telegram_retry_delay() == expected
