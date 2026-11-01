"""Conservative read-only coverage proof for the preserved execution owner."""
import json
from decimal import Decimal
from time import time


def number(value):
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise ValueError('non-finite or negative protection quantity')
    return result


def zero_fill_event(event):
    """Missing or contradictory execution facts cannot prove zero exposure."""
    try:
        quantity = event.get('z', event.get('executedQty'))
        quote = event.get('Z', event.get('cummulativeQuoteQty'))
        status = str(event.get('X') or event.get('orderStatus') or event.get('status') or '').upper()
        execution = str(event.get('x') or event.get('executionType') or '').upper()
        return (number(quantity) == 0 and number(quote) == 0
                and status not in {'PARTIALLY_FILLED', 'FILLED', 'EXPIRED_IN_MATCH'}
                and execution != 'TRADE'
                and all(number(event[field]) == 0 for field in ('l', 'Y') if field in event)
                and not (event.get('eR') or event.get('expiryReason')))
    except (ValueError, ArithmeticError):
        return False


def zero_fill_terminal_proof(adapter, broker, position):
    """Bind authenticated terminal facts to one durable, unfilled entry."""
    store = adapter.state_store
    entry_id = int(position.entry_order_id)
    if store is None or number(position.filled_qty) != 0:
        raise ValueError('durable zero-fill entry unavailable')
    symbol = position.symbol
    with store._connect() as connection:
        rows = connection.execute('SELECT * FROM trade_records WHERE pair=? AND entry_order_id=?',
            (store._symbol_to_pair(symbol), entry_id)).fetchall()
        cursor = connection.execute('SELECT COALESCE(MAX(id),0) FROM exchange_events WHERE symbol=?',
                                    (symbol,)).fetchone()[0]
    if len(rows) != 1 or not rows[0]['entry_client_order_id']:
        raise ValueError('durable entry identity unavailable or ambiguous')
    saved = dict(rows[0])
    binding = {
        'entry_order_id': position.entry_order_id,
        'entry_client_order_id': getattr(position, 'entry_client_id', None),
        'order_list_id': getattr(position, 'order_list_id', None),
        'take_profit_order_id': getattr(position, 'tp_order_id', None),
        'stop_order_id': getattr(position, 'sl_order_id', None) or getattr(position, 'exit_order_id', None),
    }
    if any(saved[field] != value for field, value in binding.items()):
        raise ValueError('position identity differs from durable entry')
    ids = set(adapter._known_order_ids(position))
    if entry_id <= 0 or entry_id not in ids or any(oid <= 0 for oid in ids):
        raise ValueError('known order identity is invalid')
    list_ids = {int(saved['order_list_id'])} if saved['order_list_id'] else set()
    if any(lid <= 0 for lid in list_ids):
        raise ValueError('known order list identity is invalid')
    facts = {}
    for oid in ids:
        fact = adapter._query_order(broker, symbol, oid)
        if (int(fact['orderId']) != oid or fact.get('symbol') != symbol
                or adapter._unsafe_exchange_fact(fact)
                or adapter._order_status(fact) not in {'CANCELED', 'EXPIRED', 'REJECTED'}
                or number(fact['executedQty']) != 0 or number(fact['cummulativeQuoteQty']) != 0
                or not fact.get('clientOrderId')
                or fact.get('side') != ('BUY' if oid == entry_id else 'SELL')
                or int(fact['orderListId']) not in list_ids | {-1}):
            raise ValueError('known order is not authenticated empty and terminal')
        if oid == entry_id and fact['clientOrderId'] != saved['entry_client_order_id']:
            raise ValueError('entry client identity changed')
        facts[str(oid)] = fact
    lists = []
    for lid in list_ids:
        fact = adapter._query_list(broker, lid)
        if (int(fact['orderListId']) != lid or fact.get('symbol') != symbol
                or adapter._unsafe_exchange_fact(fact) or fact.get('listOrderStatus') != 'ALL_DONE'
                or not isinstance(fact.get('orders'), list) or not fact['orders']):
            raise ValueError('known order list is not authenticated terminal')
        members = fact['orders']
        expected = {oid for oid in ids if int(facts[str(oid)]['orderListId']) == lid}
        if ({int(item['orderId']) for item in members} != expected
                or len(members) != len(expected)
                or any(item.get('symbol') != symbol
                       or item.get('clientOrderId') != facts[str(item['orderId'])]['clientOrderId']
                       for item in members)):
            raise ValueError('known order list membership differs from queried orders')
        lists.append(fact)
    open_orders = broker.c.get_open_orders(symbol=symbol)
    broker._sync_weight()
    open_lists = broker.c._get('openOrderList', True, data={})
    broker._sync_weight()
    if (not isinstance(open_orders, list) or open_orders or not isinstance(open_lists, list)
            or any(not isinstance(item, dict) or not item.get('symbol')
                   or item['symbol'] == symbol for item in open_lists)):
        raise ValueError('open order obligations remain unknown or active')
    return saved, cursor, {'orders': facts, 'order_lists': lists, 'position_binding': binding,
                           'open_orders': [], 'open_order_lists_for_symbol': [], 'verified_at': time()}


