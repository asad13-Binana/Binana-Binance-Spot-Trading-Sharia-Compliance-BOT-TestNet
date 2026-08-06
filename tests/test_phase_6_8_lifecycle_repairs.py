from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_WORKSPACE = Path.cwd()
_TEMP = tempfile.TemporaryDirectory(prefix='phase-6-8-lifecycle-')
_ROOT = Path(_TEMP.name)
os.environ.setdefault('SHARED_ROOT', str(_ROOT))
os.environ.setdefault('LEGACY_RUNTIME_DIR', str(_ROOT / 'legacy'))
os.environ.setdefault('RUNTIME_DIR', str(_ROOT / 'runtime'))
os.environ.setdefault('AUDIT_LOG', str(_ROOT / 'audit' / 'events.jsonl'))

import _harness  # noqa: E402,F401
from services.common.models import LifecycleState, ProtectionMode  # noqa: E402
from services.execution_sidecar import core_adapter as module  # noqa: E402
from services.execution_sidecar.core_adapter import (  # noqa: E402
    BalanceSnapshotUnavailable, CoreAdapter,
)
from services.execution_sidecar.filters import FilterViolation, SpotFilterValidator  # noqa: E402
from services.execution_sidecar.protection_modes import OrderRequestFactory  # noqa: E402
from services.execution_sidecar.state_store import StateStore  # noqa: E402

os.chdir(_WORKSPACE)


def _sym():
    return module.legacy.Sym(
        symbol='ETHUSDT', base='ETH', quote='USDT', step='0.001', tick='0.01',
        min_notional='1', min_qty='0.001', max_qty='1000',
        trail_min=10, trail_max=2000, oco_allowed=True, oto_allowed=True,
    )


def _position(**overrides):
    values = dict(
        symbol='ETHUSDT', sym=_sym(), entry_order_id=1,
        trade_size_usdt=Decimal('100'), filled_qty=Decimal('1'),
        entry_price=Decimal('100'), state=module.legacy.PosState.ARMED_TRAIL,
    )
    values.update(overrides)
    return module.legacy.Position(**values)


class _Portfolio:
    def __init__(self, position):
        self.positions = {position.symbol: position}
        self.lock = threading.RLock()
        self.autotrade_on = True
        self.protection_halt = ''
        self.halt_reason = ''
        self.save = mock.Mock()
        self.close = mock.Mock()


class _Client:
    def __init__(self, *, balances=None, order_facts=None, list_fact=None, events=None):
        self.balances = list(balances or [])
        self.order_facts = order_facts or {}
        self.list_fact = list_fact
        self.events = events if events is not None else []
        self.sent = []

    def get_account(self):
        self.events.append('account')
        if not self.balances:
            raise TimeoutError('mock account timeout')
        value = self.balances.pop(0)
        if isinstance(value, BaseException):
            raise value
        return {'balances': value}

    def get_order(self, *, symbol, orderId=None, origClientOrderId=None):
        key = orderId if orderId is not None else origClientOrderId
        self.events.append(f'query:{key}')
        value = self.order_facts.get(key)
        if callable(value):
            value = value()
        if isinstance(value, BaseException) or value is None:
            raise value if isinstance(value, BaseException) else TimeoutError('mock order timeout')
        return dict(value)

    def _get(self, endpoint, signed, data):
        self.events.append(f'get:{endpoint}')
        if endpoint == 'orderList':
            if isinstance(self.list_fact, BaseException) or self.list_fact is None:
                raise self.list_fact if isinstance(self.list_fact, BaseException) else TimeoutError(
                    'mock list timeout')
            return dict(self.list_fact)
        if endpoint == 'openOrderList':
            return []
        raise AssertionError(endpoint)

    def order_limit_sell(self, **params):
        self.events.append('send:ioc')
        self.sent.append(params)
        return {'orderId': 99, 'status': 'FILLED', 'executedQty': params['quantity']}


