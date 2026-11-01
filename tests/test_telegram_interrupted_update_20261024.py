"""Crash between offset claim and dispatch must leave a durable owner notice."""
import json
from unittest import mock

import pytest

from tests import _harness  # noqa: F401
from services.telegram_broker import bot


def test_claim_survives_restart_as_an_observable_interrupted_request(tmp_path):
    offset = tmp_path / 'offset.json'
    outbox = tmp_path / 'outbox'
    with mock.patch.object(bot, 'OFFSET_PATH', offset), \
            mock.patch.object(bot, 'TELEGRAM_ALERT_OUTBOX', outbox), \
            mock.patch.object(bot, 'audit'):
        next_offset, claimed = bot._claim_update(42, 0)
        assert (next_offset, claimed) == (43, True)
        # Simulate death before dispatch: only durable state is available.
        assert json.loads(offset.read_text())['pending_update_id'] == 42
        bot._recover_interrupted_update()
        assert bot._load_offset() == 43
        assert bot._claim_update(42, bot._load_offset()) == (43, False)
        notice = list(outbox.glob('*.json'))
        assert len(notice) == 1
        payload = json.loads(notice[0].read_text())
        assert '42' in payload['text']
        assert 'unknown' in payload['text']
        assert 'replayed' in payload['text']
        bot._recover_interrupted_update()
        assert list(outbox.glob('*.json')) == notice


def test_recovery_cannot_erase_pending_claim_when_notice_storage_fails(tmp_path):
    offset = tmp_path / 'offset.json'
    with mock.patch.object(bot, 'OFFSET_PATH', offset), \
            mock.patch.object(bot, 'audit'):
        bot._claim_update(42, 0)
        writer = bot.atomic_write_json

        def deny_notice(path, payload, **kwargs):
            if path != offset:
                raise PermissionError('outbox unavailable')
            writer(path, payload, **kwargs)

        with mock.patch.object(bot, 'atomic_write_json', side_effect=deny_notice):
            with pytest.raises(PermissionError):
                bot._recover_interrupted_update()
        assert json.loads(offset.read_text())['pending_update_id'] == 42


def test_completed_claim_does_not_generate_an_interruption_notice(tmp_path):
    offset = tmp_path / 'offset.json'
    outbox = tmp_path / 'outbox'
    with mock.patch.object(bot, 'OFFSET_PATH', offset), \
            mock.patch.object(bot, 'TELEGRAM_ALERT_OUTBOX', outbox):
        bot._claim_update(42, 0)
        bot._store_offset(43)
        bot._recover_interrupted_update()
    assert not outbox.exists()


@pytest.mark.parametrize('pending', [True, -1, '42', 40])
def test_corrupt_pending_claim_fails_closed(tmp_path, pending):
    offset = tmp_path / 'offset.json'
    offset.write_text(json.dumps({'offset': 43, 'pending_update_id': pending}))
    with mock.patch.object(bot, 'OFFSET_PATH', offset):
        with pytest.raises(RuntimeError, match='corrupt'):
            bot._recover_interrupted_update()
