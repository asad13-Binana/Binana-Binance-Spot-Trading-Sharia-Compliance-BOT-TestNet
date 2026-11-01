"""Offline reproductions from the AWS handoff; no exchange credentials/orders."""
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests import _harness
from services.execution_sidecar import order_manager as om
from services.execution_sidecar.risk_checks import FreshSignalGuard
from services.execution_sidecar.simulation_adapter import SimulationAdapter
from services.execution_sidecar.state_store import StateStore


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    monkeypatch.setenv('SHARIA_SIGNAL_GATE_MODE', 'cached')
    monkeypatch.setattr(om, 'load_package_mode', lambda: 'testnet')
    store = StateStore(tmp_path / 'state.json', tmp_path / 'state.sqlite')
    store.data['simulation'] = True
    store.set_entries(True)
    guard = FreshSignalGuard(tmp_path / 'risk.json', state_store=store)
    fault = tmp_path / 'fault.json'
    adapter = SimulationAdapter(store, guard, fault_path=fault)
    adapter.start()
    sharia = _harness.write_attested_status(tmp_path / 'sharia.json', [('ETH', 'GREEN')])
    manager = om.OrderManager(adapter, store, guard, store, sharia,
                              {name: tmp_path / name for name in ('processed', 'rejected')})
    now = datetime.now(timezone.utc).isoformat()
    payload = dict(signal_id='sig-recovery', pair='ETH/USDT', symbol='ETHUSDT',
                   candle_time=now, generated_at=now, strategy='IctSmcStrategy',
                   universe_hash='h', sharia_status='GREEN')
    signal = tmp_path / 'signal.json'
    signal.write_text(json.dumps(_harness.sign_signal(payload)), encoding='utf-8')
    return manager, store, adapter, signal, fault


def trade(store):
    with store._connect() as con:
        return dict(con.execute('SELECT * FROM trade_records').fetchone())


@pytest.mark.parametrize('fault', ['none', 'partial_fill'])
def test_submission_ack_preserves_unverified_protection(scenario, fault):
    manager, store, adapter, signal, plan = scenario
    plan.write_text(json.dumps({'queue': [fault]}))
    assert manager.process_signal(signal, {'ETH/USDT'}, 'h')[0] is True
    # Raw simulator stream events are exposure evidence, not authenticated
    # coverage proof. The submission ACK must retain pending reconciliation.
    row = trade(store)
    assert row['lifecycle_state'] == 'RECONCILIATION_REQUIRED'
    assert row['reconciliation_status'] == 'PROTECTION_COVERAGE_PENDING_RECONCILE_REQUIRED'
    assert row['protected_quantity'] == ('0.5' if fault == 'partial_fill' else '1')
    assert row['filled_quantity'] == row['protected_quantity']
    assert store._active_trade_id('ETH/USDT') == row['trade_id']
    store.finalize_submission(row['trade_id'], 'ACCEPTED')
    assert trade(store)['lifecycle_state'] == 'RECONCILIATION_REQUIRED'


def test_unknown_submission_survives_restart_and_cannot_rearm(scenario):
    manager, store, adapter, signal, plan = scenario
    plan.write_text(json.dumps({'queue': ['timeout']}))
    assert manager.process_signal(signal, {'ETH/USDT'}, 'h')[0] is False
    assert not store.entries()
    assert store.safety_halts()
    assert 'UNKNOWN' in store.signal_result('sig-recovery')
    assert trade(store)['lifecycle_state'] == 'RECONCILIATION_REQUIRED'
    restored = StateStore(store.path, store.db_path)
    assert restored.safety_halts() == store.safety_halts()
    with pytest.raises(RuntimeError):
        restored.set_entries(True)
    restarted = SimulationAdapter(restored)
    restarted.start()
    assert restarted.verified_reconcile()['ok'] is False
    with pytest.raises(RuntimeError):
        restarted.set_enabled(True)


def test_known_exchange_rejection_is_not_ambiguous(scenario):
    manager, store, adapter, signal, plan = scenario
    plan.write_text(json.dumps({'queue': ['reject']}))
    assert manager.process_signal(signal, {'ETH/USDT'}, 'h')[0] is False
    assert adapter.last_submission_outcome == 'REJECTED'
    assert 'UNKNOWN' not in store.signal_result('sig-recovery')
    # A known transport rejection does not authenticate flat exposure; raw
    # REJECTED events must retain the slot until the zero-fill proof completes.
    row = trade(store)
    assert row['lifecycle_state'] == 'RECONCILIATION_REQUIRED'
    assert row['reconciliation_status'] == 'ENTRY_TERMINAL_NO_FILL_RECONCILE_REQUIRED'
    assert store._active_trade_id('ETH/USDT') == row['trade_id']
    assert not store.safety_halts()


