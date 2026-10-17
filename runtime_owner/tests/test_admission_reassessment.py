"""Live flow may recover; order ownership and exposure limits never reset."""
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

import pandas as pd

from registry_fixture import registry_document
from binana_tests.test_partial_promotion_recovery import Exchange, cfg
from freqtrade.binana.execution_manager import AdmissionRejected, BinanaExecutionManager
from freqtrade.binana.market_data import MarketSnapshot


class FlowFeed:
    def __init__(self):
        self.calls = 0
        self.on_refresh = None
        self.snapshot = MarketSnapshot('ADA/USDT', time.time(), 'GOOD', '1', '1.001',
            '10', '10000', '10000', '1', '1', '10000000', '.30', '-500', 'fresh',
            'EXECUTION_HEADROOM', 'fresh', '.6', '250')

    def refresh(self, pair, quote_budget=None):
        self.calls += 1
        if self.on_refresh:
            self.on_refresh()
        return self.snapshot

    def recovered(self):
        self.snapshot = replace(self.snapshot, taker_buy_ratio_60s='.70', cvd_quote_60s='1000',
                                state='GOOD', reason='EXECUTION_HEADROOM', flow_status='fresh')


class AdmissionReassessment(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        registry = root / 'registry.json'
        registry.write_text(json.dumps(registry_document(['ADAUSDT'])))
        self.config = cfg(root, registry)
        self.config['binana']['confluence_policy'] = {'enabled': True, 'min_score': 5,
            'minimum_volume_votes': 2, 'minimum_momentum_votes': 2}
        self.manager = BinanaExecutionManager(self.config, Exchange())
        self.feed = FlowFeed()
        self.manager.market_data = self.feed
        self.now = int(time.time() // 300) * 300 + 30
        self.candle = datetime.fromtimestamp(int(self.now // 300) * 300 - 300, timezone.utc)
        self.frame = pd.DataFrame([{'date': self.candle - timedelta(minutes=5 * (99-i)),
            'open': 100+.1*i*i, 'high': 101+.1*i*i, 'low': 99+.1*i*i,
            'close': 100+.1*i*i, 'volume': 100} for i in range(100)])
        self.intent = self.manager._intent_id('ADA/USDT', '3', self.candle)

    def attempt(self, seconds=0):
        with patch('time.time', return_value=self.now+seconds), \
             patch('freqtrade.binana.state_store.time', return_value=self.now+seconds):
            return self.manager.prepare_candidate(pair='ADA/USDT', enter_tag='3',
                candle_date=self.candle, candles=self.frame)

    def reject_flow(self):
        with self.assertRaisesRegex(AdmissionRejected, 'BULLISH_FLOW_NOT_CONFIRMED'):
            self.attempt()

    def occupy(self, count, nominal='250'):
        for i in range(count):
            self.manager.store.put_intent(intent_id='other-'+str(i), pair='DOT/USDT',
                signal_id='other', halal_allowed=True, registry_sha256='registry',
                nominal_usdt=nominal, state='RESERVED')

    def test_recovered_flow_reassesses_same_unsubmitted_candle_with_original_halal_decision(self):
        self.reject_flow()
        self.feed.recovered()
        admission = self.attempt(6)
        self.assertEqual(admission.intent_id, self.intent)
        self.assertEqual(self.manager.registry.lookup_count, 1)
        self.assertEqual(self.feed.calls, 2)
        self.assertEqual(self.manager.store.get_intent(self.intent)['state'], 'RESERVED')
        self.assertIsNone(self.manager.store.latest_generation(self.intent))

    def test_reassessment_survives_restart_without_second_registry_lookup(self):
        self.reject_flow()
        self.manager = BinanaExecutionManager(self.config, Exchange())
        self.manager.market_data = self.feed
        self.feed.recovered()
        self.assertEqual(self.attempt(6).intent_id, self.intent)
        self.assertEqual(self.manager.registry.lookup_count, 0)

    def test_recheck_is_bounded_to_once_per_five_seconds(self):
        self.reject_flow()
        self.feed.recovered()
        with self.assertRaisesRegex(AdmissionRejected, 'BULLISH_FLOW_NOT_CONFIRMED'):
            self.attempt(1)
        self.assertEqual(self.feed.calls, 1)
        self.assertEqual(self.attempt(6).intent_id, self.intent)

    def test_expired_candle_cannot_be_readmitted(self):
        self.reject_flow()
        self.feed.recovered()
        with self.assertRaisesRegex(AdmissionRejected, 'SIGNAL_REASSESSMENT_EXPIRED'):
            self.attempt(601)
        self.assertEqual(self.feed.calls, 1)

    def test_flow_recovery_still_requires_original_confluence_thresholds(self):
        self.reject_flow()
        self.feed.recovered()
        self.frame['open'] = self.frame['high'] = self.frame['low'] = self.frame['close'] = 100.
        with self.assertRaisesRegex(AdmissionRejected, 'CONFLUENCE_'):
            self.attempt(6)
        self.assertEqual(self.manager.store.get_intent(self.intent)['state'], 'REJECTED_CONFLUENCE')

    def test_different_candle_frame_cannot_qualify_original_signal(self):
        self.reject_flow()
        self.feed.recovered()
        self.frame['date'] -= timedelta(minutes=5)
        with self.assertRaisesRegex(AdmissionRejected, 'SIGNAL_CANDLE_MISMATCH'):
            self.attempt(6)
        self.assertEqual(self.feed.calls, 1)

    def test_candle_expiring_during_market_read_cannot_be_reserved(self):
        self.reject_flow()
        self.feed.recovered()
        def expire():
            self.now += 600
        self.feed.on_refresh = expire
        with patch('time.time', side_effect=lambda: self.now+6):
            with self.assertRaisesRegex(AdmissionRejected, 'SIGNAL_REASSESSMENT_EXPIRED'):
                self.manager.prepare_candidate(pair='ADA/USDT', enter_tag='3',
                    candle_date=self.candle, candles=self.frame)
        self.assertEqual(self.manager.store.get_intent(self.intent)['state'], 'REJECTED_FLOW')

    def test_repeated_weak_flow_is_journaled_without_duplicate_admission_event(self):
        self.reject_flow()
        with self.assertRaisesRegex(AdmissionRejected, 'BULLISH_FLOW_NOT_CONFIRMED'):
            self.attempt(6)
        events = [json.loads(line) for line in (Path(self.tmp.name)/'runtime/freqtrade_lifecycle_events.jsonl').read_text().splitlines()]
        self.assertEqual(sum(row['type'] == 'ADMISSION_REJECTED' for row in events), 1)
        checks = [row for row in events if row['type'] == 'FLOW_REASSESSMENT']
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0]['market']['taker_buy_ratio_60s'], '.30')

    def test_order_identity_alone_prevents_flow_readmission(self):
        self.reject_flow()
        self.manager.store.bind_order_identity(scope_id=self.manager.scope_id,
            intent_id=self.intent, generation=1, pair='ADA/USDT', role='WORKING',
            client_id='already-attempted')
        self.feed.recovered()
        with self.assertRaisesRegex(AdmissionRejected, 'SIGNAL_ALREADY_OWNED'):
            self.attempt(6)
        self.assertEqual(self.feed.calls, 1)

    def test_fill_receipt_alone_prevents_flow_readmission(self):
        self.reject_flow()
        self.manager.store.record_fill(fill_key='receipt', scope_id=self.manager.scope_id,
            intent_id=self.intent, pair='ADA/USDT', order_id='123', side='buy',
            base_qty='1', quote_qty='1', average_price='1', exchange_trade_id='9')
        self.feed.recovered()
        with self.assertRaisesRegex(AdmissionRejected, 'SIGNAL_ALREADY_OWNED'):
            self.attempt(6)

    def test_accepted_candidate_cannot_be_prepared_a_second_time(self):
        self.feed.recovered()
        self.attempt()
        with self.assertRaisesRegex(AdmissionRejected, 'SIGNAL_ALREADY_PREPARED'):
            self.attempt(6)

    def test_pair_not_ready_retry_rechecks_four_slot_limit(self):
        self.feed.snapshot = replace(self.feed.snapshot, state='UNKNOWN', reason='PAIR_NOT_READY')
        with self.assertRaisesRegex(AdmissionRejected, 'PAIR_NOT_READY'):
            self.attempt()
        self.occupy(4)
        self.feed.recovered()
        with self.assertRaisesRegex(AdmissionRejected, 'MAX_SLOTS_REACHED'):
            self.attempt(6)
        self.assertEqual(self.manager.store.occupied_count(), 4)

    def test_pair_not_ready_retry_rechecks_total_allocation(self):
        self.feed.snapshot = replace(self.feed.snapshot, state='UNKNOWN', reason='PAIR_NOT_READY')
        with self.assertRaisesRegex(AdmissionRejected, 'PAIR_NOT_READY'):
            self.attempt()
        self.occupy(3, '300')
        self.feed.recovered()
        with self.assertRaisesRegex(AdmissionRejected, 'ALLOCATION_LIMIT_EXCEEDED'):
            self.attempt(6)
        self.assertEqual(self.manager.store.occupied_nominal_usdt(), Decimal('900'))

    def test_reservation_rechecks_capacity_changed_during_market_read(self):
        self.feed.recovered()
        self.feed.on_refresh = lambda: self.occupy(4)
        with self.assertRaisesRegex(AdmissionRejected, 'MAX_SLOTS_REACHED'):
            self.attempt()
        self.assertNotEqual(self.manager.store.get_intent(self.intent)['state'], 'RESERVED')

    def test_reassessment_rejection_cannot_overwrite_concurrent_submission(self):
        self.reject_flow()
        owned = []

        def submit_while_refreshing():
            self.manager.store.put_generation(intent_id=self.intent, generation=1,
                pair='ADA/USDT', mode='FIXED_OCO', status='SUBMISSION_PENDING',
                ids={'working_client_id': 'persisted-before-post'}, expected_qty='250')
            self.manager.store.set_intent_state(self.intent, 'ENTRY_PENDING')
            owned.append(self.manager.store.get_intent(self.intent))

        self.feed.on_refresh = submit_while_refreshing
        with self.assertRaises(AdmissionRejected):
            self.attempt(6)
        self.assertEqual(self.manager.store.get_intent(self.intent), owned[0])
        self.assertEqual(self.manager.store.occupied_count(), 1)
        self.assertEqual(self.manager.store.latest_generation(self.intent)['status'],
                         'SUBMISSION_PENDING')

    def test_candle_expiring_while_waiting_for_reservation_lock_cannot_be_reserved(self):
        self.reject_flow()
        self.feed.recovered()
        original_tx = self.manager.store.tx

        @contextmanager
        def delayed_transaction():
            self.now += 300
            with original_tx() as connection:
                yield connection

        with patch.object(self.manager.store, 'tx', delayed_transaction), \
             patch('time.time', side_effect=lambda: self.now+6), \
             patch('freqtrade.binana.state_store.time', side_effect=lambda: self.now+6):
            with self.assertRaisesRegex(AdmissionRejected, 'SIGNAL_REASSESSMENT_EXPIRED'):
                self.manager.prepare_candidate(pair='ADA/USDT', enter_tag='3',
                    candle_date=self.candle, candles=self.frame)
        self.assertEqual(self.manager.store.get_intent(self.intent)['state'], 'REJECTED_FLOW')
        self.assertNotIn('ADA/USDT', self.manager.prepared)

    def test_unknown_state_cannot_become_retryable_after_weak_flow(self):
        self.reject_flow()
        self.manager.store.set_intent_state(self.intent, 'UNRECOGNIZED_STATE')
        before = self.manager.store.get_intent(self.intent)
        for seconds in (6, 12):
            if seconds == 12:
                self.feed.recovered()
            with self.assertRaisesRegex(AdmissionRejected, 'SIGNAL_STATE_REQUIRES_RECONCILIATION'):
                self.attempt(seconds)
            self.assertEqual(self.manager.store.get_intent(self.intent), before)
        self.assertEqual(self.feed.calls, 1)
        self.assertNotIn('ADA/USDT', self.manager.prepared)


if __name__ == '__main__':
    unittest.main()
