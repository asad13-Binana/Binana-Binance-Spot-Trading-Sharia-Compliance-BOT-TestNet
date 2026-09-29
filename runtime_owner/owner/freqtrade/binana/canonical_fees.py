"""Exact, replayable Spot economics from receipts owned by canonical Trades.

Only authenticated executions are persisted, as Freqtrade custom data. The
projection below is rebuilt from them; it is not a second portfolio or ledger.
"""
from copy import deepcopy
from decimal import Decimal


class FeeEvidenceError(RuntimeError):
    pass


ZERO = Decimal('0')
EPSILON = Decimal('0.00000001')


def number(value):
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise FeeEvidenceError('MISSING_OR_INVALID_EXECUTION_NUMBER') from exc
    if not result.is_finite() or result < 0:
        raise FeeEvidenceError('INVALID_EXECUTION_NUMBER')
    return result


def receipt(pair, order, executions):
    """Validate a complete cumulative myTrades response against its order."""
    base, quote = pair.split('/')
    order_id = str(order['id'])
    side = str(order.get('side') or '').lower()
    if side not in ('buy', 'sell') or order.get('symbol') != pair:
        raise FeeEvidenceError('ORDER_PAIR_OR_SIDE_MISMATCH')
    if not order.get('clientOrderId'):
        raise FeeEvidenceError('ORDER_CLIENT_ID_MISSING')
    fills = {}
    for item in executions:
        if str(item.get('order')) != order_id or item.get('symbol') != pair or item.get('side') != side:
            raise FeeEvidenceError('EXECUTION_OWNERSHIP_MISMATCH')
        if item.get('id') is None or str(item['id']) == '':
            raise FeeEvidenceError('EXECUTION_ID_MISSING')
        qty, cost = number(item.get('amount')), number(item.get('cost'))
        if qty == 0 or cost == 0:
            raise FeeEvidenceError('EMPTY_EXECUTION')
        fees = [item['fee']] if isinstance(item.get('fee'), dict) else item.get('fees')
        if not isinstance(fees, list) or not fees:
            raise FeeEvidenceError('COMMISSION_EVIDENCE_MISSING')
        base_fee, quote_fee = ZERO, ZERO
        fee_totals = {}
        for fee in fees:
            asset, amount = fee.get('currency'), number(fee.get('cost'))
            if not asset or (amount and asset not in (base, quote)):
                raise FeeEvidenceError('UNSUPPORTED_COMMISSION_ASSET')
            fee_totals[asset] = fee_totals.get(asset, ZERO) + amount
            if asset == base:
                base_fee += amount
            elif asset == quote:
                quote_fee += amount
        timestamp = number(item.get('timestamp'))
        if timestamp < number(order.get('timestamp')) - 1000:
            raise FeeEvidenceError('EXECUTION_PREDATES_ORDER')
        value = {'id':str(item['id']), 'quantity':str(qty), 'quote':str(cost),
                 'base_fee':str(base_fee), 'quote_fee':str(quote_fee),
                 'fees':{key:str(value) for key,value in sorted(fee_totals.items())},
                 'timestamp':str(timestamp)}
        prior = fills.get(value['id'])
        if prior is not None and prior != value:
            raise FeeEvidenceError('CONFLICTING_EXECUTION_REPLAY')
        fills[value['id']] = value
    if abs(sum((number(f['quantity']) for f in fills.values()), ZERO)-number(order.get('filled'))) > EPSILON:
        raise FeeEvidenceError('EXECUTION_QUANTITY_INCOMPLETE')
    if abs(sum((number(f['quote']) for f in fills.values()), ZERO)-number(order.get('cost'))) > EPSILON:
        raise FeeEvidenceError('EXECUTION_COST_INCOMPLETE')
    return {'id':order_id,'client_id':order['clientOrderId'],'side':side,
            'amount':str(number(order.get('amount'))), 'created':str(number(order.get('timestamp'))),
            'fills':fills}


def merge_receipt(previous, scope_id, pair, item):
    data = deepcopy(previous) if previous else {'schema_version':1,'scope_id':scope_id,'pair':pair,'orders':{}}
    if data.get('schema_version') != 1 or data.get('scope_id') != scope_id or data.get('pair') != pair:
        raise FeeEvidenceError('CANONICAL_RECEIPT_SCOPE_MISMATCH')
    old = data['orders'].get(item['id'])
    if old:
        if any(old[key] != item[key] for key in ('id','client_id','side','amount','created')):
            raise FeeEvidenceError('CANONICAL_RECEIPT_IDENTITY_CHANGED')
        if any(item['fills'].get(key) != value for key,value in old['fills'].items()):
            raise FeeEvidenceError('CANONICAL_EXECUTION_CHANGED_OR_DISAPPEARED')
    # Exchange execution IDs are unique for this pair, including across orders.
    for order_id, other in data['orders'].items():
        if order_id != item['id'] and set(other['fills']).intersection(item['fills']):
            raise FeeEvidenceError('EXECUTION_ASSIGNED_TO_TWO_ORDERS')
    data['orders'][item['id']] = deepcopy(item)
    return data


