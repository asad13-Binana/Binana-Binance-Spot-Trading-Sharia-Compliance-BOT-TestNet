from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock
import unittest

from freqtrade.binana.canonical_fees import FeeEvidenceError, receipt, merge_receipt
from freqtrade.binana.execution_manager import BinanaExecutionManager
from freqtrade.binana.state_store import StateStore
from freqtrade.enums import TradingMode
from freqtrade.freqtradebot import FreqtradeBot
from freqtrade.persistence import Trade, Order, init_db
from freqtrade.persistence.custom_data import _CustomData
from freqtrade.rpc.rpc import RPC


class ExactCommissions(unittest.TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory(); self.path=Path(self.tmp.name)
        init_db('sqlite:///'+str(self.path/'owner.sqlite'))
        self.manager=BinanaExecutionManager.__new__(BinanaExecutionManager)
        self.manager.store=StateStore(self.path/'extension.sqlite')
        self.manager.scope_id='testnet|account|epoch'; self.manager._trade_intent={}
        self.manager.store.put_intent(intent_id='owned',pair='ADA/USDT',signal_id='64',
            halal_allowed=True,registry_sha256='registry',nominal_usdt='1000',state='OPEN')
        self.fills={}
        def record(**kwargs):
            self.manager._verified_order_fills=self.fills[str(kwargs['order_id'])]
            return sum((Decimal(str(row['amount'])) for row in self.manager._verified_order_fills),Decimal(0))
        self.manager._record_order_trades=record
        self.manager.exchange=SimpleNamespace(_api=SimpleNamespace(fetch_open_orders=Mock(return_value=[])),
            check_order_canceled_empty=lambda order:order['status']=='canceled' and not order['filled'])
        self.bot=SimpleNamespace(binana=self.manager,exchange=self.manager.exchange,
            order_obj_or_raise=FreqtradeBot.order_obj_or_raise,
            _update_trade_after_fill=lambda trade,order,send_msg:trade,
            order_close_notify=Mock(),
            handle_order_fee=Mock(side_effect=AssertionError('Generic percentage fee path forbidden')))
        self.bot.order_obj_or_raise=lambda oid,obj:obj
        self.trade=None

    def tearDown(self):
        engine=Trade.session.get_bind()
        Trade.session.remove(); _CustomData.session.remove(); engine.dispose(); self.tmp.cleanup()

    def order(self, oid, side, qty, price, fee=0, currency='USDT', status='closed', amount=None):
        timestamp=1790160000000+int(oid)*1000
        value={'id':str(oid),'clientOrderId':'B-owned-'+str(oid),'symbol':'ADA/USDT','side':side,
            'type':'limit','status':status,'amount':float(amount if amount is not None else qty),
            'filled':float(qty),'price':float(price),'average':float(price),
            'cost':float(Decimal(str(qty))*Decimal(str(price))),
            'remaining':float(Decimal(str(amount if amount is not None else qty))-Decimal(str(qty))),
            'timestamp':timestamp,'lastTradeTimestamp':timestamp}
        self.fills[str(oid)]=[{'id':str(int(oid)*100),'order':str(oid),'symbol':'ADA/USDT','side':side,
            'amount':value['filled'],'cost':value['cost'],'timestamp':timestamp,
            'fee':{'currency':currency,'cost':fee}}] if qty else []
        self.manager.store.bind_order_identity(scope_id=self.manager.scope_id,intent_id='owned',
            generation=1,pair='ADA/USDT',role='WORKING' if side=='buy' else 'TAKE_PROFIT',
            client_id=value['clientOrderId'],order_id=str(oid))
        return value

    def enter(self, value):
        self.trade=Trade(pair='ADA/USDT',base_currency='ADA',stake_currency='USDT',
            amount=0,stake_amount=value['cost'],open_rate=value['price'],fee_open=.001,fee_close=.001,
            is_open=True,is_short=False,exchange='binance',leverage=1,trading_mode=TradingMode.SPOT,
            open_date=datetime.fromtimestamp(value['timestamp']/1000,timezone.utc),
            contract_size=1,amount_precision=.01,price_precision=.001,precision_mode=4,precision_mode_price=4)
        self.trade.orders.append(Order.parse_from_ccxt_object(value,'ADA/USDT','buy',value['amount'],value['price']))
        self.trade.set_binana_entry_identity({'scope_id':self.manager.scope_id,'intent_id':'owned',
            'order_id':value['id'],'client_id':value['clientOrderId'],'accounting_version':1})
        self.manager.stage_canonical_receipts(self.trade,value)
        self.trade.recalc_trade_from_orders()
        Trade.session.add(self.trade);Trade.commit()
        self.manager.store.bind_recovered_trade('owned',self.trade.id)
        return self.trade

    def update(self, value):
        if self.trade.select_order_by_order_id(value['id']) is None:
            self.trade.orders.append(Order.parse_from_ccxt_object(value,'ADA/USDT',value['side'],value['amount'],value['price']))
        FreqtradeBot.update_trade_state(self.bot,self.trade,value['id'],value,send_msg=False)

    def test_base_entry_fee_never_uses_faucet_and_preserves_full_quote_basis(self):
        trade=self.enter(self.order(1,'buy',10,10,.01,'ADA'))
        self.assertAlmostEqual(trade.amount,9.99)
        self.assertAlmostEqual(trade.stake_amount,100)
        self.assertAlmostEqual(trade.open_trade_value,100)
        self.assertEqual(trade.open_rate,10)
        self.assertEqual(trade.fee_open_cost,.01)
        self.assertAlmostEqual(trade.calc_profit_ratio(10),(99.9*.999-100)/100)

    def test_sell_base_commission_consumes_inventory_without_sale_proceeds(self):
        trade=self.enter(self.order(1,'buy',10,10))
        self.update(self.order(2,'sell',4,12,.01,'ADA'))
        self.assertAlmostEqual(trade.amount,5.99)
        self.assertAlmostEqual(trade.stake_amount,59.9)
        self.assertAlmostEqual(trade.realized_profit,7.9)

    def test_quote_commissions_sum_actual_fills_instead_of_averaging_rates(self):
        order=self.order(1,'buy',100,10)
        self.fills['1']=[dict(self.fills['1'][0],id='100',amount=10,cost=100,fee={'currency':'USDT','cost':.1}),
                         dict(self.fills['1'][0],id='101',amount=90,cost=900,fee={'currency':'USDT','cost':1.8})]
        trade=self.enter(order)
        self.assertAlmostEqual(trade.stake_amount,1001.9)
        self.assertAlmostEqual(trade.fee_open_cost,1.9)

    def test_two_partial_exits_replay_ten_times_and_restart_without_double_counting(self):
        trade=self.enter(self.order(1,'buy',10,10,.1,'USDT'))
        first=self.order(2,'sell',4,12,.048,'USDT',status='canceled',amount=10)
        self.update(first)
        self.assertTrue(trade.is_open)
        second=self.order(3,'sell',6,11,.066,'USDT')
        for _ in range(10):
            self.update(second)
            Trade.session.expire_all()
            self.assertFalse(trade.is_open)
            self.assertAlmostEqual(trade.realized_profit,13.786)
            self.assertAlmostEqual(trade.close_profit_abs,13.786)
            self.assertAlmostEqual(trade.calculate_profit(999).total_profit,13.786)
            self.assertAlmostEqual(trade.calculate_profit(999).profit_ratio,13.786/100.1)
        self.assertEqual(len(trade.orders),3)
        self.assertEqual(len(trade.binana_custom_data('binana_fill_receipts')['orders']),3)
        self.assertAlmostEqual(trade.binana_exit_profit('3').profit_abs,5.874)

    def test_open_partial_exit_updates_accounting_without_closing_exchange_order(self):
        trade=self.enter(self.order(1,'buy',10,10))
        order=self.order(2,'sell',4,11,.04,'USDT',status='open',amount=10)
        self.update(order)
        self.assertTrue(trade.is_open)
        self.assertTrue(trade.orders[-1].ft_is_open)
        self.assertAlmostEqual(trade.amount,6)
        self.assertAlmostEqual(trade.realized_profit,3.96)

    def test_unowned_exit_and_changed_fee_replay_are_rejected(self):
        trade=self.enter(self.order(1,'buy',10,10))
        foreign={**self.order(2,'sell',4,11),'id':'999','clientOrderId':'manual'}
        with self.assertRaisesRegex(FeeEvidenceError,'OWNERSHIP_UNVERIFIED'):
            self.manager.stage_canonical_receipts(trade,foreign)
        row=receipt('ADA/USDT',self.order(3,'sell',4,11),self.fills['3'])
        first=merge_receipt(None,self.manager.scope_id,'ADA/USDT',row)
        row['fills']['300']['base_fee']='.01'
        with self.assertRaisesRegex(FeeEvidenceError,'CHANGED_OR_DISAPPEARED'):
            merge_receipt(first,self.manager.scope_id,'ADA/USDT',row)

    def test_unverified_exchange_obligation_prevents_committed_close(self):
        trade=self.enter(self.order(1,'buy',10,10))
        self.manager.exchange._api.fetch_open_orders.return_value=[{'id':'unresolved-sibling'}]
        self.update(self.order(2,'sell',10,11))
        self.assertTrue(trade.is_open)
        self.assertEqual(trade.amount,10)
        self.assertTrue(self.manager.store.has_blockers())
        self.manager.exchange._api.fetch_open_orders.return_value=[]
        self.update(self.order(2,'sell',10,11))
        self.assertFalse(trade.is_open)

    def test_base_fee_dust_is_not_quantized_out_of_owned_inventory(self):
        trade=self.enter(self.order(1,'buy',10,10,.001,'ADA'))
        self.update(self.order(2,'sell',9.99,10))
        self.assertTrue(trade.is_open)
        self.assertAlmostEqual(trade.amount,.009)
        self.assertGreater(trade.stake_amount,0)

    def test_performance_uses_cash_invested_including_quote_entry_commission(self):
        self.enter(self.order(1,'buy',10,10,1,'USDT'))
        self.update(self.order(2,'sell',10,10.9))
        performance=Trade.get_overall_performance()
        self.assertEqual(len(performance),1)
        self.assertAlmostEqual(performance[0]['profit_abs'],8)
        self.assertAlmostEqual(performance[0]['profit_ratio'],8/101)

    def test_partial_child_never_enters_native_timeout_or_replace_paths(self):
        trade=self.enter(self.order(1,'buy',10,10))
        empty=self.order(2,'sell',0,11,status='open',amount=10)
        trade.orders.append(Order.parse_from_ccxt_object(empty,'ADA/USDT','sell',10,11))
        Trade.commit()
        partial=self.order(2,'sell',4,11,.04,'USDT',status='open',amount=10)
        self.bot.exchange.fetch_order=Mock(return_value=partial)
        self.bot.strategy=SimpleNamespace(ft_check_timed_out=Mock(return_value=True))
        self.bot.handle_cancel_order=Mock(); self.bot.replace_order=Mock()
        self.bot.update_trade_state=lambda *args:FreqtradeBot.update_trade_state(self.bot,*args)
        valid=self.fills['2'][0].pop('fee')
        FreqtradeBot.manage_open_orders(self.bot)
        self.assertEqual(trade.amount,10)
        self.assertTrue(self.manager.store.has_blockers())
        self.fills['2'][0]['fee']=valid
        FreqtradeBot.manage_open_orders(self.bot)
        self.assertEqual(trade.amount,6)
        self.bot.strategy.ft_check_timed_out.assert_not_called()
        self.bot.handle_cancel_order.assert_not_called()
        self.bot.replace_order.assert_not_called()

    def test_entry_notification_reports_net_acquisition_and_initial_entry(self):
        trade=self.enter(self.order(1,'buy',10,10,.01,'ADA'))
        bot=SimpleNamespace(exchange=SimpleNamespace(get_rate=lambda *a,**k:10,
            get_pair_base_currency=lambda pair:'ADA',get_pair_quote_currency=lambda pair:'USDT'),
            config={'stake_currency':'USDT'},rpc=SimpleNamespace(send_msg=Mock()))
        bot._notify_enter=lambda *a,**k:FreqtradeBot._notify_enter(bot,*a,**k)
        FreqtradeBot.order_close_notify(bot,trade,trade.orders[0],False,True)
        message=bot.rpc.send_msg.call_args.args[0]
        self.assertAlmostEqual(message['amount'],9.99)
        self.assertEqual(message['stake_amount'],100)
        self.assertFalse(message['sub_trade'])

    def test_profit_statistics_use_total_realized_and_remaining_return(self):
        trade=self.enter(self.order(1,'buy',10,10))
        trade.fee_close=0
        self.update(self.order(2,'sell',9,12))
        rpc=SimpleNamespace(_freqtrade=SimpleNamespace(exchange=SimpleNamespace(get_rate=lambda *a,**k:10)))
        result=RPC._collect_trade_statistics_data(rpc,[trade],'USDT','')
        self.assertAlmostEqual(result['profit_all_coin'][0],18)
        self.assertAlmostEqual(result['profit_all_ratio'][0],.18)
