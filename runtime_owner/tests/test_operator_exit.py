import copy
from decimal import Decimal
from threading import Lock
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from freqtrade.binana.binance_order_lists import BinanceOrderLists
from freqtrade.binana.recovery_contract import RecoveryBlocked
from freqtrade.freqtradebot import FreqtradeBot
from freqtrade.persistence import Trade
from freqtrade.rpc.rpc import RPC, RPCException
from freqtrade.enums import ExitCheckTuple, ExitType, State
from binana_tests import test_exact_commissions as helpers
from binana_tests.test_partial_promotion_recovery import cfg


class OperatorExitTests(unittest.TestCase):
    setUp = helpers.ExactCommissions.setUp
    tearDown = helpers.ExactCommissions.tearDown
    order = helpers.ExactCommissions.order
    enter = helpers.ExactCommissions.enter
    update = helpers.ExactCommissions.update

    def seed(self):
        entry = self.order(1, 'buy', 10, 10, .01, 'ADA')
        self.enter(entry)
        self.orders = {'1': entry}
        for oid, role in (('2','TAKE_PROFIT'),('3','STOP')):
            self.orders[oid] = dict(entry, id=oid, clientOrderId='B-owned-'+oid,
                side='sell', amount=9.99, filled=0, cost=0, remaining=9.99, status='open')
            self.manager.store.bind_order_identity(scope_id=self.manager.scope_id, intent_id='owned',
                generation=1, pair='ADA/USDT', role=role, client_id='B-owned-'+oid, order_id=oid)
        self.manager.store.put_generation(intent_id='owned', generation=1, pair='ADA/USDT',
            mode='FIXED_OCO', status='FIXED_ACTIVE', expected_qty='9.99', ids={
                'order_list_id':'500','list_client_id':'list-500','working_order_id':'1','working_client_id':'B-owned-1',
                'tp_order_id':'2','tp_client_id':'B-owned-2','sl_order_id':'3','sl_client_id':'B-owned-3'})
        self.manager.config = cfg(self.path, self.path/'unused-registry')
        self.manager._promotion_last = {}
        self.manager.legacy_scope_ids = set()
        api = self.manager.exchange._api
        api.urls = {'api':{'private':'https://testnet.binance.vision/api/v3'}}
        self.filters = [
            {'filterType':'PRICE_FILTER','tickSize':'.0001','minPrice':'.0001','maxPrice':'10000'},
            {'filterType':'LOT_SIZE','stepSize':'.01','minQty':'.01','maxQty':'100000'},
            {'filterType':'MARKET_LOT_SIZE','stepSize':'.01','minQty':'.01','maxQty':'100000'},
            {'filterType':'NOTIONAL','minNotional':'.1','maxNotional':'1000000','applyMinToMarket':True,'applyMaxToMarket':True},
            {'filterType':'MAX_NUM_ORDERS','maxNumOrders':100},
            {'filterType':'MAX_NUM_ORDER_LISTS','maxNumOrderLists':100},
            {'filterType':'MAX_NUM_ALGO_ORDERS','maxNumAlgoOrders':100}]
        api.publicGetExchangeInfo = lambda params: {'symbols':[{'symbol':'ADAUSDT','status':'TRADING',
            'ocoAllowed':True,'otoAllowed':True,'filters':self.filters}]}
        api.publicGetTickerBookTicker = lambda params: {'bidPrice':'10.05','askPrice':'10.06'}
        api.publicGetAvgPrice = lambda params: {'price':'10.05','mins':0}
        api.fetch_open_orders = Mock(side_effect=lambda pair:[copy.deepcopy(row) for row in self.orders.values() if row['status']=='open'])
        api.fetch_balance = Mock(return_value={'ADA':{'free':10000,'used':0}})
        self.manager.exchange.fetch_order = lambda oid, pair: copy.deepcopy(self.orders[str(oid)])
        self.cancel_mode = 'success'; self.post_mode = 'success'; self.race = None; self.market_partial = None
        self.market_fee = None
        api.privateDeleteOrderList = Mock(side_effect=self.cancel)
        api.privatePostOrder = Mock(side_effect=self.post)
        api.privateGetOrder = Mock(side_effect=self.query)
        api.privateGetOpenOrders = lambda params=None: api.fetch_open_orders('ADA/USDT')
        api.privateGetOpenOrderList = lambda params=None: []
        api.privatePostOrderListOco = Mock(side_effect=self.reprotect)
        api.privateGetOrderList = Mock(side_effect=lambda params: {
            'symbol':'ADAUSDT','orderListId':500,'listClientOrderId':'list-500',
            'listOrderStatus':'EXECUTING' if any(self.orders[k]['status']=='open' for k in ('2','3')) else 'ALL_DONE'})
        self.manager.order_lists = BinanceOrderLists(api)
        self.manager.store.incident(incident_id='unrelated',code='UNRELATED',detail='keep')
        self.bot.order_close_notify.reset_mock()

    def fill(self, oid, quantity, status='closed', client=None):
        row = self.orders.get(str(oid), dict(self.orders['2'],id=str(oid),clientOrderId=client))
        timestamp = 1790160000000 + int(oid)*1000
        row.update(status=status,filled=float(quantity),cost=float(Decimal(str(quantity))*Decimal('10.05')),
            average=10.05,price=10.05,timestamp=timestamp,lastTradeTimestamp=timestamp,
            remaining=float(Decimal(str(row['amount']))-Decimal(str(quantity))))
        self.orders[str(oid)] = row
        self.fills[str(oid)] = [{'id':str(int(oid)*100),'order':str(oid),'symbol':'ADA/USDT','side':'sell',
            'amount':row['filled'],'cost':row['cost'],'timestamp':timestamp,'fee':{'currency':'USDT','cost':0}}] if quantity else []
        return row

    def cancel(self, params):
        if self.cancel_mode == 'timeout-active': raise TimeoutError('cancel uncertain')
        for oid in ('2','3'): self.orders[oid]['status']='canceled'
        if self.race:
            oid, quantity = self.race
            self.fill(oid, quantity, 'closed' if quantity==9.99 else 'canceled')
        if self.cancel_mode == 'timeout-done': raise TimeoutError('cancel response lost')
        return {'orderListId':500}

    def post(self, params):
        self.assertEqual(params['maxRetriesOnFailure'],0)
        if self.post_mode=='timeout-absent': raise TimeoutError('no proof of submission')
        qty = float(params['quantity'])
        self.orders['9'] = dict(self.orders['2'],id='9',clientOrderId=params['newClientOrderId'],amount=qty)
        row=self.fill('9',self.market_partial if self.market_partial is not None else qty,
                      'expired' if self.market_partial is not None else 'closed')
        if self.market_fee:
            asset, cost = self.market_fee
            self.fills['9'][0]['fee'] = {'currency':asset,'cost':cost}
        if self.post_mode=='timeout-accepted': raise TimeoutError('response lost after execution')
        return {'symbol':'ADAUSDT','orderId':9,'clientOrderId':row['clientOrderId'],'side':'SELL'}

    def reprotect(self, params):
        for oid, key in (('20','aboveClientOrderId'),('21','belowClientOrderId')):
            self.orders[oid] = dict(self.orders['2'], id=oid, clientOrderId=params[key],
                amount=float(params['quantity']),filled=0,cost=0,remaining=float(params['quantity']),status='open')
        return {'orderListId':600,'orders':[{'orderId':int(oid),'clientOrderId':self.orders[oid]['clientOrderId']}
                                           for oid in ('20','21')]}

    def query(self, params):
        row=next((r for r in self.orders.values() if r['clientOrderId']==params['origClientOrderId']),None)
        if row is None: raise RuntimeError('-2013 order does not exist')
        return {'symbol':'ADAUSDT','orderId':row['id'],'clientOrderId':row['clientOrderId'],'side':row['side'].upper()}

    def request(self): return self.manager.request_operator_exit(self.trade)

    def step(self):
        result=self.manager.reconcile_trade(self.trade)
        if result and result.get('exit_order'): self.update(result['exit_order'])
        if result and result.get('dust_retained'):
            evidence=result['dust_retained']
            self.trade.set_custom_data(key='binana_retained_dust',value={**evidence,'phase':'prepared'})
            Trade.commit(); self.manager.acknowledge_retained_dust(self.trade,evidence)
            self.trade.set_custom_data(key='binana_retained_dust',value={**evidence,'phase':'retained'})
            Trade.commit(); self.manager.acknowledge_canonical_reconciliation(self.trade)
        return result

    def test_full_exit_cancels_then_sells_actual_net_quantity_once(self):
        self.seed(); self.request(); self.step()
        api=self.manager.exchange._api
        api.privatePostOrder.assert_not_called()
        self.step()
        self.assertEqual(api.privatePostOrder.call_args.args[0]['quantity'],'9.99')
        self.step()
        self.assertFalse(self.trade.is_open)
        self.assertTrue(self.manager.recover_closed_acknowledgments())
        self.assertEqual(self.manager.store.get_intent('owned')['state'],'EXIT_FILLED')
        self.assertEqual([row['incident_id'] for row in self.manager.store.unresolved()],['unrelated'])
        self.assertEqual(api.privatePostOrder.call_count,1)

    def test_repeated_requests_reuse_one_durable_generation(self):
        self.seed(); first=self.request(); second=self.request()
        self.assertEqual(first['generation'],second['generation'])
        self.assertEqual(self.manager.store.get_intent('owned')['state'],'EXIT_PENDING')

    def test_invalid_options_rejected_before_any_mutation(self):
        self.seed()
        for options in ({'ordertype':'limit'},{'amount':1},{'price':10}):
            with self.assertRaises(RecoveryBlocked): self.manager.request_operator_exit(self.trade,**options)
        self.assertEqual(self.manager.store.latest_generation('owned')['generation'],1)
        self.manager.exchange._api.privateDeleteOrderList.assert_not_called()

    def test_cancel_timeout_still_active_never_submits(self):
        self.seed(); self.cancel_mode='timeout-active'; self.request()
        self.step(); self.step()
        self.manager.exchange._api.privatePostOrder.assert_not_called()
        self.assertTrue(self.trade.is_open)

    def test_cancel_timeout_already_done_recovers_from_reads(self):
        self.seed(); self.cancel_mode='timeout-done'; self.request()
        self.step(); self.step(); self.step()
        self.assertFalse(self.trade.is_open)
        self.assertEqual(self.manager.exchange._api.privatePostOrder.call_count,1)

    def test_full_tp_fill_during_cancel_needs_no_market_sell(self):
        self.seed(); self.race=('2',9.99); self.request(); self.step(); self.step()
        self.assertFalse(self.trade.is_open)
        self.assertTrue(self.manager.recover_closed_acknowledgments())
        self.manager.exchange._api.privatePostOrder.assert_not_called()

    def test_partial_stop_fill_is_imported_before_sizing_market_exit(self):
        self.seed(); self.race=('3',4); self.request(); self.step(); self.step()
        self.assertAlmostEqual(self.trade.amount,5.99)
        self.manager.exchange._api.privatePostOrder.assert_not_called()
        self.step(); self.step()
        self.assertEqual(self.manager.exchange._api.privatePostOrder.call_args.args[0]['quantity'],'5.99')
        self.assertFalse(self.trade.is_open)

    def test_unknown_post_accepted_is_recovered_without_second_post(self):
        self.seed(); self.post_mode='timeout-accepted'; self.request(); self.step(); self.step()
        self.assertEqual(self.manager.store.latest_generation('owned')['status'],'SUBMISSION_PENDING')
        Trade.session.expire_all(); self.step()
        self.assertFalse(self.trade.is_open)
        self.assertEqual(self.manager.exchange._api.privatePostOrder.call_count,1)

    def test_unknown_post_absent_never_reposts(self):
        self.seed(); self.post_mode='timeout-absent'; self.request(); self.step(); self.step()
        for _ in range(3): self.step()
        self.assertEqual(self.manager.exchange._api.privatePostOrder.call_count,1)
        self.assertTrue(self.manager.store.has_blockers())

    def test_crash_after_preparing_before_post_only_queries(self):
        self.seed(); self.request(); self.step()
        self.manager.store.prepare_operator_submission(intent_id='owned',generation=2,quantity=Decimal('9.99'))
        self.step(); self.step()
        self.manager.exchange._api.privatePostOrder.assert_not_called()
        self.assertTrue(self.manager.store.has_blockers())

    def test_invalid_market_filter_does_not_cancel_protection(self):
        self.seed(); self.filters[2]['minQty']='20'; self.request(); self.step()
        self.manager.exchange._api.privateDeleteOrderList.assert_not_called()
        self.manager.exchange._api.privatePostOrder.assert_not_called()

    def test_insufficient_free_balance_does_not_use_used_or_faucet_ownership(self):
        self.seed(); self.request(); self.step()
        self.manager.exchange._api.fetch_balance.return_value={'ADA':{'free':1,'used':1000}}
        self.step(); self.manager.exchange._api.privatePostOrder.assert_not_called()

    def partial_reprotection(self, currency, fee, expected):
        self.seed(); self.market_partial=4; self.market_fee=(currency,fee)
        self.request(); self.step(); self.step(); self.step()
        self.assertTrue(self.trade.is_open); self.assertAlmostEqual(self.trade.amount,expected)
        self.step()
        latest=self.manager.store.latest_generation('owned')
        self.assertEqual(latest['status'],'FIXED_ACTIVE',self.manager.store.unresolved())
        self.assertLessEqual(float(latest['expected_qty']),expected)
        self.manager.acknowledge_canonical_reconciliation(self.trade)
        self.assertEqual([r['incident_id'] for r in self.manager.store.unresolved()],['unrelated'])
        self.assertEqual(self.manager.exchange._api.privatePostOrder.call_count,1)
        self.assertEqual(self.manager.exchange._api.privatePostOrderListOco.call_count,1)

    def test_partial_market_quote_fee_restores_residual_protection(self):
        self.partial_reprotection('USDT',.04,5.99)

    def test_partial_market_base_fee_restores_only_remaining_owned_quantity(self):
        self.partial_reprotection('ADA',.01,5.98)

    def test_terminal_market_dust_is_retained_without_another_sell(self):
        self.seed(); self.market_partial=9.98; self.filters[3]['minNotional']='.2'
        self.request(); self.step(); self.step(); self.step(); self.step()
        self.assertAlmostEqual(self.trade.amount,.01)
        self.assertTrue(self.trade.is_retained_dust)
        self.assertEqual(self.manager.store.get_intent('owned')['state'],'DUST_RETAINED')
        self.assertEqual(self.manager.exchange._api.privatePostOrder.call_count,1)

    def test_foreign_open_order_is_never_canceled(self):
        self.seed(); self.orders['foreign']=dict(self.orders['2'],id='foreign',clientOrderId='manual')
        self.request(); self.step()
        self.manager.exchange._api.privateDeleteOrderList.assert_not_called()
        self.manager.exchange._api.privatePostOrder.assert_not_called()

    def test_rpc_all_validates_options_and_reports_mixed_results(self):
        self.seed()
        bot=SimpleNamespace(binana=self.manager,_exit_lock=Lock(),state=State.PAUSED)
        rpc=SimpleNamespace(_freqtrade=bot)
        with self.assertRaises(RPCException): RPC._rpc_force_exit(rpc,'all',amount=1)
        self.assertEqual(self.manager.store.latest_generation('owned')['generation'],1)
        self.assertIn('accepted',RPC._rpc_force_exit(rpc,'all')['result'])
        self.manager.scope_id='foreign'
        self.assertIn('blocked',RPC._rpc_force_exit(rpc,'all')['result'])
        self.manager.exchange._api.privateDeleteOrderList.assert_not_called()

    def test_emergency_path_routes_before_native_cancellation(self):
        self.seed()
        bot=SimpleNamespace(binana=self.manager)
        self.assertTrue(FreqtradeBot.execute_trade_exit(bot,self.trade,10,
            ExitCheckTuple(exit_type=ExitType.EMERGENCY_EXIT)))
        self.manager.exchange._api.privateDeleteOrderList.assert_not_called()


if __name__=='__main__': unittest.main()
