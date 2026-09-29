"""Real canonical SQLite closes followed by read-only exchange acknowledgment."""
import copy
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from freqtrade.persistence import Trade
from binana_tests import test_exact_commissions as helpers


class ClosedAcknowledgmentRecovery(unittest.TestCase):
    setUp = helpers.ExactCommissions.setUp
    tearDown = helpers.ExactCommissions.tearDown
    enter = helpers.ExactCommissions.enter
    update = helpers.ExactCommissions.update

    def seed(self, *, remainder=False, missing_list=False, final_generation=2):
        self.final_generation = final_generation
        entry = self.order(1, 'buy', 10, 10, .01, 'ADA')
        self.enter(entry)
        partial = self.order(2, 'sell', 4, 12, .04, 'USDT', 'canceled', 9.99)
        self.update(partial)
        final = self.order(3, 'sell', 5.98 if remainder else 5.99, 11, .06, 'USDT')
        self.update(final)
        if remainder:
            # Adversarial legacy row in this disposable test DB. The production
            # close method correctly refuses to create this inconsistent state.
            self.trade.is_open = False
            Trade.commit()
        self.assertFalse(self.trade.is_open)
        self.assertGreater(self.trade.amount, 0)  # Freqtrade retains the historical amount.
        canceled = dict(final, id='4', clientOrderId='B-owned-4', amount=9.99,
                        filled=0, cost=0, remaining=9.99, status='canceled')
        self.manager.store.bind_order_identity(scope_id=self.manager.scope_id, intent_id='owned',
            generation=1, pair='ADA/USDT', role='STOP', client_id='B-owned-4', order_id='4')
        self.manager.store.put_generation(intent_id='owned', generation=1, pair='ADA/USDT',
            mode='FIXED_OCO', status='PARTIAL_EXIT', expected_qty='9.99',
            ids={'order_list_id':None if missing_list else '500','list_client_id':'list-500',
                 'working_order_id':'1','working_client_id':'B-owned-1',
                 'tp_order_id':'2','tp_client_id':'B-owned-2',
                 'sl_order_id':'4','sl_client_id':'B-owned-4'})
        self.manager.store.put_generation(intent_id='owned', generation=final_generation, pair='ADA/USDT',
            mode='TARGET_EXIT', status='SUBMITTED_UNVERIFIED', expected_qty=str(final['amount']),
            ids={'tp_order_id':'3','tp_client_id':'B-owned-3'})
        self.orders = {row['id']: row for row in (entry, partial, final, canceled)}
        self.manager.exchange.fetch_order = lambda oid, pair: copy.deepcopy(self.orders[str(oid)])
        self.manager.exchange._api.create_order = Mock(side_effect=AssertionError('No order submission'))
        self.manager.exchange._api.cancel_order = Mock(side_effect=AssertionError('No cancellation'))
        self.manager.order_lists = SimpleNamespace(query_list=Mock(return_value={
            'orderListId':500,'listClientOrderId':'list-500','symbol':'ADAUSDT','listOrderStatus':'ALL_DONE'}))
        self.manager.store.set_intent_state('owned', 'UNKNOWN')
        for identifier in ('recovery-owned', 'canonical-fees-owned', f'canonical-trade-{self.trade.id}'):
            self.manager.store.incident(incident_id=identifier, intent_id='owned', pair='ADA/USDT',
                                        code='TEST_ACK_PENDING', detail='crash after canonical commit')
        self.manager.store.incident(incident_id='unrelated-audit', code='UNRELATED', detail='keep open')
        self.before = self.snapshot()
        self.bot.order_close_notify.reset_mock()

    def order(self, *args, **kwargs):
        original = self.manager.store.bind_order_identity
        def bind(**values):
            if values['order_id'] == '3':
                values['generation'] = getattr(self, 'final_generation', 2)
            return original(**values)
        self.manager.store.bind_order_identity = bind
        try:
            return helpers.ExactCommissions.order(self, *args, **kwargs)
        finally:
            self.manager.store.bind_order_identity = original

    def snapshot(self):
        with sqlite3.connect(self.path/'owner.sqlite') as connection:
            return {table: connection.execute(f'SELECT * FROM {table} ORDER BY id').fetchall()
                    for table in ('trades', 'orders', 'trade_custom_data')}

    def sweep(self):
        result = self.manager.recover_closed_acknowledgments()
        self.assertEqual(self.before, self.snapshot())
        self.manager.exchange._api.create_order.assert_not_called()
        self.manager.exchange._api.cancel_order.assert_not_called()
        self.bot.order_close_notify.assert_not_called()
        return result

    def test_restart_after_close_acknowledges_once_without_canonical_changes(self):
        self.seed()
        Trade.session.remove()
        self.manager._trade_intent = {}
        self.assertTrue(self.sweep())
        self.assertEqual(self.manager.store.get_intent('owned')['state'], 'EXIT_FILLED')
        self.assertEqual([row['incident_id'] for row in self.manager.store.unresolved()], ['unrelated-audit'])
        self.manager.exchange.fetch_order = Mock(side_effect=AssertionError('Already acknowledged'))
        self.assertTrue(self.sweep())
        self.manager.exchange.fetch_order.assert_not_called()

    def absent_generation(self):
        from time import time
        self.seed(final_generation=3)
        self.manager.config = {'binana': {'environment': 'testnet'}}
        self.manager.store.put_generation(intent_id='owned', generation=2, pair='ADA/USDT',
            mode='FIXED_OCO', status='SUBMISSION_ABSENT', expected_qty='5.99',
            ids={'list_client_id': 'absent-list', 'tp_client_id': 'absent-tp', 'sl_client_id': 'absent-sl'},
            payload={'verified_absent_at': time(), 'list_client_id': 'absent-list'})
        original_query = self.manager.order_lists.query_list
        def query(**kwargs):
            if kwargs.get('list_client_id') == 'absent-list':
                raise RuntimeError('binance {"code":-2018,"msg":"Order list does not exist."}')
            return original_query(**kwargs)
        self.manager.order_lists.query_list = query
        self.manager.exchange._api.privateGetOrder = Mock(side_effect=RuntimeError(
            'binance {"code":-2013,"msg":"Order does not exist."}'))

    def test_closed_ack_revalidates_persisted_replacement_absence(self):
        self.absent_generation()
        self.assertTrue(self.sweep())
        self.assertEqual(self.manager.exchange._api.privateGetOrder.call_count, 2)
        self.assertEqual(self.manager.store.get_intent('owned')['state'], 'EXIT_FILLED')

    def test_absent_label_cannot_hide_existing_child_or_network_error(self):
        self.absent_generation()
        probe = self.manager.exchange._api.privateGetOrder
        for response, error in (({'orderId': 999}, None), (None, TimeoutError('timeout')),
                                (None, RuntimeError('{"code":-2015,"msg":"key rejected"}'))):
            with self.subTest(error=error, response=response):
                probe.side_effect, probe.return_value = error, response
                self.assertFalse(self.sweep())
                self.assertEqual(self.manager.store.get_intent('owned')['state'], 'UNKNOWN')

    def test_exit_filled_with_unacknowledged_incident_is_recovered(self):
        self.seed()
        self.manager.store.set_intent_state('owned', 'EXIT_FILLED')
        self.assertTrue(self.sweep())
        self.assertEqual(len(self.manager.store.unresolved()), 1)

    def test_missing_execution_stays_blocked(self):
        self.seed(); self.fills['3'] = []
        self.assertFalse(self.sweep())
        self.assertEqual(self.manager.store.get_intent('owned')['state'], 'UNKNOWN')

    def test_changed_commission_stays_blocked(self):
        self.seed(); self.fills['3'][0]['fee']['cost'] = .07
        self.assertFalse(self.sweep())

    def test_foreign_scope_stays_blocked(self):
        self.seed(); self.manager.scope_id = 'other-account'
        self.assertFalse(self.sweep())

    def test_reused_order_id_with_foreign_client_stays_blocked(self):
        self.seed(); self.orders['3']['clientOrderId'] = 'foreign-client'
        self.assertFalse(self.sweep())

    def test_retained_history_is_not_acknowledged_as_fully_sold(self):
        self.seed(remainder=True)
        self.assertFalse(self.sweep())

    def test_live_sibling_and_unresolved_list_stay_blocked(self):
        self.seed(); self.orders['4']['status'] = 'open'
        self.assertFalse(self.sweep())
        self.orders['4']['status'] = 'canceled'
        self.manager.order_lists.query_list.return_value['listOrderStatus'] = 'EXECUTING'
        self.assertFalse(self.sweep())

    def test_foreign_open_order_is_left_untouched(self):
        self.seed(); self.manager.exchange._api.fetch_open_orders.return_value = [{'id':'foreign'}]
        self.assertFalse(self.sweep())

    def test_unresolved_submission_is_not_acknowledged(self):
        self.seed()
        self.manager.store.put_generation(intent_id='owned', generation=3, pair='ADA/USDT',
            mode='FIXED_OCO', status='SUBMISSION_PENDING', expected_qty='1',
            ids={'list_client_id':'pending-list','tp_client_id':'pending-tp','sl_client_id':'pending-sl'})
        self.assertFalse(self.sweep())

    def test_unavailable_history_is_not_terminal_proof(self):
        self.seed(); self.manager.exchange.fetch_order = Mock(side_effect=RuntimeError('history unavailable'))
        self.assertFalse(self.sweep())

    def test_uncommitted_canonical_changes_are_not_used_as_proof(self):
        self.seed()
        # Only the disposable owner session sees this uncommitted edit.
        self.trade.realized_profit = 999
        self.assertTrue(self.sweep())
        Trade.session.rollback()

    def test_missing_oco_list_identity_stays_blocked(self):
        self.seed(missing_list=True)
        self.assertFalse(self.sweep())

    def test_missing_canonical_trade_creates_blocker(self):
        self.seed()
        self.manager.store.put_intent(intent_id='missing', pair='ADA/USDT', signal_id='x',
            halal_allowed=True, registry_sha256='h', nominal_usdt='250', state='UNKNOWN')
        self.manager.store.set_trade_id('missing', 999)
        self.assertFalse(self.sweep())
        self.assertIn('closed-ack-missing', [row['incident_id'] for row in self.manager.store.unresolved()])

    def test_failure_during_finalization_rolls_back_intent_and_incidents(self):
        self.seed()
        with self.manager.store.tx() as connection:
            connection.execute("CREATE TRIGGER fail_ack BEFORE UPDATE ON incidents "
                "WHEN NEW.status='CLOSED' BEGIN SELECT RAISE(ABORT,'injected ack crash'); END")
        self.assertFalse(self.sweep())
        self.assertEqual(self.manager.store.get_intent('owned')['state'], 'UNKNOWN')
        self.assertTrue(any(row['incident_id'] == 'recovery-owned' for row in self.manager.store.unresolved()))
        with self.manager.store.tx() as connection: connection.execute('DROP TRIGGER fail_ack')
        self.assertTrue(self.sweep())


if __name__ == '__main__':
    unittest.main()
