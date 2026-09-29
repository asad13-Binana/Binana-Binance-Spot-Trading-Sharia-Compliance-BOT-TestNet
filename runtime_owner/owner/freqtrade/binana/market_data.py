"""Bounded execution snapshot built before the final entry callback."""
from __future__ import annotations
from dataclasses import asdict, dataclass
from decimal import Decimal
import json
import math
from pathlib import Path
from time import time
from typing import Any


D=Decimal


def _walk(levels, *, quote_budget: Decimal | None = None, base_qty: Decimal | None = None):
    quote=D("0"); base=D("0"); first=None
    for raw_p, raw_q in levels:
        p=D(str(raw_p)); q=D(str(raw_q))
        if p<=0 or q<=0: continue
        if first is None: first=p
        if quote_budget is not None:
            remaining=quote_budget-quote
            take=min(q,remaining/p)
        else:
            remaining=base_qty-base
            take=min(q,remaining)
        if take<=0: break
        base+=take; quote+=take*p
        if quote_budget is not None and quote>=quote_budget-D("0.00000001"): break
        if base_qty is not None and base>=base_qty-D("0.00000001"): break
    complete=(quote_budget is not None and quote>=quote_budget*D("0.9999")) or (base_qty is not None and base>=base_qty*D("0.9999"))
    avg=quote/base if base else None
    slip=(abs(avg-first)/first*D("10000")) if avg is not None and first else None
    return complete,quote,base,slip


@dataclass(frozen=True)
class MarketSnapshot:
    pair: str
    created_ts: float
    state: str
    best_bid: str | None
    best_ask: str | None
    spread_bps: str | None
    bid_depth_20_quote: str
    ask_depth_20_quote: str
    buy_250_slippage_bps: str | None
    exit_250_slippage_bps: str | None
    quote_volume_24h: str | None
    taker_buy_ratio_60s: str | None
    cvd_quote_60s: str | None
    flow_status: str
    reason: str
    sequence_book_status: str = "unavailable"
    book_imbalance_20: str | None = None
    slippage_quote_budget: str | None = None

    def to_dict(self): return asdict(self)


