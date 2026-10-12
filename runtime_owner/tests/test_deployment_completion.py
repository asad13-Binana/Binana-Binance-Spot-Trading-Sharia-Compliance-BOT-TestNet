"""Regression cases that must pass before the October 12 AWS rollout."""
import json
import tempfile
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
import unittest

import test_restart_partial_recovery as recovery_fixture
from test_audit_20261012 import plan
from test_engine_core import FakeApi
from freqtrade.binana.binance_order_lists import BinanceOrderLists, OrderListError
from test_telegram_release import load
import time


class NativeWorkingExit(unittest.TestCase):
    def scenario(self, directory, *, working=True):
        helper = recovery_fixture.RestartPartialRecovery()
        manager, exchange = helper._manager(directory)
        iid = helper._seed(manager)
        p = replace(plan(), quantity=Decimal('9.98'))
        manager.store.put_generation(intent_id=iid,generation=1,pair='ADA/USDT',mode='FIXED_OCO',
            status='FIXED_ACTIVE',payload={'plan':json.loads(json.dumps(p.__dict__,default=str))})
        original = exchange.fetch_order
        children = {}
        for key, oid, cid in [('tp','11','TP'),('sl','12','SL')]:
            children[oid] = {'id':oid,'clientOrderId':cid,'symbol':'ADA/USDT','side':'sell',
                'status':'open' if key=='sl' else 'canceled','amount':9.98,'filled':0,
                'info':{'orderListId':10,'type':'STOP_LOSS_LIMIT' if key=='sl' else 'LIMIT_MAKER',
                    'price':str(p.stop_limit if key=='sl' else p.tp_price),
                    'stopPrice':str(p.stop_trigger) if key=='sl' else '0','timeInForce':'GTC',
                    'status':'NEW' if key=='sl' else 'CANCELED',
                    'isWorking':working if key=='sl' else False,'workingTime':1234 if working else -1}}
        exchange.fetch_order = lambda oid,pair: children.get(str(oid)) or original(oid,pair)
        exchange._api.fetch_open_orders = lambda pair:[children['12']]
        return manager,exchange,iid,SimpleNamespace(id=1,pair='ADA/USDT',amount=10.0,open_rate=1.0,orders=[])

    def test_triggered_native_stop_is_preserved_and_reconciled(self):
        with tempfile.TemporaryDirectory() as directory:
            manager,exchange,iid,trade = self.scenario(directory)
            manager.reconcile_trade(trade)
            self.assertEqual(manager.store.generation(iid,1)['status'],'NATIVE_EXIT_WORKING')
            self.assertEqual(manager.store.unresolved(),[])
            self.assertEqual(exchange._api.oco_calls,[])

    def test_untriggered_survivor_does_not_authorize_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            manager,exchange,iid,trade = self.scenario(directory,working=False)
            manager.reconcile_trade(trade)
            self.assertTrue(manager.store.has_blockers())
            self.assertEqual(exchange._api.oco_calls,[])


class MaximumPosition(unittest.TestCase):
    def test_working_buy_includes_free_locked_and_pending_buys(self):
        api=FakeApi()
        api.privateGetMyFilters=lambda p:{'symbolFilters':[{'filterType':'MAX_POSITION','maxPosition':'15'}],
            'exchangeFilters':[],'assetFilters':[]}
        api.privateGetAccount=lambda p:{'balances':[{'asset':'ADA','free':'2','locked':'2'}]}
        api.privateGetOpenOrders=lambda p:[{'symbol':'ADAUSDT','side':'BUY','origQty':'2','executedQty':'0'}]
        with self.assertRaisesRegex(OrderListError,'MAX_POSITION'):
            BinanceOrderLists(api).preflight(plan(),otoco=True)


class SignedResumeProjection(unittest.TestCase):
    def state(self):
        return dict(cfg={'state':'paused'},ext={'available':True,'incidents':[],'intents':[],
            'protection':[]},market={'status':'fresh','stream':{'connected':True,'probation_complete':True}},
            sidecar={'order_authority':False,'exchange_credentials_present':False},telegram={'ok':True},
            code_marker={'schema':'binana-release-ed25519-v1','payload':{},'signature':'not-ui-authority'},
            owner_readiness={'generated_at':time.time(),'valid_until':time.time()+30,
                'release_approved':True,'blockers':[]})

    def test_panel_uses_fresh_authenticated_owner_decision(self):
        self.assertEqual(load('_resume_blockers_from_state')(**self.state()),[])

    def test_unverified_owner_still_blocks(self):
        state=self.state(); state['owner_readiness']['release_approved']=False
        state['owner_readiness']['blockers']=['release_evidence_unavailable']
        self.assertIn('release_evidence_unavailable',load('_resume_blockers_from_state')(**state))
