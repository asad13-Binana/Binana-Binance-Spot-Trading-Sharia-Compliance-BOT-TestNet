"""Durable full-position operator exits, serialized by Freqtrade's exit lock."""
import json
from decimal import Decimal
from .canonical_fees import receipt
from .recovery_contract import RecoveryBlocked, TERMINAL, dec


OPERATOR_PRE_SUBMISSION = frozenset({'EXIT_REQUESTED', 'EXIT_CANCEL_PENDING', 'EXIT_CANCEL_UNKNOWN'})


class OperatorExit:
    @staticmethod
    def validate_operator_exit_options(*, ordertype=None, amount=None, price=None):
        if ordertype not in (None, 'market') or amount is not None or price is not None:
            raise RecoveryBlocked('OPERATOR_EXIT_REQUIRES_FULL_POSITION_MARKET_WITHOUT_PRICE')

    def request_operator_exit(self, trade, *, reason='force_exit', ordertype=None, amount=None, price=None):
        self.validate_operator_exit_options(ordertype=ordertype, amount=amount, price=price)
        if self.exchange._api.urls['api']['private'] != 'https://testnet.binance.vision/api/v3':
            raise RecoveryBlocked('OPERATOR_EXIT_REQUIRES_TESTNET')
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

    def _operator_market_quantity(self, pair, owned, filters, price):
        rules = {row['filterType']: row for row in filters}
        lot = rules.get('LOT_SIZE')
        market = rules.get('MARKET_LOT_SIZE')
        if not lot or not market or price <= 0:
            raise RecoveryBlocked('OPERATOR_MARKET_FILTERS_UNAVAILABLE')
        quantity = owned
        for rule in (lot, market):
            step = dec(rule.get('stepSize', '0'))
            if step > 0:
                quantity = quantity // step * step
        if quantity <= 0:
            raise RecoveryBlocked('OPERATOR_REMAINDER_NOT_EXECUTABLE')
        for rule in (lot, market):
            self.order_lists._validate_quantity(quantity, rule, 'operator exit')
        for rule in (rules.get('MIN_NOTIONAL'), rules.get('NOTIONAL')):
            if not rule:
                continue
            minimum = (rule.get('applyToMarket', False) if rule['filterType'] == 'MIN_NOTIONAL'
                       else rule.get('applyMinToMarket', False))
            maximum = rule.get('applyMaxToMarket', False)
            if not minimum and not maximum:
                continue
            reference = price
            minutes = int(rule.get('avgPriceMins') or 0)
            if minutes:
                average = self.order_lists.public.publicGetAvgPrice({'symbol': pair.replace('/', '')})
                if int(average.get('mins', -1)) != minutes:
                    raise RecoveryBlocked('OPERATOR_NOTIONAL_REFERENCE_UNVERIFIED')
                reference = dec(average['price'])
            if minimum and min(price, reference) * quantity < dec(rule.get('minNotional', '0')):
                raise RecoveryBlocked('OPERATOR_BELOW_MIN_NOTIONAL')
            if maximum and dec(rule.get('maxNotional', '0')) and max(price, reference) * quantity > dec(rule['maxNotional']):
                raise RecoveryBlocked('OPERATOR_ABOVE_MAX_NOTIONAL')
        if not any(key in rules for key in ('MIN_NOTIONAL', 'NOTIONAL')):
            raise RecoveryBlocked('OPERATOR_NOTIONAL_FILTER_UNAVAILABLE')
        return quantity

    def reconcile_operator_exit(self, trade, iid, request, histories, all_owned_ids, filters, price, open_orders):
        """Old history has been authenticated; raced fills were imported first."""
        if request['status'] not in OPERATOR_PRE_SUBMISSION:
            raise RecoveryBlocked('OPERATOR_SUBMISSION_MUST_RECOVER_BY_CLIENT_ID')
        pair = trade.pair
        payload = json.loads(request['payload_json'])
        if payload.get('scope_id') != self.scope_id or payload.get('trade_id') != trade.id:
            raise RecoveryBlocked('OPERATOR_REQUEST_SCOPE_MISMATCH')
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
        if any(str(order.get('id')) not in all_owned_ids for order in open_orders):
            raise RecoveryBlocked('OPERATOR_FOREIGN_OPEN_ORDER')
        quantity = self._operator_market_quantity(pair, economics['quantity'], filters, price)
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
