"""One bounded Testnet OTOCO FOK no-fill rehearsal using candidate transport.

This is a matching-engine test, not a release certificate or a trading resume.
Unexpected fills require immediate protection verification and reconciliation.
"""
import json
import os
from pathlib import Path
import time
import re
from urllib.parse import urlsplit

HOST = 'testnet.binance.vision'
GET_PATHS = {'/api/v3/exchangeInfo', '/api/v3/ticker/bookTicker', '/api/v3/avgPrice',
             '/api/v3/openOrders', '/api/v3/openOrderList', '/api/v3/account/commission',
             '/api/v3/order', '/api/v3/orderList', '/api/v3/myTrades', '/api/v3/account'}


def check_transport(url, method):
    target = urlsplit(url)
    if (target.scheme != 'https' or target.hostname != HOST
            or target.port not in (None, 443) or target.username or target.password
            or target.fragment
            or not ((method == 'GET' and target.path in GET_PATHS)
                    or (method == 'POST' and target.path == '/api/v3/orderList/otoco'))):
        raise RuntimeError('OTOCO_REHEARSAL_TRANSPORT_DENIED')


def save(report):
    path = Path('/probe/report.json')
    with path.with_suffix('.tmp').open('w') as stream:
        json.dump(report, stream, indent=2, default=str)
        stream.flush()
        os.fsync(stream.fileno())
    path.with_suffix('.tmp').replace(path)
    descriptor = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def restrict_session(session, report):
    original = session.send
    def guarded(request, **kwargs):
        check_transport(request.url, request.method)
        if request.method == 'POST':
            if report['matching_engine_post_attempts'] != 0 or 'plan' not in report:
                raise RuntimeError('SECOND_OR_UNJOURNALED_POST_DENIED')
            report['matching_engine_post_attempts'] = 1
            save(report)  # durable before transmission; an ambiguous POST is never replayed
        kwargs['allow_redirects'] = False
        response = original(request, **kwargs)
        if 300 <= response.status_code < 400:
            raise RuntimeError('OTOCO_REHEARSAL_REDIRECT_DENIED')
        return response
    session.send = guarded


def initialize_report(report, path=Path('/probe/report.json')):
    # Exclusive creation preserves an ambiguous prior attempt across restarts.
    with path.open('x') as stream:
        json.dump(report, stream, default=str)
        stream.flush()
        os.fsync(stream.fileno())


def safe_error(exc):
    # CCXT exception text may embed signed URLs. Emit only a numeric API code.
    match = re.search(r'"code"\s*:\s*(-?\d+)', str(exc))
    return {'type': type(exc).__name__, 'exchange_code': int(match.group(1)) if match else None}


def query_list_after_ack(lists, ids, response, attempts=10, wait=time.sleep):
    # Binance may acknowledge an immediately expired list before its query
    # indexes are visible. Retry reads only; preserve the acknowledged identity.
    order_list_id = response.get('orderListId') if isinstance(response, dict) else None
    for attempt in range(attempts):
        try:
            current = lists.query_list(order_list_id=str(order_list_id)) if order_list_id is not None else lists.query_list(list_client_id=ids['list'])
            if order_list_id is not None and str(current.get('orderListId')) != str(order_list_id):
                raise RuntimeError('ACKNOWLEDGED_LIST_ID_MISMATCH')
            if current.get('listClientOrderId') != ids['list'] or current.get('symbol') != 'LTCUSDT':
                raise RuntimeError('LIST_IDENTITY_MISMATCH')
            return current
        except Exception as exc:
            # Only the exchange's explicit not-found visibility response is
            # retried here. Transport and identity errors retain ambiguity.
            if safe_error(exc)['exchange_code'] != -2013 or attempt + 1 == attempts:
                raise
            wait(1)
    raise RuntimeError('LIST_QUERY_ATTEMPTS_EXHAUSTED')


