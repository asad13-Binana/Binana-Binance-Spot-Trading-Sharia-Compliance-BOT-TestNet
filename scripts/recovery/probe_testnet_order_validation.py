"""Validate candidate order parameters on Spot Testnet without matching orders.

Run only in the immutable owner candidate with /probe inputs prepared by the
host runner. This supplies limited API evidence, never a release certificate.
"""
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import urlsplit


HOST = 'testnet.binance.vision'
GET_PATHS = {'/api/v3/exchangeInfo', '/api/v3/ticker/bookTicker',
             '/api/v3/avgPrice', '/api/v3/openOrders', '/api/v3/openOrderList',
             '/api/v3/account/commission'}


def check_transport(url, method):
    target = urlsplit(url)
    if (target.scheme != 'https' or target.hostname != HOST
            or target.port not in (None, 443) or target.username or target.password
            or target.fragment
            or not ((method == 'GET' and target.path in GET_PATHS)
                    or (method == 'POST' and target.path == '/api/v3/order/test'))):
        raise RuntimeError('ORDER_VALIDATION_TRANSPORT_DENIED')
    return {'method': method, 'host': target.hostname, 'path': target.path}


def restrict_session(session):
    original_send = session.send

    def send(request, **kwargs):
        check_transport(request.url, request.method)
        # Requests follows redirects below CCXT.fetch, so guard that boundary too.
        kwargs['allow_redirects'] = False
        response = original_send(request, **kwargs)
        if 300 <= response.status_code < 400:
            raise RuntimeError('ORDER_VALIDATION_REDIRECT_DENIED')
        return response

    session.send = send


def save(report):
    target = Path('/probe/report.json')
    with target.with_suffix('.tmp').open('w') as stream:
        json.dump(report, stream, indent=2, default=str)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    target.with_suffix('.tmp').replace(target)


