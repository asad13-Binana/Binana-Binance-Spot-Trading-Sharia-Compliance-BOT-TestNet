from __future__ import annotations

import json
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
_TEMP = tempfile.TemporaryDirectory(prefix='codex-j-hardening-')
_ROOT = Path(_TEMP.name)
os.environ.setdefault('SHARED_ROOT', str(_ROOT))
os.environ.setdefault('LEGACY_RUNTIME_DIR', str(_ROOT / 'legacy'))
os.environ.setdefault('RUNTIME_DIR', str(_ROOT / 'runtime'))
os.environ.setdefault('AUDIT_LOG', str(_ROOT / 'audit' / 'events.jsonl'))

import _harness  # noqa: E402
from services.common.models import LifecycleState  # noqa: E402
from services.common.sharia_v19 import ResultValidationError, validate_result  # noqa: E402
from services.execution_sidecar import core_adapter as core_module  # noqa: E402
from services.execution_sidecar import main as sidecar_main  # noqa: E402
from services.execution_sidecar.core_adapter import CoreAdapter  # noqa: E402
from services.execution_sidecar.filters import (  # noqa: E402
    FilterDataUnavailable, FilterViolation, SpotFilterValidator,
)
from services.execution_sidecar.state_store import StateStore  # noqa: E402
from services.telegram_broker import bot as telegram_bot  # noqa: E402
from services.universe_service.snapshot_store import (  # noqa: E402
    UniverseSnapshotError, load_current, store,
)

os.chdir(_WORKSPACE)


def _symbol():
    return core_module.legacy.Sym(
        symbol='ETHUSDT', base='ETH', quote='USDT', step='0.001', tick='0.01',
        min_notional='1', min_qty='0.001', max_qty='1000',
        trail_min=10, trail_max=2000, oco_allowed=True, oto_allowed=True,
    )


def _position():
    return core_module.legacy.Position(
        symbol='ETHUSDT', sym=_symbol(), entry_order_id=1,
        trade_size_usdt=Decimal('100'), filled_qty=Decimal('1'),
        entry_price=Decimal('100'), state=core_module.legacy.PosState.ARMED_TRAIL,
    )


def _filters(*, percent: dict | None = None, trailing: dict | None = None,
             capacity: dict | None = None) -> list[dict]:
    rows = [
        {'filterType': 'PRICE_FILTER', 'tickSize': '0.01',
         'minPrice': '0.01', 'maxPrice': '100000'},
        {'filterType': 'LOT_SIZE', 'stepSize': '0.001',
         'minQty': '0.001', 'maxQty': '1000'},
        {'filterType': 'MARKET_LOT_SIZE', 'stepSize': '0',
         'minQty': '0', 'maxQty': '1000'},
        {'filterType': 'NOTIONAL', 'minNotional': '1', 'maxNotional': '1000000',
         'applyMinToMarket': False, 'applyMaxToMarket': False},
    ]
    if percent is not None:
        rows.append(dict({'filterType': 'PERCENT_PRICE_BY_SIDE'}, **percent))
    if trailing is not None:
        rows.append(dict({'filterType': 'TRAILING_DELTA'}, **trailing))
    if capacity is not None:
        rows.append(capacity)
    return rows


class _Public:
    NO_REFERENCE_PRICE = object()

    def __init__(self, filters, *, last='100', reference='100', entry_overrides=None):
        self.filters = filters
        self.last = last
        self.reference = reference
        self.entry_overrides = entry_overrides or {}

    def exchange_info(self, symbol):
        entry = {
            'symbol': symbol, 'status': 'TRADING', 'isSpotTradingAllowed': True,
            'baseAsset': 'ETH', 'quoteAsset': 'USDT', 'filters': self.filters,
        }
        entry.update(self.entry_overrides)
        return {'symbols': [entry]}

    def reference_price(self, _symbol):
        return self.reference

    def ticker_price(self, _symbol):
        return {'price': self.last}

    def get(self, _path, _params=None):
        return {'price': self.last}


