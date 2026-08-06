from __future__ import annotations
from decimal import Decimal
from services.common.models import ProtectionMode

class OrderRequestFactory:
    """Builds Binance Spot order-list requests. No network calls occur here."""
    def __init__(self, legacy): self.l=legacy

    def pending_qty(self, sym, qty: Decimal):
        # V101-NEW-006 fix: when the fee shave rounds the protective quantity
        # to zero the old code restored the FULL quantity, defeating the shave
        # and risking an insufficient-balance rejection on the protective sell.
        # Zero now stays zero; the entry path must reject or resize instead.
        q=qty
        if getattr(self.l.CFG,'EXIT_FEE_SHAVE',True):
            q=self.l.round_down(qty*(Decimal(1)-Decimal(str(self.l.CFG.FEE_PCT_PER_SIDE))/100),sym.step)
        return q if q>0 else Decimal(0)

    def entry(self, mode: ProtectionMode, sym, qty: Decimal, entry: Decimal, tp: Decimal, trail_bips: int):
        l=self.l; qty=l.round_down(qty,sym.step); entry=l.round_down(entry,sym.tick); tp=l.round_down(tp,sym.tick)
        pend=self.pending_qty(sym,qty); delta=max(sym.trail_min,min(trail_bips,sym.trail_max))
        if pend<=0:
            raise ValueError(
                f'{sym.symbol}: fee-shaved protective quantity rounds to zero for qty {qty} '
                f'(step {sym.step}); entry too small to protect safely — rejected before submission')
        sl_stop=l.round_down(entry*(Decimal(1)-Decimal(str(getattr(l.CFG,'FIXED_STOP_PCT',2.0)))/100),sym.tick)
        sl_limit=l.round_down(sl_stop*l.bips_mult(-l.CFG.LIMIT_FILL_BUFFER_BIPS),sym.tick)
        list_id=l._new_coid('FORTRESS')
        common={'symbol':sym.symbol,'listClientOrderId':list_id,'workingType':'LIMIT','workingSide':'BUY',
            'workingPrice':l.dstr(entry),'workingQuantity':l.dstr(qty),'workingTimeInForce':'GTC',
            'pendingSide':'SELL','pendingQuantity':l.dstr(pend),'newOrderRespType':'FULL'}
        if mode is ProtectionMode.TRAILING_ONLY:
            common.update({'pendingType':'STOP_LOSS_LIMIT','pendingPrice':l.dstr(sl_limit),
                'pendingTrailingDelta':delta,'pendingTimeInForce':'GTC'})
            return 'orderList/oto', common
        common.update({'pendingAboveType':'LIMIT_MAKER','pendingAbovePrice':l.dstr(tp),
            'pendingBelowType':'STOP_LOSS_LIMIT','pendingBelowPrice':l.dstr(sl_limit),
            'pendingBelowTimeInForce':'GTC'})
        if mode is ProtectionMode.FIXED_OCO: common['pendingBelowStopPrice']=l.dstr(sl_stop)
        else: common['pendingBelowTrailingDelta']=delta
        return 'orderList/otoco', common

    def existing(self, mode: ProtectionMode, sym, qty: Decimal, entry: Decimal, current: Decimal,
                 tp_pct: float, stop_pct: float, trail_bips: int):
        l=self.l; qty=l.round_down(qty,sym.step); delta=max(sym.trail_min,min(trail_bips,sym.trail_max))
        tp_target=max(entry*(Decimal(1)+Decimal(str(tp_pct))/100), current*l.bips_mult(20))
        tp=l.round_down(tp_target,sym.tick)
        if tp <= current:
            tp=l.round_down(current+sym.tick,sym.tick)
        stop=l.round_down(max(entry*(Decimal(1)-Decimal(str(stop_pct))/100), Decimal('0')),sym.tick)
        floor=l.round_down(stop*l.bips_mult(-l.CFG.LIMIT_FILL_BUFFER_BIPS),sym.tick)
        list_id=l._new_coid('FORTRESS')
        if mode is ProtectionMode.TRAILING_ONLY:
            lim=l.round_down(current*l.bips_mult(-(delta+l.CFG.LIMIT_FILL_BUFFER_BIPS)),sym.tick)
            return 'order', {'symbol':sym.symbol,'side':'SELL','type':'STOP_LOSS_LIMIT','quantity':l.dstr(qty),
                'price':l.dstr(lim),'trailingDelta':delta,'timeInForce':'GTC','newClientOrderId':list_id}
        params={'symbol':sym.symbol,'side':'SELL','quantity':l.dstr(qty),'listClientOrderId':list_id,
            'aboveType':'LIMIT_MAKER','abovePrice':l.dstr(tp),'belowType':'STOP_LOSS_LIMIT',
            'belowPrice':l.dstr(floor),'belowTimeInForce':'GTC','newOrderRespType':'FULL'}
        if mode is ProtectionMode.FIXED_OCO: params['belowStopPrice']=l.dstr(stop)
        else: params['belowTrailingDelta']=delta
        return 'orderList/oco',params
