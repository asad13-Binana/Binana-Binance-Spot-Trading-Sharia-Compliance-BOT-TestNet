"""Finish acknowledgments after a committed close without replaying economics."""
from .canonical_fees import receipt
from .recovery_contract import RecoveryBlocked, TERMINAL, dec
from .verified_recovery import RESOLVED_PREFIXES
from .operator_exit import OPERATOR_PRE_SUBMISSION
from .submission_absence import verify_absent_replacement


NO_SUBMISSION_PHASES = frozenset({
    'PROMOTION_INTENT', 'CANCEL_UNKNOWN', 'ABORTED_OLD_FILL',
    'FILL_RECONCILIATION_UNKNOWN', 'RESIDUAL_PLAN_INVALID',
    'NOT_NEEDED_POSITION_EXITED',
})


def acknowledgment_ids(intent_id, trade_id):
    return tuple(prefix + intent_id for prefix in RESOLVED_PREFIXES) + (
        'canonical-fees-' + intent_id, 'closed-ack-' + intent_id, 'operator-exit-' + intent_id,
        f'canonical-trade-{trade_id}',
    )


class ClosedRecovery:
    def recover_closed_acknowledgments(self):
        """Caller holds the owner exit lock. Only extension state can change."""
        from sqlalchemy.orm import Session, selectinload
        from freqtrade.persistence import Trade
        with self.store._connect() as connection:
            intents = [dict(row) for row in connection.execute(
                'SELECT * FROM intents WHERE freqtrade_trade_id IS NOT NULL')]
            incidents = {row[0] for row in connection.execute(
                "SELECT incident_id FROM incidents WHERE status='OPEN'")}
        ready = True
        for intent in intents:
            iid, tid = intent['intent_id'], intent['freqtrade_trade_id']
            if intent['state'] in self.TERMINAL_INTENT_STATES and not incidents.intersection(
                    acknowledgment_ids(iid, tid)):
                continue
            try:
                # Separate read session sees committed rows even if the owner
                # has cached or uncommitted objects. Load lazy='raise' data here.
                with Session(Trade.session.get_bind(), autoflush=False) as canonical:
                    trade = canonical.get(Trade, tid, options=[
                        selectinload(Trade.orders), selectinload(Trade.custom_data)])
                    if trade is None:
                        raise RecoveryBlocked('BOUND_CANONICAL_TRADE_MISSING')
                    if trade.is_open:
                        continue
                    self._acknowledge_closed_candidate(trade, intent)
            except Exception as exc:
                ready = False
                self.store.incident(incident_id='closed-ack-' + iid, intent_id=iid,
                    pair=intent['pair'], code='CLOSED_ACKNOWLEDGMENT_BLOCKED',
                    detail=str(exc)[:180] if isinstance(exc, RecoveryBlocked) else type(exc).__name__)
        return ready

    def _acknowledge_closed_candidate(self, trade, intent):
        iid, pair = intent['intent_id'], intent['pair']
        identity = trade.binana_custom_data('binana_entry_identity')
        if (trade.is_open or trade.pair != pair or trade.id != intent['freqtrade_trade_id']
                or not identity or identity.get('scope_id') != self.scope_id
                or identity.get('intent_id') != iid or identity.get('accounting_version') != 1):
            raise RecoveryBlocked('CLOSED_CANONICAL_OWNER_UNVERIFIED')
        committed = trade.binana_custom_data('binana_fill_receipts')
        if (not committed or committed.get('scope_id') != self.scope_id
                or committed.get('pair') != pair):
            raise RecoveryBlocked('CLOSED_CANONICAL_RECEIPTS_UNVERIFIED')
        economics = trade.binana_economics()
        if (not economics or economics['quantity'] != 0 or economics['basis'] != 0
                or not economics['entries'] or not economics['exits']
                or any(order.ft_is_open for order in trade.orders)):
            raise RecoveryBlocked('CLOSED_CANONICAL_SETTLEMENT_INCOMPLETE')
        if any(abs(dec(value) - economics['realized']) > dec('0.00000001')
               for value in (trade.realized_profit, trade.close_profit_abs)):
            raise RecoveryBlocked('CLOSED_CANONICAL_PROFIT_MISMATCH')
        with self.store._connect() as connection:
            generations = [dict(row) for row in connection.execute(
                'SELECT * FROM protection WHERE intent_id=? ORDER BY generation', (iid,))]
            bindings = {str(row['order_id']): dict(row) for row in connection.execute(
                'SELECT * FROM order_identity WHERE scope_id=? AND intent_id=? AND pair=?',
                (self.scope_id, iid, pair)) if row['order_id'] is not None}
        if not generations:
            raise RecoveryBlocked('CLOSED_PROTECTION_HISTORY_MISSING')
        seen, verified_receipts = set(), {}
        for generation in generations:
            if generation['mode'] not in ('FIXED_OCO', 'TRAILING_OCO', 'TARGET_EXIT', 'STOP_EXIT', 'OPERATOR_EXIT'):
                raise RecoveryBlocked('CLOSED_PROTECTION_MODE_UNKNOWN')
            if generation['pair'] != pair:
                raise RecoveryBlocked('CLOSED_GENERATION_PAIR_MISMATCH')
            known = [key for key in ('working', 'tp', 'sl') if generation.get(key + '_order_id')]
            if not known:
                if generation['status'] == 'SUBMISSION_ABSENT':
                    verify_absent_replacement(self, generation, pair)
                    continue
                if generation['mode'] == 'OPERATOR_EXIT' and generation['status'] in OPERATOR_PRE_SUBMISSION:
                    continue
                if generation['status'] in NO_SUBMISSION_PHASES:
                    continue
                raise RecoveryBlocked('CLOSED_SUBMISSION_OUTCOME_UNVERIFIED')
            required = ('tp',) if generation['mode'] in ('TARGET_EXIT','OPERATOR_EXIT') else (
                ('sl',) if generation['mode'] == 'STOP_EXIT' else ('tp', 'sl'))
            if any(not generation.get(key + '_order_id') for key in required):
                raise RecoveryBlocked('CLOSED_CHILD_IDENTITY_INCOMPLETE')
            if generation['mode'] not in ('TARGET_EXIT', 'STOP_EXIT','OPERATOR_EXIT') and (
                    not generation.get('order_list_id') or not generation.get('list_client_id')):
                raise RecoveryBlocked('CLOSED_LIST_IDENTITY_INCOMPLETE')
            if generation.get('order_list_id'):
                report = self.order_lists.query_list(order_list_id=generation['order_list_id'])
                if (str(report.get('orderListId')) != str(generation['order_list_id'])
                        or report.get('listClientOrderId') != generation['list_client_id']
                        or report.get('symbol') != pair.replace('/', '')
                        or report.get('listOrderStatus') != 'ALL_DONE'):
                    raise RecoveryBlocked('CLOSED_EXCHANGE_LIST_NOT_TERMINAL')
            for key in known:
                oid = str(generation[key + '_order_id'])
                if oid in seen:
                    raise RecoveryBlocked('CLOSED_ORDER_REUSED_ACROSS_GENERATIONS')
                seen.add(oid)
                if key == 'working' and (oid != identity.get('order_id')
                        or generation['working_client_id'] != identity.get('client_id')):
                    raise RecoveryBlocked('CLOSED_ENTRY_IDENTITY_MISMATCH')
                binding = bindings.get(oid)
                role = 'CANONICAL_EXIT' if generation['mode']=='OPERATOR_EXIT' else {'working': 'WORKING', 'tp': 'TAKE_PROFIT', 'sl': 'STOP'}[key]
                if (not binding or binding['generation'] != generation['generation']
                        or binding['role'] != role):
                    raise RecoveryBlocked('CLOSED_GENERATION_OWNER_UNVERIFIED')
                order = self._owned_child(pair, generation, key, 'buy' if key == 'working' else 'sell')
                self.verify_canonical_order_owner(trade, order)
                if order['status'] not in TERMINAL:
                    raise RecoveryBlocked('CLOSED_EXCHANGE_CHILD_NOT_TERMINAL')
                if dec(order.get('filled') or 0) > 0 or oid in committed['orders']:
                    self._record_order_trades(intent_id=iid, pair=pair, order_id=oid,
                                              side=order['side'].upper())
                    actual = receipt(pair, order, self._verified_order_fills)
                    if committed['orders'].get(oid) != actual:
                        raise RecoveryBlocked('CLOSED_AUTHENTICATED_RECEIPT_MISMATCH')
                    verified_receipts[oid] = actual
        if verified_receipts != committed['orders']:
            raise RecoveryBlocked('CLOSED_CANONICAL_RECEIPT_COVERAGE_INCOMPLETE')
        # A conflicting active order is left untouched, including foreign orders.
        if self.exchange._api.fetch_open_orders(pair):
            raise RecoveryBlocked('CLOSED_EXCHANGE_OBLIGATION_REMAINS')
        self.store.finalize_closed_acknowledgment(intent_id=iid, trade_id=trade.id,
            pair=pair, expected_state=intent['state'], generations=generations,
            incident_ids=acknowledgment_ids(iid, trade.id))