class _Broker:
    def __init__(self, client, events=None):
        self.c = client
        self.events = events if events is not None else client.events
        self._sync_weight = mock.Mock()
        self._check_lot = mock.Mock()

    def cancel(self, symbol, order_id):
        self.events.append(f'cancel:{order_id}')
        return {'orderId': order_id}

    def cancel_order_list(self, symbol, order_list_id):
        self.events.append(f'cancel-list:{order_list_id}')
        return {'orderListId': order_list_id}

    def price(self, symbol):
        return Decimal('100')

    def clamp_delta(self, sym, value):
        return max(sym.trail_min, min(int(value), sym.trail_max))

    def _place_idempotent(self, send, lookup, what):
        return send()

    def _find_order(self, symbol, coid):
        return None


class LifecycleRepairTests(unittest.TestCase):
    def setUp(self):
        self.raw = tempfile.TemporaryDirectory(prefix='lifecycle-case-')
        root = Path(self.raw.name)
        self.store = StateStore(root / 'state.json', root / 'state.sqlite')

    def tearDown(self):
        module.legacy.Broker.free = module._ORIGINAL_BROKER_FREE
        module.legacy.Broker.invalidate_balance_cache = module._ORIGINAL_BROKER_INVALIDATE
        module.legacy.Portfolio._reconcile_position = module._ORIGINAL_RECONCILE_POSITION
        module.legacy.EntryEngine._await_fill = module._ORIGINAL_AWAIT_FILL
        module.legacy.EntryEngine._reprice = module._ORIGINAL_REPRICE
        module.legacy.AutoTrader._bracket_fill = module._ORIGINAL_BRACKET_FILL
        module.legacy.ExitEngine._emergency = module._ORIGINAL_EMERGENCY
        module.legacy.ExitEngine.reprotect_if_naked = module._ORIGINAL_REPROTECT
        self.raw.cleanup()

    def adapter(self, broker, position=None):
        adapter = CoreAdapter.__new__(CoreAdapter)
        adapter.state_store = self.store
        adapter._balance_cache = {}
        adapter.filter_validator = SimpleNamespace(validate_replacement=mock.Mock(
            return_value={'filters_checked': ['TEST']}))
        pf = _Portfolio(position or _position())
        adapter.trader = SimpleNamespace(
            broker=broker, pf=pf, is_running=lambda: True,
        )
        adapter.mirror_positions = mock.Mock(return_value=1)
        return adapter, pf

    def test_only_repaired_emergency_path_exists(self):
        self.assertFalse(hasattr(CoreAdapter, '_emergency_exit_pre_repair'))
        self.assertTrue(callable(CoreAdapter.emergency_exit))

    def test_balance_is_strict_free_locked_total_and_unknown(self):
        client = _Client(balances=[[
            {'asset': 'ETH', 'free': '1.25', 'locked': '0.75'},
        ], TimeoutError('offline')])
        broker = _Broker(client)
        adapter, _pf = self.adapter(broker)
        known = adapter.strict_balance_snapshot('ETH', broker=broker, force=True)
        self.assertEqual(
            (known['status'], known['free'], known['locked'], known['total']),
            ('KNOWN', '1.25', '0.75', '2.00'))
        unknown = adapter.strict_balance_snapshot('ETH', broker=broker, force=True)
        self.assertEqual(unknown['status'], 'UNKNOWN')
        self.assertIsNone(unknown['free'])
        adapter._install_safety_interpositions()
        with self.assertRaises(BalanceSnapshotUnavailable):
            module.legacy.Broker.free(broker, 'ETH')

    def test_emergency_halts_then_cancels_validates_and_verifies_total_dust(self):
        events = []
        client = _Client(
            balances=[
                [{'asset': 'ETH', 'free': '1', 'locked': '0'}],
                [{'asset': 'ETH', 'free': '0', 'locked': '0'}],
            ],
            order_facts={
                1: {'symbol': 'ETHUSDT', 'orderId': 1, 'status': 'FILLED',
                    'executedQty': '1', 'cummulativeQuoteQty': '100'},
                2: {'symbol': 'ETHUSDT', 'orderId': 2, 'status': 'CANCELED',
                    'executedQty': '0'},
                99: {'symbol': 'ETHUSDT', 'orderId': 99, 'status': 'FILLED',
                     'executedQty': '1', 'cummulativeQuoteQty': '99'},
            }, events=events)
        broker = _Broker(client, events)
        position = _position(exit_order_id=2)
        adapter, _pf = self.adapter(broker, position)
        result = adapter.emergency_exit('ETHUSDT')
        self.assertTrue(result['ok'])
        self.assertLess(events.index('cancel:2'), events.index('send:ioc'))
        adapter.filter_validator.validate_replacement.assert_called_once()
        self.assertFalse(self.store.entries())
        self.assertEqual(self.store.safety_halts(), {})
        self.assertEqual(position.state, module.legacy.PosState.CLOSED)

    def test_emergency_cancel_uncertainty_preserves_ids_and_never_submits(self):
        events = []
        client = _Client(
            order_facts={
                1: {'symbol': 'ETHUSDT', 'orderId': 1, 'status': 'FILLED',
                    'executedQty': '1'},
                2: {'symbol': 'ETHUSDT', 'orderId': 2, 'status': 'NEW',
                    'executedQty': '0'},
                3: {'symbol': 'ETHUSDT', 'orderId': 3, 'status': 'NEW',
                    'executedQty': '0'},
                4: {'symbol': 'ETHUSDT', 'orderId': 4, 'status': 'NEW',
                    'executedQty': '0'},
            },
            list_fact={'orderListId': 7, 'listOrderStatus': 'EXECUTING'}, events=events)
        broker = _Broker(client, events)
        position = _position(
            order_list_id=7, tp_order_id=3, sl_order_id=4, exit_order_id=2,
            bracket=True)
        adapter, _pf = self.adapter(broker, position)
        result = adapter.emergency_exit('ETHUSDT')
        self.assertEqual(result['stage'], 'cancel-unverified')
        self.assertNotIn('send:ioc', events)
        self.assertEqual((position.order_list_id, position.tp_order_id,
                          position.sl_order_id, position.exit_order_id), (7, 3, 4, 2))
        self.assertIn('emergency:ETHUSDT', self.store.safety_halts())

    def test_emergency_durable_halt_failure_prevents_any_exchange_action(self):
        client = mock.Mock()
        broker = _Broker(client, [])
        adapter, _pf = self.adapter(broker)
        adapter.state_store.latch_safety = mock.Mock(side_effect=OSError('disk full'))
        result = adapter.emergency_exit('ETHUSDT')
        self.assertEqual(result['stage'], 'halt-persist-failed')
        client.get_account.assert_not_called()
        client.get_order.assert_not_called()

    def test_emergency_double_timeout_returns_structured_unknown_and_stays_halted(self):
        client = _Client(
            balances=[[
                {'asset': 'ETH', 'free': '1', 'locked': '0'},
            ]],
            order_facts={
                1: {'orderId': 1, 'status': 'FILLED', 'executedQty': '1',
                    'cummulativeQuoteQty': '100'},
            })
        broker = _Broker(client)
        broker._place_idempotent = mock.Mock(side_effect=TimeoutError('submit timeout'))
        broker._find_order = mock.Mock(side_effect=TimeoutError('lookup timeout'))
        adapter, _pf = self.adapter(broker)
        result = adapter.emergency_exit('ETHUSDT')
        self.assertFalse(result['ok'])
        self.assertEqual(result['stage'], 'submission-unknown')
        self.assertIn('emergency:ETHUSDT', self.store.safety_halts())
        intent = self.store.data['recovery_intents']['emergency:ETHUSDT']
        self.assertEqual(intent['stage'], 'EXIT_SUBMISSION_UNKNOWN')

    def test_emergency_preexisting_dust_is_reconciled_not_exit_filled(self):
        client = _Client(
            balances=[[
                {'asset': 'ETH', 'free': '0', 'locked': '0'},
            ]],
            order_facts={
                1: {'orderId': 1, 'status': 'FILLED', 'executedQty': '1',
                    'cummulativeQuoteQty': '100'},
            })
        broker = _Broker(client)
        position = _position()
        adapter, _pf = self.adapter(broker, position)
        result = adapter.emergency_exit('ETHUSDT')
        self.assertEqual(result['stage'], 'already-exited')
        self.assertEqual(position._sidecar_lifecycle, LifecycleState.RECONCILED.value)
        self.assertFalse(client.sent)

    def test_partial_entry_timeout_unknown_retains_qty_intent_and_pending(self):
        client = _Client(order_facts={1: TimeoutError('query timeout')})
        broker = _Broker(client)
        position = _position(
            filled_qty=Decimal('0.4'), entry_price=Decimal('100'),
            state=module.legacy.PosState.PENDING_ENTRY)
        adapter, pf = self.adapter(broker, position)
        engine = SimpleNamespace(
            b=broker, pf=pf, ex=SimpleNamespace(),
            _pending={'ETHUSDT': {'p': position, 't0': 0, 'repriced': 0}},
        )
        adapter._await_fill_interposed(engine, 'ETHUSDT')
        self.assertIn('ETHUSDT', engine._pending)
        self.assertEqual(position.filled_qty, Decimal('0.4'))
        pf.close.assert_not_called()
        self.assertIn('entry-timeout:ETHUSDT', self.store.safety_halts())

    def test_late_fill_from_cancel_verification_cannot_be_closed_as_zero(self):
        calls = {'count': 0}

        def entry_fact():
            calls['count'] += 1
            if calls['count'] == 1:
                raise TimeoutError('final query lost')
            return {'symbol': 'ETHUSDT', 'orderId': 1, 'status': 'CANCELED',
                    'executedQty': '0.4', 'cummulativeQuoteQty': '40'}

        client = _Client(order_facts={1: entry_fact})
        broker = _Broker(client)
        position = _position(
            filled_qty=Decimal('0'), entry_price=Decimal('0'),
            state=module.legacy.PosState.PENDING_ENTRY)
        adapter, pf = self.adapter(broker, position)
        adapter._place_verified_rescue_trail = mock.Mock(return_value=False)
        engine = SimpleNamespace(
            b=broker, pf=pf, ex=SimpleNamespace(),
            _pending={'ETHUSDT': {'p': position, 't0': 0, 'repriced': 0}},
        )
        adapter._await_fill_interposed(engine, 'ETHUSDT')
        self.assertEqual(position.filled_qty, Decimal('0.4'))
        self.assertEqual(position.entry_price, Decimal('100'))
        pf.close.assert_not_called()
        adapter._place_verified_rescue_trail.assert_called_once()
        self.assertIn('ETHUSDT', engine._pending)

    def test_partial_otoco_cancel_unknown_preserves_handles_and_no_replacement(self):
        client = _Client(
            order_facts={
                1: {'orderId': 1, 'status': 'PARTIALLY_FILLED', 'executedQty': '0.4'},
                3: {'orderId': 3, 'status': 'NEW', 'executedQty': '0'},
                4: {'orderId': 4, 'status': 'NEW', 'executedQty': '0'},
            }, list_fact={'orderListId': 7, 'listOrderStatus': 'EXECUTING'})
        broker = _Broker(client)
        position = _position(
            filled_qty=Decimal('0.4'), order_list_id=7, tp_order_id=3,
            sl_order_id=4, bracket=True, state=module.legacy.PosState.PENDING_ENTRY)
        adapter, pf = self.adapter(broker, position)
        adapter._place_verified_rescue_trail = mock.Mock()
        engine = SimpleNamespace(b=broker, pf=pf, ex=SimpleNamespace())
        adapter._reprice_interposed(engine, position)
        self.assertEqual((position.order_list_id, position.tp_order_id,
                          position.sl_order_id), (7, 3, 4))
        adapter._place_verified_rescue_trail.assert_not_called()
        self.assertIn('cancel-reprice:ETHUSDT', self.store.safety_halts())

    def test_terminal_zero_fill_bracket_does_not_close_until_list_is_verified(self):
        client = _Client(
            order_facts={
                1: {'orderId': 1, 'status': 'CANCELED', 'executedQty': '0'},
                3: {'orderId': 3, 'status': 'NEW', 'executedQty': '0'},
                4: {'orderId': 4, 'status': 'NEW', 'executedQty': '0'},
            }, list_fact={'orderListId': 7, 'listOrderStatus': 'EXECUTING'})
        broker = _Broker(client)
        position = _position(
            filled_qty=Decimal('0'), entry_price=Decimal('0'), order_list_id=7,
            tp_order_id=3, sl_order_id=4, bracket=True,
            state=module.legacy.PosState.PENDING_ENTRY)
        adapter, pf = self.adapter(broker, position)
        engine = SimpleNamespace(
            b=broker, pf=pf, ex=SimpleNamespace(),
            _pending={'ETHUSDT': {'p': position, 't0': time.time(), 'repriced': 0}},
        )
        adapter._await_fill_interposed(engine, 'ETHUSDT')
        pf.close.assert_not_called()
        self.assertIn('ETHUSDT', engine._pending)
        self.assertEqual(position.order_list_id, 7)

    def test_reprotect_query_unknown_never_places_against_free_balance(self):
        client = _Client(
            balances=[[
                {'asset': 'ETH', 'free': '1', 'locked': '0'},
            ]], order_facts={2: TimeoutError('status unknown')})
        broker = _Broker(client)
        position = _position(exit_order_id=2, replacing_protection=True)
        adapter, pf = self.adapter(broker, position)
        adapter._place_verified_rescue_trail = mock.Mock()
        adapter._reprotect_interposed(
            SimpleNamespace(b=broker, pf=pf), position, Decimal('100'))
        adapter._place_verified_rescue_trail.assert_not_called()
        self.assertIn('reprotect:ETHUSDT', self.store.safety_halts())

    def test_relatch_does_not_erase_deterministic_recovery_identifiers(self):
        self.store.latch_safety(
            'reprotect:ETHUSDT', 'first', symbol='ETHUSDT', kind='reprotect')
        self.store.update_recovery_intent(
            'reprotect:ETHUSDT', client_order_id='FORTRESS_ABC', order_id=77,
            stage='ACCEPTED')
        self.store.latch_safety(
            'reprotect:ETHUSDT', 'still uncertain', symbol='ETHUSDT',
            kind='reconciliation')
        intent = self.store.data['recovery_intents']['reprotect:ETHUSDT']
        self.assertEqual(intent['client_order_id'], 'FORTRESS_ABC')
        self.assertEqual(intent['order_id'], 77)
        self.assertEqual(intent['stage'], 'ACCEPTED')

    def test_all_done_requires_filled_leg_for_runtime_close(self):
        client = _Client(
            balances=[[
                {'asset': 'ETH', 'free': '0', 'locked': '0'},
            ], [
                {'asset': 'ETH', 'free': '0', 'locked': '0'},
            ]],
            order_facts={
                3: {'orderId': 3, 'status': 'CANCELED', 'executedQty': '0'},
                4: {'orderId': 4, 'status': 'CANCELED', 'executedQty': '0'},
            }, list_fact={'orderListId': 7, 'listOrderStatus': 'ALL_DONE'})
        broker = _Broker(client)
        position = _position(order_list_id=7, tp_order_id=3, sl_order_id=4, bracket=True)
        adapter, pf = self.adapter(broker, position)
        with self.assertRaises(BalanceSnapshotUnavailable):
            adapter._bracket_fill_interposed(SimpleNamespace(broker=broker), 'ETHUSDT', position)
        pf.close.assert_not_called()

        position.replacing_protection = True
        engine = SimpleNamespace(b=broker, pf=pf)
        adapter._reprotect_interposed(engine, position, Decimal('100'))
        self.assertEqual(position._sidecar_lifecycle, LifecycleState.RECONCILED.value)
        self.assertNotEqual(position._sidecar_lifecycle, LifecycleState.EXIT_FILLED.value)

    def test_offline_known_entry_and_exit_query_failures_latch_without_delegating(self):
        for field in ('entry', 'exit'):
            with self.subTest(field=field):
                client = _Client(order_facts={1: TimeoutError('unknown'), 2: TimeoutError('unknown')})
                broker = _Broker(client)
                position = _position(
                    entry_order_id=1,
                    exit_order_id=2 if field == 'exit' else None,
                    filled_qty=Decimal('1') if field == 'exit' else Decimal('0'),
                    state=(module.legacy.PosState.ARMED_TRAIL if field == 'exit'
                           else module.legacy.PosState.PENDING_ENTRY))
                adapter, _pf = self.adapter(broker, position)
                portfolio = SimpleNamespace(b=broker)
                with mock.patch.object(module, '_ORIGINAL_RECONCILE_POSITION') as original:
                    self.assertTrue(adapter._reconcile_position_interposed(portfolio, position))
                original.assert_not_called()
                self.assertEqual(position.entry_order_id, 1)
                if field == 'exit':
                    self.assertEqual(position.exit_order_id, 2)
                self.assertIn('offline-fill:ETHUSDT', self.store.safety_halts())

    def test_offline_fill_without_protection_is_explicitly_latched(self):
        client = _Client(order_facts={
            1: {'orderId': 1, 'status': 'FILLED', 'executedQty': '1',
                'cummulativeQuoteQty': '100'},
        })
        broker = _Broker(client)
        position = _position(
            filled_qty=Decimal('0'), entry_price=Decimal('0'),
            state=module.legacy.PosState.PENDING_ENTRY)
        adapter, _pf = self.adapter(broker, position)

        def adopted(_portfolio, value):
            value.filled_qty = Decimal('1')
            value.entry_price = Decimal('100')
            return True

        with mock.patch.object(module, '_ORIGINAL_RECONCILE_POSITION', side_effect=adopted):
            self.assertTrue(adapter._reconcile_position_interposed(SimpleNamespace(b=broker), position))
        self.assertTrue(position.replacing_protection)
        self.assertEqual(position._sidecar_lifecycle, LifecycleState.REPROTECT_REQUIRED.value)
        self.assertIn('offline-fill:ETHUSDT', self.store.safety_halts())

    def test_restart_reconciles_verified_filled_single_exit_with_total_dust(self):
        client = _Client(
            balances=[[
                {'asset': 'ETH', 'free': '0', 'locked': '0'},
            ]],
            order_facts={
                2: {'orderId': 2, 'status': 'FILLED', 'executedQty': '1',
                    'cummulativeQuoteQty': '105'},
            })
        broker = _Broker(client)
        position = _position(exit_order_id=2, bracket=False)
        adapter, pf = self.adapter(broker, position)
        self.store.latch_safety(
            'offline-fill:ETHUSDT', 'restart check', symbol='ETHUSDT',
            kind='reconciliation')
        issues = adapter._service_reconcile_positions()
        self.assertEqual(issues, [])
        pf.close.assert_called_once_with('ETHUSDT', Decimal('105'))
        self.assertEqual(position._sidecar_lifecycle, LifecycleState.EXIT_FILLED.value)
        self.assertEqual(self.store.safety_halts(), {})
        self.assertFalse(self.store.entries())

    def test_later_positive_reconcile_resolves_emergency_unknown_but_not_entries(self):
        client = _Client(
            balances=[[
                {'asset': 'ETH', 'free': '0', 'locked': '0'},
            ]],
            order_facts={
                'FORTRESS_EMERGENCY': {
                    'orderId': 99, 'status': 'FILLED', 'executedQty': '1',
                    'cummulativeQuoteQty': '99'},
            })
        broker = _Broker(client)
        position = _position(exit_order_id=None, bracket=False,
                             state=module.legacy.PosState.EMERGENCY)
        adapter, pf = self.adapter(broker, position)
        self.store.latch_safety(
            'emergency:ETHUSDT', 'submission unknown', symbol='ETHUSDT',
            kind='emergency')
        self.store.update_recovery_intent(
            'emergency:ETHUSDT', client_order_id='FORTRESS_EMERGENCY')
        issues = adapter._service_reconcile_positions()
        self.assertEqual(issues, [])
        pf.close.assert_called_once_with('ETHUSDT', Decimal('99'))
        self.assertEqual(self.store.safety_halts(), {})
        self.assertFalse(self.store.entries(), 'positive recovery still requires owner resume')

    def test_expiry_fields_are_journaled_as_unsafe_nonterminal(self):
        self.store.upsert_trade(
            'trade-1', 'ETH/USDT', lifecycle_state=LifecycleState.ENTRY_SUBMITTED.value,
            entry_order_id=1)
        event = {
            'e': 'executionReport', 's': 'ETHUSDT', 'i': 1, 'I': 22, 'E': 33,
            'S': 'BUY', 'X': 'EXPIRED_IN_MATCH', 'x': 'EXPIRED',
            'z': '0.4', 'Z': '40', 'q': '1', 'expiryReason': 'STP',
        }
        self.assertTrue(self.store.record_exchange_event(event))
        with self.store._connect() as con:
            row = con.execute(
                'SELECT lifecycle_state,reconciliation_status FROM trade_records WHERE trade_id=?',
                ('trade-1',)).fetchone()
            payload = con.execute('SELECT payload_json FROM exchange_events').fetchone()[0]
        self.assertEqual(row['lifecycle_state'], LifecycleState.ENTRY_PARTIALLY_FILLED.value)
        self.assertIn('UNSAFE_EXPIRY_RECONCILE_REQUIRED', row['reconciliation_status'])
        self.assertIn('"expiryReason": "STP"', payload)
        self.store.latch_safety('expiry:ETHUSDT', 'unsafe expiry', symbol='ETHUSDT')
        with self.assertRaisesRegex(RuntimeError, 'fail-closed'):
            self.store.set_entries(True)

    def test_default_ocot_entry_strict_filter_failure_blocks_legacy_submission(self):
        original_submission = mock.Mock(return_value={'orderListId': 7})
        broker = SimpleNamespace(
            place_otoco=original_submission,
            limit_buy=mock.Mock(),
            _validate_otoco_filters=mock.Mock(), c=SimpleNamespace(),
            _sync_weight=mock.Mock(),
        )
        adapter = CoreAdapter.__new__(CoreAdapter)
        adapter.mode_getter = lambda: ProtectionMode.OCO_TRAILING.value
        adapter.factory = OrderRequestFactory(module.legacy)
        adapter.filter_validator = SimpleNamespace(
            validate_replacement=mock.Mock(side_effect=FilterViolation('capacity exhausted')))
        adapter._balance_cache = {}
        adapter._patched = False
        adapter.state_store = self.store
        adapter.trader = SimpleNamespace(start=lambda: True, broker=broker, pf=None)
        adapter.mirror_positions = mock.Mock()
        adapter._configure_environment = mock.Mock()
        adapter.start()
        with self.assertRaisesRegex(FilterViolation, 'capacity exhausted'):
            broker.place_otoco(
                _sym(), Decimal('1'), Decimal('100'), Decimal('110'), 100)
        original_submission.assert_not_called()
        adapter.filter_validator.validate_replacement.assert_called_once()

    def test_default_ocot_request_counts_all_three_legs_against_capacity(self):
        class Public:
            NO_REFERENCE_PRICE = object()

            def exchange_info(self, symbol):
                return {'symbols': [{
                    'symbol': symbol, 'status': 'TRADING',
                    'isSpotTradingAllowed': True, 'baseAsset': 'ETH',
                    'quoteAsset': 'USDT',
                    'filters': [
                        {'filterType': 'PRICE_FILTER', 'tickSize': '0.01',
                         'minPrice': '0.01', 'maxPrice': '100000'},
                        {'filterType': 'LOT_SIZE', 'stepSize': '0.001',
                         'minQty': '0.001', 'maxQty': '1000'},
                        {'filterType': 'NOTIONAL', 'minNotional': '1',
                         'maxNotional': '0', 'applyMinToMarket': False,
                         'applyMaxToMarket': False},
                        {'filterType': 'TRAILING_DELTA',
                         'minTrailingBelowDelta': 10, 'maxTrailingBelowDelta': 2000,
                         'minTrailingAboveDelta': 10, 'maxTrailingAboveDelta': 2000},
                        {'filterType': 'MAX_NUM_ORDERS', 'maxNumOrders': 2},
                        {'filterType': 'MAX_NUM_ORDER_LISTS', 'maxNumOrderLists': 10},
                    ],
                }]}

            def ticker_price(self, _symbol):
                return {'price': '100'}

        endpoint, params = OrderRequestFactory(module.legacy).entry(
            ProtectionMode.OCO_TRAILING, _sym(), Decimal('1'),
            Decimal('100'), Decimal('110'), 100)
        validator = SpotFilterValidator(public_client=Public(), max_age_seconds=300)
        with self.assertRaisesRegex(FilterViolation, 'MAX_NUM_ORDERS'):
            validator.validate_replacement(
                'ETHUSDT', endpoint, params,
                open_orders_provider=lambda _symbol: [],
                open_order_lists_provider=lambda: [])


if __name__ == '__main__':
    unittest.main()
