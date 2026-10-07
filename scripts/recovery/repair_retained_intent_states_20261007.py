"""Restore three proven historical retained states; never clear incidents or trade."""
import argparse
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import time


SCOPE = 'binance-testnet|owner-testnet-account-1|binana-20260910-owner-cutover-1'
TARGETS = {
    34: ('b752fdc844dd9567bab221248bfacb72', 'NEAR/USDT', 'REJECTED_SEQUENCE_BOOK'),
    47: ('f6369b499ccfe59c2c28d85a1f64c4cb', 'ONE/USDT', 'REJECTED_FLOW'),
    53: ('f6ed0ceeda10b9aa494d6977c967d030', 'LPT/USDT', 'REJECTED_FLOW'),
}
AUDIT_SHA = '719d578c707c056ba8b5abeb1cdd23d8501c03029097e41a51efed3a098f5b4f'
TERMINAL_SHA = '28f805ea5aee8d67a7f5d17b2c77635b90bec8c19c1ed0722f00410170882d09'
RETENTION_AUDIT_SHA = 'b80e4ee2f077a813b782cb3323c5b82ee09073dd0bcd8b151729ede742f4880e'


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def evidence(path, expected):
    raw = Path(path).read_bytes()
    require(sha256(raw).hexdigest() == expected, 'Evidence digest mismatch')
    document = json.loads(raw)
    require(0 <= time.time() - document['generated_at'] < 7200, 'Stale evidence')
    return document


def snapshot(connection, namespace):
    require(namespace in {'main', 'canonical'}, 'Invalid namespace')
    schema = [dict(row) for row in connection.execute(
        f'SELECT type,name,tbl_name,sql FROM {namespace}.sqlite_master ORDER BY type,name')]
    tables = {}
    for item in schema:
        if item['type'] == 'table':
            name = '"' + item['name'].replace('"', '""') + '"'
            tables[item['name']] = sorted(
                [dict(row) for row in connection.execute(f'SELECT * FROM {namespace}.{name}')],
                key=lambda row: repr(sorted(row.items())))
    return {'schema': schema, 'tables': tables}


