from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from binana2.sharia.models import ShariaDecision, ShariaResult
from binana2.state.repositories import StateRepository

@dataclass(frozen=True)
class RiskContext:
    symbol:str; signal_generated_at:datetime; candle_time:datetime; requested_quote:Decimal; available_quote:Decimal; open_positions:int
@dataclass(frozen=True)
class RiskDecision:
    approved:bool; reason:str
class RiskEngine:
    def __init__(self,state:StateRepository,*,max_positions:int,max_signal_age_seconds:int,max_candle_age_seconds:int)->None:
        self.state=state; self.max_positions=max_positions; self.max_signal_age_seconds=max_signal_age_seconds; self.max_candle_age_seconds=max_candle_age_seconds
    def approve_entry(self,context:RiskContext,sharia:ShariaResult)->RiskDecision:
        if sharia.decision is not ShariaDecision.PASS: return RiskDecision(False,f"Sharia gate is {sharia.decision}: {sharia.reason}")
        paused,reason=self.state.is_entry_paused();
        if paused: return RiskDecision(False,f"global entry pause: {reason}")
        halted,reason=self.state.has_active_halt();
        if halted: return RiskDecision(False,f"safety halt: {reason}")
        if context.open_positions>=self.max_positions: return RiskDecision(False,"maximum open positions reached")
        if context.requested_quote<=Decimal("0"): return RiskDecision(False,"requested quote amount must be positive")
        if context.requested_quote>context.available_quote: return RiskDecision(False,"requested quote amount exceeds available balance")
        now=datetime.now(timezone.utc); signal_age=(now-self._aware(context.signal_generated_at)).total_seconds(); candle_age=(now-self._aware(context.candle_time)).total_seconds()
        if signal_age<0 or signal_age>self.max_signal_age_seconds: return RiskDecision(False,f"signal age {signal_age:.1f}s outside freshness bound")
        if candle_age<0 or candle_age>self.max_candle_age_seconds: return RiskDecision(False,f"candle age {candle_age:.1f}s outside freshness bound")
        return RiskDecision(True,"risk checks passed")
    @staticmethod
    def _aware(value:datetime)->datetime: return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
