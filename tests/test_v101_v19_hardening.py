from __future__ import annotations
"""V10.1 + V19.1 hardening coverage.

Each test exercises a specific audit-finding fix from this consolidation:
envelope authentication (V101-NEW-001), the immutable V19.1 controller and
strict result validation (master protocol 8.1/8.5/8.6), complete pre-cancel
filter validation (V101-NEW-002), the structured emergency exit (C-002),
the deterministic simulation lifecycle with fault injection (C-004),
endpoint-complete reconciliation (H-005), bounded risk configuration
(V101-NEW-007), the fee-shave zero guard (V101-NEW-006), the durable screening
queue and single-writer bridge (8.3/8.4), the signal-time fresh-screening seam
(8.5), the signed live-evidence gate (C-003/H-007), SQLite integrity/backup
(H-006), and Telegram Sharia controls (8.7).
"""
import json
import hashlib
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _harness

from services.common import envelope
from services.common.config_bounds import ConfigError, env_float, env_int
from services.common.sharia_v19 import (
    ControllerIntegrityError, ResultValidationError, V19_CONTROLLER_FILENAME,
    V19_CONTROLLER_SHA256, V19_MAIN_FRAMEWORK, V19_RUNNER_CONTROLLER,
    GREEN_PROOF_CHECKS, KEYWORD_CATEGORIES, REQUIRED_WHITEPAPER_SECTIONS,
    controller_sha256, fail_closed_report, load_controller, validate_result,
)


def _green_report(base='ETH'):
    green_checks = {name: True for name in GREEN_PROOF_CHECKS}
    sections = {name: {'status': 'NOT_FOUND', 'quote': ''}
                for name in REQUIRED_WHITEPAPER_SECTIONS}
    sections['S1_PROJECT_OVERVIEW'] = {
        'status': 'FOUND', 'quote': 'The official project provides decentralized payment utility.'}
    sections['S2_TOKEN_UTILITY'] = {
        'status': 'FOUND', 'quote': 'The token pays for useful network transaction services.'}
    sections['S3_REVENUE_MODEL'] = {
        'status': 'FOUND', 'quote': 'Revenue comes from ordinary network service fees.'}
    screeners = {name: 'not listed' for name in (
        'cryptoummah', 'sharlife', 'islamicfinanceguru', 'saraf',
        'halalscreener', 'gethalalcrypto', 'musaffa')}
    keywords = {name: {'hits': 0, 'quotes': []} for name in KEYWORD_CATEGORIES}
    return {
        'coin_name': base, 'ticker': base, 'main_framework': V19_MAIN_FRAMEWORK,
        'runner_controller': V19_RUNNER_CONTROLLER, 'token_type': 'PAYMENT',
        'mal_status': 'CONFIRMED', 'sub_framework_applied': 'NONE',
        'shariah_screener_check': screeners, 'whitepaper_parsing': sections,
        'keyword_scan_results': keywords, 'contradictions_found': [],
        'contradiction_resolution': 'all checked; none found', 'sources_opened': [{
            'url': 'https://example.org/whitepaper', 'tier': 'TIER_1_OFFICIAL',
            'opened': True, 'identity_match': True,
            'quote': 'The official project provides useful network transaction services.',
        }],
        'sources_failed': [], 'tool_access_limits': [],
        'tool_evidence': {
            'provider': 'openai-responses',
            'completed_web_search_calls': [{
                'id': 'ws_test_fixture', 'status': 'completed', 'action_type': 'search',
                'source_urls': ['https://example.org/whitepaper'],
            }],
            'url_citations': [{
                'url': 'https://example.org/whitepaper', 'title': 'Official whitepaper',
                'start_index': 0, 'end_index': 10,
            }],
        },
        'final_code': 'GREEN', 'direct_result': 'HALAL',
        'haram_narrative_code': 'NOT_PROVEN', 'haram_narrative_name': 'NOT_PROVEN',
        'haram_proof_card': {'C1': False, 'C2': False, 'C3': False, 'C4': False, 'C5': False,
                             'quote': None, 'url': None, 'tier': None},
        'green_proof_card': green_checks, 'tech_stop_trigger': 'NONE',
        'purification_required': 'NO', 'human_escalation_required': False,
        'human_escalation_reason': 'none',
        'next_rescreen_date': (datetime.now(timezone.utc) + timedelta(days=30)).date().isoformat(),
        'shariah_result': 'HALAL under this screening', 'user_personal_action': 'Spot buy/sell permitted',
        'confidence_level': 'HIGH',
    }


