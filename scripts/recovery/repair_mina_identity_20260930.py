"""Bind one historical canonical exit using its authenticated Testnet receipt."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import sys

from freqtrade.binana.state_store import StateStore

if sys.flags.optimize:
    raise RuntimeError("Optimized Python is forbidden for this guarded repair")

SCOPE = "binance-testnet|owner-testnet-account-1|binana-20260910-owner-cutover-1"
INTENT = "3ed714dee86baa583263391b5faeea01"
AUDIT_SHA = "b80e4ee2f077a813b782cb3323c5b82ee09073dd0bcd8b151729ede742f4880e"


def snapshot(connection, namespace='main'):
    assert namespace in {'main', 'canonical'}
    schema = [dict(r) for r in connection.execute(
        'SELECT type,name,tbl_name,sql FROM ' + namespace + '.sqlite_master ORDER BY type,name')]
    tables = {}
    for item in schema:
        if item['type'] == 'table':
            quoted = '"' + item['name'].replace('"', '""') + '"'
            tables[item['name']] = sorted(
                [dict(r) for r in connection.execute('SELECT * FROM ' + namespace + '.' + quoted)],
                key=lambda r: repr(sorted(r.items())))
    return {'schema': schema, 'tables': tables}


class TransactionBoundStore(StateStore):
    """Reuse the deployed identity API without initialization or an inner commit."""
    def __init__(self, connection):
        self._repair_connection = connection

    @contextmanager
    def tx(self):
        yield self._repair_connection


def apply_repair(args, connection):
    database = Path(args.database)
    assert database.is_file() and not database.is_symlink()
    raw = Path(args.economic).read_bytes()
    assert sha256(raw).hexdigest() == AUDIT_SHA
    report = json.loads(raw)
    assert all(report['summary'][key] == 0 for key in
               ('quantity_mismatches', 'cost_mismatches', 'errors', 'incomplete_pairs'))
    matches = [r for r in report['orders'] if r['trade_id'] == 29 and r['order_id'] == '64987']
    assert len(matches) == 1
    receipt = matches[0]
    exchange = receipt['exchange_receipt']
    assert exchange['symbol'] == 'MINAUSDT' and exchange['side'] == 'SELL'
    assert exchange['status'] == 'FILLED' and str(exchange['orderId']) == '64987'
    assert Decimal(exchange['executedQty']) == Decimal('2304.1')
    assert Decimal(exchange['cummulativeQuoteQty']) == Decimal('237.55271')
    client = exchange['clientOrderId']
    assert isinstance(client, str) and client
    canonical = snapshot(connection, 'canonical')
    trades = [r for r in canonical['tables']['trades'] if r['id'] == 29]
    assert len(trades) == 1 and trades[0]['pair'] == 'MINA/USDT' and not trades[0]['is_open']
    orders = [r for r in canonical['tables']['orders'] if r['ft_trade_id'] == 29]
    assert len(orders) == 2 and {r['order_id'] for r in orders} == {'63277', '64987'}
    order = next(r for r in orders if r['order_id'] == '64987')
    assert all(order[k] == v for k, v in receipt['canonical_order'].items())
    assert not any(r['ft_is_open'] for r in orders)
    timestamp = datetime.fromisoformat(order['order_date']).replace(tzinfo=timezone.utc).timestamp()
    assert abs(timestamp * 1000 - exchange['time']) < 1000
    before = snapshot(connection)
    intents = [r for r in before['tables']['intents'] if r['intent_id'] == INTENT]
    assert len(intents) == 1 and intents[0]['freqtrade_trade_id'] == 29
    assert intents[0]['pair'] == 'MINA/USDT' and intents[0]['state'] == 'EXIT_FILLED'
    identities = before['tables']['order_identity']
    entry = [r for r in identities if r['intent_id'] == INTENT and r['order_id'] == '63277']
    assert len(entry) == 1 and entry[0]['scope_id'] == SCOPE and entry[0]['generation'] == 1
    assert entry[0]['pair'] == 'MINA/USDT' and entry[0]['role'] == 'WORKING'
    entry_receipts = [r for r in report['orders']
                      if r['trade_id'] == 29 and r['order_id'] == '63277']
    assert len(entry_receipts) == 1 and entry_receipts[0]['client_identity_matches'] is True
    assert entry[0]['client_id'] == entry_receipts[0]['exchange_receipt']['clientOrderId']
    expected = dict(scope_id=SCOPE, intent_id=INTENT, generation=1, pair='MINA/USDT',
                    role='CANONICAL_EXIT', client_id=client, order_id='64987')
    existing = [r for r in identities if r['pair'] == 'MINA/USDT'
                and (r['client_id'] == client or r['order_id'] == '64987')]
    assert len(existing) <= 1
    if existing:
        assert all(existing[0][k] == v for k, v in expected.items())
    store = TransactionBoundStore(connection)
    store.bind_order_identity(**expected)
    after = snapshot(connection)
    assert before['schema'] == after['schema']
    for table in before['tables']:
        if table != 'order_identity':
            assert before['tables'][table] == after['tables'][table], table
    new_rows = [r for r in after['tables']['order_identity'] if r not in identities]
    assert len(new_rows) == (0 if existing else 1)
    assert all(r in after['tables']['order_identity'] for r in identities)
    assert all(row[k] == v for row in new_rows for k, v in expected.items())
    assert snapshot(connection, 'canonical') == canonical, 'Canonical DB changed'
    if args.inject_failure:
        raise RuntimeError('injected failure before commit')
    return {'identity_rows_added': len(new_rows), 'trade_id': 29,
                      'order_id': '64987', 'canonical_unchanged': True,
                      'other_extension_tables_unchanged': True,
                      'incidents_unchanged': True, 'audit_sha256': AUDIT_SHA}


def run(args):
    database, canonical = Path(args.database), Path(args.canonical)
    assert database.is_file() and not database.is_symlink()
    assert canonical.is_file() and not canonical.is_symlink()
    assert database.resolve() != canonical.resolve()
    with sqlite3.connect(f'file:{database.resolve()}?mode=rw', uri=True) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute('ATTACH DATABASE ? AS canonical',
                           (f'file:{canonical.resolve()}?mode=rw',))
        # Reserve both databases before reading. Only main.order_identity is written.
        connection.execute('BEGIN IMMEDIATE')
        result = apply_repair(args, connection)
        connection.commit()
    print(json.dumps(result))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('database', 'canonical', 'economic'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--inject-failure', action='store_true')
    run(parser.parse_args())