class MarketDataService:
    def __init__(self, exchange, context_file: str | Path, policy: dict[str, Any]):
        self.exchange=exchange; self.context_file=Path(context_file); self.policy=policy
        self.cache: dict[str,MarketSnapshot]={}

    def _context(self,pair):
        try:
            d=json.loads(self.context_file.read_text())
            if d.get('universe_ready') is False:
                return {},'universe_unavailable'
            row=(d.get('symbols') or {}).get(pair.replace('/','')) or {}
            age=time()-__import__('datetime').datetime.fromisoformat(d['generated_at'].replace('Z','+00:00')).timestamp()
            if not math.isfinite(age) or not 0<=age<=float(self.policy.get('max_context_age_seconds',30)):return {},'stale'
            flow=row.get('spot_aggressive_flow') or {}
            if flow.get('status')!='fresh':return row,'stale'
            coverage=float(flow.get('continuous_coverage_seconds',0))
            if not math.isfinite(coverage) or coverage<60:return row,'warming_up'
            observed=__import__('datetime').datetime.fromisoformat(flow['last_agg_trade_received_at'].replace('Z','+00:00')).timestamp()
            event_age=time()*1000-float(flow['last_agg_trade_event_time_ms'])
            if not (0<=time()-observed<=5 and -1000<=event_age<=5000):return row,'stale'
            row['context_age_ms']=age*1000
            return row,'fresh'
        except Exception:
            return {},'missing'

    def refresh(self,pair: str, quote_budget: Decimal | str | float | None = None) -> MarketSnapshot:
        budget=D(str('250' if quote_budget is None else quote_budget))
        if not budget.is_finite() or budget <= 0:
            raise ValueError('quote_budget must be finite and positive')
        ctx,flow_state=self._context(pair)
        if flow_state != 'fresh':
            snap=MarketSnapshot(
                pair,time(),'UNKNOWN',None,None,None,'0','0',None,None,None,
                None,None,flow_state,'PAIR_NOT_READY','unavailable',None,str(budget)
            )
            self.cache[pair]=snap
            return snap
        try:
            execution_public=getattr(getattr(self.exchange,'_api',None),'binana_testnet_public',None)
            book=(execution_public.publicGetDepth({'symbol':pair.replace('/',''),'limit':20})
                  if execution_public is not None else self.exchange.fetch_l2_order_book(pair,20))
            bids=book.get('bids') or []; asks=book.get('asks') or []
            if not bids or not asks: raise ValueError('empty book')
            best_bid=D(str(bids[0][0])); best_ask=D(str(asks[0][0])); mid=(best_bid+best_ask)/2
            spread=(best_ask-best_bid)/mid*D('10000') if mid else None
            bid_depth=sum(D(str(p))*D(str(q)) for p,q,*_ in bids[:20])
            ask_depth=sum(D(str(p))*D(str(q)) for p,q,*_ in asks[:20])
            buy_ok,_,buy_base,buy_slip=_walk(asks,quote_budget=budget)
            exit_ok,_,_,exit_slip=_walk(bids,base_qty=buy_base if buy_ok else budget/best_ask)
            ticker=(self.exchange.get_tickers(symbols=[pair], cached=False).get(pair) or {})
            qv=ticker.get('quoteVolume') or (ticker.get('info') or {}).get('quoteVolume')
            qv=D(str(qv)) if qv is not None else None
            flow=ctx.get('spot_aggressive_flow') or {}
            ratio=flow.get('taker_buy_ratio_60s'); cvd=flow.get('cvd_quote_60s')
            depth_context=ctx.get('l2_order_book') or {}
            depth_status='fresh' if (depth_context.get('sequence_verified') is True and depth_context.get('status')=='fresh' and 0<=float(depth_context.get('depth_age_ms',float('inf')))+float(ctx.get('context_age_ms',float('inf')))<=5000) else 'unavailable'
            imbalance=depth_context.get('imbalance_20_quote')
            hard_spread=D(str(self.policy.get('hard_max_spread_bps',100)))
            hard_depth=D(str(self.policy.get('hard_min_depth_quote',250)))
            hard_slip=D(str(self.policy.get('hard_max_slippage_bps',75)))
            min_qv=D(str(self.policy.get('min_quote_volume_24h',1000000)))
            good_spread=D(str(self.policy.get('good_max_spread_bps',30)))
            good_depth=D(str(self.policy.get('good_min_depth_quote',1000)))
            good_slip=D(str(self.policy.get('good_max_slippage_bps',25)))
            if not buy_ok or not exit_ok: state,reason='DANGEROUS','INSUFFICIENT_OBSERVED_DEPTH'
            elif spread is None or spread>hard_spread: state,reason='DANGEROUS','SPREAD_HARD_LIMIT'
            elif min(bid_depth,ask_depth)<hard_depth: state,reason='DANGEROUS','DEPTH_HARD_LIMIT'
            elif (buy_slip is not None and buy_slip>hard_slip) or (exit_slip is not None and exit_slip>hard_slip): state,reason='DANGEROUS','SLIPPAGE_HARD_LIMIT'
            elif qv is None or qv<min_qv: state,reason='DANGEROUS','VOLUME_HARD_LIMIT'
            elif spread<=good_spread and min(bid_depth,ask_depth)>=good_depth and (buy_slip or D('0'))<=good_slip and (exit_slip or D('0'))<=good_slip: state,reason='GOOD','EXECUTION_HEADROOM'
            else: state,reason='ACCEPTABLE','EXECUTION_WITHIN_HARD_LIMITS'
            snap=MarketSnapshot(pair,time(),state,str(best_bid),str(best_ask),str(spread) if spread is not None else None,str(bid_depth),str(ask_depth),str(buy_slip) if buy_slip is not None else None,str(exit_slip) if exit_slip is not None else None,str(qv) if qv is not None else None,str(ratio) if ratio is not None else None,str(cvd) if cvd is not None else None,flow_state,reason,depth_status,str(imbalance) if imbalance is not None else None,str(budget))
        except Exception as exc:
            snap=MarketSnapshot(pair,time(),'UNKNOWN',None,None,None,'0','0',None,None,None,None,None,'missing',f'DATA_UNAVAILABLE:{type(exc).__name__}','unavailable',None,str(budget))
        self.cache[pair]=snap; return snap

    def get(self,pair: str,max_age_seconds: float=20) -> MarketSnapshot | None:
        s=self.cache.get(pair)
        return s if s and time()-s.created_ts<=max_age_seconds else None