# ── V101-NEW-001: envelope authentication ─────────────────────────────────
class EnvelopeAuthTests(unittest.TestCase):
    def test_valid_envelope_round_trips(self):
        env = _harness.sign_command('status', command_id='c1')
        payload = envelope.verify_envelope(env, purpose=envelope.BUS_COMMAND,
                                           expected_producers={'telegram-broker'})
        self.assertEqual(payload['command'], 'status')

    def test_tampered_payload_is_rejected(self):
        env = _harness.sign_command('status', command_id='c1')
        env['payload']['command'] = 'emergency_exit'
        with self.assertRaises(envelope.EnvelopeError):
            envelope.verify_envelope(env, purpose=envelope.BUS_COMMAND,
                                     expected_producers={'telegram-broker'})

    def test_wrong_producer_is_rejected(self):
        env = _harness.sign_signal({'signal_id': 's1'})
        with self.assertRaises(envelope.EnvelopeError):
            envelope.verify_envelope(env, purpose=envelope.BUS_SIGNAL,
                                     expected_producers={'sharia-screener'})

    def test_wrong_purpose_is_rejected(self):
        # A command-key signature cannot pass as a signal (different key + purpose).
        env = _harness.sign_command('status', command_id='c1')
        with self.assertRaises(envelope.EnvelopeError):
            envelope.verify_envelope(env, purpose=envelope.BUS_SIGNAL,
                                     expected_producers={'freqtrade-strategy'})

    def test_expired_envelope_is_rejected(self):
        env = _harness.sign_command('status', command_id='c1', ttl=1)
        future = env['expires_at'] + 10
        with self.assertRaises(envelope.EnvelopeError):
            envelope.verify_envelope(env, purpose=envelope.BUS_COMMAND,
                                     expected_producers={'telegram-broker'}, now=future)

    def test_cross_release_replay_is_rejected(self):
        env = _harness.sign_command('status', command_id='c1')
        with mock.patch.dict('os.environ', {'ENVELOPE_RELEASE_HASH': 'b' * 64}):
            with self.assertRaises(envelope.EnvelopeError):
                envelope.verify_envelope(env, purpose=envelope.BUS_COMMAND,
                                         expected_producers={'telegram-broker'})

    def test_missing_key_fails_closed(self):
        with mock.patch.dict('os.environ', {'COMMAND_HMAC_KEY': ''}):
            with self.assertRaises(envelope.EnvelopeError):
                envelope.load_key(envelope.BUS_COMMAND)

    def test_short_key_fails_closed(self):
        with mock.patch.dict('os.environ', {'COMMAND_HMAC_KEY': 'short'}):
            with self.assertRaises(envelope.EnvelopeError):
                envelope.load_key(envelope.BUS_COMMAND)


class ForgedBusFileTests(unittest.TestCase):
    def test_unsigned_command_file_is_rejected_by_sidecar(self):
        from services.execution_sidecar.main import process_command
        from services.execution_sidecar.state_store import StateStore
        from services.execution_sidecar.risk_checks import FreshSignalGuard
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = StateStore(root / 'state.json', root / 'state.sqlite')
            guard = FreshSignalGuard(root / 'risk.json')
            forged = root / 'forged.json'
            forged.write_text(json.dumps({'command_id': 'x', 'command': 'entries',
                                          'args': {'enabled': True}, 'created_at': 0}))
            adapter = SimpleNamespace(trader=SimpleNamespace(is_running=lambda: False))
            with mock.patch('services.execution_sidecar.main.RUNTIME', root / 'runtime'):
                process_command(adapter, store, guard, forged)
            # No result file is written for an unauthenticated command, and it is removed.
            self.assertFalse((root / 'runtime' / 'command_result_x.json').exists())
            self.assertFalse(forged.exists())

    def test_sibling_container_cannot_forge_a_command_with_the_signal_key(self):
        from services.execution_sidecar.main import process_command
        from services.execution_sidecar.state_store import StateStore
        from services.execution_sidecar.risk_checks import FreshSignalGuard
        # Freqtrade only holds the signal key; a command it signs must not verify.
        forged = envelope.sign_envelope(producer='freqtrade-strategy', purpose=envelope.BUS_SIGNAL,
                                        payload={'command_id': 'x', 'command': 'entries',
                                                 'args': {'enabled': True}, 'created_at': 0},
                                        ttl_seconds=300)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = StateStore(root / 'state.json', root / 'state.sqlite')
            guard = FreshSignalGuard(root / 'risk.json')
            path = root / 'forged.json'; path.write_text(json.dumps(forged))
            adapter = SimpleNamespace(trader=SimpleNamespace(is_running=lambda: False))
            with mock.patch('services.execution_sidecar.main.RUNTIME', root / 'runtime'):
                process_command(adapter, store, guard, path)
            self.assertFalse((root / 'runtime' / 'command_result_x.json').exists())


# ── master protocol 8.1: immutable V19.1 controller ───────────────────────
class ControllerIntegrityTests(unittest.TestCase):
    CONTROLLER = ROOT / 'shared/sharia' / V19_CONTROLLER_FILENAME

    def test_controller_present_and_byte_exact(self):
        self.assertTrue(self.CONTROLLER.exists())
        self.assertEqual(controller_sha256(self.CONTROLLER), V19_CONTROLLER_SHA256)

    def test_load_controller_accepts_the_immutable_file(self):
        raw, parsed = load_controller(self.CONTROLLER)
        self.assertEqual(parsed['VERSION'], V19_MAIN_FRAMEWORK)

    def test_tampered_controller_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / V19_CONTROLLER_FILENAME
            bad.write_bytes(self.CONTROLLER.read_bytes() + b'\n')  # one byte changed
            with self.assertRaises(ControllerIntegrityError):
                load_controller(bad)


