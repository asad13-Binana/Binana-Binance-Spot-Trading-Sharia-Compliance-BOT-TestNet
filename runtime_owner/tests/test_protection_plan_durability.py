import json
import importlib.util
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from registry_fixture import registry_document
from binana_tests.test_partial_promotion_recovery import Exchange, GoodMarket, cfg
from binana_tests import test_restart_partial_recovery as restart_fixture
from freqtrade.binana.execution_manager import BinanaExecutionManager
from freqtrade.binana.binance_order_lists import OrderListError
from freqtrade.binana.state_store import StateStore
from freqtrade.binana.market_data import MarketDataService
from freqtrade.binana.release_gate import certificate_blockers


class Durability(unittest.TestCase):
    def test_status_only_upsert_retains_plan_after_reopen(self):
        with TemporaryDirectory() as td:
            for mode in ('FIXED_OCO','TRAILING_OCO'):
                with self.subTest(mode=mode):
                    store = StateStore(Path(td)/f'{mode}.sqlite')
                    row = dict(intent_id='i', generation=2, pair='ADA/USDT', mode=mode)
                    store.put_intent(intent_id='i', pair='ADA/USDT', signal_id='s', halal_allowed=True,
                                     registry_sha256='h', nominal_usdt='10', state='OPEN')
                    plan = {'quantity':'8.98','mode':mode,'tp_price':'1.02'}
                    store.put_generation(**row,status='SUBMISSION_PENDING',payload={'plan':plan})
                    store.put_generation(**row,status='SUBMITTED_UNVERIFIED')
                    restored = StateStore(Path(td)/f'{mode}.sqlite').latest_generation('i')
                    self.assertEqual(json.loads(restored['payload_json']).get('plan'),plan)

    def test_status_evidence_preserves_plan(self):
        with TemporaryDirectory() as td:
            store = StateStore(Path(td)/'state.sqlite')
            store.put_intent(intent_id='i',pair='ADA/USDT',signal_id='s',halal_allowed=True,
                             registry_sha256='h',nominal_usdt='10',state='OPEN')
            plan={'quantity':'8.98','mode':'TRAILING_OCO'}
            store.put_generation(intent_id='i',generation=2,pair='ADA/USDT',mode='TRAILING_OCO',
                                 status='SUBMISSION_PENDING',payload={'plan':plan})
            store.update_generation_status('i',2,'TRAILING_ACTIVE',payload={'verified_at':123})
            payload=json.loads(StateStore(Path(td)/'state.sqlite').latest_generation('i')['payload_json'])
            self.assertEqual(payload.get('plan'),plan)
            self.assertEqual(payload['verified_at'],123)


