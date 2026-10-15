"""Behavioral admission for the 2026.9 upstream/BINANA overlay merge."""
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from freqtrade.enums import CandleType, SignalDirection, TradingMode
from freqtrade.exceptions import OperationalException
from freqtrade.freqtradebot import FreqtradeBot
from freqtrade.persistence import Trade, init_db
from freqtrade.persistence.custom_data import _CustomData


class Upstream20269SpotAdmission(unittest.TestCase):
    def test_spot_cycle_never_fetches_positions_for_liquidation_warnings(self):
        bot = SimpleNamespace(config={}, trading_mode=TradingMode.SPOT)
        with patch.object(Trade, 'get_open_trades', side_effect=AssertionError('derivative query')):
            FreqtradeBot.check_liquidation_warnings(bot)

    def test_informative_candles_validate_support_before_runtime(self):
        bot = SimpleNamespace(
            strategy=SimpleNamespace(
                gather_informative_pairs=lambda: [('ADA/USDT', '1h', CandleType.SPOT)],
                get_strategy_name=lambda: 'BinanaNfiSpot',
            ),
            exchange=SimpleNamespace(name='Binance', check_candle_type_support=Mock(return_value=True)),
        )
        FreqtradeBot.validate_informative_candle_types(bot)
        bot.exchange.check_candle_type_support.assert_called_once_with(CandleType.SPOT)
        bot.exchange.check_candle_type_support.return_value = False
        with self.assertRaisesRegex(OperationalException, 'requests informative data of type spot'):
            FreqtradeBot.validate_informative_candle_types(bot)

    def test_depth_gate_counts_every_level_in_uneven_spot_order_books(self):
        bot = SimpleNamespace(exchange=SimpleNamespace(fetch_l2_order_book=Mock(return_value={
            'bids': [[1.0, 2.0], [0.99, 3.0], [0.98, 5.0]],
            'asks': [[1.01, 4.0]],
        })))
        self.assertTrue(FreqtradeBot._check_depth_of_market(
            bot, 'ADA/USDT', {'bids_to_ask_delta': 2.5}, SignalDirection.LONG))
        self.assertFalse(FreqtradeBot._check_depth_of_market(
            bot, 'ADA/USDT', {'bids_to_ask_delta': 2.6}, SignalDirection.LONG))

    def test_partial_cancel_includes_already_owned_position_in_minimum_stake(self):
        order = {'status': 'open', 'filled': 0.5}
        canceled = {'status': 'canceled', 'filled': 0.5}
        trade = SimpleNamespace(
            entry_side='buy', amount=250.0, open_rate=1.0, pair='ADA/USDT')
        order_obj = SimpleNamespace(order_id='17', ft_cancel_reason=None, ft_amount=1.0)
        bot = SimpleNamespace(
            exchange=SimpleNamespace(
                get_min_pair_stake_amount=Mock(return_value=10.0),
                cancel_order_with_result=Mock(return_value=canceled),
            ),
            strategy=SimpleNamespace(stoploss=-0.01, order_types={'entry': 'limit'}),
            update_trade_state=Mock(), wallets=SimpleNamespace(update=Mock()),
            _notify_enter_cancel=Mock(),
        )
        self.assertFalse(FreqtradeBot.handle_cancel_enter(bot, trade, order, order_obj, 'timeout'))
        bot.exchange.cancel_order_with_result.assert_called_once_with('17', 'ADA/USDT', 250.0)
        bot.update_trade_state.assert_called_once_with(trade, '17', canceled)


class Upstream20269CustomDataAdmission(unittest.TestCase):
    def setUp(self):
        init_db('sqlite://')

    def tearDown(self):
        engine = Trade.session.get_bind()
        Trade.session.remove()
        _CustomData.session.remove()
        engine.dispose()

    def test_key_deletion_preserves_binana_identity_and_other_trade_data(self):
        trades = [Trade(
            pair='ADA/USDT', exchange='binance', amount=250.0, stake_amount=250.0,
            open_rate=1.0, fee_open=0.0, fee_close=0.0,
            open_date=datetime.now(timezone.utc), trading_mode=TradingMode.SPOT,
        ) for _ in range(2)]
        Trade.session.add_all(trades)
        Trade.commit()
        for trade in trades:
            trade.set_custom_data('binana_entry_identity', {'intent_id': str(trade.id)})
            trade.set_custom_data('transient_upgrade_probe', {'ready': True})
        trades[0].delete_custom_data('transient_upgrade_probe')
        self.assertIsNone(trades[0].get_custom_data('transient_upgrade_probe'))
        self.assertEqual(trades[0].get_custom_data('binana_entry_identity'),
                         {'intent_id': str(trades[0].id)})
        self.assertEqual(trades[1].get_custom_data('transient_upgrade_probe'), {'ready': True})
        trades[0].delete_custom_data()
        self.assertEqual(trades[0].get_all_custom_data(), [])
        self.assertEqual(trades[1].get_custom_data('binana_entry_identity'),
                         {'intent_id': str(trades[1].id)})


if __name__ == '__main__':
    unittest.main()