# ── master protocol 8.5/8.6: strict result validation ─────────────────────
class ResultValidationTests(unittest.TestCase):
    def test_valid_green_passes(self):
        self.assertEqual(validate_result(_green_report('ETH'), expected_base='ETH')['final_code'], 'GREEN')

    def test_ticker_collision_is_rejected(self):
        with self.assertRaisesRegex(ResultValidationError, 'identity mismatch'):
            validate_result(_green_report('ETH'), expected_base='ETC')

    def test_haram_without_full_proof_is_rejected(self):
        r = _green_report('XYZ'); r['final_code'] = 'HARAM'; r['direct_result'] = 'HARAM'
        with self.assertRaises(ResultValidationError):
            validate_result(r, expected_base='XYZ')

    def test_haram_with_short_quote_is_rejected(self):
        r = _green_report('XYZ'); r['final_code'] = 'HARAM'; r['direct_result'] = 'HARAM'
        r['haram_narrative_code'] = 'N1'; r['haram_narrative_name'] = 'RIBA_LENDING'
        r['haram_proof_card'] = {'C1': True, 'C2': True, 'C3': True, 'C4': True, 'C5': True,
                                 'quote': 'too short quote', 'url': 'https://x', 'tier': 'tier_1_official'}
        with self.assertRaisesRegex(ResultValidationError, 'quote'):
            validate_result(r, expected_base='XYZ')

    def test_green_without_opened_source_is_rejected(self):
        r = _green_report('ETH'); r['sources_opened'] = []
        with self.assertRaisesRegex(ResultValidationError, 'Tier 1 source'):
            validate_result(r, expected_base='ETH')

    def test_green_with_insufficient_proof_checks_is_rejected(self):
        r = _green_report('ETH'); r['green_proof_card'] = {'a': True, 'b': True}
        with self.assertRaisesRegex(ResultValidationError, 'proof card'):
            validate_result(r, expected_base='ETH')

    def test_direct_result_inconsistent_with_code_is_rejected(self):
        r = _green_report('ETH'); r['direct_result'] = 'NO TRADE'
        with self.assertRaisesRegex(ResultValidationError, 'inconsistent'):
            validate_result(r, expected_base='ETH')

    def test_fail_closed_report_is_valid_no_trade(self):
        r = fail_closed_report('ETH', reason='quota exhausted')
        validate_result(r, expected_base='ETH')  # must not raise
        self.assertEqual(r['final_code'], 'NO_TRADE_INFO')
        self.assertEqual(r['direct_result'], 'NO TRADE')


# ── V101-NEW-002: complete pre-cancel filter validation ───────────────────
from services.common.binance_public import BinancePublicClient  # noqa: E402


class _FakePublic:
    """Fake public client. ``reference`` None => the symbol has no reference
    price (null / -2043) and the validator falls back to ``price`` (the prior
    avgPrice/ticker behavior). ``ref_error`` raises to exercise fail-closed."""
    NO_REFERENCE_PRICE = BinancePublicClient.NO_REFERENCE_PRICE

    def __init__(self, entry, price='10', reference=None, ref_error=None):
        self._entry = entry
        self._price = price
        self._reference = reference
        self._ref_error = ref_error

    def exchange_info(self, symbol=None):
        return {'symbols': [self._entry]}

    def ticker_price(self, symbol):
        return {'price': self._price}

    def get(self, path, params=None):
        return {'price': self._price}

    def reference_price(self, symbol):
        if self._ref_error is not None:
            raise self._ref_error
        if self._reference is None:
            return self.NO_REFERENCE_PRICE
        return self._reference


def _symbol_entry(filters):
    return {'symbol': 'ETHUSDT', 'status': 'TRADING', 'isSpotTradingAllowed': True,
            'baseAsset': 'ETH', 'quoteAsset': 'USDT', 'filters': filters}


class FilterPrevalidationTests(unittest.TestCase):
    def _validator(self, filters, price='10'):
        from services.execution_sidecar.filters import SpotFilterValidator
        return SpotFilterValidator(public_client=_FakePublic(_symbol_entry(filters), price))

    def test_percent_price_band_violation_rejected(self):
        from services.execution_sidecar.filters import FilterViolation
        filters = [
            {'filterType': 'PRICE_FILTER', 'tickSize': '0.01', 'minPrice': '0.01', 'maxPrice': '100000'},
            {'filterType': 'LOT_SIZE', 'stepSize': '0.001', 'minQty': '0.001', 'maxQty': '9000'},
            {'filterType': 'NOTIONAL', 'minNotional': '5', 'maxNotional': '0'},
            {'filterType': 'PERCENT_PRICE_BY_SIDE', 'askMultiplierUp': '1.5', 'askMultiplierDown': '0.5', 'avgPriceMins': 0},
        ]
        v = self._validator(filters, price='10')
        params = {'quantity': '1', 'aboveType': 'LIMIT_MAKER', 'abovePrice': '100.00',
                  'belowType': 'STOP_LOSS_LIMIT', 'belowPrice': '9.00', 'belowStopPrice': '9.10'}
        with self.assertRaisesRegex(FilterViolation, 'percent-price'):
            v.validate_replacement('ETHUSDT', 'orderList/oco', params)

    def test_max_notional_violation_rejected(self):
        from services.execution_sidecar.filters import FilterViolation
        filters = [
            {'filterType': 'PRICE_FILTER', 'tickSize': '0.01', 'minPrice': '0.01', 'maxPrice': '100000'},
            {'filterType': 'LOT_SIZE', 'stepSize': '0.001', 'minQty': '0.001', 'maxQty': '9000'},
            {'filterType': 'NOTIONAL', 'minNotional': '5', 'maxNotional': '50'},
        ]
        v = self._validator(filters)
        params = {'symbol': 'ETHUSDT', 'side': 'SELL', 'type': 'STOP_LOSS_LIMIT',
                  'quantity': '100', 'price': '10.00', 'stopPrice': '10.00'}
        with self.assertRaisesRegex(FilterViolation, 'maxNotional'):
            v.validate_replacement('ETHUSDT', 'order', params)

    def test_unavailable_filter_data_refuses_before_cancel(self):
        from services.execution_sidecar.filters import FilterDataUnavailable, SpotFilterValidator

        class Broken:
            def exchange_info(self, symbol=None):
                raise RuntimeError('network down')
        v = SpotFilterValidator(public_client=Broken())
        with self.assertRaises(FilterDataUnavailable):
            v.validate_replacement('ETHUSDT', 'order',
                                   {'quantity': '1', 'price': '10', 'stopPrice': '10'})

    def test_clean_replacement_passes(self):
        filters = [
            {'filterType': 'PRICE_FILTER', 'tickSize': '0.01', 'minPrice': '0.01', 'maxPrice': '100000'},
            {'filterType': 'LOT_SIZE', 'stepSize': '0.001', 'minQty': '0.001', 'maxQty': '9000'},
            {'filterType': 'NOTIONAL', 'minNotional': '5', 'maxNotional': '1000000'},
            {'filterType': 'PERCENT_PRICE_BY_SIDE', 'askMultiplierUp': '5', 'askMultiplierDown': '0.2', 'avgPriceMins': 0},
        ]
        v = self._validator(filters, price='10')
        params = {'quantity': '1', 'aboveType': 'LIMIT_MAKER', 'abovePrice': '12.00',
                  'belowType': 'STOP_LOSS_LIMIT', 'belowPrice': '9.00', 'belowStopPrice': '9.10'}
        summary = v.validate_replacement('ETHUSDT', 'orderList/oco', params)
        self.assertIn('PRICE_FILTER', summary['filters_checked'])


