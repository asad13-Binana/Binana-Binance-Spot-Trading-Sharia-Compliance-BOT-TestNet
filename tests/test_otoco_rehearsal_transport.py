"""The matching-engine rehearsal has a one-POST, durable, Testnet-only boundary."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

PATH = Path(__file__).resolve().parents[1] / 'scripts/recovery/probe_testnet_otoco_no_fill.py'
spec = importlib.util.spec_from_file_location('otoco_probe', PATH)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


@pytest.mark.parametrize('url,method', [
    ('https://api.binance.com/api/v3/orderList/otoco', 'POST'),
    ('https://testnet.binance.vision/api/v3/order', 'POST'),
    ('https://testnet.binance.vision/api/v3/orderList', 'DELETE'),
    ('http://testnet.binance.vision/api/v3/orderList/otoco', 'POST'),
    ('https://testnet.binance.vision:444/api/v3/order', 'GET'),
    ('https://testnet.binance.vision@evil.test/api/v3/order', 'GET'),
])
def test_other_transport_rejected(url, method):
    with pytest.raises(RuntimeError):
        probe.check_transport(url, method)


def test_post_is_durable_before_transmission_and_cannot_repeat_after_timeout(monkeypatch):
    events = []
    report = {'plan': {'quantity': '1'}, 'matching_engine_post_attempts': 0}
    monkeypatch.setattr(probe, 'save', lambda r: events.append(('saved', r['matching_engine_post_attempts'])))
    def send(request, **kwargs):
        events.append(('sent', kwargs['allow_redirects']))
        raise TimeoutError()
    session = SimpleNamespace(send=send)
    probe.restrict_session(session, report)
    request = SimpleNamespace(method='POST', url='https://testnet.binance.vision/api/v3/orderList/otoco')
    with pytest.raises(TimeoutError):
        session.send(request)
    with pytest.raises(RuntimeError, match='SECOND_OR_UNJOURNALED_POST'):
        session.send(request)
    assert events == [('saved', 1), ('sent', False)]


def test_redirect_fails_without_following(monkeypatch):
    report = {'matching_engine_post_attempts': 0}
    session = SimpleNamespace(send=lambda request, **kwargs: SimpleNamespace(status_code=302))
    probe.restrict_session(session, report)
    with pytest.raises(RuntimeError, match='REDIRECT_DENIED'):
        session.send(SimpleNamespace(method='GET', url='https://testnet.binance.vision/api/v3/account'))


def test_existing_journal_is_never_overwritten_on_restart(tmp_path):
    path = tmp_path / 'report.json'
    report = {'matching_engine_post_attempts': 1, 'ids': {'list': 'retained'}}
    probe.initialize_report(report, path)
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        probe.initialize_report({'matching_engine_post_attempts': 0}, path)
    assert path.read_bytes() == before


def test_signed_request_details_are_never_in_error_output():
    message = 'request https://testnet.binance.vision/api/v3/order?signature=SENSITIVE {"code": -2010}'
    result = probe.safe_error(RuntimeError(message))
    assert result == {'type': 'RuntimeError', 'exchange_code': -2010}
    assert 'SENSITIVE' not in str(result)


def test_host_timeout_preserves_journal_and_writes_failed_receipt(tmp_path, monkeypatch):
    import json
    import subprocess
    import sys
    runner_spec = importlib.util.spec_from_file_location('otoco_runner', PATH.with_name('run_testnet_otoco_no_fill.py'))
    runner = importlib.util.module_from_spec(runner_spec)
    sys.modules['otoco_runner'] = runner
    runner_spec.loader.exec_module(runner)
    source = tmp_path / 'source'
    for name in ['sharia/halal_coins.json', 'universe/current_pairlist.json']:
        file = source / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text('{}')
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'initial_state': 'stopped', 'exchange': {'binana_testnet': True, 'name': 'binance'}}))
    monkeypatch.setattr(runner, 'ROOT', tmp_path / 'run')
    monkeypatch.setattr(runner, 'SOURCE', source)
    monkeypatch.setattr(runner, 'CONFIG', config)
    monkeypatch.setattr(runner.os, 'chown', lambda *args: None, raising=False)
    monkeypatch.setattr(runner.os, 'umask', lambda *args: 0)
    monkeypatch.setattr(runner, 'owner_state', lambda: {'state': 'paused', 'dry_run': False, 'trading_mode': 'spot'})
    monkeypatch.setattr(runner, 'database_summary', lambda: {'unchanged': True})
    monkeypatch.setattr(runner, 'inspect_owner', lambda: {'Id': 'owner', 'Image': 'retained', 'Config': {'Env': ['FREQTRADE__EXCHANGE__KEY=fixture', 'FREQTRADE__EXCHANGE__SECRET=fixture']}})
    def timeout(command, **kwargs):
        if command[1] == 'run':
            (runner.ROOT / 'data/report.json').write_text(json.dumps({'matching_engine_post_attempts': 1, 'ids': {'list': 'retained'}}))
            raise subprocess.TimeoutExpired(command, 180)
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(runner.subprocess, 'run', timeout)
    assert runner.main() == 1
    receipt = json.loads((runner.ROOT / 'receipt.json').read_text())
    assert receipt['passed'] is False
    assert receipt['requires_exchange_reconciliation'] is True
    assert receipt['execution_error'] == 'TimeoutExpired'
    assert receipt['owner_state_unchanged'] is True
    assert json.loads((runner.ROOT / 'data/report.json').read_text())['ids']['list'] == 'retained'
    assert not (runner.ROOT / 'credentials.env').exists()


def test_delayed_list_visibility_retries_reads_by_acknowledged_id_only():
    queries = []
    def query(**kwargs):
        queries.append(kwargs)
        if len(queries) == 1:
            raise RuntimeError('{"code": -2013}')
        return {'orderListId': 1583, 'listClientOrderId': 'owned', 'symbol': 'LTCUSDT'}
    lists = SimpleNamespace(query_list=query)
    result = probe.query_list_after_ack(lists, {'list': 'owned'}, {'orderListId': 1583}, wait=lambda _: None)
    assert result['orderListId'] == 1583
    assert queries == [{'order_list_id': '1583'}, {'order_list_id': '1583'}]


def test_acknowledged_list_identity_change_is_not_retried():
    queries = []
    def query(**kwargs):
        queries.append(kwargs)
        return {'orderListId': 999, 'listClientOrderId': 'owned', 'symbol': 'LTCUSDT'}
    with pytest.raises(RuntimeError, match='ACKNOWLEDGED_LIST_ID_MISMATCH'):
        probe.query_list_after_ack(SimpleNamespace(query_list=query), {'list': 'owned'}, {'orderListId': 1583}, wait=lambda _: None)
    assert len(queries) == 1
