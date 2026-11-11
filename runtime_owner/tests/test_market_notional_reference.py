"""Exchange filter prices for operator exits; no order or network client exists."""
from decimal import Decimal
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from freqtrade.binana.binance_order_lists import BinanceOrderLists
from freqtrade.binana.operator_exit import OperatorExit
from freqtrade.binana.recovery_contract import RecoveryBlocked


class MarketNotionalReference(unittest.TestCase):
    def fixture(self, reference='100', average='100', last='100', minutes=5):
        self.public = SimpleNamespace(
            publicGetReferencePrice=Mock(return_value={
                'symbol': 'ADAUSDT', 'referencePrice': reference}),
            publicGetAvgPrice=Mock(return_value={'mins': minutes, 'price': average}),
            publicGetTickerPrice=Mock(return_value={'symbol': 'ADAUSDT', 'price': last}),
        )
        owner = OperatorExit()
        owner.order_lists = BinanceOrderLists(self.public)
        self.rules = [
            {'filterType': name, 'stepSize': '.01', 'minQty': '.01', 'maxQty': '100'}
            for name in ('LOT_SIZE', 'MARKET_LOT_SIZE')
        ] + [{'filterType': 'NOTIONAL', 'minNotional': '8', 'maxNotional': '15',
              'applyMinToMarket': True, 'applyMaxToMarket': True, 'avgPriceMins': 5}]
        return lambda: owner._operator_market_quantity(
            'ADA/USDT', Decimal('.105'), self.rules, Decimal(last))

    def test_nonnull_reference_rejects_below_minimum_without_average(self):
        for kind in ('MIN_NOTIONAL', 'NOTIONAL'):
            with self.subTest(kind=kind):
                quantity = self.fixture(reference='50')
                self.rules[-1].update(filterType=kind, applyToMarket=True)
                with self.assertRaisesRegex(RecoveryBlocked, 'OPERATOR_BELOW_MIN_NOTIONAL'):
                    quantity()
                self.public.publicGetAvgPrice.assert_not_called()

    def test_nonnull_reference_rejects_above_maximum(self):
        quantity = self.fixture(reference='200')
        with self.assertRaisesRegex(RecoveryBlocked, 'OPERATOR_ABOVE_MAX_NOTIONAL'):
            quantity()
        self.public.publicGetAvgPrice.assert_not_called()

    def test_nonnull_reference_is_not_overridden_by_average_or_last_price(self):
        for average, last in (('50', '100'), ('100', '50'), ('100', '200')):
            with self.subTest(average=average, last=last):
                quantity = self.fixture(average=average, last=last)
                self.assertEqual(quantity(), Decimal('.10'))
                self.public.publicGetAvgPrice.assert_not_called()
                self.public.publicGetTickerPrice.assert_not_called()

    def test_null_reference_uses_the_verified_average_window(self):
        quantity = self.fixture(reference=None)
        self.assertEqual(quantity(), Decimal('.10'))
        self.public.publicGetReferencePrice.assert_called_once_with({'symbol': 'ADAUSDT'})
        self.public.publicGetAvgPrice.assert_called_once_with({'symbol': 'ADAUSDT'})

    def test_null_reference_with_zero_window_uses_verified_last_price(self):
        quantity = self.fixture(reference=None, average='50')
        self.rules[-1]['avgPriceMins'] = 0
        self.assertEqual(quantity(), Decimal('.10'))
        self.public.publicGetTickerPrice.assert_called_once_with({'symbol': 'ADAUSDT'})
        self.public.publicGetAvgPrice.assert_not_called()

    def test_wrong_average_window_blocks(self):
        quantity = self.fixture(reference=None, minutes=1)
        with self.assertRaisesRegex(RecoveryBlocked, 'OPERATOR_NOTIONAL_REFERENCE_UNVERIFIED'):
            quantity()

    def test_reference_timeout_is_not_permission_to_use_an_average(self):
        quantity = self.fixture()
        self.public.publicGetReferencePrice.side_effect = TimeoutError('synthetic timeout')
        with self.assertRaisesRegex(RecoveryBlocked, 'OPERATOR_NOTIONAL_REFERENCE_UNVERIFIED'):
            quantity()
        self.public.publicGetAvgPrice.assert_not_called()

    def test_invalid_reference_identity_or_value_blocks_without_fallback(self):
        for response in ({'symbol': 'OTHER', 'referencePrice': '100'},
                         {'symbol': 'ADAUSDT', 'referencePrice': '0'},
                         {'symbol': 'ADAUSDT', 'referencePrice': 'NaN'}):
            with self.subTest(response=response):
                quantity = self.fixture()
                self.public.publicGetReferencePrice.return_value = response
                with self.assertRaisesRegex(RecoveryBlocked, 'OPERATOR_NOTIONAL_REFERENCE_UNVERIFIED'):
                    quantity()
                self.public.publicGetAvgPrice.assert_not_called()

    def test_filters_not_applied_to_market_need_no_reference_request(self):
        quantity = self.fixture()
        self.rules[-1].update(applyMinToMarket=False, applyMaxToMarket=False)
        self.assertEqual(quantity(), Decimal('.10'))
        self.public.publicGetReferencePrice.assert_not_called()
        self.public.publicGetAvgPrice.assert_not_called()

    def test_two_market_filters_share_one_reference_snapshot(self):
        quantity = self.fixture()
        self.rules.append({'filterType': 'MIN_NOTIONAL', 'minNotional': '8',
                           'applyToMarket': True, 'avgPriceMins': 3})
        self.assertEqual(quantity(), Decimal('.10'))
        self.public.publicGetReferencePrice.assert_called_once_with({'symbol': 'ADAUSDT'})
        self.public.publicGetAvgPrice.assert_not_called()
