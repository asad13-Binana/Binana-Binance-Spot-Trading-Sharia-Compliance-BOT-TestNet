"""A terminal exchange list permits ID reuse; local ownership must prevent it.

The simulator owns an independent accepted-order ledger. It deliberately accepts
the same client IDs again after a completed list, as Binance documents. No
network, account or real trading operation is used.
"""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Lock
from types import SimpleNamespace
import sqlite3
import unittest

from freqtrade.binana.binance_order_lists import BinanceOrderLists
from freqtrade.binana.execution_manager import (
    AdmissionRejected, BinanaExecutionManager, PreparedAdmission,
)
from freqtrade.binana.state_store import StateStore


class CompletedListExchange:
    def __init__(self):
        self.accepted = []
        self.lock = Lock()
        self.entered = Event()
        self.release = Event()
        self.release.set()
        self.timeout_after_accept = False

    def publicGetExchangeInfo(self, params):
        return {'symbols': [{'symbol': params['symbol'], 'status': 'TRADING',
            'ocoAllowed': True, 'otoAllowed': True, 'filters': [
                {'filterType': 'PRICE_FILTER', 'minPrice': '.0001',
                 'maxPrice': '1000', 'tickSize': '.0001'},
                {'filterType': 'LOT_SIZE', 'minQty': '.01',
                 'maxQty': '1000000', 'stepSize': '.01'},
                {'filterType': 'NOTIONAL', 'minNotional': '.1',
                 'maxNotional': '1000000000'},
            ]}]}

    def privateGetMyFilters(self, params):
        return {'symbolFilters': [], 'exchangeFilters': [], 'assetFilters': []}

    def privateGetOpenOrders(self, params):
        return []

    def privateGetOpenOrderList(self):
        return []

    def privatePostOrderListOtoco(self, params):
        with self.lock:
            number = len(self.accepted) + 1
            self.accepted.append(dict(params))
        self.entered.set()
        if number == 1 and not self.release.wait(5):
            raise TimeoutError('simulated exchange wait exceeded')
        if self.timeout_after_accept:
            raise TimeoutError('accepted and completed before connection failed')
        return {'orderListId': number, 'listOrderStatus': 'ALL_DONE', 'orders': [
            {'orderId': number * 10, 'clientOrderId': params['workingClientOrderId']},
            {'orderId': number * 10 + 1, 'clientOrderId': params['pendingAboveClientOrderId']},
            {'orderId': number * 10 + 2, 'clientOrderId': params['pendingBelowClientOrderId']}],
            'orderReports': [{'orderId': number * 10,
                'clientOrderId': params['workingClientOrderId'], 'status': 'FILLED',
                'executedQty': params['workingQuantity'],
                'origQty': params['workingQuantity'], 'price': params['workingPrice'],
                'cummulativeQuoteQty': str(Decimal(params['workingQuantity']) *
                                          Decimal(params['workingPrice']))}]}

    def privateGetOrderList(self, params):
        raise TimeoutError('exchange read unavailable')


def coordinator(path, exchange):
    manager = BinanaExecutionManager.__new__(BinanaExecutionManager)
    manager.store = StateStore(path)
    manager.scope_id = 'binance-testnet|fixture|epoch'
    manager.config = {'binana': {'normal_stop_fraction': '.01',
        'defensive_stop_fraction': '.006', 'fixed_target_fraction': '.015',
        'stop_limit_buffer_fraction': '.0015'}}
    manager.order_lists = BinanceOrderLists(exchange)
    manager._quantity_step = lambda pair: Decimal('.01')
    manager._tick_size = lambda pair: Decimal('.0001')
    # Commission evidence is a separate admission prerequisite in this fixture.
    manager.entry_commission_reserve = lambda pair: Decimal('.002')
    manager.prepared = {'ADA/USDT': PreparedAdmission('intent', 'ADA/USDT', '3',
        'NORMAL', SimpleNamespace(), 'registry', Decimal('250'))}
    return manager


class EntrySubmissionOnce(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'state.sqlite'
        self.exchange = CompletedListExchange()
        self.manager = coordinator(self.path, self.exchange)
        self.manager.store.put_intent(intent_id='intent', pair='ADA/USDT', signal_id='3',
            halal_allowed=True, registry_sha256='registry', nominal_usdt='250', state='RESERVED')

    @staticmethod
    def submit(manager):
        return manager.submit_entry(pair='ADA/USDT', amount=10, rate=1, enter_tag='3')

    def test_completed_list_cannot_submit_same_unbound_intent_again(self):
        self.assertEqual(self.submit(self.manager)['status'], 'closed')
        try:
            self.submit(self.manager)
        except Exception as exc:
            rejection = exc
        else:
            rejection = None
        self.assertEqual(len(self.exchange.accepted), 1)
        self.assertIsInstance(rejection, AdmissionRejected)
        self.assertEqual(str(rejection), 'ENTRY_SUBMISSION_ALREADY_CLAIMED')
        self.assertEqual(self.manager.store.occupied_count(), 1)

    def test_separate_coordinators_race_for_one_durable_submission(self):
        other = coordinator(self.path, self.exchange)
        self.exchange.release.clear()
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(self.submit, self.manager)
            self.assertTrue(self.exchange.entered.wait(3))
            try:
                try:
                    self.submit(other)
                except Exception as exc:
                    rejection = exc
                else:
                    rejection = None
            finally:
                self.exchange.release.set()
            # Capture the first result after releasing its barrier even when the
            # buggy second call caused a durable identity conflict.
            try:
                first_result = first.result(timeout=5)
            except Exception:
                first_result = None
        self.assertEqual(len(self.exchange.accepted), 1)
        self.assertIsInstance(rejection, AdmissionRejected)
        self.assertEqual(str(rejection), 'ENTRY_SUBMISSION_ALREADY_CLAIMED')
        self.assertEqual(first_result['status'], 'closed')

    def test_accepted_timeout_never_authorizes_second_post(self):
        self.exchange.timeout_after_accept = True
        with self.assertRaisesRegex(AdmissionRejected, 'ENTRY_SUBMISSION_UNKNOWN'):
            self.submit(self.manager)
        try:
            self.submit(self.manager)
        except Exception as exc:
            rejection = exc
        else:
            rejection = None
        self.assertEqual(len(self.exchange.accepted), 1)
        self.assertIsInstance(rejection, AdmissionRejected)
        self.assertEqual(str(rejection), 'ENTRY_SUBMISSION_ALREADY_CLAIMED')
        self.assertTrue(self.manager.store.has_blockers())

    def test_interrupted_submission_claim_leaves_no_partial_generation(self):
        with self.manager.store._connect() as connection:
            connection.execute("""CREATE TRIGGER interrupt_entry_claim
                BEFORE UPDATE OF state ON intents WHEN NEW.state='ENTRY_PENDING'
                BEGIN SELECT RAISE(ABORT, 'simulated claim interruption'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.submit(self.manager)
        self.assertEqual(self.exchange.accepted, [])
        self.assertIsNone(self.manager.store.latest_generation('intent'))
        self.assertEqual(self.manager.store.get_intent('intent')['state'], 'RESERVED')

    def test_self_trade_prevention_expiry_is_terminal_in_direct_ack(self):
        response = {'orderReports': [{'orderId': 42, 'clientOrderId': 'owned',
                    'status': 'EXPIRED_IN_MATCH', 'executedQty': '0'}]}
        order = self.manager._working_ccxt_order(response, 'owned', 'ADA/USDT',
                                               Decimal('10'), Decimal('1'))
        self.assertEqual(order['status'], 'expired')


if __name__ == '__main__':
    unittest.main()