def main():
    import ccxt
    from decimal import Decimal, ROUND_DOWN, ROUND_UP
    from types import SimpleNamespace
    from freqtrade.binana.binance_order_lists import BinanceOrderLists
    from freqtrade.binana.environment import assert_spot_pair, validate_runtime_contract
    from freqtrade.binana.execution_manager import BinanaExecutionManager
    from freqtrade.binana.protection_plan import provisional_plan
    from freqtrade.binana.registry import OwnerRegistry

    report = {'started_at': time.time(), 'matching_engine_orders_submitted': 0,
              'authenticated_lifecycle_acceptance': False, 'requests': [], 'pairs': [],
              'passed': False}
    original = ccxt.Exchange.fetch

    def guarded(self, url, method='GET', headers=None, body=None):
        report['requests'].append(check_transport(url, method))
        return original(self, url, method, headers, body)

    ccxt.Exchange.fetch = guarded
    try:
        config = json.loads(Path('/probe/policy.json').read_text())
        validate_runtime_contract(config)
        api = ccxt.binance({'apiKey': os.environ['FREQTRADE__EXCHANGE__KEY'],
                            'secret': os.environ['FREQTRADE__EXCHANGE__SECRET'],
                            'enableRateLimit': True, 'timeout': 10000,
                            'options': {'defaultType': 'spot'}})
        api.set_sandbox_mode(True)
        api.urls['apiBackup'] = {}
        restrict_session(api.session)
        for kind in ['private', 'public']:
            if api.urls['api'][kind] != 'https://' + HOST + '/api/v3':
                raise RuntimeError('TESTNET_ENDPOINT_MISMATCH')
        lists = BinanceOrderLists(api)
        registry = OwnerRegistry('/probe/halal_coins.json')
        universe = set(json.loads(Path('/probe/pairlist.json').read_text())['pairs'])
        if api.privateGetOpenOrders() or api.privateGetOpenOrderList():
            raise RuntimeError('VALIDATION_REQUIRES_NO_OPEN_ORDERS')
        policy = config['binana']
        # Different price and quantity steps; all must be currently admitted.
        for pair in ['LTC/USDT', 'LINK/USDT', 'ADA/USDT', 'NEAR/USDT', 'PAXG/USDT']:
            assert_spot_pair(pair, side='long', leverage=1)
            if pair not in universe or not registry.decide(intent_id='validation-'+pair, pair=pair).allowed:
                raise RuntimeError('VALIDATION_PAIR_NOT_ADMITTED')
            info = lists.symbol_info(pair)
            filters = {row['filterType']: row for row in info['filters']}
            step = Decimal(filters['LOT_SIZE']['stepSize'])
            tick = Decimal(filters['PRICE_FILTER']['tickSize'])
            book = api.publicGetTickerBookTicker({'symbol': pair.replace('/', '')})
            price = (Decimal(book['askPrice']) / tick).to_integral_value(rounding=ROUND_UP)*tick
            quantity = (Decimal(str(config['stake_amount'])) / price / step).to_integral_value(rounding=ROUND_DOWN)*step
            # Exercise the candidate's exact authenticated commission contract.
            fee = BinanaExecutionManager.entry_commission_reserve(
                SimpleNamespace(exchange=SimpleNamespace(_api=api)), pair)
            protected = (quantity*(1-fee)/step).to_integral_value(rounding=ROUND_DOWN)*step
            plan = provisional_plan(intent_id='validation-'+pair, pair=pair, quantity=quantity,
                entry_limit=price, tick_size=tick, target_fraction=policy['fixed_target_fraction'],
                stop_fraction=policy['normal_stop_fraction'],
                stop_limit_buffer_fraction=policy['stop_limit_buffer_fraction'],
                estimated_exit_fee_fraction=policy['estimated_exit_fee_fraction'])
            preflight = lists.preflight(plan, pending_quantity=protected, otoco=True)
            row = {'pair': pair, 'notional': str(quantity*price), 'preflight': preflight,
                   'quantity': str(quantity), 'protected_quantity': str(protected),
                   'commission_reserve': str(fee), 'tests': []}
            report['pairs'].append(row)
            save(report)
            # Individual test endpoint validates signatures/filters, not OTOCO activation,
            # balances after fill, partial fills, cancellation, or protection recovery.
            for role, params in [
                ('working', {'side': 'BUY', 'type': 'LIMIT', 'timeInForce': 'FOK',
                             'price': format(plan.reference_price, 'f'), 'quantity': format(quantity, 'f')}),
                ('take_profit', {'side': 'SELL', 'type': 'LIMIT_MAKER',
                                 'price': format(plan.tp_price, 'f'), 'quantity': format(protected, 'f')}),
                ('stop', {'side': 'SELL', 'type': 'STOP_LOSS_LIMIT', 'timeInForce': 'GTC',
                          'price': format(plan.stop_limit, 'f'), 'stopPrice': format(plan.stop_trigger, 'f'),
                          'quantity': format(protected, 'f')})]:
                response = api.privatePostOrderTest({'symbol': pair.replace('/', ''), **params})
                if response != {}:
                    raise RuntimeError('UNEXPECTED_ORDER_TEST_RESPONSE')
                row['tests'].append({'role': role, 'passed': True})
                save(report)
        report['open_orders_after'] = len(api.privateGetOpenOrders())
        report['open_lists_after'] = len(api.privateGetOpenOrderList())
        report['passed'] = (len(report['pairs']) == 5
                            and all(len(row['tests']) == 3 for row in report['pairs'])
                            and report['open_orders_after'] == report['open_lists_after'] == 0)
    except Exception as exc:
        # CCXT exceptions can contain signed request URLs. Never emit those.
        report['error_type'] = type(exc).__name__
        match = re.search(r'"code"\s*:\s*(-?\d+)', str(exc))
        if match:
            report['exchange_error_code'] = int(match.group(1))
    finally:
        ccxt.Exchange.fetch = original
        report['completed_at'] = time.time()
        save(report)
        print(json.dumps({k: v for k, v in report.items() if k != 'requests'}))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
