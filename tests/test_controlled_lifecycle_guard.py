"""Prevent a rehearsal from crossing Testnet, ownership, and replay boundaries."""
import importlib.util
import json
from pathlib import Path
import sqlite3
from urllib.parse import urlencode

import pytest

SPEC = importlib.util.spec_from_file_location('controlled', Path(__file__).parents[1]/'scripts/recovery/probe_controlled_owner_lifecycle.py')
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


@pytest.fixture
def guard(tmp_path, monkeypatch):
    # AWS uses directory fsync; Windows test filesystem supports only file fsync.
    monkeypatch.setattr(module, "sync_directory", lambda path: None)
    db = tmp_path/'state.sqlite'
    with sqlite3.connect(db) as conn:
        conn.executescript('''CREATE TABLE protection(intent_id,pair,generation,mode,status,
            list_client_id,order_list_id,working_client_id,tp_client_id,sl_client_id,expected_qty,payload_json);
            CREATE TABLE fill_ledger(intent_id,pair,source_kind,side,base_qty,fee_asset,fee_amount);''')
        conn.execute('INSERT INTO protection VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
            ('test','LTC/USDT',1,'FIXED_OCO','SUBMISSION_PENDING','list','17','working','tp','sl','2',json.dumps({'plan':{
                'quantity':'2','reference_price':'100','tp_price':'102','stop_limit':'98','stop_trigger':'99'}})))
    return module.TransportGuard(tmp_path, db, 'test', 'entry')


def entry():
    return {'symbol':'LTCUSDT','workingSide':'BUY','workingType':'LIMIT','workingTimeInForce':'FOK',
        'pendingSide':'SELL','listClientOrderId':'list','workingClientOrderId':'working',
        'pendingAboveClientOrderId':'tp','pendingBelowClientOrderId':'sl','pendingAboveType':'LIMIT_MAKER',
        'pendingBelowType':'STOP_LOSS_LIMIT','workingQuantity':'2','workingPrice':'100','pendingQuantity':'2',
        'pendingAbovePrice':'102','pendingBelowPrice':'98','pendingBelowStopPrice':'99'}


def submit(guard, params=None):
    guard.validate('https://testnet.binance.vision/api/v3/orderList/otoco','POST',{'X-MBX-APIKEY':'test'},urlencode(params or entry()))


@pytest.mark.parametrize('url', ['https://api.binance.com/api/v3/orderList/otoco',
    'https://testnet.binance.vision.evil.example/api/v3/orderList/otoco',
    'http://testnet.binance.vision/api/v3/orderList/otoco',
    'https://user@testnet.binance.vision/api/v3/orderList/otoco',
    'https://testnet.binance.vision:444/api/v3/orderList/otoco'])
def test_rejects_credential_destination(guard,url):
    with pytest.raises(RuntimeError):
        guard.validate(url,'POST',{'X-MBX-APIKEY':'test'},urlencode(entry()))
    assert not guard.journal['attempts']


def test_restart_never_repeats_ambiguous_entry(guard):
    submit(guard)  # journal is written before any transport result
    restarted = module.TransportGuard(guard.path.parent,guard.state,guard.intent,'entry')
    with pytest.raises(RuntimeError,match='REPEATED_MUTATION'):
        submit(restarted)


@pytest.mark.parametrize('key,value',[('workingQuantity','3'),('workingPrice','126'),
    ('workingClientOrderId','foreign'),('pendingBelowStopPrice','1'),('pendingQuantity','3')])
def test_entry_drift_and_overspend_denied(guard,key,value):
    params = entry()
    params[key] = value
    with pytest.raises(RuntimeError):
        submit(guard,params)
    assert not guard.journal['attempts']


def test_foreign_cancel_denied(guard):
    with pytest.raises(RuntimeError,match='FOREIGN_LIST'):
        guard.validate('https://testnet.binance.vision/api/v3/orderList?symbol=LTCUSDT&orderListId=999',
                       'DELETE',{'X-MBX-APIKEY':'test'})


