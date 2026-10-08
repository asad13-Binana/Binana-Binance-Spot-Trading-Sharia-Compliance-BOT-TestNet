"""Controlled Testnet execution on cloned databases, never a release certificate.

Manual test admission is explicitly separate from strategy signal admission.
The candidate submit, canonical recovery, protection and operator exit paths
are unchanged. Each phase is a fresh process with a persistent request journal.
"""
import glob
import json
import logging
import os
from pathlib import Path
import sqlite3
import sys
import time
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from urllib.parse import parse_qs, urlsplit

PAIR = 'LTC/USDT'
SYMBOL = 'LTCUSDT'
ROOT = Path('/probe')
STATE = Path('/freqtrade/shared/freqtrade/binana-extension.sqlite')
PUBLIC_HOSTS = {'api.binance.com', 'api1.binance.com', 'api2.binance.com',
                'api3.binance.com', 'api4.binance.com', 'data-api.binance.vision'}
TESTNET = 'testnet.binance.vision'


def durable(path, value):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as stream:
        json.dump(value, stream, indent=2, default=str)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    sync_directory(path.parent)


def sync_directory(path):
    descriptor = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def safe_error(exc):
    import re
    match = re.search(r'"code"\s*:\s*(-?\d+)', str(exc))
    reason = str(exc) if re.fullmatch(r'[A-Z][A-Z0-9_]{0,120}', str(exc)) else None
    return {'type': type(exc).__name__, 'exchange_code': int(match.group(1)) if match else None, 'reason':reason}


def request_values(url, body):
    values = parse_qs(urlsplit(url).query, keep_blank_values=True)
    if body:
        for key, rows in parse_qs(body.decode() if isinstance(body, bytes) else body, keep_blank_values=True).items():
            if key in values:
                raise RuntimeError('DUPLICATE_REQUEST_FIELD')
            values[key] = rows
    if any(len(rows) != 1 for rows in values.values()):
        raise RuntimeError('DUPLICATE_REQUEST_FIELD')
    return {key: rows[0] for key, rows in values.items()}


def validate_oco_plan(params, generation):
    """Bind every transmitted OCO leg field to the persisted protection plan."""
    try:
        plan = json.loads(generation['payload_json'])['plan']
        mode = generation['mode']
        if plan['mode'] != mode or mode not in {'FIXED_OCO', 'TRAILING_OCO'}:
            raise RuntimeError('OCO_PLAN_MODE_DENIED')
        expected = {'symbol':generation['pair'].replace('/', ''), 'side':'SELL',
                    'listClientOrderId':generation['list_client_id'],
                    'aboveClientOrderId':generation['tp_client_id'],
                    'belowClientOrderId':generation['sl_client_id'],
                    'aboveType':'LIMIT_MAKER', 'newOrderRespType':'FULL'}
        numbers = {'quantity':plan['quantity'], 'abovePrice':plan['tp_price']}
        if mode == 'FIXED_OCO':
            expected.update(belowType='STOP_LOSS_LIMIT', belowTimeInForce='GTC')
            numbers.update(belowPrice=plan['stop_limit'], belowStopPrice=plan['stop_trigger'])
        else:
            expected['belowType'] = 'STOP_LOSS'
            delta = Decimal(str(plan['trailing_delta_bips']))
            if not delta.is_finite() or delta <= 0 or delta != delta.to_integral_value():
                raise RuntimeError('OCO_TRAILING_DELTA_DENIED')
            expected['belowTrailingDelta'] = str(int(delta))
        allowed = set(expected) | set(numbers) | {'timestamp','recvWindow','signature'}
        if set(params) - allowed or any(params.get(k) != v for k,v in expected.items()):
            raise RuntimeError('OCO_IDENTITY_TYPE_OR_FIELDS_DENIED')
        for key,value in numbers.items():
            actual, intended = Decimal(params[key]), Decimal(str(value))
            if not actual.is_finite() or not intended.is_finite() or actual <= 0 or actual != intended:
                raise RuntimeError('OCO_PROTECTION_PLAN_DENIED')
        if Decimal(str(plan['quantity'])) != Decimal(generation['expected_qty']):
            raise RuntimeError('OCO_GENERATION_QUANTITY_DENIED')
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        raise RuntimeError('OCO_MALFORMED_PLAN_OR_REQUEST_DENIED') from exc


