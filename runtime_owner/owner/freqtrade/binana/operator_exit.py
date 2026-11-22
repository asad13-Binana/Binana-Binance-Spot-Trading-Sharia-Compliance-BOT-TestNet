"""Durable full-position operator exits, serialized by Freqtrade's exit lock."""
import json
from decimal import Decimal
from math import lcm
from .canonical_fees import receipt
from .recovery_contract import RecoveryBlocked, TERMINAL, dec
from .environment import execution_rest_endpoint


OPERATOR_PRE_SUBMISSION = frozenset({'EXIT_REQUESTED', 'EXIT_CANCEL_PENDING', 'EXIT_CANCEL_UNKNOWN'})
OPERATOR_DUST_REPLAY = frozenset({'OPERATOR_REMAINDER_DUST', 'DUST_PENDING', 'DUST_RETAINED'})


class MarketBelowMinimum(RecoveryBlocked):
    """A market remainder below minimum after all applicable filters were verified."""

    def __init__(self, reason, classification, bases):
        super().__init__(reason)
        self.classification = classification
        self.bases = bases


def unsubmitted_operator_exit(generation):
    return (generation['mode'] == 'OPERATOR_EXIT' and not generation.get('tp_order_id')
            and generation.get('expected_qty') is None
            and generation['status'] in OPERATOR_PRE_SUBMISSION | OPERATOR_DUST_REPLAY)


