"""Authenticated execution ingestion into Freqtrade's own custom-data rows."""
from .canonical_fees import FeeEvidenceError, receipt, merge_receipt


class CanonicalReceipts:
    def verify_canonical_order_owner(self, trade, order):
        identity = trade.binana_custom_data('binana_entry_identity')
        if not identity or identity.get('scope_id') != self.scope_id:
            raise FeeEvidenceError('CANONICAL_FEE_OWNER_SCOPE_UNVERIFIED')
        with self.store._connect() as connection:
            binding = connection.execute('SELECT * FROM order_identity WHERE scope_id=? AND pair=? AND order_id=?',
                (self.scope_id, trade.pair, str(order['id']))).fetchone()
        valid_roles = {'WORKING'} if order['side'] == 'buy' else {'TAKE_PROFIT','STOP','CANONICAL_EXIT'}
        if (binding is None or binding['intent_id'] != identity['intent_id']
                or binding['client_id'] != order.get('clientOrderId') or binding['role'] not in valid_roles):
            raise FeeEvidenceError('CANONICAL_ORDER_OWNERSHIP_UNVERIFIED')
        return identity

    def stage_canonical_receipts(self, trade, order):
        identity = trade.binana_custom_data('binana_entry_identity')
        if not identity or identity.get('scope_id') != self.scope_id:
            raise FeeEvidenceError('CANONICAL_FEE_OWNER_SCOPE_UNVERIFIED')
        intent_id = identity['intent_id']
        try:
            self.verify_canonical_order_owner(trade, order)
            if trade.id is not None and self.intent_for_trade(int(trade.id)) not in (None, intent_id):
                raise FeeEvidenceError('CANONICAL_FEE_INTENT_MISMATCH')
            if order['side'] == 'buy' and (str(order['id']) != identity['order_id']
                                         or order.get('clientOrderId') != identity['client_id']):
                raise FeeEvidenceError('CANONICAL_FEE_ENTRY_IDENTITY_MISMATCH')
            self._record_order_trades(intent_id=intent_id, pair=trade.pair,
                                      order_id=str(order['id']), side=str(order['side']).upper())
            item = receipt(trade.pair, order, self._verified_order_fills)
            merged = merge_receipt(trade.binana_custom_data('binana_fill_receipts'),
                                   self.scope_id, trade.pair, item)
            trade.stage_binana_receipts(merged)
        except Exception as exc:
            self.store.incident(incident_id='canonical-fees-'+intent_id,
                intent_id=intent_id,pair=trade.pair,code='CANONICAL_COMMISSION_EVIDENCE_BLOCKED',
                detail=str(exc)[:180] if isinstance(exc,FeeEvidenceError) else type(exc).__name__)
            raise

    def verify_canonical_settlement(self, trade):
        """Called before Freqtrade can commit closure, while the Trade is recoverable."""
        result = trade.binana_economics()
        if result and result['quantity'] == 0 and result['exits']:
            if self.exchange._api.fetch_open_orders(trade.pair):
                raise FeeEvidenceError('EXCHANGE_OBLIGATIONS_REMAIN_BEFORE_CLOSE')

    def acknowledge_canonical_receipts(self, trade):
        # Caller has committed receipt, Order and economics already. On a crash
        # before this acknowledgement the ordinary reconciliation retries it.
        if trade.binana_economics() is not None:
            identity = trade.binana_custom_data('binana_entry_identity')
            self.store.close_incident('canonical-fees-'+identity['intent_id'])
