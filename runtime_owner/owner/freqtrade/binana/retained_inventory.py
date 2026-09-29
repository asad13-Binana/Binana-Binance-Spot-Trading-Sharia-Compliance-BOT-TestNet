"""Recheck retained canonical inventory against current Testnet execution filters.

This is an admission check. It never combines trades, submits an order, changes
economic fields, or treats the exchange wallet as evidence of bot ownership.
"""
from collections import defaultdict
from decimal import Decimal
import json
from time import time

from .recovery_contract import RecoveryBlocked, classify_executability, dec


def verified_slot_exemption(trade):
    """Incomplete retention metadata never hides a trade from reconciliation.

    This is a persisted-proof check. The admission monitor separately repeats
    the filter/price check against the current exchange before new entries.
    """
    try:
        evidence = trade.binana_custom_data('binana_retained_dust')
        identity = trade.binana_custom_data('binana_entry_identity')
        if (not isinstance(evidence, dict) or evidence.get('phase') != 'retained'
                or evidence.get('non_executable') is not True
                or type(evidence.get('exchange_open_orders')) is not int
                or evidence['exchange_open_orders'] != 0
                or not isinstance(identity, dict) or identity.get('accounting_version') != 1
                or not identity.get('scope_id') or evidence.get('scope') != identity['scope_id']):
            return False
        if any(o.ft_is_open or o.status not in {'closed', 'canceled', 'cancelled',
                                                'expired', 'rejected'} for o in trade.orders):
            return False
        exact = trade.binana_economics()
        if not exact or not exact.get('exits'):
            return False
        quantity, basis = dec(evidence.get('quantity')), dec(evidence.get('cost_basis'))
        if (quantity <= 0 or basis < 0
                or abs(quantity-dec(trade.amount)) > Decimal('0.00000001')
                or abs(basis-dec(trade.stake_amount)) > Decimal('0.0000001')
                or abs(quantity-exact['quantity']) > Decimal('0.00000001')
                or abs(basis-exact['basis']) > Decimal('0.0000001')):
            return False
        classification, _ = classify_executability(quantity, evidence.get('price'),
                                                   evidence.get('filters'))
        return (classification in {'NON_EXECUTABLE_DUST_MIN_QTY', 'NON_EXECUTABLE_DUST_MIN_NOTIONAL'}
                and classification == evidence.get('classification'))
    except Exception:
        # A missing receipt, invalid decimal, stale ORM row or malformed
        # filter retains the slot; it cannot authorize additional exposure.
        return False


def retained_quantities(trades, scope_id):
    quantities = defaultdict(Decimal)
    owners = defaultdict(list)
    for trade in trades:
        evidence = trade.binana_custom_data('binana_retained_dust')
        if not isinstance(evidence, dict) or evidence.get('phase') not in ('prepared', 'retained'):
            continue
        if evidence.get('scope') != scope_id:
            raise RecoveryBlocked('RETAINED_SCOPE_UNVERIFIED')
        quantity, basis = dec(evidence.get('quantity')), dec(evidence.get('cost_basis'))
        if (quantity <= 0 or basis < 0
                or abs(quantity-dec(trade.amount)) > Decimal('0.00000001')
                or abs(basis-dec(trade.stake_amount)) > Decimal('0.0000001')):
            raise RecoveryBlocked('RETAINED_CANONICAL_AMOUNT_OR_COST_MISMATCH')
        exact = trade.binana_economics()
        if exact is None:
            digest = evidence.get('evidence_sha256', '')
            if (evidence.get('repair_version') != 'authenticated-retention-replay-v1'
                    or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest)):
                raise RecoveryBlocked('HISTORICAL_RETENTION_EVIDENCE_UNVERIFIED')
        elif (abs(exact['quantity']-quantity) > Decimal('0.00000001')
                or abs(exact['basis']-basis) > Decimal('0.0000001')):
            raise RecoveryBlocked('RETAINED_RECEIPT_MISMATCH')
        quantities[trade.pair] += quantity
        owners[trade.pair].append(trade.id)
    return quantities, owners


class RetainedInventory:
    def refresh_retained_inventory(self, trades, open_orders):
        now = time()
        self._retained_health = {'ok':False, 'checked_at':now}
        try:
            quantities, owners = retained_quantities(trades, self.scope_id)
            retained_ids = {identifier for values in owners.values() for identifier in values}
            active_ids = {trade.id for trade in trades
                          if getattr(trade, 'is_open', False) and trade.id not in retained_ids}
            results = []
            if quantities:
                public = self.order_lists.public
                metadata = {r['symbol']:r for r in public.publicGetExchangeInfo({})['symbols']}
                tickers = {r['symbol']:r for r in public.publicGetTickerBookTicker({})}
                for pair, quantity in sorted(quantities.items()):
                    symbol = pair.replace('/', '')
                    market = metadata[symbol]
                    if market.get('status') != 'TRADING':
                        raise RecoveryBlocked('RETAINED_SYMBOL_NOT_TRADING')
                    for order in open_orders:
                        if order.get('symbol') not in (symbol, pair):
                            continue
                        order_id = order.get('orderId', order.get('id'))
                        with self.store._connect() as connection:
                            binding = connection.execute('''
                                SELECT i.freqtrade_trade_id FROM order_identity o
                                JOIN intents i ON i.intent_id=o.intent_id
                                WHERE o.scope_id=? AND o.pair=? AND o.order_id=? AND o.client_id=?
                            ''', (self.scope_id,pair,str(order_id),order.get('clientOrderId'))).fetchone()
                        if binding is None or binding['freqtrade_trade_id'] not in active_ids:
                            raise RecoveryBlocked('RETAINED_SYMBOL_HAS_UNRECONCILED_OBLIGATIONS')
                    price = dec(tickers[symbol]['bidPrice'])
                    classification, rounded = classify_executability(quantity, price, market['filters'])
                    row = {'pair':pair, 'quantity':str(quantity), 'rounded_quantity':str(rounded),
                           'trade_ids':owners[pair], 'classification':classification, 'bid':str(price)}
                    results.append(row)
            # Do not clear an earlier result unless the complete fresh scan passed.
            for row in results:
                incident_id = 'retained-executable-'+row['pair'].replace('/', '')
                if row['classification'] == 'EXECUTABLE':
                    self.store.incident(incident_id=incident_id, pair=row['pair'],
                        code='AGGREGATE_RETAINED_INVENTORY_EXECUTABLE', detail=json.dumps(row,sort_keys=True))
                else:
                    self.store.close_incident(incident_id)
            self.store.close_incident('retained-inventory-verification')
            self._retained_health = {'ok':True, 'checked_at':time(), 'pairs':results,
                'executable_pairs':sum(row['classification']=='EXECUTABLE' for row in results)}
        except Exception as exc:
            reason = str(exc) if isinstance(exc,RecoveryBlocked) else type(exc).__name__
            self._retained_health['reason'] = reason
            self.store.incident(incident_id='retained-inventory-verification',
                code='RETAINED_INVENTORY_VERIFICATION_UNAVAILABLE', detail=reason)
        return dict(self._retained_health)