class OperatorExit:
    @staticmethod
    def validate_operator_exit_options(*, ordertype=None, amount=None, price=None):
        if ordertype not in (None, 'market') or amount is not None or price is not None:
            raise RecoveryBlocked('OPERATOR_EXIT_REQUIRES_FULL_POSITION_MARKET_WITHOUT_PRICE')

    def request_operator_exit(self, trade, *, reason='force_exit', ordertype=None, amount=None, price=None):
        self.validate_operator_exit_options(ordertype=ordertype, amount=amount, price=price)
        if self.exchange._api.urls['api']['private'] != execution_rest_endpoint():
            raise RecoveryBlocked('OPERATOR_EXIT_EXECUTION_ENDPOINT_MISMATCH')
        identity = trade.binana_custom_data('binana_entry_identity')
        if (not trade.is_open or trade.is_short or trade.leverage != 1
                or not identity or identity.get('scope_id') != self.scope_id
                or identity.get('accounting_version') != 1):
            raise RecoveryBlocked('OPERATOR_EXIT_OWNER_UNVERIFIED')
        iid = identity['intent_id']
        intent = self.store.get_intent(iid)
        if not intent or intent['pair'] != trade.pair or intent['freqtrade_trade_id'] != trade.id:
            raise RecoveryBlocked('OPERATOR_EXIT_CANONICAL_BINDING_MISMATCH')
        economics = trade.binana_economics()
        if not economics or economics['quantity'] <= 0 or not economics['entries']:
            raise RecoveryBlocked('OPERATOR_EXIT_RECEIPTS_UNVERIFIED')
        entry = (trade.binana_custom_data('binana_fill_receipts')['orders']).get(identity['order_id'])
        if not entry or entry['client_id'] != identity['client_id'] or entry['side'] != 'buy':
            raise RecoveryBlocked('OPERATOR_EXIT_ENTRY_IDENTITY_MISMATCH')
        generation = self.store.request_operator_exit(intent_id=iid, trade_id=trade.id,
            pair=trade.pair, scope_id=self.scope_id, reason=reason)
        return {'trade_id': trade.id, 'state': 'accepted', 'generation': generation['generation'],
                'phase': generation['status'], 'message': 'Durable exit request; exchange reconciliation pending'}

    def _operator_market_quantity(self, pair, owned, filters, price, *, reserved=None):
        rules = {row['filterType']: row for row in filters}
        if price <= 0 or not {'LOT_SIZE', 'MARKET_LOT_SIZE'} <= rules.keys():
            raise RecoveryBlocked('OPERATOR_MARKET_FILTERS_UNAVAILABLE')
        # A quantity must align to both increments, even when neither divides the other.
        lots = []
        for name in ('LOT_SIZE', 'MARKET_LOT_SIZE'):
            rule = rules[name]
            minimum, maximum, step = (dec(rule[key]) for key in ('minQty', 'maxQty', 'stepSize'))
            if min(minimum, maximum, step) < 0 or (maximum and minimum > maximum):
                raise RecoveryBlocked('OPERATOR_MARKET_FILTERS_INVALID')
            lots.append((minimum, maximum, step))
        steps = [step for _, _, step in lots if step > 0]
        increment = Decimal(0)
        if steps:
            scale = 10 ** max(0, -min(step.as_tuple().exponent for step in steps))
            increment = Decimal(lcm(*(int(step * scale) for step in steps))) / scale

        symbol = pair.replace('/', '')
        bounds, bases = [], {}
        reference = None
        if not {'MIN_NOTIONAL', 'NOTIONAL'}.intersection(rules):
            raise RecoveryBlocked('OPERATOR_NOTIONAL_FILTER_UNAVAILABLE')
        for name in ('MIN_NOTIONAL', 'NOTIONAL'):
            if name not in rules:
                continue
            rule = rules[name]
            use_min = rule['applyToMarket' if name == 'MIN_NOTIONAL' else 'applyMinToMarket']
            use_max = rule['applyMaxToMarket'] if name == 'NOTIONAL' else False
            if type(use_min) is not bool or type(use_max) is not bool:
                raise RecoveryBlocked('OPERATOR_MARKET_FILTERS_INVALID')
            if not use_min and not use_max:
                continue
            minimum = dec(rule['minNotional']) if use_min else Decimal(0)
            maximum = dec(rule['maxNotional']) if use_max else Decimal(0)
            if min(minimum, maximum) < 0 or (maximum and minimum > maximum):
                raise RecoveryBlocked('OPERATOR_MARKET_FILTERS_INVALID')
            try:
                if not bases:
                    reference = self.order_lists._reference_price(symbol)
                basis = self.order_lists._percent_basis(symbol, rule, reference)
            except Exception:
                raise RecoveryBlocked('OPERATOR_NOTIONAL_REFERENCE_UNVERIFIED') from None
            bases[name] = str(basis)
            bounds.append((minimum, maximum, basis))

        def size(amount):
            if amount < 0:
                raise RecoveryBlocked('OPERATOR_CANONICAL_QUANTITY_UNVERIFIED')
            quantity = (amount // increment) * increment if increment else amount
            if any(maximum and quantity > maximum for _, maximum, _ in lots):
                raise RecoveryBlocked('OPERATOR_ABOVE_MAX_QTY')
            if any(maximum and quantity * basis > maximum for _, maximum, basis in bounds):
                raise RecoveryBlocked('OPERATOR_ABOVE_MAX_NOTIONAL')
            if not quantity or any(quantity < minimum for minimum, _, _ in lots):
                raise MarketBelowMinimum('OPERATOR_REMAINDER_NOT_EXECUTABLE',
                                         'NON_EXECUTABLE_DUST_MIN_QTY', bases)
            if any(quantity * basis < minimum for minimum, _, basis in bounds):
                raise MarketBelowMinimum('OPERATOR_BELOW_MIN_NOTIONAL',
                                         'NON_EXECUTABLE_DUST_MIN_NOTIONAL', bases)
            return quantity

        if reserved is not None and reserved != owned:
            try:
                return size(reserved)
            except MarketBelowMinimum:
                pass  # Retry the verified canonical balance against the same price evidence.
        return size(owned)

    def _verified_exit_history(self, trade, iid, histories):
        pair = trade.pair
        committed = trade.binana_custom_data('binana_fill_receipts')
        economics = trade.binana_economics()
        if not committed or not economics or economics['quantity'] <= 0:
            raise RecoveryBlocked('OPERATOR_CANONICAL_QUANTITY_UNVERIFIED')
        verified = {}
        active = []
        for generation, children in histories:
            for key, order in children.items():
                self.verify_canonical_order_owner(trade, order)
                if dec(order.get('filled') or 0) or str(order['id']) in committed['orders']:
                    self._record_order_trades(intent_id=iid, pair=pair,
                        order_id=str(order['id']), side=order['side'].upper())
                    item = receipt(pair, order, self._verified_order_fills)
                    if committed['orders'].get(str(order['id'])) != item:
                        raise RecoveryBlocked('OPERATOR_CANONICAL_FILL_IMPORT_PENDING')
                    verified[str(order['id'])] = item
            live = [order for key, order in children.items() if key != 'working' and order['status'] not in TERMINAL]
            if generation.get('order_list_id'):
                report = self.order_lists.query_list(order_list_id=generation['order_list_id'])
                if (str(report.get('orderListId')) != str(generation['order_list_id'])
                        or report.get('listClientOrderId') != generation['list_client_id']
                        or report.get('symbol') != pair.replace('/', '')):
                    raise RecoveryBlocked('OPERATOR_LIST_IDENTITY_MISMATCH')
                if not live and report.get('listOrderStatus') != 'ALL_DONE':
                    raise RecoveryBlocked('OPERATOR_LIST_TERMINATION_UNVERIFIED')
            elif generation['mode'] not in ('TARGET_EXIT', 'STOP_EXIT', 'OPERATOR_EXIT'):
                raise RecoveryBlocked('OPERATOR_OLD_LIST_IDENTITY_MISSING')
            if live:
                active.append((generation, live))
        if verified != committed['orders']:
            raise RecoveryBlocked('OPERATOR_RECEIPT_COVERAGE_INCOMPLETE')
        return economics, active

    def reconcile_operator_exit(self, trade, iid, request, histories, all_owned_ids, filters, price, open_orders):
        """Old history has been authenticated; raced fills were imported first."""
        if request['status'] not in OPERATOR_PRE_SUBMISSION | OPERATOR_DUST_REPLAY:
            raise RecoveryBlocked('OPERATOR_SUBMISSION_MUST_RECOVER_BY_CLIENT_ID')
        pair = trade.pair
        payload = json.loads(request['payload_json'])
        if payload.get('scope_id') != self.scope_id or payload.get('trade_id') != trade.id:
            raise RecoveryBlocked('OPERATOR_REQUEST_SCOPE_MISMATCH')
        economics, active = self._verified_exit_history(trade, iid, histories)
        if any(str(order.get('id')) not in all_owned_ids for order in open_orders):
            raise RecoveryBlocked('OPERATOR_FOREIGN_OPEN_ORDER')
        try:
            quantity = self._operator_market_quantity(pair, economics['quantity'], filters, price)
        except MarketBelowMinimum as below:
            if active or open_orders or not economics['exits']:
                raise RecoveryBlocked(str(below)) from below
            raise
        if request['status'] in OPERATOR_DUST_REPLAY:
            if request['status'] == 'DUST_RETAINED':
                raise RecoveryBlocked('OPERATOR_RETAINED_REMAINDER_NOW_EXECUTABLE')
            if active or open_orders or request.get('expected_qty') is not None:
                raise RecoveryBlocked('OPERATOR_REMAINDER_REPLAY_CONFLICT')
        if active:
            self.store.update_generation_status(iid, request['generation'], 'EXIT_CANCEL_PENDING', payload=payload)
            for generation, orders in active:
                try:
                    if generation.get('order_list_id'):
                        self.order_lists.cancel_list(symbol=pair, order_list_id=generation['order_list_id'],
                                                     list_client_id=generation['list_client_id'])
                    else:
                        for order in orders:
                            self.exchange._api.cancel_order(str(order['id']), pair, params={'maxRetriesOnFailure': 0})
                except Exception as exc:
                    self.store.update_generation_status(iid, request['generation'], 'EXIT_CANCEL_UNKNOWN', payload=payload)
                    self._recovery_block(iid, pair, 'OPERATOR_CANCEL_UNKNOWN:' + type(exc).__name__)
                    return None
            # Never trust a cancel response as proof of final fills or free funds.
            return None
        if open_orders:
            raise RecoveryBlocked('OPERATOR_OPEN_ORDER_SNAPSHOT_CONFLICT')
        balance = self.exchange._api.fetch_balance()
        if dec((balance.get(pair.split('/')[0]) or {}).get('free') or 0) < quantity:
            raise RecoveryBlocked('OPERATOR_INSUFFICIENT_FREE_OWNED_ASSET')
        self._supersede_retained_dust(trade)
        self.store.prepare_operator_submission(intent_id=iid, generation=request['generation'], quantity=quantity)
        # CCXT removes maxRetriesOnFailure before signing. A timeout is never a
        # second POST: subsequent passes query this persisted client identity.
        try:
            raw = self.exchange._api.privatePostOrder({'symbol': pair.replace('/', ''), 'side': 'SELL',
                'type': 'MARKET', 'quantity': format(quantity, 'f'), 'newClientOrderId': request['tp_client_id'],
                'newOrderRespType': 'FULL', 'maxRetriesOnFailure': 0})
            if (raw.get('symbol') != pair.replace('/', '') or raw.get('clientOrderId') != request['tp_client_id']
                    or raw.get('side') != 'SELL' or raw.get('orderId') is None):
                raise RecoveryBlocked('OPERATOR_SUBMISSION_RESPONSE_IDENTITY_MISMATCH')
            self.store.put_generation(intent_id=iid, generation=request['generation'], pair=pair,
                mode='OPERATOR_EXIT', status='SUBMITTED_UNVERIFIED', expected_qty=str(quantity), payload=payload,
                ids={'tp_order_id': str(raw['orderId']), 'tp_client_id': request['tp_client_id']})
            self.store.bind_order_identity(scope_id=self.scope_id, intent_id=iid, generation=request['generation'],
                pair=pair, role='CANONICAL_EXIT', client_id=request['tp_client_id'], order_id=str(raw['orderId']))
        except Exception as exc:
            self._recovery_block(iid, pair, 'OPERATOR_SUBMISSION_UNKNOWN:' + type(exc).__name__)
        return None
