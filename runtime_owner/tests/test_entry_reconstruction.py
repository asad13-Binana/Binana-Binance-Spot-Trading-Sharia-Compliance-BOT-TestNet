from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from freqtrade.binana.execution_manager import BinanaExecutionManager
from freqtrade.binana.binance_order_lists import BinanceOrderLists
from freqtrade.binana.state_store import StateStore
from freqtrade.enums import TradingMode
from freqtrade.freqtradebot import FreqtradeBot
from freqtrade.persistence import Trade, Order, init_db
from freqtrade.persistence.custom_data import _CustomData


class EntryReconstruction(unittest.TestCase):
    def setUp(self):
        self.directory=TemporaryDirectory()
        self.root=Path(self.directory.name)
        self.order={'id':'10','clientOrderId':'BW-working','symbol':'ADA/USDT','side':'buy',
                    'status':'closed','type':'limit','amount':250.0,'filled':250.0,'remaining':0,
                    'cost':250.0,'price':1.0,'average':1.0,'timestamp':1789850000000,
                    'datetime':'2026-09-19T22:00:00Z','info':{'timeInForce':'FOK'}}
        self.response={'orderListId':7,'listClientOrderId':'BL-list','orders':[
            {'orderId':10,'clientOrderId':'BW-working'},
            {'orderId':11,'clientOrderId':'BT-tp'},{'orderId':12,'clientOrderId':'BS-sl'}]}
        self.store=StateStore(self.root/'extension.sqlite')
        self.store.put_intent(intent_id='intent',pair='ADA/USDT',signal_id='64',
            halal_allowed=True,registry_sha256='registry-hash',nominal_usdt='250',state='ENTRY_PENDING')
        self.store.put_generation(intent_id='intent',generation=1,pair='ADA/USDT',
            mode='FIXED_OCO',status='SUBMISSION_PENDING',expected_qty='250',
            ids={'list_client_id':'BL-list','working_client_id':'BW-working',
                 'tp_client_id':'BT-tp','sl_client_id':'BS-sl'},payload={'plan':{'quantity':'250'}})
        self.manager=self.fresh_manager()

    def tearDown(self):
        for model in (Trade,_CustomData):
            if hasattr(model,'session'):
                model.session.remove()
        self.directory.cleanup()

    def fresh_manager(self):
        manager=BinanaExecutionManager.__new__(BinanaExecutionManager)
        manager.store=self.store;manager.scope_id='testnet|account|epoch';manager._trade_intent={}
        manager.exchange=SimpleNamespace(fetch_order=Mock(side_effect=lambda oid,pair:dict(self.order)),
            _api=SimpleNamespace(fetch_open_orders=Mock(return_value=[])),id='binance',
            get_fee=lambda **kw:0)
        manager.order_lists=SimpleNamespace(query_list=Mock(side_effect=lambda **kw:self.response),
            _extract_ids=BinanceOrderLists._extract_ids)
        manager._quantity_step=lambda pair:Decimal('0.01')
        manager._tick_size=lambda pair:Decimal('0.0001')
        def record(**kwargs):
            manager._verified_order_fills = ([{'id':'100','order':self.order['id'],
                'symbol':'ADA/USDT','side':'buy','amount':self.order['filled'],'cost':self.order['cost'],
                'timestamp':self.order['timestamp'],'fee':{'currency':'USDT','cost':0}}]
                if self.order['filled'] else [])
            return Decimal(str(self.order['filled']))
        manager._record_order_trades=Mock(side_effect=record)
        return manager

    def test_restart_after_unknown_post_recovers_without_resubmission(self):
        self.store.set_intent_state('intent','UNKNOWN')
        self.assertTrue(self.store.has_blockers())
        evidence=self.manager.recover_unbound_entries()
        self.assertEqual(len(evidence),1)
        self.assertEqual(evidence[0]['order']['id'],'10')
        self.assertTrue(self.store.has_blockers(), 'Until canonical Trade commits, entries remain blocked')
        self.manager.order_lists.query_list.assert_called_once_with(list_client_id='BL-list')

    def test_terminal_fok_no_fill_releases_slot_only_without_open_obligations(self):
        self.order.update(status='expired',filled=0,cost=0,remaining=250)
        self.response['listOrderStatus']='ALL_DONE'
        self.manager.exchange._api.fetch_open_orders.return_value=[{'orderId':11}]
        self.assertEqual(self.manager.recover_unbound_entries(),[])
        self.assertTrue(self.store.has_blockers())
        self.manager.exchange._api.fetch_open_orders.return_value=[]
        self.assertEqual(self.manager.recover_unbound_entries(),[])
        self.assertFalse(self.store.has_blockers())
        self.assertEqual(self.store.occupied_count(),0)

    def test_unknown_missing_order_or_wrong_client_never_reconstructs(self):
        self.manager.order_lists.query_list.side_effect=TimeoutError('exchange unavailable')
        self.assertEqual(self.manager.recover_unbound_entries(),[])
        self.assertTrue(self.store.has_blockers())
        self.manager=self.fresh_manager()
        self.order['clientOrderId']='foreign-order'
        self.assertEqual(self.manager.recover_unbound_entries(),[])
        self.assertTrue(self.store.has_blockers())

    def test_pending_fok_response_never_creates_trade_before_expiry_recovery(self):
        init_db('sqlite:///'+str(self.root/'owner.sqlite'))
        self.order.update(status='open',filled=0,cost=0,remaining=250)
        self.manager.prepared={'ADA/USDT':SimpleNamespace(intent_id='intent')}
        self.manager.entry_parameters=lambda *args:(1,.01,.0001)
        self.manager.assert_release_ready=Mock()
        self.manager.submit_entry=Mock(side_effect=lambda **kwargs:dict(self.order))
        bot=SimpleNamespace(binana=self.manager,exchange=self.manager.exchange,
            get_valid_enter_price_and_stake=lambda *args:(1,250,1),
            strategy=SimpleNamespace(order_time_in_force={'entry':'FOK'},order_types={'entry':'limit'},
                confirm_trade_entry=lambda **kwargs:True))
        self.assertFalse(FreqtradeBot.execute_entry(bot,'ADA/USDT',250,enter_tag='64'))
        self.assertEqual(Trade.get_trades_proxy(),[])
        self.assertIsNone(self.store.get_intent('intent')['freqtrade_trade_id'])
        self.assertTrue(self.store.has_blockers())
        self.order.update(status='expired')
        self.response['listOrderStatus']='EXECUTING'
        self.assertEqual(self.manager.recover_unbound_entries(),[])
        self.assertTrue(self.store.has_blockers())
        self.response['listOrderStatus']='ALL_DONE'
        self.assertEqual(self.manager.recover_unbound_entries(),[])
        self.assertFalse(self.store.has_blockers())
        self.assertEqual(self.store.get_intent('intent')['state'],'CLOSED_NO_FILL')
        self.assertEqual(Trade.get_trades_proxy(),[])
        self.manager.submit_entry.assert_called_once()
        self.assertEqual(self.manager.prepared,{})

    def test_canonical_commit_before_binding_recovers_exactly_one_trade_and_order(self):
        init_db('sqlite:///'+str(self.root/'owner.sqlite'))
        def update(trade, order_id, order, **kwargs):
            self.assertIs(kwargs.get('send_msg'),False)
            Order.update_orders(trade.orders,order)
            trade.update_trade(trade.select_order_by_order_id(order_id))
            Trade.commit()
            return False  # Verified, filled update; None now means evidence pending.
        bot=SimpleNamespace(binana=self.manager,exchange=self.manager.exchange,
            config={'stake_currency':'USDT','timeframe':'5m'},trading_mode=TradingMode.SPOT,
            strategy=SimpleNamespace(get_strategy_name=lambda:'BinanaNfiSpot',stoploss=-0.01),
            update_trade_state=update)
        bind=self.manager.bind_recovered_entry
        self.manager.bind_recovered_entry=Mock(side_effect=RuntimeError('crash after canonical commit'))
        with self.assertRaisesRegex(RuntimeError,'crash'):
            FreqtradeBot._binana_recover_unbound_entries(bot)
        self.assertEqual(len(Trade.get_trades_proxy()),1)
        self.assertTrue(self.store.has_blockers())
        self.manager=self.fresh_manager();bot.binana=self.manager
        recovered=FreqtradeBot._binana_recover_unbound_entries(bot)
        self.assertEqual(len(recovered),1)
        trade=recovered[0]
        self.assertEqual((len(trade.orders),trade.amount,trade.stake_amount),(1,250,250))
        self.assertEqual(self.store.get_intent('intent')['freqtrade_trade_id'],trade.id)
        self.assertFalse(self.store.has_blockers())
        self.assertEqual(FreqtradeBot._binana_recover_unbound_entries(bot),[])
        self.assertEqual(len(Trade.get_trades_proxy()),1)

    def test_foreign_epoch_or_binding_is_rejected_before_canonical_update(self):
        init_db('sqlite:///'+str(self.root/'owner.sqlite'))
        evidence=self.manager.recover_unbound_entries()[0]
        from datetime import datetime, timezone
        trade=Trade(pair='ADA/USDT',amount=250,stake_amount=250,open_rate=1,
                    open_date=datetime.now(timezone.utc),fee_open=0,fee_close=0,
                    is_open=False,is_short=False,exchange='binance',leverage=1,
                    trading_mode=TradingMode.SPOT)
        trade.orders.append(Order.parse_from_ccxt_object(self.order,'ADA/USDT','buy',250,1))
        trade.set_binana_entry_identity({**self.manager.entry_identity('intent',self.order),
                                         'scope_id':'different-epoch'})
        Trade.session.add(trade);Trade.commit()
        update=Mock(side_effect=AssertionError('Must not mutate the historical trade'))
        bot=SimpleNamespace(binana=self.manager,update_trade_state=update)
        with self.assertRaisesRegex(RuntimeError,'SCOPE_UNVERIFIED'):
            FreqtradeBot._binana_recover_unbound_entries(bot)
        update.assert_not_called()
        self.assertEqual((trade.amount,trade.stake_amount,len(trade.orders)),(250,250,1))

    def test_recovered_binding_commits_state_and_incident_resolution_atomically(self):
        self.store.incident(incident_id='submit-intent',intent_id='intent',pair='ADA/USDT',
                            code='ENTRY_SUBMISSION_UNKNOWN',detail='timeout')
        self.store.incident(incident_id='entry-recovery-intent',intent_id='intent',pair='ADA/USDT',
                            code='ENTRY_RECONSTRUCTION_BLOCKED',detail='retry')
        self.store.bind_recovered_trade('intent',7)
        after_restart=StateStore(self.root/'extension.sqlite')
        self.assertEqual(after_restart.get_intent('intent')['freqtrade_trade_id'],7)
        self.assertEqual(after_restart.get_intent('intent')['state'],'OPEN')
        self.assertFalse(after_restart.has_blockers())

    def test_no_fill_transition_rolls_back_all_changes_on_interrupted_commit(self):
        from contextlib import contextmanager
        from unittest.mock import patch
        self.order.update(status='expired',filled=0,cost=0,remaining=250)
        self.response['listOrderStatus']='ALL_DONE'
        self.store.incident(incident_id='submit-intent',intent_id='intent',pair='ADA/USDT',
                            code='ENTRY_SUBMISSION_UNKNOWN',detail='timeout')
        # Fail at the last write, after both terminal-state writes have executed.
        original_tx=self.store.tx
        @contextmanager
        def interrupted():
            with original_tx() as connection:
                def execute(sql,args):
                    if sql.startswith('UPDATE incidents'):
                        raise RuntimeError('simulated crash')
                    return connection.execute(sql,args)
                yield SimpleNamespace(execute=execute)
        with patch.object(self.store,'tx',interrupted):
            with self.assertRaisesRegex(RuntimeError,'simulated crash'):
                self.store.finalize_entry_no_fill('intent')
        after_restart=StateStore(self.root/'extension.sqlite')
        self.assertEqual(after_restart.get_intent('intent')['state'],'ENTRY_PENDING')
        self.assertEqual(after_restart.generation('intent',1)['status'],'SUBMISSION_PENDING')
        self.assertTrue(after_restart.has_blockers())
        self.manager=self.fresh_manager()
        self.assertEqual(self.manager.recover_unbound_entries(),[])
        self.assertEqual(after_restart.get_intent('intent')['state'],'CLOSED_NO_FILL')
        self.assertEqual(after_restart.generation('intent',1)['status'],'CLOSED_NO_FILL')
        self.assertFalse(after_restart.has_blockers())

    def test_same_owned_open_order_can_advance_from_new_to_filled_on_restart(self):
        from datetime import datetime, timezone
        init_db('sqlite:///'+str(self.root/'owner.sqlite'))
        self.manager.recover_unbound_entries()
        pending={**self.order,'status':'open','filled':0,'cost':0,'remaining':250}
        trade=Trade(pair='ADA/USDT',amount=0,stake_amount=250,open_rate=1,
                    open_date=datetime.now(timezone.utc),fee_open=0,fee_close=0,
                    is_open=True,is_short=False,exchange='binance',leverage=1,
                    trading_mode=TradingMode.SPOT)
        trade.orders.append(Order.parse_from_ccxt_object(pending,'ADA/USDT','buy',250,1))
        trade.set_binana_entry_identity(self.manager.entry_identity('intent',self.order))
        trade.stage_binana_receipts({'schema_version':1,'scope_id':self.manager.scope_id,
                                    'pair':trade.pair,'orders':{}})
        Trade.session.add(trade);Trade.commit()
        for _ in range(2):
            self.manager.verify_existing_entry(self.store.get_intent('intent'),self.order,trade)
            self.manager.stage_canonical_receipts(trade,self.order)
            Order.update_orders(trade.orders,self.order)
            trade.update_trade(trade.orders[0],True)
            Trade.commit()
        self.assertEqual((trade.amount,trade.stake_amount,len(trade.orders)),(250,250,1))
        with self.assertRaisesRegex(RuntimeError,'ECONOMICS_MISMATCH'):
            self.manager.verify_existing_entry(self.store.get_intent('intent'),
                                               {**self.order,'filled':249,'cost':249},trade)