class TransportGuard:
    def __init__(self, root, state, intent, phase, *, pair=PAIR, mutation_budget=10):
        if pair not in {"LTC/USDT","LINK/USDT","ADA/USDT","NEAR/USDT"} or mutation_budget not in {10,20}:
            raise RuntimeError("REHEARSAL_SCOPE_DENIED")
        self.pair, self.symbol, self.mutation_budget = pair, pair.replace("/", ""), mutation_budget
        self.path, self.state, self.intent, self.phase = root/'transport.json', state, intent, phase
        self.journal = json.loads(self.path.read_text()) if self.path.exists() else {'attempts': {}}
        self.read_count = 0
        self.denials = []

    def validate(self, url, method, headers=None, body=None):
        target = urlsplit(url)
        authenticated = any(key.lower() == 'x-mbx-apikey' for key in (headers or {}))
        if (target.scheme != 'https' or target.hostname not in PUBLIC_HOSTS | {TESTNET, 'api.coingecko.com'}
                or target.port not in (None, 443) or target.username or target.password or target.fragment
                or (authenticated and target.hostname != TESTNET)):
            raise RuntimeError('LIFECYCLE_ENDPOINT_DENIED')
        params = request_values(url, body)
        if target.hostname != TESTNET and any(k in params for k in ('signature', 'apiKey')):
            raise RuntimeError('LIFECYCLE_CREDENTIAL_DESTINATION_DENIED')
        if method == 'GET':
            if target.hostname == 'api.coingecko.com':
                if target.path != '/api/v3/coins/list':
                    raise RuntimeError('UNEXPECTED_OPTIONAL_DISPLAY_LOOKUP')
            elif not target.path.startswith('/api/v3/'):
                raise RuntimeError('LIFECYCLE_READ_PATH_DENIED')
            self.read_count += 1
            return
        if target.hostname != TESTNET or not authenticated or params.get('symbol') != self.symbol:
            raise RuntimeError('LIFECYCLE_MUTATION_DESTINATION_DENIED')
        with sqlite3.connect('file:'+str(self.state)+'?mode=ro', uri=True) as connection:
            connection.row_factory = sqlite3.Row
            generations = [dict(row) for row in connection.execute(
                'SELECT * FROM protection WHERE intent_id=? AND pair=?', (self.intent, self.pair))]
            fills = [dict(row) for row in connection.execute(
                'SELECT * FROM fill_ledger WHERE intent_id=? AND pair=?', (self.intent, self.pair))]
        match = None
        if method == 'POST' and target.path == '/api/v3/orderList/otoco':
            if self.phase != 'entry' or len(generations) != 1:
                raise RuntimeError('ENTRY_PHASE_OR_GENERATION_DENIED')
            match = generations[0]
            plan = json.loads(match['payload_json'])['plan']
            expected = {'workingSide': 'BUY', 'workingType': 'LIMIT', 'workingTimeInForce': 'FOK',
                        'pendingSide': 'SELL', 'listClientOrderId': match['list_client_id'],
                        'workingClientOrderId': match['working_client_id'],
                        'pendingAboveClientOrderId': match['tp_client_id'],
                        'pendingBelowClientOrderId': match['sl_client_id'],
                        'pendingAboveType': 'LIMIT_MAKER', 'pendingBelowType': 'STOP_LOSS_LIMIT'}
            if any(params.get(key) != value for key, value in expected.items()):
                raise RuntimeError('ENTRY_IDENTITY_OR_TYPE_DENIED')
            quantity, price = Decimal(params['workingQuantity']), Decimal(params['workingPrice'])
            if (not 0 < quantity*price <= Decimal('250')
                    or quantity != Decimal(str(plan['quantity']))
                    or price != Decimal(str(plan['reference_price']))
                    or Decimal(params['pendingQuantity']) != Decimal(match['expected_qty'])):
                raise RuntimeError('ENTRY_BOUND_DENIED')
            for key, field in [('pendingAbovePrice','tp_price'), ('pendingBelowPrice','stop_limit'),
                               ('pendingBelowStopPrice','stop_trigger')]:
                if Decimal(params[key]) != Decimal(str(plan[field])):
                    raise RuntimeError('ENTRY_PROTECTION_PLAN_DENIED')
            identity = match['list_client_id']
        elif method == 'DELETE' and target.path == '/api/v3/orderList':
            supplied = {key:params[key] for key in ('orderListId','listClientOrderId') if key in params}
            fields = {'orderListId':'order_list_id','listClientOrderId':'list_client_id'}
            match = next((row for row in generations if supplied and all(
                row[fields[key]] is not None and str(row[fields[key]]) == value
                for key, value in supplied.items())), None)
            if match is None:
                raise RuntimeError('FOREIGN_LIST_CANCEL_DENIED')
            identity = match['list_client_id']
        elif method == 'POST' and target.path in {'/api/v3/order', '/api/v3/orderList/oco'}:
            client = params.get('newClientOrderId') if target.path == '/api/v3/order' else params.get('listClientOrderId')
            match = next((row for row in generations if client and client == (
                row['tp_client_id'] if target.path == '/api/v3/order' else row['list_client_id'])), None)
            if match is None or params.get('side') != 'SELL':
                raise RuntimeError('FOREIGN_SELL_DENIED')
            # Authenticated immutable receipts for this rehearsal alone bound the sale.
            owned = Decimal('0')
            for row in fills:
                if row['source_kind'] != 'exchange_trade':
                    raise RuntimeError('UNVERIFIED_FILL_DENIED')
                sign = 1 if row['side'].upper() == 'BUY' else -1
                owned += sign * Decimal(row['base_qty'])
                if row['fee_asset'] == self.pair.split('/')[0]:
                    owned -= Decimal(row['fee_amount'])
            quantity = Decimal(params['quantity'])
            if not 0 < quantity <= owned or quantity != Decimal(match['expected_qty']):
                raise RuntimeError('SELL_OWNED_QUANTITY_DENIED')
            if target.path == '/api/v3/order' and (params.get('type') != 'MARKET' or match['mode'] not in {'OPERATOR_EXIT','TARGET_EXIT','STOP_EXIT'}):
                raise RuntimeError('SELL_ORDER_TYPE_DENIED')
            if target.path == '/api/v3/orderList/oco':
                validate_oco_plan(params, match)
            identity = client
        else:
            raise RuntimeError('LIFECYCLE_MUTATION_PATH_DENIED')
        key = method+' '+target.path+' '+identity
        if self.journal['attempts'].get(key):
            raise RuntimeError('REPEATED_MUTATION_DENIED')
        if len(self.journal['attempts']) >= self.mutation_budget:
            raise RuntimeError('REHEARSAL_MUTATION_BUDGET_EXHAUSTED')
        self.journal['attempts'][key] = {'at': time.time(), 'phase': self.phase}
        durable(self.path, self.journal)  # before transmission, including ambiguous outcomes

    def install(self):
        import requests
        import aiohttp
        import websockets
        from websockets.asyncio.connection import Connection
        original = requests.Session.send
        guard = self
        def send(session, request, **kwargs):
            try:
                guard.validate(request.url, request.method, request.headers, request.body)
            except Exception as exc:
                guard.denials.append({'method':request.method,'host':urlsplit(request.url).hostname,
                                      'path':urlsplit(request.url).path,'error_type':type(exc).__name__})
                raise
            kwargs['allow_redirects'] = False
            response = original(session, request, **kwargs)
            if 300 <= response.status_code < 400:
                raise RuntimeError('LIFECYCLE_REDIRECT_DENIED')
            return response
        requests.Session.send = send
        original_async = aiohttp.ClientSession._request
        async def request(session, method, url, **kwargs):
            if method != 'GET':
                raise RuntimeError('ASYNC_MUTATION_DENIED')
            from yarl import URL
            effective_url = str(URL(url).extend_query(kwargs.get('params') or {}))
            effective_headers = dict(session.headers)
            effective_headers.update(kwargs.get('headers') or {})
            if kwargs.get('json') is not None or kwargs.get('auth') is not None:
                raise RuntimeError('ASYNC_ALTERNATIVE_CREDENTIAL_FORM_DENIED')
            try:
                guard.validate(effective_url, method, effective_headers, kwargs.get('data'))
            except Exception as exc:
                guard.denials.append({'method':method,'host':urlsplit(effective_url).hostname,
                                      'path':urlsplit(effective_url).path,'error_type':type(exc).__name__})
                raise
            kwargs['allow_redirects'] = False
            response = await original_async(session, method, url, **kwargs)
            if 300 <= response.status < 400:
                response.close()
                raise RuntimeError('LIFECYCLE_REDIRECT_DENIED')
            return response
        aiohttp.ClientSession._request = request
        connect, ws_send = websockets.connect, Connection.send
        if not hasattr(connect, 'process_redirect'):
            raise RuntimeError('WEBSOCKET_REDIRECT_CONTROL_UNAVAILABLE')
        class NoRedirectConnect(connect):
            def process_redirect(self, exc):
                return RuntimeError('LIFECYCLE_WEBSOCKET_REDIRECT_DENIED')
        def guarded_connect(uri, *args, **kwargs):
            if uri != 'wss://ws-api.testnet.binance.vision/ws-api/v3':
                raise RuntimeError('LIFECYCLE_WEBSOCKET_ENDPOINT_DENIED')
            return NoRedirectConnect(uri, *args, **kwargs)
        async def guarded_ws_send(connection, message, *args, **kwargs):
            if json.loads(message).get('method') != 'userDataStream.subscribe.signature':
                raise RuntimeError('LIFECYCLE_WEBSOCKET_METHOD_DENIED')
            return await ws_send(connection, message, *args, **kwargs)
        websockets.connect, Connection.send = guarded_connect, guarded_ws_send


