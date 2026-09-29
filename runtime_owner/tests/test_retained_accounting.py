from datetime import datetime, timezone
from unittest.mock import patch
import unittest
from freqtrade.enums import TradingMode
from freqtrade.persistence import Trade, Order


def position(exits):
    trade=Trade(id=1,pair='ADA/USDT',base_currency='ADA',stake_currency='USDT',
        amount=10,stake_amount=10,open_rate=1,fee_open=0,fee_close=0,
        open_date=datetime.now(timezone.utc),is_open=True,is_short=False,
        leverage=1,trading_mode=TradingMode.SPOT,contract_size=1,
        amount_precision=0.01,price_precision=0.001,precision_mode=4,precision_mode_price=4)
    for index,(side,amount,price) in enumerate([('buy',10,1)]+exits):
        trade.orders.append(Order(order_id=str(index),ft_order_side=side,
            ft_pair='ADA/USDT',ft_is_open=False,status='closed',filled=amount,
            amount=amount,remaining=0,price=price,average=price,cost=amount*price,
            order_filled_date=datetime.now(timezone.utc),
            ft_amount=amount,ft_price=price,order_type='limit'))
    return trade


class RetainedAccounting(unittest.TestCase):
    def test_closed_retained_inventory_preserves_cumulative_realized_profit(self):
        trade=position([('sell',4,1.1),('sell',5.9,1.2)])
        with patch.object(Trade,'get_custom_data',return_value={'phase':'retained'}):
            trade.close(1.2,show_msg=False)
        self.assertAlmostEqual(trade.amount,0.1)
        self.assertAlmostEqual(trade.stake_amount,0.1)
        self.assertAlmostEqual(trade.realized_profit,1.58)
        self.assertAlmostEqual(trade.close_profit_abs,1.58)

    def test_zero_net_exit_replay_clears_previous_realized_value(self):
        trade=position([('sell',5,1.1),('sell',5,0.9)])
        trade.realized_profit=123
        trade.recalc_trade_from_orders(is_closing=True)
        self.assertAlmostEqual(trade.realized_profit,0)
        self.assertAlmostEqual(trade.close_profit_abs,0)
