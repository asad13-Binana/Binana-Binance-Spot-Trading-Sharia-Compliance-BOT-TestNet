"""Accepted side effects must survive failed replies as an unknown outcome."""
import json
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from tests import _harness  # noqa: F401
from services.telegram_broker import bot


@pytest.mark.parametrize('all_replies_fail', [False, True])
def test_accepted_command_reply_failure_is_durably_reported_without_replay(tmp_path, all_replies_fail):
    update = {'update_id': 100, 'message': {'chat': {'id': '42'}}}
    response = SimpleNamespace(json=lambda: {'ok': True, 'result': [update]})
    replies = []

    def send(text, *_args, **_kwargs):
        replies.append(text)
        if all_replies_fail or len(replies) == 1:
            raise TimeoutError('offline simulated delivery failure')
        return {'ok': True}

    accepted = Mock(return_value={'ok': True, 'result': 'accepted by simulated sidecar'})
    offset_path = tmp_path / 'offset.json'
    outbox = tmp_path / 'outbox'
    with ExitStack() as stack:
        for name, value in {
            'TOKEN': 'offline-test-token', 'OWNER': '42', 'RUNTIME': tmp_path,
            'OFFSET_PATH': offset_path, 'TELEGRAM_ALERT_OUTBOX': outbox,
            'sidecar_command': accepted, 'send': send,
            'handle_message': lambda _message: bot._confirm_action('emergency_exit', {'pair': 'ADA/USDT'}, '42'),
            'deliver_sidecar_notifications': lambda: None,
            'deliver_signal_notifications': lambda: None,
            '_maybe_restore_testnet_entries': lambda: None,
            '_telegram_rate_guard': lambda: None, 'audit': Mock(),
        }.items():
            stack.enter_context(patch.object(bot, name, value))
        stack.enter_context(patch.object(bot.envelope, 'load_key', return_value=b'offline'))
        stack.enter_context(patch.object(bot.requests, 'get', side_effect=[response, KeyboardInterrupt()]))
        with pytest.raises(KeyboardInterrupt):
            bot.main()
        accepted.assert_called_once()
        assert accepted.call_args.args[0] == 'emergency_exit'
        assert not any('no trading action was performed' in text for text in replies)
        notices = list(outbox.glob('*.json'))
        assert len(notices) == 1
        assert 'unknown' in json.loads(notices[0].read_text())['text']
        assert bot._claim_update(100, bot._load_offset()) == (101, False)
        bot._recover_interrupted_update()
        assert list(outbox.glob('*.json')) == notices
