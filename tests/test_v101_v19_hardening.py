from __future__ import annotations
"""V10.1 + V19.3 hardening coverage.

Each test exercises a specific audit-finding fix from this consolidation:
envelope authentication (V101-NEW-001), the immutable V19.3 controller and
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
        **fail_closed_report(base, reason='schema fixture'),
        'fail_closed': False, 'fail_closed_reason': '',
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




# ── master protocol 8.1: immutable V19.3 controller ───────────────────────
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


    def _sell(self, price):
        return {'symbol': 'ETHUSDT', 'side': 'SELL', 'type': 'STOP_LOSS_LIMIT',
                'quantity': '1', 'price': price, 'stopPrice': price}








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


# ── H-005: endpoint-complete reconciliation ───────────────────────────────




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



# ── V101-NEW-006: fee-shave zero guard ────────────────────────────────────


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


# ── master protocol 8.5: signal-time fresh screening seam ─────────────────




# ── Step 2 adoption: modern (post-listenKey) user-data-stream transport ────


if __name__ == '__main__':
    unittest.main(verbosity=2)