def test_sell_cannot_consume_preexisting_inventory_or_base_commission(guard):
    with sqlite3.connect(guard.state) as conn:
        conn.execute("INSERT INTO fill_ledger VALUES('test','LTC/USDT','exchange_trade','BUY','2','LTC','0.01')")
        conn.execute("UPDATE protection SET mode='OPERATOR_EXIT',expected_qty='2'")
    params = {'symbol':'LTCUSDT','side':'SELL','type':'MARKET','quantity':'2','newClientOrderId':'tp'}
    with pytest.raises(RuntimeError,match='SELL_OWNED_QUANTITY'):
        guard.validate('https://testnet.binance.vision/api/v3/order','POST',{'X-MBX-APIKEY':'test'},urlencode(params))
    with sqlite3.connect(guard.state) as conn:
        conn.execute("UPDATE protection SET expected_qty='1.99'")
    params['quantity']='1.99'
    guard.validate('https://testnet.binance.vision/api/v3/order','POST',{'X-MBX-APIKEY':'test'},urlencode(params))
    assert len(guard.journal['attempts']) == 1


def test_duplicate_signed_parameter_rejected(guard):
    with pytest.raises(RuntimeError,match='DUPLICATE_REQUEST_FIELD'):
        guard.validate('https://testnet.binance.vision/api/v3/order?symbol=LTCUSDT','POST',
                       {'X-MBX-APIKEY':'test'},'symbol=BTCUSDT')


def test_entry_forbidden_in_restart_phase(guard):
    guard.phase='restart'
    with pytest.raises(RuntimeError,match='ENTRY_PHASE'):
        submit(guard)


def test_conflicting_cancel_identifiers_never_transmitted(guard):
    with pytest.raises(RuntimeError,match='FOREIGN_LIST'):
        guard.validate('https://testnet.binance.vision/api/v3/orderList?symbol=LTCUSDT&orderListId=999&listClientOrderId=list',
                       'DELETE',{'X-MBX-APIKEY':'test'})
    assert not guard.journal['attempts']


def test_websocket_handshake_redirect_rejected(guard, monkeypatch):
    import websockets
    import requests
    import aiohttp
    from websockets.asyncio.connection import Connection
    original = (requests.Session.send,aiohttp.ClientSession._request,websockets.connect,Connection.send)
    try:
        guard.install()
        connection = websockets.connect('wss://ws-api.testnet.binance.vision/ws-api/v3')
        result = connection.process_redirect(RuntimeError('simulated redirect'))
        assert isinstance(result, RuntimeError)
        assert str(result) == 'LIFECYCLE_WEBSOCKET_REDIRECT_DENIED'
    finally:
        requests.Session.send,aiohttp.ClientSession._request,websockets.connect,Connection.send = original


def test_cleanup_timeout_becomes_reportable_error(monkeypatch):
    import subprocess
    spec = importlib.util.spec_from_file_location('controlled_runner', Path(__file__).parents[1]/'scripts/recovery/run_controlled_owner_lifecycle.py')
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired('docker',20)
    monkeypatch.setattr(runner.subprocess,'run',timeout)
    assert runner.stop_rehearsal('only-rehearsal') == 'TimeoutExpired'


@pytest.mark.parametrize('default_headers,params', [({'X-MBX-APIKEY':'test'},{}), ({},{'signature':'test'})])
def test_async_effective_credentials_cannot_reach_public_production(guard,default_headers,params):
    import asyncio
    import aiohttp
    import requests
    import websockets
    from websockets.asyncio.connection import Connection
    original = (requests.Session.send,aiohttp.ClientSession._request,websockets.connect,Connection.send)
    transmitted = []
    async def network(*args, **kwargs):
        transmitted.append(True)
        raise AssertionError('network should never be reached')
    async def exercise():
        async with aiohttp.ClientSession(headers=default_headers) as session:
            with pytest.raises(RuntimeError):
                await session.get('https://api.binance.com/api/v3/account',params=params)
    try:
        aiohttp.ClientSession._request = network
        guard.install()
        asyncio.run(exercise())
        assert not transmitted
    finally:
        requests.Session.send,aiohttp.ClientSession._request,websockets.connect,Connection.send = original