def snapshot(bot, intent, pair=PAIR):
    from freqtrade.persistence import Trade
    saved = bot.binana.store.get_intent(intent)
    trades = [trade for trade in Trade.get_trades_proxy(pair=pair)
              if (trade.binana_custom_data('binana_entry_identity') or {}).get('intent_id') == intent]
    result = {'intent': saved, 'canonical_ready': bot.binana.canonical_reconciliation_ready,
              'trades': [{'id': trade.id, 'is_open': trade.is_open, 'amount': trade.amount,
                          'stake_amount':trade.stake_amount, 'open_rate':trade.open_rate, 'close_rate':trade.close_rate,
                          'realized_profit':trade.realized_profit, 'close_profit_abs':trade.close_profit_abs,
                          'fee_open':trade.fee_open, 'fee_close':trade.fee_close,
                          'economics': trade.binana_economics(),
                          'receipts':trade.binana_custom_data('binana_fill_receipts'),
                          'open_canonical_orders':len(trade.open_orders),
                          'orders': [{'id': order.order_id, 'filled': order.filled, 'cost': order.cost}
                                     for order in trade.orders]} for trade in trades],
              'incidents': [{'id': row['incident_id'], 'code': row['code']} for row in bot.binana.store.unresolved()]}
    return result, trades


