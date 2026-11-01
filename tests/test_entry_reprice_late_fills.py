"""Final cancellation facts must size protection after a partial OTOCO entry."""
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from time import time

from test_phase_6_8_lifecycle_repairs import _Broker, _Client, _Portfolio, _position
from services.execution_sidecar.core_adapter import CoreAdapter
from services.execution_sidecar.state_store import StateStore


class CancelRaceExchange(_Client):
    """Independent order ledger: another .3 base fills while cancel is in flight."""
    def __init__(self, *, malformed=False):
        stamp = int(time()*1000)-1000
        entry = {'symbol': 'ETHUSDT', 'orderId': 1, 'status': 'CANCELED',
                 'executedQty': '.7', 'cummulativeQuoteQty': '70', 'origQty':'1',
                 'side':'BUY', 'type':'LIMIT', 'time':stamp,
                 'clientOrderId':'FORTRESS_owned_entry'}
        if malformed:
            entry.pop('executedQty')
        super().__init__(balances=[[{'asset': 'ETH', 'free': '.7', 'locked': '0'}]],
            order_facts={1: entry,
                3: {'symbol': 'ETHUSDT', 'orderId': 3, 'status': 'CANCELED', 'executedQty': '0'},
                4: {'symbol': 'ETHUSDT', 'orderId': 4, 'status': 'CANCELED', 'executedQty': '0'}},
            list_fact={'symbol':'ETHUSDT', 'orderListId': 7, 'listOrderStatus': 'ALL_DONE'})
        self.fills = [{'symbol':'ETHUSDT','id':101,'orderId':1,'qty':'.7',
            'quoteQty':'70','price':'100','commission':'0','commissionAsset':'USDT',
            'isBuyer':True,'time':stamp+10}]

    def get_my_trades(self, **params):
        return [dict(fill) for fill in self.fills
                if int(fill['id']) >= int(params.get('fromId',0))
                and ('orderId' not in params or fill['orderId']==params['orderId'])][:params.get('limit',1000)]

    def get_open_orders(self, **params):
        return [dict(row) for row in self.order_facts.values()
                if row.get('status') in {'NEW','PARTIALLY_FILLED'}]

    def create_order(self, **params):
        self.sent.append(dict(params))
        self.order_facts[99] = {'symbol': params['symbol'], 'orderId': 99,
            'status': 'NEW', 'origQty': params['quantity'], 'executedQty': '0',
            'side':params['side'],'type':params['type'],'clientOrderId':params['newClientOrderId'],
            'price':params['price'],'trailingDelta':params['trailingDelta'],'stopPrice':'0'}
        return dict(self.order_facts[99])