def test_restart_signature_covers_economics_fees_and_identity():
    import copy
    baseline = {'canonical':{'trades':[{'id':86,'amount':2,'orders':[{'id':'4','cost':200}],
                                      'receipts':{'fee':'0.01'},'economics':{'quantity':'1.99'}}]}}
    assert module.canonical_signature(baseline) == module.canonical_signature(copy.deepcopy(baseline))
    for field,value in [('amount',3),('receipts',{'fee':'0'}),('orders',[{'id':'5','cost':200}])]:
        changed = copy.deepcopy(baseline)
        changed['canonical']['trades'][0][field]=value
        assert module.canonical_signature(baseline) != module.canonical_signature(changed)


def test_other_pair_cannot_spend_ltc_receipts(guard):
    with sqlite3.connect(guard.state) as conn:
        conn.execute("UPDATE protection SET pair='ADA/USDT',mode='OPERATOR_EXIT',expected_qty='2'")
        conn.execute("INSERT INTO fill_ledger VALUES('test','LTC/USDT','exchange_trade','BUY','2','LTC','0')")
    eth = module.TransportGuard(guard.path.parent,guard.state,'test','exit',pair='ADA/USDT',mutation_budget=20)
    with pytest.raises(RuntimeError,match='SELL_OWNED_QUANTITY'):
        eth.validate('https://testnet.binance.vision/api/v3/order','POST',{'X-MBX-APIKEY':'test'},
            'symbol=ADAUSDT&side=SELL&type=MARKET&quantity=2&newClientOrderId=tp')


def test_slot_router_denies_unassigned_pair(guard):
    import sys
    previous = sys.modules.get('probe_base')
    sys.modules['probe_base'] = module
    try:
        spec = importlib.util.spec_from_file_location('slot_soak',Path(__file__).parents[1]/'scripts/recovery/probe_owner_slot_soak.py')
        soak = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(soak)
        router = soak.SlotGuard(guard.path.parent,guard.state,{'LTC/USDT':'test'},'entry')
        with pytest.raises(RuntimeError,match='SOAK_FOREIGN_PAIR'):
            router.validate('https://testnet.binance.vision/api/v3/order','POST',{'X-MBX-APIKEY':'test'},
                'symbol=ADAUSDT&side=SELL&type=MARKET&quantity=2&newClientOrderId=tp')
    finally:
        if previous is None:
            del sys.modules['probe_base']
        else:
            sys.modules['probe_base'] = previous


def test_soak_transport_budget_remains_bounded(guard):
    with pytest.raises(RuntimeError,match='REHEARSAL_SCOPE'):
        module.TransportGuard(guard.path.parent,guard.state,'test','entry',mutation_budget=1000)
    with pytest.raises(RuntimeError,match='REHEARSAL_SCOPE'):
        module.TransportGuard(guard.path.parent,guard.state,'test','entry',pair='BTC/USDT')


@pytest.fixture
def oco_guard(guard):
    with sqlite3.connect(guard.state) as conn:
        conn.execute("INSERT INTO fill_ledger VALUES('test','LTC/USDT','exchange_trade','BUY','2','USDT','0')")
        plan = json.loads(conn.execute('SELECT payload_json FROM protection').fetchone()[0])
        plan['plan']['mode'] = 'FIXED_OCO'
        conn.execute('UPDATE protection SET payload_json=?', (json.dumps(plan),))
    guard.phase = 'restart'
    return guard


def oco_request(guard, *, trailing=False):
    params = {'symbol':'LTCUSDT','side':'SELL','quantity':'2','listClientOrderId':'list',
        'aboveClientOrderId':'tp','belowClientOrderId':'sl','aboveType':'LIMIT_MAKER',
        'abovePrice':'102','belowType':'STOP_LOSS_LIMIT','belowPrice':'98',
        'belowStopPrice':'99','belowTimeInForce':'GTC','newOrderRespType':'FULL'}
    if trailing:
        with sqlite3.connect(guard.state) as conn:
            payload = json.loads(conn.execute('SELECT payload_json FROM protection').fetchone()[0])
            payload['plan'].update(mode='TRAILING_OCO',stop_limit=None,stop_trigger=None,trailing_delta_bips=75)
            conn.execute("UPDATE protection SET mode='TRAILING_OCO',payload_json=?", (json.dumps(payload),))
        for key in ('belowPrice','belowStopPrice','belowTimeInForce'):
            params.pop(key)
        params.update(belowType='STOP_LOSS',belowTrailingDelta='75')
    return params