def repair(connection, audit, terminal, *, inject_failure=False):
    before = snapshot(connection, 'main')
    canonical = snapshot(connection, 'canonical')
    require(not any(row['is_open'] for row in canonical['tables']['trades']), 'Open canonical trades')
    require(all(audit['summary'][key] == 0 for key in
                ['quantity_mismatches', 'cost_mismatches', 'identity_unverified', 'errors', 'incomplete_pairs']),
            'Unverified economic audit')
    terminal_orders = {(row['symbol'], str(row['orderId'])): row for row in terminal['orders']}
    expected_intents = [dict(row) for row in before['tables']['intents']]
    changed = 0
    for trade_id, (intent_id, pair, old_state) in TARGETS.items():
        intents = [row for row in expected_intents if row['intent_id'] == intent_id]
        require(len(intents) == 1, 'Missing unique intent')
        intent = intents[0]
        require(intent['freqtrade_trade_id'] == trade_id and intent['pair'] == pair
                and intent['state'] in {old_state, 'DUST_RETAINED'}, 'Unexpected intent state')
        trades = [row for row in canonical['tables']['trades'] if row['id'] == trade_id]
        require(len(trades) == 1 and trades[0]['pair'] == pair, 'Canonical identity mismatch')
        trade = trades[0]
        retained_rows = [row for row in canonical['tables']['trade_custom_data']
                         if row['ft_trade_id'] == trade_id and row['cd_key'] == 'binana_retained_dust']
        require(len(retained_rows) == 1, 'Missing retention proof')
        retained = json.loads(retained_rows[0]['cd_value'])
        require(retained['phase'] == 'retained' and retained['scope'] == SCOPE
                and retained['trade_id'] == trade_id and retained['pair'] == pair
                and retained['evidence_sha256'] == RETENTION_AUDIT_SHA
                and retained['repair_version'] == 'authenticated-retention-replay-v1'
                and retained['non_executable'] is True, 'Invalid retention proof')
        audited = next(row for row in audit['trades'] if row['id'] == trade_id)
        quantity, basis = Decimal(retained['quantity']), Decimal(retained['cost_basis'])
        require(quantity > 0 and basis > 0
                and abs(quantity - Decimal(str(trade['amount']))) < Decimal('0.00000001')
                and abs(quantity - Decimal(audited['owned_residual'])) < Decimal('0.00000001')
                and abs(basis - Decimal(str(trade['stake_amount']))) < Decimal('0.0000001')
                and abs(Decimal(audited['cash_pnl_usdt']) + basis
                        - Decimal(str(trade['close_profit_abs']))) < Decimal('0.0000001'),
                'Canonical retained economics mismatch')
        generations = [row for row in before['tables']['protection'] if row['intent_id'] == intent_id]
        require(generations and max(generations, key=lambda row: row['generation'])['status'] == 'DUST_RETAINED',
                'Latest protection is not retained')
        bindings = [row for row in before['tables']['order_identity'] if row['intent_id'] == intent_id]
        require(len(bindings) == 5, 'Unexpected order identity set')
        for binding in bindings:
            exchange = terminal_orders[(pair.replace('/', ''), str(binding['order_id']))]
            require(binding['scope_id'] == SCOPE and binding['pair'] == pair
                    and exchange['clientOrderId'] == binding['client_id']
                    and exchange['status'] in {'FILLED', 'CANCELED', 'EXPIRED', 'EXPIRED_IN_MATCH', 'REJECTED'},
                    'Order not conclusively terminal')
        orders = [row for row in canonical['tables']['orders'] if row['ft_trade_id'] == trade_id]
        require(orders and not any(row['ft_is_open'] for row in orders), 'Canonical orders not terminal')
        for order in orders:
            if order['filled']:
                rows = [row for row in audit['orders'] if row['trade_id'] == trade_id
                        and str(row['order_id']) == str(order['order_id'])]
                require(len(rows) == 1 and rows[0]['client_identity_matches'] is True
                        and all(order[key] == value for key, value in rows[0]['canonical_order'].items()),
                        'Canonical order changed since authenticated audit')
        if intent['state'] != 'DUST_RETAINED':
            connection.execute('UPDATE intents SET state=? WHERE intent_id=?', ('DUST_RETAINED', intent_id))
            intent['state'] = 'DUST_RETAINED'
            changed += 1
    after = snapshot(connection, 'main')
    expected = {'schema': before['schema'], 'tables': {**before['tables'], 'intents': expected_intents}}
    require(after == expected, 'Unexpected extension change')
    require(snapshot(connection, 'canonical') == canonical, 'Canonical database changed')
    if inject_failure:
        raise RuntimeError('Injected failure before commit')
    return {'intent_states_changed': changed, 'canonical_unchanged': True,
            'incidents_unchanged': True, 'other_extension_fields_unchanged': True}


def run(args):
    database, canonical = Path(args.database), Path(args.canonical)
    require(all(path.is_file() and not path.is_symlink() for path in [database, canonical]), 'Invalid DB path')
    require(database.resolve() != canonical.resolve(), 'Database paths must differ')
    audit, terminal = evidence(args.economic, AUDIT_SHA), evidence(args.terminal, TERMINAL_SHA)
    with sqlite3.connect(f'file:{database.resolve()}?mode=rw', uri=True, timeout=1) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute('ATTACH DATABASE ? AS canonical', (f'file:{canonical.resolve()}?mode=rw',))
        connection.execute('BEGIN IMMEDIATE')
        result = repair(connection, audit, terminal, inject_failure=args.inject_failure)
        connection.commit()
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ['database', 'canonical', 'economic', 'terminal']:
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--inject-failure', action='store_true')
    print(json.dumps(run(parser.parse_args())))