class RepriceLateFills(unittest.TestCase):
    def fixture(self, *, malformed=False):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        client = CancelRaceExchange(malformed=malformed)
        broker = _Broker(client)
        position = _position(filled_qty=Decimal('.4'), order_list_id=7,
                             tp_order_id=3, sl_order_id=4, bracket=True)
        portfolio = _Portfolio(position)
        adapter = CoreAdapter.__new__(CoreAdapter)
        adapter.state_store = StateStore(Path(directory.name)/'state.json',
                                         Path(directory.name)/'state.sqlite')
        adapter._balance_cache = {}
        adapter.filter_validator = SimpleNamespace(validate_replacement=lambda *a, **k: {})
        adapter.trader = SimpleNamespace(broker=broker, pf=portfolio, is_running=lambda: True)
        adapter.mirror_positions = Mock(return_value=1)
        adapter.state_store.mirror_legacy_position(position.symbol, position)
        exit_engine = SimpleNamespace(b=broker, pf=portfolio)
        engine = SimpleNamespace(b=broker, pf=portfolio, ex=exit_engine)
        return adapter, engine, position, client

    def test_reprice_protects_final_cancellation_fill_not_earlier_poll(self):
        adapter, engine, position, exchange = self.fixture()
        adapter._reprice_interposed(engine, position)
        self.assertEqual(Decimal(exchange.sent[0]['quantity']), Decimal('.7'))
        self.assertEqual(position.filled_qty, Decimal('.7'))
        self.assertEqual(position.entry_price, Decimal('100'))

    def test_malformed_final_fill_keeps_recovery_blocked_without_replacement(self):
        adapter, engine, position, exchange = self.fixture(malformed=True)
        adapter._reprice_interposed(engine, position)
        self.assertEqual(exchange.sent, [])
        self.assertIn('cancel-reprice:ETHUSDT', adapter.state_store.safety_halts())
        self.assertEqual(position.order_list_id, 7)

    def live_single(self, *, quantity='.7', executed='0'):
        adapter, engine, position, exchange = self.fixture()
        position.filled_qty = Decimal('.7')
        position.bracket = False
        position.order_list_id = position.tp_order_id = position.sl_order_id = None
        position.exit_order_id = 99
        exchange.order_facts[99] = {'symbol':'ETHUSDT','orderId':99,
            'status':'PARTIALLY_FILLED' if Decimal(executed)>0 else 'NEW',
            'side':'SELL','type':'STOP_LOSS_LIMIT','origQty':quantity,
            'executedQty':executed,'clientOrderId':'FORTRESS_owned_stop',
            'price':'99.2','trailingDelta':50,'stopPrice':'0'}
        adapter.state_store.mirror_legacy_position(position.symbol, position)
        return adapter, engine, position, exchange

    def test_live_protection_rejects_wrong_side_type_symbol_and_order_identity(self):
        for change in ({'side':'BUY'}, {'type':'LIMIT'}, {'symbol':'ADAUSDT'}, {'orderId':999}):
            with self.subTest(change=change):
                adapter, engine, position, exchange = self.live_single()
                exchange.order_facts[99].update(change)
                self.assertFalse(adapter._verify_live_protection(engine.b, position)[0])

    def test_live_protection_requires_complete_fill_and_fee_history(self):
        for change in ('missing-fill','missing-commission','foreign-fill'):
            with self.subTest(change=change):
                adapter, engine, position, exchange = self.live_single()
                if change == 'missing-fill':
                    exchange.fills = []
                elif change == 'missing-commission':
                    exchange.fills[0].pop('commission')
                else:
                    exchange.fills.append({**exchange.fills[0],'id':999,'orderId':999})
                self.assertFalse(adapter._verify_live_protection(engine.b, position)[0])

    def test_undersized_live_stop_does_not_cover_known_owned_remainder(self):
        adapter, engine, position, exchange = self.live_single(quantity='.4')
        self.assertFalse(adapter._verify_live_protection(engine.b, position)[0])

    def test_partial_exit_requires_its_fill_receipts_before_coverage_is_confirmed(self):
        adapter, engine, position, exchange = self.live_single(executed='.2')
        self.assertFalse(adapter._verify_live_protection(engine.b, position)[0])
        exchange.fills.append({'symbol':'ETHUSDT','id':102,'orderId':99,'qty':'.2',
            'quoteQty':'20','price':'100','commission':'.02','commissionAsset':'USDT',
            'isBuyer':False,'time':exchange.order_facts[1]['time']+20})
        self.assertTrue(adapter._verify_live_protection(engine.b, position)[0])

    def test_base_fee_reduces_owned_quantity_and_quote_fee_preserves_it(self):
        adapter, engine, position, exchange = self.live_single()
        exchange.fills[0].update(commission='.001',commissionAsset='ETH')
        self.assertFalse(adapter._verify_live_protection(engine.b, position)[0])
        exchange.order_facts[99]['origQty'] = '.699'
        self.assertTrue(adapter._verify_live_protection(engine.b, position)[0])
        adapter, engine, position, exchange = self.live_single()
        exchange.fills[0].update(commission='.07',commissionAsset='USDT')
        self.assertTrue(adapter._verify_live_protection(engine.b, position)[0])

    def test_competing_open_order_prevents_confirming_protection(self):
        adapter, engine, position, exchange = self.live_single()
        exchange.order_facts[100] = {**exchange.order_facts[99],'orderId':100}
        self.assertFalse(adapter._verify_live_protection(engine.b, position)[0])

    def test_rescue_uses_owned_net_fill_even_if_wallet_contains_extra_base(self):
        adapter, engine, position, exchange = self.fixture()
        exchange.fills[0].update(commission='.001',commissionAsset='ETH')
        adapter._reprice_interposed(engine,position)
        self.assertEqual(Decimal(exchange.sent[0]['quantity']),Decimal('.699'))

    def test_rescue_does_not_submit_without_complete_owned_fill_history(self):
        adapter, engine, position, exchange = self.fixture()
        exchange.fills = []
        adapter._reprice_interposed(engine,position)
        self.assertEqual(exchange.sent,[])

    def test_old_entry_uses_trade_id_pagination_without_an_age_cutoff(self):
        adapter, engine, position, exchange = self.live_single()
        old = int(time()*1000)-90*24*3600*1000
        exchange.order_facts[1]['time'] = old
        exchange.fills[0]['time'] = old+10
        self.assertTrue(adapter._verify_live_protection(engine.b,position)[0])

    def test_incomplete_stop_geometry_cannot_be_verified_as_protection(self):
        adapter, engine, position, exchange = self.live_single()
        exchange.order_facts[99].pop('trailingDelta')
        self.assertFalse(adapter._verify_live_protection(engine.b,position)[0])

    def test_previously_verified_client_identity_cannot_change(self):
        adapter, engine, position, exchange = self.live_single()
        self.assertTrue(adapter._verify_live_protection(engine.b,position)[0])
        exchange.order_facts[99]['clientOrderId'] = 'foreign'
        self.assertFalse(adapter._verify_live_protection(engine.b,position)[0])

    def test_verified_protection_can_be_rechecked_after_sqlite_restart(self):
        adapter, engine, position, exchange = self.live_single()
        self.assertTrue(adapter._verify_live_protection(engine.b,position)[0])
        previous = adapter.state_store
        adapter.state_store = StateStore(previous.path,previous.db_path)
        self.assertTrue(adapter._verify_live_protection(engine.b,position)[0])

    def test_more_than_one_page_of_entry_fills_can_prove_coverage(self):
        adapter, engine, position, exchange = self.live_single(quantity='2')
        position.filled_qty = Decimal('2')
        exchange.order_facts[1].update(origQty='2',executedQty='2',cummulativeQuoteQty='200')
        fill = exchange.fills[0]
        exchange.fills = [{**fill,'id':101+i,'qty':'.001','quoteQty':'.1'} for i in range(2000)]
        adapter.state_store.mirror_legacy_position(position.symbol,position)
        self.assertTrue(adapter._verify_live_protection(engine.b,position)[0])

    def read_trade(self, adapter):
        with adapter.state_store._connect() as connection:
            return dict(connection.execute('SELECT * FROM trade_records WHERE entry_order_id=1').fetchone())

    def test_authenticated_zero_fill_rejection_releases_durable_slot(self):
        adapter, engine, position, exchange = self.fixture()
        position.filled_qty = Decimal('0')
        position.entry_price = Decimal('0')
        position.entry_client_id = 'FORTRESS_owned_entry'
        position.bracket = False
        position.order_list_id = position.tp_order_id = position.sl_order_id = None
        position.state = __import__('services.execution_sidecar.core_adapter',fromlist=['legacy']).legacy.PosState.PENDING_ENTRY
        exchange.order_facts = {1: {**exchange.order_facts[1], 'status':'REJECTED',
                                   'executedQty':'0','cummulativeQuoteQty':'0','orderListId':-1}}
        exchange.fills = []
        adapter.mirror_positions = lambda status='': adapter.state_store.mirror_legacy_position(position.symbol,position)
        adapter.state_store.mirror_legacy_position(position.symbol,position)
        adapter.state_store.record_exchange_event({'e':'executionReport','s':'ETHUSDT',
            'i':1,'I':301,'E':int(time()*1000),'S':'BUY','o':'LIMIT','X':'REJECTED','z':'0','Z':'0'})
        self.assertIsNotNone(adapter.state_store._active_trade_id('ETH/USDT'))
        engine._pending = {'ETHUSDT':{'p':position,'t0':time(),'repriced':0}}
        adapter._await_fill_interposed(engine,'ETHUSDT')
        self.assertIsNone(adapter.state_store._active_trade_id('ETH/USDT'))
        self.assertEqual(self.read_trade(adapter)['lifecycle_state'],'ERROR')
        self.assertNotIn('ETHUSDT',engine.pf.positions)
        self.assertNotIn('ETHUSDT',engine._pending)
        self.assertEqual(exchange.sent,[])


    def test_verified_advanced_mode_survives_periodic_projection(self):
        adapter, engine, position, exchange = self.live_single()
        position.state = __import__('services.execution_sidecar.core_adapter',fromlist=['legacy']).legacy.PosState.ARMED_TRAIL
        self.assertTrue(adapter._verify_live_protection(engine.b,position)[0])
        position._sidecar_lifecycle = 'PROFIT_LOCKED'
        position._sidecar_protection_mode = 'TRAILING_ONLY'
        position._sidecar_protected_quantity = '.7'
        adapter.state_store.mirror_legacy_position(position.symbol,position)
        del position._sidecar_lifecycle
        adapter.state_store.mirror_legacy_position(position.symbol,position)
        row = self.read_trade(adapter)
        self.assertEqual(row['pair'],'ETH/USDT')
        self.assertEqual(row['lifecycle_state'],'PROFIT_LOCKED')
        self.assertEqual(row['protection_mode'],'TRAILING_ONLY')
        self.assertEqual(Decimal(row['protected_quantity']),Decimal('.7'))


    def test_zero_fill_recovery_waits_for_complete_facts_then_releases_slot(self):
        for failure in ('unknown','wrong-symbol','missing-quantity','live-child','fill-event','race'):
            with self.subTest(failure=failure):
                adapter, engine, position, exchange = self.fixture()
                position.filled_qty = Decimal('0')
                position.entry_price = Decimal('0')
                position.entry_client_id = 'FORTRESS_owned_entry'
                position.bracket = False
                position.order_list_id = position.tp_order_id = position.sl_order_id = None
                position.state = __import__('services.execution_sidecar.core_adapter',fromlist=['legacy']).legacy.PosState.PENDING_ENTRY
                exchange.order_facts = {1:{**exchange.order_facts[1],'status':'REJECTED',
                    'executedQty':'0','cummulativeQuoteQty':'0','orderListId':-1}}
                exchange.fills = []
                adapter.state_store.mirror_legacy_position(position.symbol,position)
                original = dict(exchange.order_facts[1])
                if failure == 'unknown':
                    exchange.order_facts[1] = TimeoutError('unavailable')
                elif failure == 'wrong-symbol':
                    exchange.order_facts[1]['symbol'] = 'ADAUSDT'
                elif failure == 'missing-quantity':
                    exchange.order_facts[1].pop('executedQty')
                elif failure == 'live-child':
                    exchange.order_facts[99] = {'symbol':'ETHUSDT','orderId':99,'status':'NEW'}
                else:
                    def event():
                        adapter.state_store.record_exchange_event({'e':'executionReport','s':'ETHUSDT',
                            'i':1,'I':401,'E':int(time()*1000),'S':'BUY','o':'LIMIT','X':'PARTIALLY_FILLED',
                            'z':'.1' if failure == 'fill-event' else '0','Z':'10'})
                    if failure == 'fill-event':
                        event()
                    else:
                        old = exchange.get_open_orders
                        def during(**params):
                            event()
                            return old(**params)
                        exchange.get_open_orders = during
                self.assertFalse(adapter._complete_zero_fill_entry(engine.b,engine.pf,position))
                self.assertIsNotNone(adapter.state_store._active_trade_id('ETH/USDT'))
                self.assertIn('ETHUSDT',engine.pf.positions)
                self.assertEqual(exchange.sent,[])
                if failure in {'unknown','wrong-symbol','missing-quantity','live-child'}:
                    exchange.order_facts = {1:original}
                    self.assertEqual(adapter._service_reconcile_positions(),[])
                    self.assertIsNone(adapter.state_store._active_trade_id('ETH/USDT'))


    def test_status_event_does_not_certify_protection_or_full_position_exit(self):
        for status in ('NEW','PARTIALLY_FILLED','FILLED'):
            with self.subTest(status=status):
                adapter, engine, position, exchange = self.live_single()
                with adapter.state_store._connect() as connection:
                    connection.execute("UPDATE trade_records SET lifecycle_state='ENTRY_FILLED'")
                adapter.state_store.record_exchange_event({'e':'executionReport','s':'ETHUSDT',
                    'i':99,'I':200,'E':int(time()*1000),'S':'SELL','o':'STOP_LOSS_LIMIT',
                    'X':status,'q':'.7','z':'.2','l':'.2','n':'0','N':'USDT'})
                self.assertEqual(self.read_trade(adapter)['lifecycle_state'],'RECONCILIATION_REQUIRED')

    def test_legacy_state_cannot_publish_active_protection_without_coverage(self):
        adapter, engine, position, exchange = self.live_single()
        position._sidecar_lifecycle = 'PROTECTION_ACTIVE'
        adapter.state_store.mirror_legacy_position(position.symbol,position)
        self.assertEqual(self.read_trade(adapter)['lifecycle_state'],'RECONCILIATION_REQUIRED')

    def test_new_event_invalidates_previous_proof_until_reverified(self):
        adapter, engine, position, exchange = self.live_single()
        self.assertTrue(adapter._verify_live_protection(engine.b,position)[0])
        position._sidecar_lifecycle = 'PROTECTION_ACTIVE'
        adapter.state_store.mirror_legacy_position(position.symbol,position)
        self.assertIn(self.read_trade(adapter)['lifecycle_state'],{'PROTECTION_ACTIVE','TRAILING_ACTIVE'})
        adapter.state_store.record_exchange_event({'e':'executionReport','s':'ETHUSDT',
            'i':99,'I':201,'E':int(time()*1000),'S':'SELL','o':'STOP_LOSS_LIMIT',
            'X':'PARTIALLY_FILLED','q':'.7','z':'.2','l':'.2','n':'0','N':'USDT'})
        adapter.state_store.mirror_legacy_position(position.symbol,position)
        self.assertEqual(self.read_trade(adapter)['lifecycle_state'],'RECONCILIATION_REQUIRED')
        self.assertFalse(adapter._verify_live_protection(engine.b,position)[0],
                         'REST snapshots must not regress a durably journaled partial fill')
        exchange.order_facts[99].update(status='PARTIALLY_FILLED',executedQty='.2')
        exchange.fills.append({'symbol':'ETHUSDT','id':102,'orderId':99,'qty':'.2',
            'quoteQty':'20','price':'100','commission':'0','commissionAsset':'USDT',
            'isBuyer':False,'time':exchange.order_facts[1]['time']+20})
        self.assertTrue(adapter._verify_live_protection(engine.b,position)[0])
        adapter.state_store.mirror_legacy_position(position.symbol,position)
        self.assertIn(self.read_trade(adapter)['lifecycle_state'],{'PROTECTION_ACTIVE','TRAILING_ACTIVE'})
        self.assertNotIn('COVERAGE_PENDING',self.read_trade(adapter)['reconciliation_status'])

    def test_other_symbol_reusing_order_id_cannot_update_owned_trade(self):
        adapter, engine, position, exchange = self.live_single()
        before = self.read_trade(adapter)
        adapter.state_store.record_exchange_event({'e':'executionReport','s':'ADAUSDT',
            'i':99,'I':202,'E':int(time()*1000),'S':'SELL','o':'STOP_LOSS_LIMIT',
            'X':'FILLED','q':'100','z':'100','l':'100','n':'0','N':'USDT'})
        self.assertEqual(self.read_trade(adapter),before)

    def test_event_arriving_during_snapshot_prevents_stale_proof_commit(self):
        adapter, engine, position, exchange = self.live_single()
        original = exchange.get_open_orders
        def racing(**params):
            adapter.state_store.record_exchange_event({'e':'executionReport','s':'ETHUSDT',
                'i':99,'I':203,'E':int(time()*1000),'S':'SELL','o':'STOP_LOSS_LIMIT',
                'X':'PARTIALLY_FILLED','q':'.7','z':'.2','l':'.2','n':'0','N':'USDT'})
            return original(**params)
        exchange.get_open_orders = racing
        self.assertFalse(adapter._verify_live_protection(engine.b,position)[0])

    def test_cross_instance_event_cannot_be_overwritten_by_stale_legacy_projection(self):
        adapter, engine, position, exchange = self.live_single()
        self.assertTrue(adapter._verify_live_protection(engine.b,position)[0])
        position._sidecar_lifecycle = 'PROTECTION_ACTIVE'
        store = adapter.state_store
        other = StateStore(store.path,store.db_path)
        original = store.upsert_trade
        def racing(*args, **kwargs):
            other.record_exchange_event({'e':'executionReport','s':'ETHUSDT',
                'i':99,'I':204,'E':int(time()*1000),'S':'SELL','o':'STOP_LOSS_LIMIT',
                'X':'CANCELED','q':'.7','z':'0','l':'0','n':'0','N':'USDT'})
            return original(*args,**kwargs)
        store.upsert_trade = racing
        store.mirror_legacy_position(position.symbol,position)
        self.assertEqual(self.read_trade(adapter)['lifecycle_state'],'RECONCILIATION_REQUIRED')

    def test_stale_state_save_cannot_resurrect_an_invalidated_coverage_proof(self):
        adapter, engine, position, exchange = self.live_single()
        self.assertTrue(adapter._verify_live_protection(engine.b,position)[0])
        store = adapter.state_store
        other = StateStore(store.path,store.db_path)
        other.record_exchange_event({'e':'executionReport','s':'ETHUSDT',
            'i':99,'I':205,'E':int(time()*1000),'S':'SELL','o':'STOP_LOSS_LIMIT',
            'X':'CANCELED','q':'.7','z':'0','l':'0','n':'0','N':'USDT'})
        store.save()
        position._sidecar_lifecycle = 'PROTECTION_ACTIVE'
        store.mirror_legacy_position(position.symbol,position)
        self.assertEqual(self.read_trade(adapter)['lifecycle_state'],'RECONCILIATION_REQUIRED')


if __name__ == '__main__':
    unittest.main()