@pytest.mark.parametrize('verified_coverage', [False, True])
def test_concurrent_ack_preserves_coverage_gate(tmp_path, verified_coverage):
    store = StateStore(tmp_path / 'state.json', tmp_path / 'state.sqlite')
    store.upsert_trade('t1', 'ETH/USDT', lifecycle_state='SIGNAL_APPROVED',
                       entry_order_id=1, stop_order_id=2, filled_quantity='1')
    if verified_coverage:
        # Fixture at the durable-proof boundary. Adapter/exchange authentication
        # itself is covered by test_entry_reprice_late_fills, not bypassed here.
        store.save_protection_proof('coverage:ETHUSDT:1', {
            'verified_at': datetime.now(timezone.utc).timestamp(),
            'entry_filled_quantity': '1', 'protected_quantity': '1', 'orders': {'2': {}}},
            symbol='ETHUSDT', event_cursor=0)
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(store.finalize_submission, 't1', 'ACCEPTED')
        b = pool.submit(store.upsert_trade, 't1', 'ETH/USDT', lifecycle_state='PROTECTION_ACTIVE')
        a.result(); b.result()
    expected = 'PROTECTION_ACTIVE' if verified_coverage else 'RECONCILIATION_REQUIRED'
    assert trade(store)['lifecycle_state'] == expected
    assert store._active_trade_id('ETH/USDT') == 't1'
    if not verified_coverage:
        assert trade(store)['reconciliation_status'] == 'PROTECTION_COVERAGE_PENDING_RECONCILE_REQUIRED'


def test_shutdown_does_not_require_simulated_portfolio(scenario):
    from services.execution_sidecar.main import shutdown_adapter
    _, store, adapter, _, _ = scenario
    assert shutdown_adapter(adapter, store) is True
    assert not store.entries()


def test_shutdown_still_saves_portfolio_after_disarm_failure():
    from services.execution_sidecar.main import shutdown_adapter
    pf = SimpleNamespace(save=Mock())
    adapter = SimpleNamespace(trader=SimpleNamespace(pf=pf, is_running=lambda: True),
                              set_enabled=Mock(return_value='OFF'))
    store = SimpleNamespace(data={'simulation': False}, set_entries=Mock(side_effect=OSError('full')))
    assert shutdown_adapter(adapter, store) is False
    pf.save.assert_called_once()
    adapter.set_enabled.assert_called_once_with(False)


def test_unknown_exception_latches_even_without_an_adapter_status(scenario, monkeypatch):
    manager, store, adapter, signal, _ = scenario
    monkeypatch.setattr(adapter, 'submit', Mock(side_effect=TimeoutError()))
    assert manager.process_signal(signal, {'ETH/USDT'}, 'h')[0] is False
    assert store.safety_halts()
    assert 'UNKNOWN' in store.signal_result('sig-recovery')


def test_failed_submission_commit_cannot_be_acknowledged(scenario, monkeypatch):
    manager, store, adapter, signal, _ = scenario
    original = store.finalize_submission
    def fail(trade_id, outcome):
        # Fail only the completion write, after the initial durable claim.
        with monkeypatch.context() as patch:
            patch.setattr(store, '_connect', Mock(side_effect=sqlite3.OperationalError('disk full')))
            original(trade_id, outcome)
    monkeypatch.setattr(store, 'finalize_submission', fail)
    with pytest.raises(sqlite3.OperationalError):
        manager.process_signal(signal, {'ETH/USDT'}, 'h')
    assert store.signal_result('sig-recovery') == 'IN_PROGRESS'
    assert not store.entries()
    assert signal.exists()


def test_replayed_unknown_signal_cannot_submit_again(scenario, monkeypatch):
    manager, store, adapter, signal, plan = scenario
    original_signal = signal.read_bytes()
    plan.write_text(json.dumps({'queue':['timeout']}))
    manager.process_signal(signal, {'ETH/USDT'}, 'h')
    signal.write_bytes(original_signal)
    submit = Mock(side_effect=AssertionError('must never submit twice'))
    monkeypatch.setattr(adapter,'submit',submit)
    assert manager.process_signal(signal, {'ETH/USDT'}, 'h')[0] is False
    submit.assert_not_called()


def test_core_adapter_transport_witness_does_not_parse_error_messages():
    from services.execution_sidecar.core_adapter import CoreAdapter
    adapter = object.__new__(CoreAdapter)
    with pytest.raises(TimeoutError):
        adapter._entry_transport(Mock(side_effect=TimeoutError('arbitrary text')))
    assert adapter.last_submission_outcome == 'UNKNOWN'
    assert adapter._entry_transport(lambda: {'orderId': 7}) == {'orderId': 7}
    assert adapter.last_submission_outcome == 'ACCEPTED'
    adapter._patched = True
    adapter.trader = SimpleNamespace(submit_signal=lambda *a:(False,'local gate'))
    assert adapter.submit('ETHUSDT')[0] is False
    assert adapter.last_submission_outcome == 'REJECTED'


def test_simulated_restart_cannot_forget_open_position_or_reuse_id(scenario):
    manager, store, adapter, signal, _ = scenario
    assert manager.process_signal(signal, {'ETH/USDT'}, 'h')[0]
    restarted = SimulationAdapter(StateStore(store.path,store.db_path))
    restarted.start()
    assert restarted._counter >= adapter._counter
    assert not restarted.state_store.entries()
    assert restarted.verified_reconcile()['ok'] is False