def project(data, orders):
    """Use moving average cost; a base SELL commission consumes inventory only."""
    if data.get('schema_version') != 1:
        raise FeeEvidenceError('CANONICAL_RECEIPTS_MISSING')
    canonical = {str(order.order_id):order for order in orders}
    if set(data['orders']) - set(canonical):
        raise FeeEvidenceError('RECEIPT_WITHOUT_CANONICAL_ORDER')
    events = []
    for order_id, order in canonical.items():
        item = data['orders'].get(order_id)
        if not item:
            if order.safe_filled:
                raise FeeEvidenceError('FILLED_ORDER_RECEIPT_MISSING')
            continue
        side = 'sell' if order.ft_order_side == 'stoploss' else order.ft_order_side
        if side != item['side']:
            raise FeeEvidenceError('CANONICAL_ORDER_SIDE_MISMATCH')
        quantity = sum((number(fill['quantity']) for fill in item['fills'].values()), ZERO)
        cost = sum((number(fill['quote']) for fill in item['fills'].values()), ZERO)
        if abs(quantity-number(order.safe_filled)) > EPSILON or abs(cost-number(order.cost or 0)) > EPSILON:
            raise FeeEvidenceError('CANONICAL_ORDER_RECEIPT_REVISION_MISMATCH')
        for fill in item['fills'].values():
            events.append((number(fill['timestamp']), int(fill['id']), order_id, side, fill))
    owned = basis = invested = realized = buy_quantity = buy_quote = ZERO
    fees = {'buy':{},'sell':{}}
    entries, exits = {}, {}
    for _, _, order_id, side, fill in sorted(events):
        qty, quote, base_fee, quote_fee = (number(fill[key]) for key in ('quantity','quote','base_fee','quote_fee'))
        for asset,value in fill['fees'].items():
            fees[side][asset] = fees[side].get(asset,ZERO)+number(value)
        if side == 'buy':
            if base_fee >= qty:
                raise FeeEvidenceError('ENTRY_COMMISSION_EXCEEDS_ACQUISITION')
            owned += qty-base_fee
            basis += quote+quote_fee
            invested += quote+quote_fee
            buy_quantity += qty
            buy_quote += quote
            entry_row = entries.setdefault(order_id, {'quantity':ZERO})
            entry_row['quantity'] += qty-base_fee
        else:
            debit = qty+base_fee
            if debit > owned or owned <= 0 or quote_fee > quote:
                raise FeeEvidenceError('EXIT_EXCEEDS_CANONICAL_OWNERSHIP')
            allocated = basis if debit == owned else basis*debit/owned
            profit = quote-quote_fee-allocated
            basis -= allocated
            owned -= debit
            realized += profit
            exit_row = exits.setdefault(order_id,{'profit':ZERO,'basis':ZERO,'quantity':ZERO})
            exit_row['profit'] += profit
            exit_row['basis'] += allocated
            exit_row['quantity'] += qty
    return {'quantity':owned,'basis':basis,'invested':invested,'realized':realized,
            'execution_rate':buy_quote/buy_quantity if buy_quantity else ZERO,
            'fees':fees,'entries':entries,'exits':exits}


def apply_projection(trade, result):
    """Assign Freqtrade economic fields; never fabricate an Order or close a Trade."""
    qty, basis = result['quantity'], result['basis']
    trade.max_stake_amount = float(result['invested'])
    trade.realized_profit = float(result['realized'])
    trade.close_profit_abs = float(result['realized'])
    trade.close_profit = float(result['realized']/result['invested']) if result['invested'] else 0.0
    trade.fee_open = 0.0  # Entry commission is already in the exact basis/net quantity.
    trade.funding_fees = 0.0
    if qty > 0:
        trade.amount = float(qty)  # Do not quantize away fee dust.
        trade.stake_amount = float(basis)
        trade.open_rate = float(result['execution_rate'])
        trade.open_trade_value = float(basis)
    for side, field in (('buy','open'),('sell','close')):
        actual = {asset:amount for asset,amount in result['fees'][side].items() if amount}
        if len(actual) <= 1:
            asset, amount = next(iter(actual.items()), (trade.safe_quote_currency,ZERO))
            setattr(trade,'fee_'+field+'_currency',asset)
            setattr(trade,'fee_'+field+'_cost',float(amount))
        else:
            # Mixed assets have no single monetary unit. Exact amounts remain
            # available in each receipt; a guessed aggregate would be misleading.
            setattr(trade,'fee_'+field+'_currency','MULTI')
            setattr(trade,'fee_'+field+'_cost',None)
