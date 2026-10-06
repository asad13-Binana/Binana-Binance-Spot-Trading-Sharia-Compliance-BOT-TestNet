"""Repeated admission must not rewrite an existing order-owner lifecycle."""
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock
import unittest

from freqtrade.binana.execution_manager import AdmissionRejected, BinanaExecutionManager
from freqtrade.binana.state_store import StateStore


class OwnedSignalReplay(unittest.TestCase):
    def manager(self, directory):
        manager = object.__new__(BinanaExecutionManager)
        manager.store = StateStore(Path(directory) / 'state.sqlite')
        manager._intent_id = Mock(return_value='existing')
        manager._requested_nominal_usdt = Mock(return_value=Decimal('250'))
        manager._capability_check = Mock()
        manager.market_data = Mock()
        manager.store.put_intent(intent_id='existing', pair='ADA/USDT', signal_id='signal',
                                 halal_allowed=True, registry_sha256='registry',
                                 nominal_usdt='250', state='OPEN')
        return manager

    def assert_replay_preserves(self, manager, reason="SIGNAL_ALREADY_OWNED"):
        before = manager.store.get_intent('existing')
        with self.assertRaisesRegex(AdmissionRejected, reason):
            manager._prepare_candidate(pair='ADA/USDT', enter_tag='signal', candle_date='same-candle')
        self.assertEqual(manager.store.get_intent('existing'), before)
        manager._capability_check.assert_not_called()
        manager.market_data.refresh.assert_not_called()

    def test_bound_trade_is_never_readmitted(self):
        for state in ['OPEN', 'ENTRY_PARTIAL', 'EXIT_PENDING', 'UNKNOWN']:
            with self.subTest(state=state), TemporaryDirectory() as directory:
                manager = self.manager(directory)
                manager.store.set_trade_id('existing', 34)
                manager.store.set_intent_state('existing', state)
                reason = "RECONCILIATION_BLOCKER_OPEN" if state in {"ENTRY_PARTIAL", "UNKNOWN"} else "SIGNAL_ALREADY_OWNED"
                self.assert_replay_preserves(manager, reason)

    def test_unbound_order_generation_requires_reconciliation(self):
        with TemporaryDirectory() as directory:
            manager = self.manager(directory)
            manager.store.set_intent_state('existing', 'RESERVED')
            manager.store.put_generation(intent_id='existing', generation=1, pair='ADA/USDT',
                                         mode='FIXED_OCO', status='SUBMITTING',
                                         ids={'list_client_id':'known'}, expected_qty='20', payload={})
            self.assert_replay_preserves(manager)