class UniversePointerTests(unittest.TestCase):
    def test_full_snapshot_is_reopened_rehashed_and_pointer_bound(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            published = store(
                root, [{'pair': 'ETH/USDT', 'rank': 1}], {'limit': 1}, 900)
            loaded = load_current(root / 'current_pairlist.json')
            self.assertEqual(loaded['snapshot_hash'], published['snapshot_hash'])
            pointer_path = root / 'current_pairlist.json'
            pointer = json.loads(pointer_path.read_text())
            pointer['pairs'] = ['SOL/USDT']
            pointer_path.write_text(json.dumps(pointer))
            with self.assertRaisesRegex(UniverseSnapshotError, 'pointer/full pairs'):
                load_current(pointer_path)

    def test_tampered_full_configuration_and_unsafe_filename_fail_closed(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            store(root, [{'pair': 'ETH/USDT', 'rank': 1}], {'limit': 1}, 900)
            pointer_path = root / 'current_pairlist.json'
            pointer = json.loads(pointer_path.read_text())
            full_path = root / 'snapshots' / pointer['snapshot_file']
            full = json.loads(full_path.read_text())
            full['configuration']['limit'] = 2
            full_path.write_text(json.dumps(full))
            with self.assertRaisesRegex(
                    UniverseSnapshotError, 'selection state|configuration hash'):
                load_current(pointer_path)
            pointer['snapshot_file'] = '../outside.json'
            pointer_path.write_text(json.dumps(pointer))
            with self.assertRaisesRegex(UniverseSnapshotError, 'unsafe'):
                load_current(pointer_path)

    def test_retention_never_deletes_the_pointed_snapshot(self):
        with tempfile.TemporaryDirectory() as raw, mock.patch.dict(
                os.environ, {'UNIVERSE_SNAPSHOT_MAX_FILES': '1'}):
            root = Path(raw)
            store(root, [{'pair': 'ETH/USDT', 'rank': 1}], {'limit': 1}, 900)
            store(root, [{'pair': 'SOL/USDT', 'rank': 1}], {'limit': 1}, 900)
            pointer = json.loads((root / 'current_pairlist.json').read_text())
            history = list((root / 'snapshots').glob('universe_*.json'))
            self.assertEqual([path.name for path in history], [pointer['snapshot_file']])
            self.assertEqual(load_current(root / 'current_pairlist.json')['pairs'], ['SOL/USDT'])

    def test_sidecar_uses_the_validated_full_snapshot(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            published = store(
                root, [{'pair': 'ETH/USDT', 'rank': 1}], {'limit': 1}, 900)
            with mock.patch.object(sidecar_main, 'UNIVERSE_CURRENT',
                                   root / 'current_pairlist.json'):
                pairs, digest, fresh = sidecar_main.universe_state()
            self.assertEqual((pairs, digest, fresh),
                             ({'ETH/USDT'}, published['snapshot_hash'], True))


class ShariaProvenanceAndTelegramTests(unittest.TestCase):
    def test_search_result_url_alone_is_not_opened_evidence(self):
        report = _harness.green_report('ETH')
        report['tool_evidence']['url_citations'] = []
        with self.assertRaisesRegex(ResultValidationError, 'citation/open-page evidence'):
            validate_result(report, expected_base='ETH')

    def test_haram_proof_url_must_be_provider_evidence_bound(self):
        report = _harness.green_report('ETH')
        report.update({
            'final_code': 'HARAM', 'direct_result': 'HARAM',
            'haram_narrative_code': 'N1',
            'haram_narrative_name': 'RIBA_LENDING',
            'haram_proof_card': {
                'C1': True, 'C2': True, 'C3': True, 'C4': True, 'C5': True,
                'quote': ('The official protocol explicitly charges guaranteed interest '
                          'on every lending balance through its core contract.'),
                'url': 'https://example.org/whitepaper',
                'tier': 'TIER_1_OFFICIAL',
            },
        })
        self.assertEqual(validate_result(report, expected_base='ETH'), report)
        report['tool_evidence']['url_citations'] = []
        with self.assertRaisesRegex(ResultValidationError, 'HARAM proof URL lacks'):
            validate_result(report, expected_base='ETH')

    def test_telegram_never_labels_a_forged_record_eligible(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            status = root / 'sharia' / 'sharia_status.json'
            _harness.write_attested_status(status, [('ETH', 'GREEN')])
            runtime = root / 'runtime'
            runtime.mkdir()
            (runtime / 'health.json').write_text(json.dumps({
                'ok': True, 'ready_for_screening': True, 'ts': time.time(),
                'queue': {}, 'completed_today': 0, 'cost_events_today': 0,
                'daily_quota': 10, 'api': {'available': True, 'reason': ''},
            }))
            with mock.patch.object(telegram_bot, 'SHARIA_FILE', status), \
                    mock.patch.object(telegram_bot, 'SHARIA_RUNTIME_DIR', runtime):
                text = telegram_bot._sharia_service_status()
                self.assertIn('trade-eligible now: ETH', text)
                forged = json.loads(status.read_text())
                forged['records'][0]['status'] = 'HARAM'
                forged['records'][0]['final_code'] = 'HARAM'
                status.write_text(json.dumps(forged))
                text = telegram_bot._sharia_service_status()
            self.assertIn('trade-eligible now: NONE (fail-closed)', text)
            self.assertNotIn('trade-eligible now: ETH', text)

    def test_telegram_health_requires_ok_and_ready_flags(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            status = root / 'sharia' / 'sharia_status.json'
            _harness.write_attested_status(status, [('ETH', 'GREEN')])
            runtime = root / 'runtime'; runtime.mkdir()
            (runtime / 'health.json').write_text(json.dumps({
                'ok': True, 'ready_for_screening': False, 'ts': time.time(),
                'queue': {}, 'api': {},
            }))
            with mock.patch.object(telegram_bot, 'SHARIA_FILE', status), \
                    mock.patch.object(telegram_bot, 'SHARIA_RUNTIME_DIR', runtime):
                text = telegram_bot._sharia_service_status()
            self.assertIn('health: NOT READY', text)


class FilterSchemaTests(unittest.TestCase):
    @staticmethod
    def _limit_params():
        return {'symbol': 'ETHUSDT', 'side': 'SELL', 'type': 'LIMIT',
                'quantity': '1', 'price': '100.00'}

    def test_duplicate_filters_and_symbol_identity_mismatch_fail_closed(self):
        duplicate = _filters()
        duplicate.append(dict(duplicate[0]))
        validator = SpotFilterValidator(_Public(duplicate), max_age_seconds=300)
        with self.assertRaisesRegex(FilterDataUnavailable, 'duplicate PRICE_FILTER'):
            validator.validate_replacement('ETHUSDT', 'order', self._limit_params())

        validator = SpotFilterValidator(
            _Public(_filters(), entry_overrides={'baseAsset': 'ETC'}),
            max_age_seconds=300)
        with self.assertRaisesRegex(FilterDataUnavailable, 'identity binding'):
            validator.validate_replacement('ETHUSDT', 'order', self._limit_params())

    def test_spot_boolean_market_flags_and_percent_ranges_are_strict(self):
        validator = SpotFilterValidator(
            _Public(_filters(), entry_overrides={'isSpotTradingAllowed': 'true'}),
            max_age_seconds=300)
        with self.assertRaisesRegex(FilterViolation, 'spot trading'):
            validator.validate_replacement('ETHUSDT', 'order', self._limit_params())

        rows = _filters()
        rows[3]['applyMinToMarket'] = 'false'
        validator = SpotFilterValidator(_Public(rows), max_age_seconds=300)
        with self.assertRaisesRegex(FilterDataUnavailable, 'JSON boolean'):
            validator.validate_replacement(
                'ETHUSDT', 'order',
                {'symbol': 'ETHUSDT', 'side': 'SELL', 'type': 'MARKET',
                 'quantity': '1'})

        rows = _filters(percent={
            'askMultiplierDown': '2', 'askMultiplierUp': '1', 'avgPriceMins': 0,
        })
        validator = SpotFilterValidator(_Public(rows), max_age_seconds=300)
        with self.assertRaisesRegex(FilterDataUnavailable, 'range is inverted'):
            validator.validate_replacement('ETHUSDT', 'order', self._limit_params())

    def test_trailing_and_oco_relationships_use_strict_fresh_values(self):
        rows = _filters(trailing={
            'minTrailingAboveDelta': 10, 'maxTrailingAboveDelta': 2000,
            'minTrailingBelowDelta': 200, 'maxTrailingBelowDelta': 100,
        })
        validator = SpotFilterValidator(_Public(rows), max_age_seconds=300)
        with self.assertRaisesRegex(FilterDataUnavailable, 'range is inverted'):
            validator.validate_replacement(
                'ETHUSDT', 'order',
                {'symbol': 'ETHUSDT', 'side': 'SELL', 'type': 'STOP_LOSS_LIMIT',
                 'quantity': '1', 'price': '95.00', 'trailingDelta': 100})

        validator = SpotFilterValidator(_Public(_filters(), last='100'), max_age_seconds=300)
        with self.assertRaisesRegex(FilterViolation, 'abovePrice > lastPrice'):
            validator.validate_replacement(
                'ETHUSDT', 'orderList/oco', {
                    'symbol': 'ETHUSDT', 'side': 'SELL', 'quantity': '1',
                    'aboveType': 'LIMIT_MAKER', 'abovePrice': '90.00',
                    'belowType': 'STOP_LOSS_LIMIT', 'belowPrice': '79.00',
                    'belowStopPrice': '80.00',
                })

    def test_malformed_capacity_rows_fail_closed(self):
        rows = _filters(capacity={
            'filterType': 'MAX_NUM_ORDERS', 'maxNumOrders': 10})
        validator = SpotFilterValidator(_Public(rows), max_age_seconds=300)
        with self.assertRaisesRegex(FilterDataUnavailable, 'malformed rows'):
            validator.validate_replacement(
                'ETHUSDT', 'order', self._limit_params(),
                open_orders_provider=lambda _symbol: [{
                    'symbol': 'ETHUSDT', 'orderId': 'not-an-int', 'type': 'LIMIT'}])


class StreamAndExactPayloadTests(unittest.TestCase):
    def _adapter(self, store):
        position = _position()
        pf = SimpleNamespace(
            positions={'ETHUSDT': position}, autotrade_on=True,
            protection_halt='', halt_reason='', save=mock.Mock())
        original_order = mock.Mock()
        original_list = mock.Mock()
        trader = SimpleNamespace(
            pf=pf, _uds_order=original_order, _uds_list=original_list,
            set_autotrade=mock.Mock(return_value='ON'))
        adapter = CoreAdapter.__new__(CoreAdapter)
        adapter.state_store = store
        adapter.event_sink = None
        adapter.trader = trader
        adapter._runtime_safety_faults = {}
        adapter._install_event_wrappers()
        return adapter, trader, position, original_order

    def test_unsafe_expiry_latches_disarms_and_keeps_callback_alive(self):
        with tempfile.TemporaryDirectory() as raw:
            store = StateStore(Path(raw) / 'state.json', Path(raw) / 'state.sqlite')
            adapter, trader, position, original = self._adapter(store)
            core_module.legacy._set_entries_armed(True)
            trader._uds_order({
                'e': 'executionReport', 's': 'ETHUSDT', 'i': 7, 'I': 9,
                'E': 10, 'X': 'EXPIRED_IN_MATCH', 'expiryReason': 'PRICE_RANGE',
            })
            original.assert_called_once()
            self.assertFalse(core_module.legacy._entries_armed())
            self.assertFalse(store.entries())
            self.assertTrue(store.safety_halts())
            self.assertEqual(position._sidecar_lifecycle,
                             LifecycleState.RECONCILIATION_REQUIRED.value)
            with self.assertRaisesRegex(RuntimeError, 'in-memory safety fault'):
                adapter.set_enabled(True)

    def test_journal_failure_also_disarms_without_killing_callback(self):
        with tempfile.TemporaryDirectory() as raw:
            store = StateStore(Path(raw) / 'state.json', Path(raw) / 'state.sqlite')
            adapter, trader, _position_value, original = self._adapter(store)
            core_module.legacy._set_entries_armed(True)
            with mock.patch.object(
                    store, 'record_exchange_event', side_effect=OSError('disk full')):
                trader._uds_order({
                    'e': 'executionReport', 's': 'ETHUSDT', 'i': 8, 'I': 10,
                    'E': 11, 'X': 'NEW',
                })
            original.assert_called_once()
            self.assertFalse(core_module.legacy._entries_armed())
            self.assertTrue(adapter.runtime_safety_faults())
            self.assertTrue(store.safety_halts())

    def test_exact_trailing_payload_is_validated_before_the_only_send(self):
        client = SimpleNamespace(create_order=mock.Mock(return_value={'orderId': 77}))
        broker = SimpleNamespace(
            c=client, price=mock.Mock(return_value=Decimal('100')),
            clamp_delta=lambda _sym, value: int(value), _check_lot=mock.Mock(),
            _sync_weight=mock.Mock(), _find_order=lambda _symbol, _coid: None,
            _place_idempotent=lambda send, _lookup, _label: send(),
        )
        adapter = CoreAdapter.__new__(CoreAdapter)
        adapter._validate_existing_request = mock.Mock()
        adapter._validate_replacement_filters = mock.Mock()
        result = adapter._strict_trailing_sell(
            broker, _symbol(), Decimal('1'), 100)
        self.assertEqual(result['orderId'], 77)
        validated = adapter._validate_replacement_filters.call_args.args[3]
        self.assertEqual(validated, client.create_order.call_args.kwargs)
        broker.price.assert_called_once_with('ETHUSDT')

        client.create_order.reset_mock()
        adapter._validate_replacement_filters.side_effect = FilterViolation('blocked')
        with self.assertRaisesRegex(FilterViolation, 'blocked'):
            adapter._strict_trailing_sell(broker, _symbol(), Decimal('1'), 100)
        client.create_order.assert_not_called()


class ComposeOwnershipTests(unittest.TestCase):
    def test_every_universe_consumer_has_a_read_only_overlay(self):
        compose = (Path(__file__).resolve().parents[1] / 'docker-compose.yml').read_text()
        overlays = [line for line in compose.splitlines()
                    if '/universe:' in line and line.rstrip().endswith(':ro')]
        self.assertEqual(len(overlays), 4)


if __name__ == '__main__':
    unittest.main(verbosity=2)