def account_totals(bot, assets=('LTC','USDT')):
    rows = bot.exchange._api.privateGetAccount()['balances']
    return {asset:str(sum((Decimal(row['free'])+Decimal(row['locked']) for row in rows if row['asset']==asset), Decimal('0')))
            for asset in assets}


def canonical_signature(report):
    return json.dumps(report['canonical']['trades'], sort_keys=True, default=str)


def main():
    sys.path[:0] = ['/freqtrade', *glob.glob('/home/ftuser/.local/lib/python*/site-packages')]
    from freqtrade.configuration import Configuration
    from freqtrade.enums import RunMode, State
    from freqtrade.freqtradebot import FreqtradeBot
    from freqtrade.persistence import Trade
    from freqtrade.binana.execution_manager import PreparedAdmission
    from freqtrade.binana.user_stream import health_is_fresh
    phase = sys.argv[1]
    if phase not in {'entry','restart','restart_again','exit','closed_restart'}:
        raise RuntimeError('UNKNOWN_REHEARSAL_PHASE')
    intent = 'controlled-'+(ROOT/'run_id').read_text().strip()
    report_path = ROOT/(phase+'.json')
    with report_path.open('x') as stream:
        json.dump({'started_at': time.time(), 'passed': False}, stream)
    report = {'started_at': time.time(), 'phase': phase, 'intent_id': intent,
              'release_certificate_created': False, 'strategy_admission_verified': False, 'passed': False}
    guard = TransportGuard(ROOT, STATE, intent, phase)
    guard.install()
    bot = None
    try:
        config = Configuration({'config': ['/probe/config.json'], 'strategy': 'BinanaNfiSpot',
                                'strategy_path': '/freqtrade/binana-strategies'}, RunMode.LIVE).get_config()
        if (config.get('dry_run') is not False or config.get('initial_state') != 'stopped'
                or config['db_url'] != 'sqlite:////freqtrade/shared/freqtrade/binana-owner.sqlite'
                or config['binana']['state_db_path'] != str(STATE)
                or config.get('cancel_open_orders_on_exit') is not True
                or Decimal(str(config['stake_amount'])) != 250 or config['max_open_trades'] != 4
                or any(config.get(name, {}).get('enabled') for name in ['telegram','api_server','webhook'])):
            raise RuntimeError('REHEARSAL_CONFIGURATION_MISMATCH')
        bot = FreqtradeBot(config)
        if bot.state != State.STOPPED:
            raise RuntimeError('REHEARSAL_MUST_REMAIN_STOPPED')
        bot.startup()
        report['account_before'] = account_totals(bot)
        if phase == 'entry':
            if Trade.get_open_trades() or bot.exchange._api.privateGetOpenOrders() or bot.exchange._api.privateGetOpenOrderList():
                raise RuntimeError('ENTRY_REQUIRES_EMPTY_OPEN_OBLIGATIONS')
            if bot.binana.store.get_intent(intent):
                raise RuntimeError('ENTRY_INTENT_ALREADY_EXISTS')
            decision = bot.binana.registry.decide(intent_id=intent, pair=PAIR)
            if not decision.allowed or PAIR not in bot.active_pair_whitelist:
                raise RuntimeError('CURRENT_REGISTRY_OR_UNIVERSE_REJECTED')
            market = bot.binana.market_data.refresh(PAIR, Decimal('250'))
            report['market'] = market.to_dict()
            # This is a manual controlled execution test, not a fabricated NFI signal.
            bot.binana.store.put_intent(intent_id=intent, pair=PAIR, signal_id='controlled_testnet_acceptance',
                halal_allowed=True, registry_sha256=decision.registry_sha256, nominal_usdt='250',
                admission={'kind':'explicit_controlled_testnet_rehearsal', 'strategy_signal':False})
            bot.binana.prepared[PAIR] = PreparedAdmission(intent, PAIR, 'controlled_testnet_acceptance',
                'NORMAL', market, decision.registry_sha256, Decimal('250'))
            info = bot.binana.order_lists.symbol_info(PAIR)
            filters = {row['filterType']: row for row in info['filters']}
            tick, step = Decimal(filters['PRICE_FILTER']['tickSize']), Decimal(filters['LOT_SIZE']['stepSize'])
            book = bot.binana.order_lists.public.publicGetTickerBookTicker({'symbol': SYMBOL})
            price = (Decimal(book['askPrice'])*Decimal('1.002')/tick).to_integral_value(rounding=ROUND_UP)*tick
            amount = (Decimal('250')/price/step).to_integral_value(rounding=ROUND_DOWN)*step
            report.update(quantity=str(amount), limit=str(price), nominal=str(amount*price))
            durable(report_path, report)
            result = bot.binana.submit_entry(pair=PAIR, amount=float(amount), rate=float(price), enter_tag='controlled_testnet_acceptance')
            report['working_response'] = result
            durable(report_path, report)
        for _ in range(15):
            bot.process_stopped()
            current, trades = snapshot(bot, intent)
            if phase == 'exit' and trades and trades[0].is_open:
                latest = bot.binana.store.latest_generation(intent)
                if latest['mode'] != 'OPERATOR_EXIT':
                    report['exit_request'] = bot.binana.request_operator_exit(trades[0], reason='controlled_testnet_acceptance')
            if phase in {'exit','closed_restart'}:
                ready = len(trades) == 1 and not trades[0].is_open and bot.binana.canonical_reconciliation_ready
            else:
                latest = bot.binana.store.latest_generation(intent)
                ready = (len(trades) == 1 and trades[0].is_open and latest
                         and latest['status'] in {'FIXED_ACTIVE','TRAILING_ACTIVE'}
                         and bot.binana.canonical_reconciliation_ready)
            # A just-closed exit is acknowledged by the next normal owner pass.
            transient_incidents = [row for row in current['incidents']
                                  if row['id'] != 'repair-economic-settlement-replay-20260920']
            if ready and not transient_incidents and health_is_fresh(bot.binana.user_stream.health()):
                break
            time.sleep(3)
        report['canonical'], trades = snapshot(bot, intent)
        report['private_stream_fresh'] = health_is_fresh(bot.binana.user_stream.health())
        report['rest'] = bot.binana._rest_health
        report['generations'] = []
        with bot.binana.store._connect() as connection:
            for row in connection.execute('SELECT * FROM protection WHERE intent_id=? ORDER BY generation', (intent,)):
                generation = dict(row)
                report['generations'].append(generation)
        report['open_exchange_orders'] = bot.exchange._api.privateGetOpenOrders()
        report['open_exchange_lists'] = bot.exchange._api.privateGetOpenOrderList()
        report['unexpected_incidents'] = [row for row in report['canonical']['incidents']
            if row['id'] != 'repair-economic-settlement-replay-20260920']
        if not ready or not report['private_stream_fresh'] or report['unexpected_incidents']:
            raise RuntimeError('CANONICAL_LIFECYCLE_PHASE_NOT_VERIFIED')
        if phase in {'exit','closed_restart'} and (report['open_exchange_orders'] or report['open_exchange_lists']):
            raise RuntimeError('REHEARSAL_OBLIGATIONS_REMAIN')
        if phase in {'restart','restart_again','closed_restart'}:
            baseline_phase = 'exit' if phase == 'closed_restart' else 'entry'
            baseline = json.loads((ROOT/(baseline_phase+'.json')).read_text())
            if not baseline.get('passed') or canonical_signature(report) != canonical_signature(baseline):
                raise RuntimeError('RESTART_CANONICAL_ACCOUNTING_OR_IDENTITY_CHANGED')
            report['restart_accounting_unchanged'] = True
        if any(row['open_canonical_orders'] for row in report['canonical']['trades']):
            raise RuntimeError('CANONICAL_OPEN_ORDER_AT_RESTART_BOUNDARY')
        report['account_after'] = account_totals(bot)
        if phase == 'closed_restart':
            baseline = json.loads((ROOT/'entry.json').read_text())['account_before']
            report['account_delta'] = {key:str(Decimal(value)-Decimal(baseline[key]))
                                       for key,value in report['account_after'].items()}
            if (Decimal(report['account_delta']['LTC']) != 0
                    or Decimal(report['account_delta']['USDT']) != Decimal(report['canonical']['trades'][0]['economics']['realized'])):
                raise RuntimeError('ACCOUNT_DELTA_DOES_NOT_MATCH_REHEARSAL_RECEIPTS')
            report['account_delta_matches_receipts'] = True
        if guard.denials:
            raise RuntimeError('REHEARSAL_TRANSPORT_DENIALS')
        report['passed'] = True
    except Exception as exc:
        report['error'] = safe_error(exc)
        if bot is not None:
            try:
                report['canonical'], _ = snapshot(bot, intent)
            except Exception as inspect_exc:
                report['inspection_error'] = safe_error(inspect_exc)
    finally:
        if bot is not None:
            try:
                before_cleanup, _ = snapshot(bot, intent)
                bot.cleanup()
                after_cleanup, _ = snapshot(bot, intent)
                if (canonical_signature({'canonical':before_cleanup}) != canonical_signature({'canonical':after_cleanup})
                        or any(row['open_canonical_orders'] for row in after_cleanup['trades']) or guard.denials):
                    report['cleanup_invariants_unchanged'] = False
                    report['passed'] = False
                else:
                    report['cleanup_invariants_unchanged'] = True
            except Exception as exc:
                report['cleanup_error'] = safe_error(exc)
                report['passed'] = False
        report.update(completed_at=time.time(), read_requests=guard.read_count, mutations=guard.journal, transport_denials=guard.denials)
        durable(report_path, report)
        print(json.dumps({key:report.get(key) for key in ['phase','passed','error','private_stream_fresh','unexpected_incidents']}))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    logging.disable(logging.CRITICAL)  # reports contain typed errors, never signed URLs
    raise SystemExit(main())