class ReferencePriceFilterTests(unittest.TestCase):
    """F23E-001: PERCENT_PRICE(_BY_SIDE) must use the Binance referencePrice
    (2026-05-06) when present, falling back to avg/last price only when it is
    null/-2043, and failing closed on any other fetch error."""

    _FILTERS = [
        {'filterType': 'PRICE_FILTER', 'tickSize': '0.01', 'minPrice': '0.01', 'maxPrice': '100000'},
        {'filterType': 'LOT_SIZE', 'stepSize': '0.001', 'minQty': '0.001', 'maxQty': '9000'},
        {'filterType': 'NOTIONAL', 'minNotional': '5', 'maxNotional': '1000000'},
        {'filterType': 'PERCENT_PRICE_BY_SIDE', 'askMultiplierUp': '1.2', 'askMultiplierDown': '0.8', 'avgPriceMins': 5},
    ]

    def _validator(self, price='50', reference=None, ref_error=None):
        from services.execution_sidecar.filters import SpotFilterValidator
        pub = _FakePublic(_symbol_entry(self._FILTERS), price=price,
                          reference=reference, ref_error=ref_error)
        return SpotFilterValidator(public_client=pub)

    def _sell(self, price):
        return {'symbol': 'ETHUSDT', 'side': 'SELL', 'type': 'STOP_LOSS_LIMIT',
                'quantity': '1', 'price': price, 'stopPrice': price}

    def test_reference_price_divergence_now_rejects(self):
        # The exact audit reproduction: avgPrice 50 would ACCEPT price 45,
        # but the real Binance referencePrice 100 must REJECT it (band 80..120).
        from services.execution_sidecar.filters import FilterViolation
        v = self._validator(price='50', reference='100')
        with self.assertRaisesRegex(FilterViolation, 'percent-price'):
            v.validate_replacement('ETHUSDT', 'order', self._sell('45.00'))

    def test_the_old_behavior_would_have_wrongly_accepted(self):
        # Guard: with NO reference price, the fallback (avgPrice=50) accepts 45,
        # proving the divergence test above is meaningful, not vacuous.
        v = self._validator(price='50', reference=None)
        summary = v.validate_replacement('ETHUSDT', 'order', self._sell('45.00'))
        self.assertEqual(summary['reference_price'], '50')

    def test_reference_price_within_band_accepts(self):
        v = self._validator(price='50', reference='100')
        summary = v.validate_replacement('ETHUSDT', 'order', self._sell('95.00'))
        self.assertEqual(summary['reference_price'], '100')

    def test_null_reference_falls_back_to_previous_behavior(self):
        # referencePrice null => documented fallback to avgPrice/last price.
        v = self._validator(price='73', reference=None)
        summary = v.validate_replacement('ETHUSDT', 'order', self._sell('73.00'))
        self.assertEqual(summary['reference_price'], '73')

    def test_minus_2043_falls_back(self):
        # A -2043 error is surfaced by the client as NO_REFERENCE_PRICE.
        v = self._validator(price='60', reference=None)
        summary = v.validate_replacement('ETHUSDT', 'order', self._sell('60.00'))
        self.assertEqual(summary['reference_price'], '60')

    def test_malformed_reference_fails_closed(self):
        from services.execution_sidecar.filters import FilterDataUnavailable
        v = self._validator(price='50', reference='not-a-number')
        with self.assertRaises(FilterDataUnavailable):
            v.validate_replacement('ETHUSDT', 'order', self._sell('50.00'))

    def test_reference_fetch_error_fails_closed(self):
        from services.common.binance_public import BinancePublicError
        from services.execution_sidecar.filters import FilterDataUnavailable
        v = self._validator(price='50', ref_error=BinancePublicError('timeout'))
        with self.assertRaises(FilterDataUnavailable):
            v.validate_replacement('ETHUSDT', 'order', self._sell('50.00'))

    def test_client_maps_minus_2043_and_null_to_no_reference(self):
        # Unit-level proof of the client contract used above.
        from services.common.binance_public import BinancePublicClient
        import requests

        c = BinancePublicClient.__new__(BinancePublicClient)
        c.get = lambda path, params=None: {'symbol': 'X', 'referencePrice': None, 'timestamp': 1}
        self.assertIs(c.reference_price('X'), BinancePublicClient.NO_REFERENCE_PRICE)
        c.get = lambda path, params=None: {'symbol': 'X', 'referencePrice': '12.50', 'timestamp': 1}
        self.assertEqual(c.reference_price('X'), '12.50')

        class _Resp:
            def json(self):
                return {'code': -2043, 'msg': "This symbol doesn't have a reference price."}

        def _raise(path, params=None):
            err = requests.HTTPError('400')
            err.response = _Resp()
            raise err
        c.get = _raise
        self.assertIs(c.reference_price('X'), BinancePublicClient.NO_REFERENCE_PRICE)