class Promotion(unittest.TestCase):
    def make(self,td):
        registry=Path(td)/'registry.json'
        registry.write_text(json.dumps(registry_document(['ADAUSDT'])))
        exchange=Exchange()
        manager=BinanaExecutionManager(cfg(td,registry),exchange)
        manager.market_data=GoodMarket()
        manager.store.put_intent(intent_id='intent',pair='ADA/USDT',signal_id='s',halal_allowed=True,
                                 registry_sha256='h',nominal_usdt='250',state='OPEN')
        manager.store.put_generation(intent_id='intent',generation=1,pair='ADA/USDT',mode='FIXED_OCO',
            status='FIXED_ACTIVE',expected_qty='9.98',ids={'order_list_id':'10','list_client_id':'L',
            'tp_order_id':'11','tp_client_id':'TP','sl_order_id':'12','sl_client_id':'SL'})
        trade=SimpleNamespace(id=1,pair='ADA/USDT',amount=10.0,open_rate=1.0)
        return manager,exchange,trade

    def test_partial_plan_is_durable_before_post(self):
        with TemporaryDirectory() as td:
            manager,ex,trade=self.make(td)
            ex._api.cancel_response={'orderReports':[{'orderId':11,'clientOrderId':'TP',
                'status':'CANCELED','executedQty':'1.0'}]}
            observed=[]
            post=ex._api.privatePostOrderListOco
            def capture(params):
                row=StateStore(manager.config['binana']['state_db_path']).latest_generation('intent')
                observed.append((row,dict(params)))
                return post(params)
            ex._api.privatePostOrderListOco=capture
            manager.maybe_promote(trade,'intent',manager.store.latest_generation('intent'))
            self.assertEqual(len(observed),1)
            row,request=observed[0]
            plan=json.loads(row['payload_json']).get('plan')
            self.assertIsNotNone(plan,'replacement POST has no durable protection plan')
            self.assertEqual(Decimal(plan['quantity']),Decimal('8.98'))
            self.assertEqual(plan['mode'],'TRAILING_OCO')
            self.assertEqual(Decimal(request['quantity']),Decimal(plan['quantity']))
            self.assertEqual(Decimal(request['abovePrice']),Decimal(plan['tp_price']))
            self.assertEqual(request['belowTrailingDelta'],plan['trailing_delta_bips'])
            self.assertEqual(request['listClientOrderId'],row['list_client_id'])

    def test_plan_survives_successful_submit_and_restart(self):
        for filled in ('0','1.0'):
            with self.subTest(filled=filled), TemporaryDirectory() as td:
                manager,ex,trade=self.make(td)
                ex._api.cancel_response={'orderReports':[{'orderId':11,'clientOrderId':'TP',
                    'status':'CANCELED','executedQty':filled}]}
                manager.maybe_promote(trade,'intent',manager.store.latest_generation('intent'))
                restored=BinanaExecutionManager(manager.config,ex)
                row=restored.store.latest_generation('intent')
                plan=json.loads(row['payload_json']).get('plan')
                self.assertIsNotNone(plan)
                self.assertEqual(Decimal(plan['quantity']),Decimal('9.98')-Decimal(filled))
                self.assertEqual(row['status'],'SUBMITTED_UNVERIFIED')
                self.assertTrue(restored.store.has_blockers())

    def test_invalid_preflight_does_not_cancel_existing_protection(self):
        with TemporaryDirectory() as td:
            manager,ex,trade=self.make(td)
            cancellations=[]
            ex._api.privateDeleteOrderList=lambda params:cancellations.append(params) or {}
            ex._api.publicGetAvgPrice=lambda params: {'price':'1000'}
            try:
                manager.maybe_promote(trade,'intent',manager.store.latest_generation('intent'))
            except OrderListError:
                pass
            self.assertEqual(cancellations,[],'old OCO canceled before known-invalid replacement preflight')
            self.assertEqual(ex._api.oco_calls,[])
            self.assertEqual(manager.store.generation('intent',1)['status'],'FIXED_ACTIVE')

    def test_cancel_unknown_does_not_submit_replacement(self):
        with TemporaryDirectory() as td:
            manager,ex,trade=self.make(td)
            def timeout(_): raise TimeoutError('synthetic cancellation timeout')
            ex._api.privateDeleteOrderList=timeout
            manager.maybe_promote(trade,'intent',manager.store.latest_generation('intent'))
            self.assertEqual(ex._api.oco_calls,[])
            self.assertTrue(manager.store.has_blockers())


