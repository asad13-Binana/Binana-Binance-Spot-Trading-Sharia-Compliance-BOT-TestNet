"""Offline regressions for market recovery and ambiguous native submissions."""
from decimal import Decimal
import unittest
from unittest.mock import Mock

import ccxt
from freqtrade.binana.binance_order_lists import BinanceOrderLists, OrderListUnknown
from freqtrade.binana.execution_manager import AdmissionRejected
from freqtrade.binana.protection_plan import provisional_plan
from binana_tests.test_engine_core import FakeApi
from binana_tests import test_operator_exit as operator_helpers
from binana_tests import test_entry_submission_once as entry_helpers


class MarketRecovery(unittest.TestCase):
    def setUp(self):
        self.fixture = operator_helpers.OperatorExitTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.fixture.seed()
        self.manager = self.fixture.manager
        self.api = self.manager.exchange._api
        self.api.create_order = Mock(return_value={'id': '900'})

    def prices(self, bid, reference, minimum='80'):
        self.fixture.filters[3].update(minNotional=minimum, maxNotional='200', avgPriceMins=5)
        self.api.publicGetTickerBookTicker = Mock(return_value={
            'symbol': 'ADAUSDT', 'bidPrice': bid, 'askPrice': bid})
        self.api.publicGetReferencePrice = Mock(return_value={
            'symbol': 'ADAUSDT', 'referencePrice': reference})
        self.api.publicGetAvgPrice = Mock(return_value={'mins': 5, 'price': bid})

    def reconcile(self):
        return self.manager.reconcile_trade(self.fixture.trade)

    def assert_blocked_without_dust(self):
        result = self.reconcile()
        self.assertFalse(result and result.get('dust_retained'))
        self.api.create_order.assert_not_called()
        self.assertTrue(self.manager.store.has_blockers())

    def test_operator_request_reaches_market_reference_validation(self):
        self.fixture.cancel({})
        self.prices('.5', '1', '8')
        self.fixture.request()
        self.assertIsNone(self.reconcile())
        self.api.privatePostOrder.assert_called_once()
        self.assertEqual(self.api.privatePostOrder.call_args.args[0]['quantity'], '9.99')

    def test_operator_invalid_reference_does_not_cancel_existing_protection(self):
        self.prices('9', '5')
        self.fixture.request()
        self.assert_blocked_without_dust()
        self.api.privateDeleteOrderList.assert_not_called()
        self.api.privatePostOrder.assert_not_called()

    def test_stop_uses_reference_when_bid_would_be_dust(self):
        self.fixture.cancel({})
        self.prices('5', '10')
        self.assertIsNone(self.reconcile())
        self.api.create_order.assert_called_once()
        self.assertEqual(self.api.create_order.call_args.args[3], 9.99)
        self.assertEqual(self.api.create_order.call_args.kwargs['params']['maxRetriesOnFailure'], 0)

    def test_stop_rejects_reference_below_minimum(self):
        self.fixture.cancel({})
        self.prices('9', '5')
        self.assert_blocked_without_dust()
        self.api.publicGetReferencePrice.assert_called()

    def test_stop_rejects_market_lot_maximum(self):
        self.fixture.cancel({})
        self.prices('9', '10')
        self.fixture.filters[2]['maxQty'] = '5'
        self.assert_blocked_without_dust()

    def test_stop_ignores_minimum_not_applied_to_market(self):
        self.fixture.cancel({})
        self.prices('5', None)
        self.fixture.filters[3].update(applyMinToMarket=False, applyMaxToMarket=False)
        self.assertIsNone(self.reconcile())
        self.api.create_order.assert_called_once()

    def test_stop_reference_timeout_never_becomes_dust(self):
        self.fixture.cancel({})
        self.prices('5', '10')
        self.api.publicGetReferencePrice.side_effect = TimeoutError('synthetic timeout')
        self.assert_blocked_without_dust()
        self.api.publicGetAvgPrice.assert_not_called()

    def test_stop_preserves_executable_fee_reserve(self):
        self.fixture.cancel({})
        self.prices('5', '10')
        for key in ('2', '3'):
            self.fixture.orders[key]['amount'] = 9.98
        self.assertIsNone(self.reconcile())
        self.api.create_order.assert_called_once()
        self.assertEqual(self.api.create_order.call_args.args[3], 9.98)

    def test_stop_uses_canonical_quantity_when_reserve_is_below_market_minimum(self):
        self.fixture.cancel({})
        self.prices('5', '10', '99.85')
        for key in ('2', '3'):
            self.fixture.orders[key]['amount'] = 9.98
        self.assertIsNone(self.reconcile())
        self.api.create_order.assert_called_once()
        self.assertEqual(self.api.create_order.call_args.args[3], 9.99)


class NativeSubmissionSafety(unittest.TestCase):
    def test_mutation_disables_configured_ccxt_retries(self):
        for kind in ('otoco', 'oco', 'cancel'):
            with self.subTest(kind=kind):
                api = ccxt.binance({'enableRateLimit': False, 'options': {'maxRetriesOnFailure': 1}})
                fixture = FakeApi()
                for name in ('publicGetExchangeInfo', 'publicGetAvgPrice', 'publicGetReferencePrice',
                             'privateGetMyFilters', 'publicGetTickerBookTicker', 'publicGetTickerPrice',
                             'privateGetOpenOrders', 'privateGetOpenOrderList'):
                    setattr(api, name, getattr(fixture, name))
                signed = []
                def sign(path, api_type='public', method='GET', params=None, headers=None, body=None):
                    signed.append(dict(params or {}))
                    return {'url': 'https://fixture.invalid', 'method': method, 'headers': {}, 'body': ''}
                api.sign = sign
                api.fetch = Mock(side_effect=ccxt.RequestTimeout('synthetic lost acknowledgment'))
                adapter = BinanceOrderLists(api)
                plan = provisional_plan(intent_id='retry', pair='ADA/USDT', quantity='100', entry_limit='1',
                    tick_size='.0001', target_fraction='.015', stop_fraction='.008',
                    stop_limit_buffer_fraction='.001')
                with self.assertRaises(OrderListUnknown):
                    if kind == 'otoco':
                        adapter.submit_fixed_otoco(plan)
                    elif kind == 'oco':
                        adapter.submit_oco(plan)
                    else:
                        adapter.cancel_list(symbol='ADA/USDT', list_client_id='synthetic-list')
                self.assertEqual(api.fetch.call_count, 1)
                self.assertNotIn('maxRetriesOnFailure', signed[0])

    def test_raw_transport_details_are_not_persisted(self):
        fixture = entry_helpers.EntrySubmissionOnce()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        submit_marker = 'SYNTHETIC_AUTH_SUBMIT'
        query_marker = 'SYNTHETIC_AUTH_QUERY'
        fixture.exchange.privatePostOrderListOtoco = Mock(side_effect=RuntimeError(
            'apiKey=' + submit_marker + '&signature=synthetic'))
        fixture.exchange.privateGetOrderList = Mock(side_effect=RuntimeError(
            'Authorization=' + query_marker))
        with self.assertRaisesRegex(AdmissionRejected, 'ENTRY_SUBMISSION_UNKNOWN'):
            fixture.submit(fixture.manager)
        self.assertEqual(fixture.manager.store.get_intent('intent')['state'], 'UNKNOWN')
        self.assertTrue(fixture.manager.store.has_blockers())
        detail = '\n'.join(row['detail'] for row in fixture.manager.store.unresolved())
        for marker in (submit_marker, query_marker, 'signature='):
            self.assertNotIn(marker, detail)
