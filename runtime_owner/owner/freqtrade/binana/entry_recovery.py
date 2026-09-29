"""Recover authenticated submitted entries that predate their canonical Trade.

This path only reads exchange state. Freqtrade imports the returned real order
through its persistence and order-update methods; no second POST is allowed.
"""
import json
from decimal import Decimal

from .recovery_contract import RecoveryBlocked, verify_child


class EntryRecovery:
    def require_confirmed_entry(self, pair, order):
        """Only a full FOK fill may create a canonical Trade.

        A NEW/unknown/zero-fill response stays in the durable unbound intent.
        Recovery queries its existing IDs; it never submits another entry.
        """
        amount, filled, cost = (Decimal(str(order.get(key) or 0))
                                for key in ('amount','filled','cost'))
        if (order.get('status') == 'closed' and amount.is_finite()
                and filled.is_finite() and cost.is_finite()
                and amount == filled and filled > 0 and cost > 0):
            return
        intent_id = self.prepared[pair].intent_id
        self.store.incident(incident_id='entry-recovery-'+intent_id,
            intent_id=intent_id,pair=pair,code='ENTRY_CONFIRMATION_PENDING',
            detail='Submitted FOK entry has not yet been verified as a terminal full fill')
        # AdmissionRejected is imported locally to avoid the manager/mixin cycle.
        from .execution_manager import AdmissionRejected
        raise AdmissionRejected('ENTRY_CONFIRMATION_PENDING')

    def entry_identity(self, intent_id, order):
        generation = self.store.generation(intent_id, 1)
        if (not generation or str(generation['working_order_id']) != str(order['id'])
                or generation['working_client_id'] != order.get('clientOrderId')):
            raise RecoveryBlocked('CANONICAL_ENTRY_IDENTITY_UNAVAILABLE')
        return {'scope_id':self.scope_id, 'intent_id':intent_id,
                'order_id':str(order['id']), 'client_id':generation['working_client_id'],
                'accounting_version':1}

    def prepared_entry_identity(self, pair, order):
        prepared = self.prepared.get(pair)
        if prepared is None:
            raise RecoveryBlocked('CANONICAL_ENTRY_ADMISSION_UNAVAILABLE')
        return self.entry_identity(prepared.intent_id, order)

    def verify_existing_entry(self, intent, order, trade):
        expected = self.entry_identity(intent['intent_id'], order)
        bound = self.intent_for_trade(int(trade.id))
        if bound is not None and bound != intent['intent_id']:
            raise RecoveryBlocked('CANONICAL_ENTRY_BELONGS_TO_ANOTHER_INTENT')
        if trade.get_custom_data(key='binana_entry_identity') != expected:
            raise RecoveryBlocked('CANONICAL_ENTRY_SCOPE_UNVERIFIED')
        existing = trade.select_order_by_order_id(str(order['id']))
        if (existing is None or existing.ft_order_side != 'buy' or trade.is_short
                or trade.pair != intent['pair'] or trade.exchange != 'binance'):
            raise RecoveryBlocked('CANONICAL_ENTRY_ROLE_MISMATCH')
        tolerance = Decimal('0.00000001')
        previous = {key:Decimal(str(getattr(existing,key) or 0)) for key in ('amount','price','filled','cost')}
        current = {key:Decimal(str(order.get(key) or 0)) for key in previous}
        if (not all(value.is_finite() and value >= 0 for value in (*previous.values(), *current.values()))
                or any(abs(previous[key]-current[key]) > tolerance for key in ('amount','price'))
                or any(previous[key] > current[key]+tolerance for key in ('filled','cost'))
                or (not existing.ft_is_open and any(abs(previous[key]-current[key]) > tolerance
                                                    for key in ('filled','cost')))
                or previous['filled'] > previous['amount']
                or (previous['filled'] == 0 and previous['cost'] != 0)):
            raise RecoveryBlocked('CANONICAL_ENTRY_ECONOMICS_MISMATCH')
        if abs(existing.order_date_utc.timestamp()*1000-order['timestamp']) > 1000:
            raise RecoveryBlocked('CANONICAL_ENTRY_TIME_MISMATCH')

    def recover_unbound_entries(self):
        with self.store._connect() as connection:
            intents = [dict(row) for row in connection.execute('''
                SELECT i.* FROM intents i WHERE i.freqtrade_trade_id IS NULL
                AND EXISTS (SELECT 1 FROM protection p WHERE p.intent_id=i.intent_id
                            AND p.generation=1 AND p.working_client_id IS NOT NULL)
                AND i.state NOT IN ('CLOSED_NO_FILL','EXIT_FILLED','CLOSED','DUST_RETAINED')
                ORDER BY i.created_ts
            ''')]
        results = []
        for intent in intents:
            intent_id = intent['intent_id']
            try:
                result = self._recover_unbound_entry(intent)
                if result is not None:
                    results.append(result)
            except Exception as exc:
                self.store.set_intent_state(intent_id, 'UNKNOWN')
                self.store.incident(incident_id='entry-recovery-'+intent_id,
                    intent_id=intent_id, pair=intent['pair'], code='ENTRY_RECONSTRUCTION_BLOCKED',
                    detail=str(exc)[:200] if isinstance(exc, RecoveryBlocked) else type(exc).__name__)
        return results

    def _recover_unbound_entry(self, intent):
        intent_id, pair = intent['intent_id'], intent['pair']
        generation = self.store.generation(intent_id, 1)
        if not intent['halal_allowed'] or not intent['registry_sha256']:
            raise RecoveryBlocked('SAVED_ENTRY_ADMISSION_MISSING')
        response = self.order_lists.query_list(list_client_id=generation['list_client_id'])
        if response.get('listClientOrderId') != generation['list_client_id']:
            raise RecoveryBlocked('ENTRY_LIST_CLIENT_MISMATCH')
        ids = self.order_lists._extract_ids(response,
            list_client_id=generation['list_client_id'],
            working_client_id=generation['working_client_id'],
            tp_client_id=generation['tp_client_id'], sl_client_id=generation['sl_client_id'])
        if ids.working_order_id is None:
            raise RecoveryBlocked('ENTRY_WORKING_ORDER_MISSING')
        order = self.exchange.fetch_order(str(ids.working_order_id), pair)
        order = verify_child(order, order_id=ids.working_order_id,
                             client_id=generation['working_client_id'], pair=pair, side='buy')
        payload = json.loads(generation.get('payload_json') or '{}')
        plan = payload.get('plan') or {}
        expected = Decimal(str(plan.get('quantity')))
        amount, filled, cost = (Decimal(str(order.get(field) or 0)) for field in ('amount','filled','cost'))
        if not all(value.is_finite() for value in (expected, amount, filled, cost)):
            raise RecoveryBlocked('ENTRY_NONFINITE_ECONOMICS')
        if amount != expected or not 0 <= filled <= amount or cost < 0:
            raise RecoveryBlocked('ENTRY_QUANTITY_MISMATCH')
        if cost > Decimal(intent['nominal_usdt']) + Decimal('0.01'):
            raise RecoveryBlocked('ENTRY_COST_EXCEEDS_SAVED_ADMISSION')
        if str((order.get('info') or {}).get('timeInForce') or order.get('timeInForce')) != 'FOK':
            raise RecoveryBlocked('ENTRY_NOT_FOK')
        self.store.put_generation(intent_id=intent_id, generation=1, pair=pair,
            mode=generation['mode'], status='ENTRY_PENDING', ids=ids.as_dict(),
            expected_qty=generation['expected_qty'], payload=payload)
        self.store.bind_order_identity(scope_id=self.scope_id, intent_id=intent_id,
            generation=1, pair=pair, role='WORKING', client_id=generation['working_client_id'],
            order_id=str(order['id']))
        if filled:
            actual = self._record_order_trades(intent_id=intent_id, pair=pair,
                                               order_id=str(order['id']), side='BUY')
            if actual != filled:
                raise RecoveryBlocked('ENTRY_EXECUTION_HISTORY_INCOMPLETE')
        status = str(order.get('status') or '').lower()
        if filled == 0 and status in {'expired','rejected','canceled','cancelled'}:
            if response.get('listOrderStatus') != 'ALL_DONE':
                raise RecoveryBlocked('ZERO_FILL_ENTRY_LIST_NOT_TERMINAL')
            if self.exchange._api.fetch_open_orders(pair):
                raise RecoveryBlocked('ZERO_FILL_ENTRY_HAS_OPEN_OBLIGATIONS')
            self.store.finalize_entry_no_fill(intent_id)
            prepared = getattr(self, 'prepared', {}).get(pair)
            if prepared is not None and prepared.intent_id == intent_id:
                self.prepared.pop(pair)
            return None
        if status != 'closed' or filled != amount:
            raise RecoveryBlocked('FOK_ENTRY_NOT_TERMINAL_FULL_FILL')
        return {'intent':intent, 'order':order,
                'amount_step':self._quantity_step(pair), 'price_tick':self._tick_size(pair)}

    def bind_recovered_entry(self, intent_id, trade):
        """Bind only after Freqtrade has committed the matching real working order."""
        generation = self.store.generation(intent_id, 1)
        order = trade.select_order_by_order_id(str(generation['working_order_id']))
        if order is None or trade.pair != generation['pair'] or not order.filled:
            raise RecoveryBlocked('RECOVERED_CANONICAL_ENTRY_MISMATCH')
        self.store.bind_recovered_trade(intent_id, int(trade.id))
        self._trade_intent[int(trade.id)] = intent_id