# ── C-004 + C-002: simulation lifecycle with faults, structured emergency ─
class SimulationLifecycleTests(unittest.TestCase):
    def _adapter(self, root):
        from services.execution_sidecar.simulation_adapter import SimulationAdapter
        from services.execution_sidecar.state_store import StateStore
        from services.execution_sidecar.risk_checks import FreshSignalGuard
        store = StateStore(root / 'state.json', root / 'state.sqlite')
        guard = FreshSignalGuard(root / 'risk.json', state_store=store)
        adapter = SimulationAdapter(store, guard, fault_path=root / 'faults.json')
        adapter.start()
        store.upsert_trade('t1', 'ETH/USDT', lifecycle_state='SIGNAL_APPROVED')
        return adapter, store

    def test_normal_fill_is_protected(self):
        with tempfile.TemporaryDirectory() as td:
            adapter, store = self._adapter(Path(td))
            ok, msg = adapter.submit('ETHUSDT')
            self.assertTrue(ok)
            self.assertIn('ETHUSDT', adapter.sim_positions)
            with store._connect() as con:
                row = con.execute("SELECT lifecycle_state FROM trade_records WHERE trade_id='t1'").fetchone()
            self.assertIn(row['lifecycle_state'], ('PROTECTION_ACTIVE', 'ENTRY_FILLED'))

    def test_reject_fault_is_not_accepted(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / 'faults.json').write_text(json.dumps({'queue': ['reject']}))
            adapter, store = self._adapter(root)
            ok, msg = adapter.submit('ETHUSDT')
            self.assertFalse(ok)
            self.assertIn('reject', msg.lower())

    def test_timeout_fault_is_uncertain(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / 'faults.json').write_text(json.dumps({'queue': ['timeout']}))
            adapter, store = self._adapter(root)
            ok, msg = adapter.submit('ETHUSDT')
            self.assertFalse(ok)
            self.assertIn('reconcil', msg.lower())

    def test_partial_fill_fault_protects_partial(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / 'faults.json').write_text(json.dumps({'queue': ['partial_fill']}))
            adapter, store = self._adapter(root)
            ok, msg = adapter.submit('ETHUSDT')
            self.assertTrue(ok)
            self.assertTrue(adapter.sim_positions['ETHUSDT']['partial'])

    def test_emergency_exit_returns_structured_verified_result(self):
        with tempfile.TemporaryDirectory() as td:
            adapter, store = self._adapter(Path(td))
            adapter.submit('ETHUSDT')
            result = adapter.emergency_exit('ETHUSDT')
            self.assertIsInstance(result, dict)
            self.assertTrue(result['ok'])
            self.assertEqual(result['stage'], 'verified-exit')

    def test_emergency_exit_missing_position_is_not_success(self):
        with tempfile.TemporaryDirectory() as td:
            adapter, store = self._adapter(Path(td))
            result = adapter.emergency_exit('ADAUSDT')
            self.assertFalse(result['ok'])


# ── H-005: endpoint-complete reconciliation ───────────────────────────────
class ReconciliationTests(unittest.TestCase):
    def test_all_endpoints_ok_reports_success(self):
        adapter = _reconcile_adapter(fail=None)
        result = adapter.verified_reconcile()
        self.assertTrue(result['ok'])

    def test_failed_enumeration_reports_failure_not_success(self):
        adapter = _reconcile_adapter(fail='openOrders')
        result = adapter.verified_reconcile()
        self.assertFalse(result['ok'])
        self.assertIn('openOrders', result['detail'])


def _reconcile_adapter(fail):
    from services.execution_sidecar.core_adapter import CoreAdapter

    class Client:
        def get_open_orders(self):
            if fail == 'openOrders':
                raise RuntimeError('timeout')
            return []

        def _get(self, path, signed, data=None):
            if fail == 'openOrderList':
                raise RuntimeError('timeout')
            return []

        def get_account(self):
            if fail == 'account':
                raise RuntimeError('timeout')
            return {'balances': []}

    broker = SimpleNamespace(c=Client(), _sync_weight=lambda: None)
    pf = SimpleNamespace(save=lambda: None)
    trader = SimpleNamespace(broker=broker, pf=pf, _uds_resync=lambda: None)
    adapter = CoreAdapter.__new__(CoreAdapter)
    adapter.trader = trader
    adapter.state_store = None
    adapter.mirror_positions = lambda status: 0
    return adapter


# ── V101-NEW-007: bounded configuration ───────────────────────────────────
class ConfigBoundsTests(unittest.TestCase):
    def test_negative_cooldown_is_rejected(self):
        with mock.patch.dict('os.environ', {'PAIR_COOLDOWN_SECONDS': '-60'}):
            with self.assertRaises(ConfigError):
                env_int('PAIR_COOLDOWN_SECONDS', 60, 0, 86_400)

    def test_nonfinite_is_rejected(self):
        with mock.patch.dict('os.environ', {'X': 'nan'}):
            with self.assertRaises(ConfigError):
                env_float('X', 1.0, 0.0, 10.0)

    def test_valid_within_bounds(self):
        with mock.patch.dict('os.environ', {'PAIR_COOLDOWN_SECONDS': '30'}):
            self.assertEqual(env_int('PAIR_COOLDOWN_SECONDS', 60, 0, 86_400), 30)

    def test_guard_rejects_unsafe_environment_at_construction(self):
        from services.execution_sidecar.risk_checks import FreshSignalGuard
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.dict('os.environ', {'MAX_SIGNAL_AGE_SECONDS': '-1'}):
            with self.assertRaises(ConfigError):
                FreshSignalGuard(Path(td) / 'risk.json')