def main():
    import ccxt
    from decimal import Decimal, ROUND_DOWN
    from types import SimpleNamespace
    from freqtrade.binana.binance_order_lists import BinanceOrderLists, _client
    from freqtrade.binana.environment import validate_runtime_contract
    from freqtrade.binana.execution_manager import BinanaExecutionManager
    from freqtrade.binana.protection_plan import provisional_plan
    from freqtrade.binana.registry import OwnerRegistry

    report = {'started_at': time.time(), 'passed': False, 'matching_engine_post_attempts': 0,
              'authenticated_lifecycle_acceptance': False, 'bounded_nominal_usdt': '250',
              'unexpected_fill_requires_reconciliation': False}
    initialize_report(report)
    save(report)
    try:
        config = json.loads(Path('/probe/policy.json').read_text())
        validate_runtime_contract(config)
        if Decimal(str(config['stake_amount'])) != Decimal('250'):
            raise RuntimeError('UNEXPECTED_STAKE_POLICY')
        pair = 'LTC/USDT'
        registry = OwnerRegistry('/probe/halal_coins.json')
        intent = 'rehearsal-' + Path('/probe/run_id').read_text().strip()
        if pair not in json.loads(Path('/probe/pairlist.json').read_text())['pairs'] or not registry.decide(intent_id=intent, pair=pair).allowed:
            raise RuntimeError('PAIR_NOT_CURRENTLY_LISTED')
        api = ccxt.binance({'apiKey': os.environ['FREQTRADE__EXCHANGE__KEY'],
                           'secret': os.environ['FREQTRADE__EXCHANGE__SECRET'],
                           'enableRateLimit': True, 'timeout': 10000,
                           'options': {'defaultType': 'spot'}})
        api.set_sandbox_mode(True)
        api.urls['apiBackup'] = {}
        for kind in ('private', 'public'):
            if api.urls['api'][kind] != 'https://' + HOST + '/api/v3':
                raise RuntimeError('TESTNET_ENDPOINT_MISMATCH')
        restrict_session(api.session, report)
        lists = BinanceOrderLists(api)
        if api.privateGetOpenOrders() or api.privateGetOpenOrderList():
            raise RuntimeError('OPEN_EXCHANGE_OBLIGATIONS')
        account = api.privateGetAccount()
        if account.get('accountType') != 'SPOT' or account.get('canTrade') is not True:
            raise RuntimeError('SPOT_ACCOUNT_UNAVAILABLE')
        info = lists.symbol_info(pair)
        rules = {x['filterType']: x for x in info['filters']}
        step = Decimal(rules['LOT_SIZE']['stepSize'])
        tick = Decimal(rules['PRICE_FILTER']['tickSize'])
        book = api.publicGetTickerBookTicker({'symbol': 'LTCUSDT'})
        # Resting below the bid should expire FOK. This is still a real Testnet
        # order: a fast market move could fill it; protection and identities persist.
        price = (Decimal(book['bidPrice']) * Decimal('0.995') / tick).to_integral_value(rounding=ROUND_DOWN) * tick
        quantity = (Decimal('250') / price / step).to_integral_value(rounding=ROUND_DOWN) * step
        reserve = BinanaExecutionManager.entry_commission_reserve(SimpleNamespace(exchange=SimpleNamespace(_api=api)), pair)
        protected = (quantity * (1-reserve) / step).to_integral_value(rounding=ROUND_DOWN)*step
        policy = config['binana']
        plan = provisional_plan(intent_id=intent, pair=pair, quantity=quantity, entry_limit=price,
            tick_size=tick, target_fraction=policy['fixed_target_fraction'], stop_fraction=policy['normal_stop_fraction'],
            stop_limit_buffer_fraction=policy['stop_limit_buffer_fraction'], estimated_exit_fee_fraction=policy['estimated_exit_fee_fraction'])
        report.update(intent_id=intent, pair=pair, plan=plan.__dict__, protected_quantity=str(protected),
                      preflight=lists.preflight(plan, pending_quantity=protected, otoco=True),
                      ids={role: _client(prefix, intent, 1, label) for role, prefix, label in [
                          ('list', 'L', 'list'), ('working', 'W', 'working'),
                          ('take_profit', 'T', 'takeprofit'), ('stop', 'S', 'stop')]})
        save(report)
        try:
            response, ids = lists.submit_fixed_otoco(plan, pending_quantity=protected)
            report['submission_response'] = response
            save(report)
        except Exception as exc:
            report['submission_error'] = safe_error(exc)
            save(report)
            # Query deterministic identity once; never retry the POST after any error.
            response = query_list_after_ack(lists, report['ids'], None)
            report['recovered_submission'] = response
            save(report)
        terminal = {'FILLED', 'CANCELED', 'EXPIRED', 'EXPIRED_IN_MATCH', 'REJECTED'}
        for _ in range(10):
            current = query_list_after_ack(lists, report['ids'], response)
            if current.get('listClientOrderId') != report['ids']['list'] or current.get('symbol') != 'LTCUSDT':
                raise RuntimeError('LIST_IDENTITY_MISMATCH')
            rows = [api.privateGetOrder({'symbol': 'LTCUSDT', 'origClientOrderId': report['ids'][role]})
                    for role in ('working', 'take_profit', 'stop')]
            expected_clients = {report['ids'][role] for role in ('working', 'take_profit', 'stop')}
            if len(rows) != 3 or {row.get('clientOrderId') for row in rows} != expected_clients or any(row.get('symbol') != 'LTCUSDT' for row in rows):
                raise RuntimeError('ORDER_IDENTITY_MISMATCH')
            report['queried_list'], report['queried_orders'] = current, rows
            save(report)
            if any(Decimal(row['executedQty']) != 0 for row in rows):
                report['unexpected_fill_requires_reconciliation'] = True
                report['pending_protection_verified'] = all(row['status'] == 'NEW' for row in rows[1:])
                raise RuntimeError('UNEXPECTED_FILL_PROTECTION_MUST_BE_RECONCILED')
            if all(row['status'] in terminal for row in rows):
                break
            time.sleep(1)
        else:
            raise RuntimeError('ORDERS_NOT_TERMINAL')
        for row in rows:
            if api.privateGetMyTrades({'symbol': 'LTCUSDT', 'orderId': row['orderId']}):
                raise RuntimeError('UNEXPECTED_EXECUTION_HISTORY')
        if api.privateGetOpenOrders() or api.privateGetOpenOrderList():
            raise RuntimeError('REHEARSAL_LEFT_OPEN_OBLIGATIONS')
        report.update(passed=True, finished_at=time.time(), zero_fill_verified=True,
                      no_open_exchange_obligations=True)
    except Exception as exc:
        report['error'] = safe_error(exc)
    save(report)
    print(json.dumps({k: report.get(k) for k in ['passed', 'error', 'matching_engine_post_attempts',
                      'unexpected_fill_requires_reconciliation', 'zero_fill_verified', 'ids']}))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