def coverage_proof(adapter, broker, position, orders, *, ownership_only=False):
    """Never infer ownership from wallet balances or a live status alone.

    The bounded full-symbol history deliberately blocks older positions whose
    historical exit identities cannot be reconstructed from durable evidence.
    """
    store = adapter.state_store
    if store is None:
        raise ValueError('durable protection ownership unavailable')
    pair = position.sym.base + '/' + position.sym.quote
    entry_id = int(position.entry_order_id)
    with store._connect() as connection:
        rows = connection.execute('SELECT * FROM trade_records WHERE pair=? AND entry_order_id=?',
                                  (pair,entry_id)).fetchall()
        event_cursor = connection.execute('SELECT COALESCE(MAX(id),0) FROM exchange_events WHERE symbol=?',
                                          (position.symbol,)).fetchone()[0]
    if len(rows) != 1:
        raise ValueError('durable entry ownership unavailable or ambiguous')
    saved = dict(rows[0])
    saved_exit_ids = {int(saved[key]) for key in ('take_profit_order_id','stop_order_id')
                      if saved[key] not in (None, '', 0)}
    if not set(orders).issubset(saved_exit_ids):
        raise ValueError('live exit identity is not durably owned')
    entry = adapter._query_order(broker,position.symbol,entry_id)
    if (int(entry['orderId']) != entry_id or entry.get('symbol') != position.symbol
            or entry.get('side') != 'BUY' or not entry.get('clientOrderId')
            or adapter._order_status(entry) not in {'FILLED','CANCELED','EXPIRED','REJECTED'}):
        raise ValueError('entry identity or terminal state unavailable')
    bought = number(entry['executedQty'])
    if bought <= 0 or bought != number(position.filled_qty):
        raise ValueError('entry fill does not match saved position')
    stamp = int(entry['time'])
    if stamp <= 0:
        raise ValueError('entry history boundary unavailable')
    key = f'coverage:{position.symbol}:{entry_id}'
    with store._connect() as connection:
        previous = connection.execute('SELECT payload_json FROM recovery_intents WHERE safety_key=?',
                                      (key,)).fetchone()
    previous = json.loads(previous[0]) if previous else {}
    known = set(saved_exit_ids) | set(orders)
    if previous:
        if (previous.get('entry_client_id') != entry['clientOrderId']
                or previous.get('entry_time') != stamp):
            raise ValueError('durable entry proof changed')
        known.update(map(int,previous.get('exit_order_ids',[])))
        for oid, order in orders.items():
            old = (previous.get('orders') or {}).get(str(oid)) or (previous.get('orders') or {}).get(oid)
            if old and any(old.get(field) != order.get(field) for field in
                    ('clientOrderId','side','type','origQty','price','stopPrice','trailingDelta','orderListId')):
                raise ValueError('previously verified protection contract changed')
    # A cancellation can finish an older generation immediately before rescue.
    # Its saved facts bind those old IDs to this same entry, never just a symbol.
    with store._connect() as connection:
        recoveries = connection.execute('SELECT payload_json FROM recovery_intents').fetchall()
    for row in recoveries:
        payload = json.loads(row[0])
        facts = (payload.get('facts') or {}).get('orders') or {}
        owned_entry = facts.get(str(entry_id)) or {}
        if (owned_entry.get('symbol') == position.symbol
                and int(owned_entry.get('orderId') or 0) == entry_id
                and owned_entry.get('clientOrderId') == entry['clientOrderId']):
            known.update(int(oid) for oid, fact in facts.items()
                         if fact.get('side') == 'SELL' and fact.get('symbol') == position.symbol)
    def pages(**filters):
        rows = []
        cursor = filters.pop('fromId',0)
        for _ in range(8):
            page = broker.c.get_my_trades(symbol=position.symbol,fromId=cursor,limit=1000,**filters)
            broker._sync_weight()
            if not isinstance(page,list):
                raise ValueError('authenticated fill history unavailable')
            if any(int(row['id']) < cursor for row in page):
                raise ValueError('execution history pagination regressed')
            rows.extend(page)
            if len(page) < 1000:
                return rows
            next_cursor = max(int(row['id']) for row in page)+1
            if next_cursor <= cursor:
                raise ValueError('execution history pagination did not advance')
            cursor = next_cursor
        raise ValueError('execution history exceeds bounded pagination; explicit recovery required')
    entry_fills = pages(orderId=entry_id)
    if not entry_fills:
        raise ValueError('entry execution history unavailable')
    fills = pages(fromId=min(int(row['id']) for row in entry_fills))
    totals = {}
    receipts = {}
    previous_fills = {str(row['id']):row for row in previous.get('fills',[])}
    quote_totals = {}
    base_fees = Decimal(0)
    for fill in fills:
        oid = int(fill['orderId'])
        identity = str(fill['id'])
        if (fill.get('symbol') != position.symbol or int(fill['time']) < stamp
                or oid not in known | {entry_id}):
            raise ValueError('unowned symbol execution requires reconciliation')
        if identity in receipts:
            if receipts[identity] != fill:
                raise ValueError('conflicting execution identity')
            continue
        receipts[identity] = fill
        if identity in previous_fills and previous_fills[identity] != fill:
            raise ValueError('previously verified execution changed')
        quantity = number(fill['qty'])
        quote = number(fill['quoteQty'])
        price = number(fill['price'])
        if quote <= 0 or price <= 0:
            raise ValueError('positive execution economics unavailable')
        commission = number(fill['commission'])
        if quantity <= 0 or fill.get('isBuyer') is not (oid == entry_id):
            raise ValueError('execution role or quantity mismatch')
        if commission and not fill.get('commissionAsset'):
            raise ValueError('commission asset unavailable')
        totals[oid] = totals.get(oid,Decimal(0)) + quantity
        quote_totals[oid] = quote_totals.get(oid,Decimal(0)) + quote
        if fill.get('commissionAsset') == position.sym.base:
            base_fees += commission
    if (totals.get(entry_id,Decimal(0)) != bought
            or quote_totals.get(entry_id,Decimal(0)) != number(entry['cummulativeQuoteQty'])
            or not set(previous_fills).issubset(receipts)):
        raise ValueError('entry execution history incomplete')
    with store._connect() as connection:
        events = connection.execute("SELECT payload_json FROM exchange_events WHERE symbol=? AND event_type='executionReport'",
                                    (position.symbol,)).fetchall()
    for row in events:
        event = json.loads(row[0])
        if int(event.get('E') or 0) < stamp:
            continue
        oid = int(event.get('i') or event.get('orderId') or 0)
        cumulative = event.get('z') if event.get('z') is not None else event.get('executedQty')
        if cumulative is not None and number(cumulative) > totals.get(oid,Decimal(0)):
            raise ValueError('REST history regressed a durable execution event')
    for oid, order in orders.items():
        if totals.get(oid,Decimal(0)) != number(order['executedQty']):
            raise ValueError('exit execution history incomplete or snapshot lagged')
    sold = sum((qty for oid,qty in totals.items() if oid != entry_id),Decimal(0))
    owned = bought - sold - base_fees
    if owned <= 0:
        raise ValueError('positive owned remainder unavailable')
    proof = {'entry_order_id':entry_id,'entry_client_id':entry['clientOrderId'],
        'entry_time':stamp,'exit_order_ids':sorted(known),
        'orders':{**(previous.get('orders') or {}),**orders},
        'fills':list(receipts.values()),'entry_filled_quantity':str(bought),
        'owned_quantity':str(owned),'verified_at':time()}
    if ownership_only:
        store.save_protection_proof(key,proof,symbol=position.symbol,event_cursor=event_cursor)
        return proof
    open_orders = broker.c.get_open_orders(symbol=position.symbol)
    broker._sync_weight()
    if (not isinstance(open_orders,list)
            or any(row.get('symbol') != position.symbol for row in open_orders)
            or {int(row['orderId']) for row in open_orders} != set(orders)):
        raise ValueError('open-order snapshot conflicts with owned protection')
    remaining = [number(row['origQty'])-number(row['executedQty']) for row in orders.values()]
    step = number(position.sym.step)
    if (step <= 0 or any(q <= 0 or q > owned for q in remaining)
            or any(number(row['origQty']) % step != 0 for row in orders.values())):
        raise ValueError('live order exceeds ownership or violates quantity precision')
    protected = min(remaining)
    residual = owned - protected
    rounded_residual = (residual // step) * step
    price = number(broker.price(position.symbol))
    minimum_qty = number(position.sym.min_qty)
    minimum_notional = number(position.sym.min_notional)
    if price <= 0 or (rounded_residual > 0 and rounded_residual >= minimum_qty
                     and rounded_residual * price >= minimum_notional):
        raise ValueError('live protection leaves executable owned inventory uncovered')
    proof.update(protected_quantity=str(protected),non_executable_residual=str(residual))
    store.save_protection_proof(key,proof,symbol=position.symbol,event_cursor=event_cursor)
    return proof