# ── V101-NEW-006: fee-shave zero guard ────────────────────────────────────
class FeeShaveTests(unittest.TestCase):
    def test_zero_shaved_quantity_rejects_entry(self):
        from services.execution_sidecar.protection_modes import OrderRequestFactory
        from services.common.models import ProtectionMode

        class L:
            CFG = SimpleNamespace(EXIT_FEE_SHAVE=True, FEE_PCT_PER_SIDE=100.0,
                                  FIXED_STOP_PCT=2.0, LIMIT_FILL_BUFFER_BIPS=10,
                                  OTOCO_TP_PCT=4.0, INITIAL_TRAIL_DELTA_BIPS=100)

            @staticmethod
            def round_down(v, s):
                from decimal import Decimal, ROUND_DOWN
                v, s = Decimal(str(v)), Decimal(str(s))
                return (v / s).to_integral_value(rounding=ROUND_DOWN) * s

            @staticmethod
            def dstr(v):
                return format(Decimal(str(v)), 'f')

            @staticmethod
            def bips_mult(b):
                return Decimal(1) + Decimal(b) / Decimal(10000)

            _c = 0

            @classmethod
            def _new_coid(cls, p='X'):
                cls._c += 1
                return f'{p}{cls._c}'

        sym = SimpleNamespace(symbol='DUSTUSDT', tick=Decimal('0.01'), step=Decimal('1'),
                              trail_min=10, trail_max=2000)
        factory = OrderRequestFactory(L)
        # 100% fee shave -> shaved qty rounds to zero -> entry must be refused.
        with self.assertRaisesRegex(ValueError, 'rounds to zero'):
            factory.entry(ProtectionMode.FIXED_OCO, sym, Decimal('1'), Decimal('10'), Decimal('12'), 100)


# ── master protocol 8.3/8.4: durable screening queue ──────────────────────
class ScreeningQueueTests(unittest.TestCase):
    def _queue(self, root):
        from services.sharia_screener.queue_store import QueueStore
        return QueueStore(root / 'q.sqlite')

    def test_duplicate_active_scan_is_suppressed(self):
        with tempfile.TemporaryDirectory() as td:
            q = self._queue(Path(td))
            self.assertTrue(q.enqueue('r1', 'ETH', 'ETH/USDT', 'manual', 'test'))
            self.assertFalse(q.enqueue('r2', 'ETH', 'ETH/USDT', 'idle', 'test'))  # lower prio, active exists

    def test_priority_ordering_signal_before_idle(self):
        with tempfile.TemporaryDirectory() as td:
            q = self._queue(Path(td))
            q.enqueue('idle1', 'AAA', 'AAA/USDT', 'idle', 'test')
            q.enqueue('sig1', 'BBB', 'BBB/USDT', 'signal', 'test')
            nxt = q.next_request()
            self.assertEqual(nxt['request_id'], 'sig1')

    def test_running_requests_requeue_after_restart(self):
        with tempfile.TemporaryDirectory() as td:
            q = self._queue(Path(td))
            q.enqueue('r1', 'ETH', 'ETH/USDT', 'manual', 'test')
            q.mark_running('r1')
            self.assertIsNone(q.next_request())
            self.assertEqual(q.requeue_running(), 1)
            self.assertEqual(q.next_request()['request_id'], 'r1')