class PendingRecovery(unittest.TestCase):
    def test_timeout_or_existing_child_does_not_authorize_replacement(self):
        fixture=restart_fixture.RestartPartialRecovery()
        for scenario in ('timeout','child_exists','open_order','initial_entry'):
            with self.subTest(scenario=scenario), TemporaryDirectory() as td:
                manager,ex=fixture._manager(td)
                iid=fixture._seed(manager)
                manager.store.update_generation_status(iid,2,'REPLACEMENT_PENDING')
                def missing_list(**kwargs):
                    if scenario=='timeout': raise TimeoutError('synthetic')
                    raise RuntimeError('{"code":-2018,"msg":"Order list does not exist."}')
                def missing_child(params):
                    if scenario=='child_exists': return {'orderId':99}
                    raise RuntimeError('{"code":-2013,"msg":"Order does not exist."}')
                manager.order_lists.query_list=missing_list
                ex._api.privateGetOrder=missing_child
                if scenario=='open_order': ex._api.fetch_open_orders=lambda pair:[{'id':'unowned'}]
                if scenario=='initial_entry':
                    manager.store.put_generation(intent_id=iid,generation=2,pair='ADA/USDT',
                        mode='TRAILING_OCO',status='REPLACEMENT_PENDING',ids={'working_client_id':'WORKING'})
                trade=SimpleNamespace(id=1,pair='ADA/USDT',amount=10.0,open_rate=1.0,orders=[])
                manager.reconcile_trade(trade)
                self.assertEqual(ex._api.oco_calls,[])
                self.assertTrue(manager.store.has_blockers())

    def test_replacement_pending_can_prove_absence_before_recovery(self):
        fixture=restart_fixture.RestartPartialRecovery()
        with TemporaryDirectory() as td:
            manager,ex=fixture._manager(td)
            iid=fixture._seed(manager)
            manager.store.update_generation_status(iid,2,'REPLACEMENT_PENDING')
            manager=BinanaExecutionManager(manager.config,ex)
            manager.market_data=GoodMarket()
            def missing_list(**kwargs):
                raise RuntimeError('{"code":-2018,"msg":"Order list does not exist."}')
            def missing_child(params):
                raise RuntimeError('{"code":-2013,"msg":"Order does not exist."}')
            manager.order_lists.query_list=missing_list
            ex._api.privateGetOrder=missing_child
            trade=SimpleNamespace(id=1,pair='ADA/USDT',amount=10.0,open_rate=1.0,orders=[])
            manager.reconcile_trade(trade)
            self.assertEqual(manager.store.generation(iid,2)['status'],'SUBMISSION_ABSENT')
            self.assertEqual(len(ex._api.oco_calls),1)
            manager.reconcile_trade(trade)
            self.assertEqual(len(ex._api.oco_calls),1)


class FreshnessAndCertificate(unittest.TestCase):
    def test_context_expiry_future_and_warmup_fail_closed(self):
        now=1800000000
        def stamp(t): return datetime.fromtimestamp(t,timezone.utc).isoformat()
        with TemporaryDirectory() as td:
            path=Path(td)/'context.json'
            service=MarketDataService(None,path,{})
            cases=[(0,0,60,'fresh'),(30,0,60,'fresh'),(30.001,0,60,'stale'),
                   (-1,0,60,'stale'),(0,5.001,60,'stale'),(0,0,59.999,'warming_up')]
            for context_age,event_age,coverage,expected in cases:
                with self.subTest(case=(context_age,event_age,coverage)):
                    path.write_text(json.dumps({'generated_at':stamp(now-context_age),
                        'symbols':{'ADAUSDT':{'spot_aggressive_flow':{'status':'fresh',
                        'continuous_coverage_seconds':coverage,'last_agg_trade_received_at':stamp(now-event_age),
                        'last_agg_trade_event_time_ms':(now-event_age)*1000}}}}))
                    with patch('freqtrade.binana.market_data.time',return_value=now):
                        self.assertEqual(service._context('ADA/USDT')[1],expected)

    def test_expiry_boundary_and_seven_day_limit(self):
        bindings={'code_sha256':'c','config_sha256':'p','exchange_epoch_id':'e'}
        cert={**bindings,'generated_at':100,'valid_until':200}
        cert.update({k:True for k in ['verified','controlled_lifecycle_verified','restart_verified',
            'backup_restore_verified','critical_suite_10x_verified','one_slot_soak_verified','four_slot_soak_verified']})
        self.assertEqual(certificate_blockers(cert,bindings,now=199.999),[])
        self.assertTrue(certificate_blockers(cert,bindings,now=200))
        self.assertTrue(certificate_blockers({**cert,'valid_until':100+7*86400+1},bindings,now=150))