def submit_oco(guard, params):
    guard.validate('https://testnet.binance.vision/api/v3/orderList/oco','POST',
        {'X-MBX-APIKEY':'test'},urlencode(params))


@pytest.mark.parametrize('trailing', [False, True])
def test_matching_oco_plan_is_admitted_once(oco_guard, trailing):
    params = oco_request(oco_guard, trailing=trailing)
    params.update(quantity='2.000',abovePrice='102.0',timestamp='123',recvWindow='5000',signature='test')
    submit_oco(oco_guard, params)
    assert len(oco_guard.journal['attempts']) == 1
    with pytest.raises(RuntimeError,match='REPEATED_MUTATION'):
        submit_oco(oco_guard, params)


@pytest.mark.parametrize('key,value', [
    ('aboveClientOrderId','foreign'),('belowClientOrderId','foreign'),
    ('aboveType','TAKE_PROFIT_LIMIT'),('belowType','STOP_LOSS'),
    ('abovePrice','103'),('belowPrice','1'),('belowStopPrice','1'),
    ('belowTimeInForce','IOC'),('belowTrailingDelta','75'),
    ('aboveTrailingDelta','75'),('aboveStopPrice','103'),('abovePegPriceType','MARKET_PEG'),
    ('belowIcebergQty','1'),('abovePrice','NaN'),('belowPrice','Infinity')])
def test_fixed_oco_parameter_drift_is_denied_before_journal(oco_guard, key, value):
    params = oco_request(oco_guard)
    params[key] = value
    with pytest.raises(RuntimeError,match='OCO_'):
        submit_oco(oco_guard, params)
    assert not oco_guard.journal['attempts']


@pytest.mark.parametrize('key,value', [
    ('belowTrailingDelta','76'),('belowTrailingDelta','75.5'),
    ('belowStopPrice','99'),('belowPrice','98'),('belowTimeInForce','GTC'),
    ('belowType','STOP_LOSS_LIMIT'),('aboveClientOrderId','foreign'),
    ('belowClientOrderId','foreign'),('abovePrice','103')])
def test_trailing_oco_parameter_drift_is_denied(oco_guard, key, value):
    params = oco_request(oco_guard, trailing=True)
    params[key] = value
    with pytest.raises(RuntimeError,match='OCO_'):
        submit_oco(oco_guard, params)
    assert not oco_guard.journal['attempts']


@pytest.mark.parametrize('key', ['aboveClientOrderId','belowClientOrderId','aboveType',
    'belowType','abovePrice','belowPrice','belowStopPrice','belowTimeInForce'])
def test_missing_fixed_oco_parameters_are_denied(oco_guard, key):
    params = oco_request(oco_guard)
    params.pop(key)
    with pytest.raises(RuntimeError,match='OCO_'):
        submit_oco(oco_guard, params)
    assert not oco_guard.journal['attempts']


@pytest.mark.parametrize('field,value', [('mode','TRAILING_OCO'),('quantity','3')])
def test_oco_generation_and_saved_plan_must_agree(oco_guard, field, value):
    params = oco_request(oco_guard)
    with sqlite3.connect(oco_guard.state) as conn:
        payload = json.loads(conn.execute('SELECT payload_json FROM protection').fetchone()[0])
        payload['plan'][field] = value
        conn.execute('UPDATE protection SET payload_json=?', (json.dumps(payload),))
    with pytest.raises(RuntimeError,match='OCO_'):
        submit_oco(oco_guard, params)
    assert not oco_guard.journal['attempts']



def test_residual_promotion_without_durable_plan_is_not_certified(oco_guard):
    params = oco_request(oco_guard, trailing=True)
    # This is the immutable owner's cancel/fill-race payload shape. It cannot
    # prove the intended replacement geometry, so rehearsal must stop closed.
    with sqlite3.connect(oco_guard.state) as conn:
        conn.execute('UPDATE protection SET payload_json=?',
            (json.dumps({'market':{},'from_generation':1,'residual_after_old_fill':'2'}),))
    with pytest.raises(RuntimeError,match='OCO_MALFORMED_PLAN_OR_REQUEST_DENIED'):
        submit_oco(oco_guard, params)
    assert not oco_guard.journal['attempts']