# ── master protocol 8.5: single-writer bridge + projection ────────────────
class ScreenerBridgeTests(unittest.TestCase):
    def _env(self, td):
        shared = Path(td) / 'shared'
        (shared / 'sharia').mkdir(parents=True)
        (shared / 'runtime').mkdir(parents=True)
        controller = shared / 'sharia' / V19_CONTROLLER_FILENAME
        shutil.copyfile(ROOT / 'shared/sharia' / V19_CONTROLLER_FILENAME, controller)
        return {
            'SHARED_ROOT': str(shared),
            'SHARIA_FILE': str(shared / 'sharia/sharia_status.json'),
            'LEGACY_HALAL_FILE': str(shared / 'sharia/halal_coins.json'),
            'SHARIA_RESULTS_DIR': str(shared / 'sharia/results'),
            'SHARIA_REPORTS_DIR': str(shared / 'sharia/reports'),
            'SHARIA_CONTROLLER_FILE': str(controller),
        }

    def test_validated_green_becomes_trade_eligible_and_signed(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.dict('os.environ', self._env(td)):
                import importlib
                from services.common import paths as paths_mod
                importlib.reload(paths_mod)
                from services.sharia_screener import bridge as bridge_mod
                importlib.reload(bridge_mod)
                out = bridge_mod.write_screening_outcome('r1', 'ETH', 'ETH/USDT',
                                                         _green_report('ETH'), validated=True)
                # Signed result verifies and is trade-eligible.
                result = envelope.read_verified_file(Path(out['result_path']),
                                                     purpose=envelope.BUS_SHARIA_RESULT,
                                                     expected_producers={'sharia-screener'})
                self.assertEqual(result['final_code'], 'GREEN')
                from services.universe_service.sharia_filter import ShariaFilter
                gate = ShariaFilter(paths_mod.SHARIA_FILE)
                self.assertTrue(gate.decision('ETH').allowed)
        _reload_paths_default()

    def test_unvalidated_green_is_downgraded_to_no_trade(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.dict('os.environ', self._env(td)):
                import importlib
                from services.common import paths as paths_mod
                importlib.reload(paths_mod)
                from services.sharia_screener import bridge as bridge_mod
                importlib.reload(bridge_mod)
                bridge_mod.write_screening_outcome('r2', 'ETH', 'ETH/USDT',
                                                   _green_report('ETH'), validated=False,
                                                   error='model returned unvalidated output')
                from services.universe_service.sharia_filter import ShariaFilter
                gate = ShariaFilter(paths_mod.SHARIA_FILE)
                self.assertFalse(gate.decision('ETH').allowed)
        _reload_paths_default()


def _reload_paths_default():
    import importlib
    from services.common import paths as paths_mod
    importlib.reload(paths_mod)
    from services.sharia_screener import bridge as bridge_mod
    importlib.reload(bridge_mod)


# ── master protocol 8.7: Telegram Sharia controls ─────────────────────────
class TelegramShariaControlTests(unittest.TestCase):
    def test_pair_input_normalization(self):
        from services.telegram_broker.bot import normalize_pair_input
        self.assertEqual(normalize_pair_input('BTC/USDT')[0], 'BTC')
        self.assertEqual(normalize_pair_input('btcusdt')[0], 'BTC')
        self.assertEqual(normalize_pair_input('DGB/USDT')[0], 'DGB')
        self.assertIsNone(normalize_pair_input('BTC/BUSD')[0])   # non-USDT
        self.assertIsNone(normalize_pair_input('BNB/USDT')[0])   # excluded
        self.assertIsNone(normalize_pair_input('ETH-PERP')[0])   # malformed

    def test_offset_persistence(self):
        from services.telegram_broker import bot as tg
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'offset.json'
            with mock.patch.object(tg, 'OFFSET_PATH', path):
                tg._store_offset(42)
                self.assertEqual(tg._load_offset(), 42)


# ── H-006: SQLite integrity / verified backup ─────────────────────────────
class SqliteIntegrityTests(unittest.TestCase):
    def test_corrupted_db_fails_closed_on_init(self):
        from services.execution_sidecar.state_store import StateStore
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / 'state.sqlite'
            db.write_bytes(b'SQLite format 3\x00' + b'\xde\xad\xbe\xef' * 400)
            with self.assertRaises(RuntimeError):
                StateStore(Path(td) / 'state.json', db)

    def test_verified_backup_is_produced(self):
        from services.execution_sidecar.state_store import StateStore
        with tempfile.TemporaryDirectory() as td:
            store = StateStore(Path(td) / 'state.json', Path(td) / 'state.sqlite')
            store.upsert_trade('t1', 'ETH/USDT', lifecycle_state='ENTRY_FILLED')
            dest = store.backup(Path(td) / 'backups', retain=3)
            self.assertTrue(Path(dest).exists())


# ── master protocol 8.5: signal-time fresh screening seam ─────────────────
class SignalTimeScreeningTests(unittest.TestCase):
    def _manager(self, root, gate_mode, signal_id):
        from services.execution_sidecar import order_manager as om
        from services.execution_sidecar.state_store import StateStore
        from services.execution_sidecar.risk_checks import FreshSignalGuard
        from services.execution_sidecar.simulation_adapter import SimulationAdapter
        store = StateStore(root / 'state.json', root / 'state.sqlite')
        store.data['simulation'] = True
        store.set_entries(True)
        guard = FreshSignalGuard(root / 'risk.json', state_store=store)
        adapter = SimulationAdapter(store, guard); adapter.start()
        sharia = root / 'sharia.json'
        _harness.write_attested_status(sharia, [('ETH', 'GREEN')])
        processed = root / 'processed'; rejected = root / 'rejected'
        processed.mkdir(); rejected.mkdir()
        # Isolate the Sharia queue/result buses per test so results never leak
        # across tests via the global shared paths.
        queue_inbox = root / 'sharia_queue_inbox'; queue_inbox.mkdir()
        results_dir = root / 'sharia_results'; results_dir.mkdir()
        self._patchers = [
            mock.patch.object(om, 'SHARIA_QUEUE_INBOX', queue_inbox),
            mock.patch.object(om, 'SHARIA_RESULTS_DIR', results_dir),
            mock.patch.object(om, 'SHARIA_REPORTS_DIR', root / 'reports'),
            mock.patch.object(om, 'load_package_mode', return_value='testnet'),
            mock.patch.dict('os.environ', {'SHARIA_SIGNAL_GATE_MODE': gate_mode}),
        ]
        for p in self._patchers:
            p.start()
        mgr = om.OrderManager(adapter, store, guard, store, sharia,
                              {'processed': processed, 'rejected': rejected})
        return mgr, store, processed, rejected, results_dir

    def tearDown(self):
        for p in getattr(self, '_patchers', []):
            p.stop()

    def _signal(self, root, signal_id):
        now = datetime.now(timezone.utc)
        payload = {'signal_id': signal_id, 'pair': 'ETH/USDT', 'symbol': 'ETHUSDT',
                   'candle_time': now.isoformat(), 'generated_at': now.isoformat(),
                   'strategy': 'IctSmcStrategy', 'universe_hash': 'h', 'sharia_status': 'GREEN'}
        path = root / 'inbox'; path.mkdir(exist_ok=True)
        f = path / f'{signal_id}.json'; f.write_text(json.dumps(_harness.sign_signal(payload)))
        return f

    def _write_result(self, results_dir, signal_id, final_code):
        report = (_green_report('ETH') if final_code == 'GREEN'
                  else fail_closed_report('ETH', reason='test prohibited result'))
        final_code = str(report['final_code'])
        report_name = f'ETH_signal-{signal_id}.json'
        report_dir = results_dir.parent / 'reports'; report_dir.mkdir(exist_ok=True)
        report_path = report_dir / report_name
        report_path.write_text(json.dumps(report), encoding='utf-8')
        payload = {'request_id': 'signal-' + signal_id, 'base': 'ETH',
                   'pair': 'ETH/USDT', 'final_code': final_code, 'validated': True,
                   'report_file': report_name,
                   'report_sha256': hashlib.sha256(report_path.read_bytes()).hexdigest()}
        (results_dir / f'result_signal-{signal_id}.json').write_text(
            json.dumps(_harness.sign_sharia_result(payload)))

    def test_fresh_gate_waits_then_processes_after_signed_eligible_result(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sid = 'sig-eligible'
            mgr, store, processed, rejected, results_dir = self._manager(root, 'fresh', sid)
            sig = self._signal(root, sid)
            # First pass: no result yet -> AWAITING, signal stays in inbox.
            out = mgr.process_signal(sig, {'ETH/USDT'}, 'h')
            self.assertEqual(out[1], OrderManagerAwaiting())
            self.assertTrue(sig.exists())
            self._write_result(results_dir, sid, 'GREEN')
            ok, msg = mgr.process_signal(sig, {'ETH/USDT'}, 'h')
            self.assertTrue(ok)
            self.assertTrue(any(processed.glob('*.json')))

    def test_fresh_gate_rejects_prohibited_result(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sid = 'sig-prohibited'
            mgr, store, processed, rejected, results_dir = self._manager(root, 'fresh', sid)
            sig = self._signal(root, sid)
            mgr.process_signal(sig, {'ETH/USDT'}, 'h')  # request enqueued, AWAITING
            self._write_result(results_dir, sid, 'HARAM')
            ok, reason = mgr.process_signal(sig, {'ETH/USDT'}, 'h')
            self.assertFalse(ok)
            self.assertIn('fail_closed', reason)
            self.assertTrue(any(rejected.glob('*.json')))


def OrderManagerAwaiting():
    from services.execution_sidecar.order_manager import OrderManager
    return OrderManager.AWAITING


# ── Step 2 adoption: modern (post-listenKey) user-data-stream transport ────
class ModernUserDataStreamTests(unittest.TestCase):
    def _stream(self):
        from services.execution_sidecar.user_data_stream import ModernUserDataStream
        return ModernUserDataStream(broker=SimpleNamespace(), testnet=True)

    def test_credentials_required(self):
        from services.execution_sidecar.user_data_stream import ModernUserDataStream
        s = ModernUserDataStream(broker=SimpleNamespace(), testnet=True)
        with mock.patch.dict('os.environ', {'BINANCE_API_KEY': '', 'BINANCE_API_SECRET': ''}, clear=False):
            with self.assertRaises(RuntimeError):
                s.start()

    def test_subscription_request_is_signed_and_uses_ws_api_method(self):
        s = self._stream()
        with mock.patch.dict('os.environ', {'BINANCE_API_KEY': 'k' * 16, 'BINANCE_API_SECRET': 's' * 16}):
            req = s._subscription_request()
        self.assertEqual(req['method'], 'userDataStream.subscribe.signature')
        self.assertIn('signature', req['params'])
        self.assertEqual(req['params']['apiKey'], 'k' * 16)
        self.assertTrue(len(req['params']['signature']) == 64)  # hex sha256

    def test_endpoint_selection(self):
        from services.execution_sidecar.user_data_stream import ModernUserDataStream
        self.assertIn('testnet', ModernUserDataStream(broker=None, testnet=True).endpoint)
        self.assertIn('ws-api.binance.com', ModernUserDataStream(broker=None, testnet=False).endpoint)

    def test_event_dispatch_routes_by_type(self):
        seen = {'order': None, 'list': None, 'resync': 0}
        from services.execution_sidecar.user_data_stream import ModernUserDataStream
        s = ModernUserDataStream(
            broker=None,
            on_order_update=lambda e: seen.__setitem__('order', e),
            on_list_update=lambda e: seen.__setitem__('list', e),
            on_resync=lambda: seen.__setitem__('resync', seen['resync'] + 1),
        )
        s._dispatch_event({'e': 'executionReport', 'X': 'FILLED'})
        s._dispatch_event({'e': 'listStatus', 'l': 'ALL_DONE'})
        s._dispatch_event({'e': 'outboundAccountPosition'})
        self.assertEqual(seen['order']['X'], 'FILLED')
        self.assertEqual(seen['list']['l'], 'ALL_DONE')
        self.assertEqual(seen['resync'], 1)

    def test_core_adapter_swaps_legacy_transport_at_start(self):
        # The adoption must replace only the runtime class reference; the
        # legacy source stays byte-identical (enforced by the preservation test).
        import services.execution_sidecar.core_adapter as ca
        from services.execution_sidecar.user_data_stream import ModernUserDataStream
        src = (ROOT / 'services/execution_sidecar/core_adapter.py').read_text(encoding='utf-8')
        self.assertIn('legacy.UserDataStream = ModernUserDataStream', src)
        self.assertTrue(issubclass(ModernUserDataStream, object))


if __name__ == '__main__':
    unittest.main(verbosity=2)
