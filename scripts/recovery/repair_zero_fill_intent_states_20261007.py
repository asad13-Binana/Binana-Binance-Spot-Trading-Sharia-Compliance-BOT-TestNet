"""Restore 25 authenticated no-fill intent states without changing accounting."""
import argparse
from decimal import Decimal
from pathlib import Path
import json
import sqlite3

from repair_retained_intent_states_20261007 import evidence, require, snapshot

AUDIT_SHA = '3b0039b6313f75bf1fe8ac7bfe499b4a40f177a29644b7ecd30a1bba583d6dd3'
SCOPE = 'binance-testnet|owner-testnet-account-1|binana-20260910-owner-cutover-1'
TERMINAL = {'EXPIRED', 'EXPIRED_IN_MATCH', 'CANCELED', 'REJECTED'}


def repair(connection, audit, *, inject_failure=False):
    before = snapshot(connection, 'main')
    canonical = snapshot(connection, 'canonical')
    require(not any(row['is_open'] for row in canonical['tables']['trades']), 'Open canonical positions')
    require(not audit['errors'] and not audit['account_open_orders'] and not audit['account_open_lists'],
            'Exchange evidence incomplete or outstanding obligations')
    targets = audit['targets']
    require(len(targets) == 25 and len({row['intent_id'] for row in targets}) == 25,
            'Unexpected target identities')
    require(len(audit['evidence']) == 25, 'Unexpected evidence count')
    expected_intents = [dict(row) for row in before['tables']['intents']]
    changed = 0
    for target in targets:
        iid, pair = target['intent_id'], target['pair']
        matches = [row for row in expected_intents if row['intent_id'] == iid]
        require(len(matches) == 1, 'Intent not unique')
        intent = matches[0]
        require(intent['pair'] == pair and intent['freqtrade_trade_id'] is None
                and intent['state'] in {target['state'], 'CLOSED_NO_FILL'}, 'Intent changed since audit')
        generations = [row for row in before['tables']['protection'] if row['intent_id'] == iid]
        require(len(generations) == 1, 'Unexpected protection generations')
        generation = generations[0]
        require(generation['status'] == 'CLOSED_NO_FILL' and generation['generation'] == 1,
                'Saved generation not terminal no-fill')
        for key, value in target.items():
            if key not in {'state', 'freqtrade_trade_id'}:
                require(generation[key] == value, 'Protection identity changed since audit')
        require(not any(row['intent_id'] == iid for row in before['tables']['fill_ledger']),
                'Unexpected persisted fills')
        reports = [row for row in audit['evidence'] if row['intent_id'] == iid]
        require(len(reports) == 1, 'Exchange evidence not unique')
        report = reports[0]
        order_list = report['list']
        require(str(order_list['orderListId']) == generation['order_list_id']
                and order_list['listClientOrderId'] == generation['list_client_id']
                and order_list['symbol'] == pair.replace('/', '')
                and order_list['listOrderStatus'] == 'ALL_DONE', 'Exchange list not conclusively terminal')
        bindings = [row for row in before['tables']['order_identity'] if row['intent_id'] == iid]
        require(len(bindings) == 3 and len(report['orders']) == 3, 'Incomplete order identities')
        require({row['role'] for row in report['orders']} == {'working', 'tp', 'sl'}, 'Invalid order roles')
        expected_list_orders = set()
        for item in report['orders']:
            role, order = item['role'], item['order']
            oid, cid = generation[role + '_order_id'], generation[role + '_client_id']
            expected_list_orders.add((oid, cid))
            require(str(order['orderId']) == oid and order['clientOrderId'] == cid
                    and str(order['orderListId']) == generation['order_list_id']
                    and order['symbol'] == pair.replace('/', '')
                    and order['side'] == ('BUY' if role == 'working' else 'SELL')
                    and order['status'] in TERMINAL and Decimal(order['executedQty']) == 0
                    and Decimal(order['cummulativeQuoteQty']) == 0 and not item['trades'],
                    'Order is not an authenticated terminal no-fill')
            if role == 'working':
                require(order['timeInForce'] == 'FOK' and order['type'] == 'LIMIT', 'Unexpected entry type')
            matched = [row for row in bindings if row['order_id'] == oid and row['client_id'] == cid]
            require(len(matched) == 1 and matched[0]['scope_id'] == SCOPE
                    and matched[0]['pair'] == pair and matched[0]['generation'] == 1,
                    'Persisted order binding mismatch')
            require(not any(row['symbol'] == pair and str(row['order_id']) == oid
                            for row in canonical['tables']['orders']), 'Unexpected canonical order')
        require({(str(row['orderId']), row['clientOrderId']) for row in order_list['orders']}
                == expected_list_orders, 'Order list membership mismatch')
        if intent['state'] != 'CLOSED_NO_FILL':
            connection.execute('UPDATE intents SET state=? WHERE intent_id=?', ('CLOSED_NO_FILL', iid))
            intent['state'] = 'CLOSED_NO_FILL'
            changed += 1
    expected = {'schema': before['schema'], 'tables': {**before['tables'], 'intents': expected_intents}}
    require(snapshot(connection, 'main') == expected, 'Unexpected extension changes')
    require(snapshot(connection, 'canonical') == canonical, 'Canonical database changed')
    if inject_failure:
        raise RuntimeError('Injected failure before commit')
    return {'intent_states_changed': changed, 'canonical_unchanged': True,
            'incidents_unchanged': True, 'other_extension_fields_unchanged': True}


def run(args):
    database, canonical = Path(args.database), Path(args.canonical)
    require(all(path.is_file() and not path.is_symlink() for path in [database, canonical]), 'Invalid DB paths')
    require(database.resolve() != canonical.resolve(), 'Database paths must differ')
    audit = evidence(args.audit, AUDIT_SHA)
    with sqlite3.connect(f'file:{database.resolve()}?mode=rw', uri=True, timeout=1) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute('ATTACH DATABASE ? AS canonical', (f'file:{canonical.resolve()}?mode=rw',))
        connection.execute('BEGIN IMMEDIATE')
        result = repair(connection, audit, inject_failure=args.inject_failure)
        connection.commit()
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ['database', 'canonical', 'audit']:
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--inject-failure', action='store_true')
    print(json.dumps(run(parser.parse_args())))
