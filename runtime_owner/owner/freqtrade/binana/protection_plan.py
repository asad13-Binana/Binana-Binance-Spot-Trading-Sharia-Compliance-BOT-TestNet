"""Protection plan math and invariants for BINANA Spot positions."""
from __future__ import annotations
from dataclasses import dataclass, replace
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Any


class ProtectionPlanError(ValueError):
    pass


def D(v: Any) -> Decimal:
    x=Decimal(str(v))
    if not x.is_finite():
        raise ProtectionPlanError("non-finite numeric value")
    return x


def round_tick(value: Decimal, tick: Decimal, *, up: bool) -> Decimal:
    if tick <= 0:
        raise ProtectionPlanError("tick must be positive")
    return (value / tick).to_integral_value(rounding=ROUND_UP if up else ROUND_DOWN) * tick


@dataclass(frozen=True)
class ProtectionPlan:
    intent_id: str
    pair: str
    generation: int
    mode: str
    quantity: Decimal
    reference_price: Decimal
    tp_price: Decimal
    stop_trigger: Decimal | None
    stop_limit: Decimal | None
    trailing_delta_bips: int | None
    target_fraction: Decimal
    stop_fraction: Decimal
    estimated_exit_fee_fraction: Decimal

    def ensure_sell_geometry(self) -> None:
        if self.quantity <= 0 or self.reference_price <= 0:
            raise ProtectionPlanError("quantity/reference must be positive")
        if self.tp_price <= self.reference_price:
            raise ProtectionPlanError("sell take-profit must be above reference")
        if self.mode == "FIXED_OCO":
            if self.stop_trigger is None or self.stop_limit is None:
                raise ProtectionPlanError("fixed OCO requires stop trigger and stop limit")
            if not (Decimal("0") < self.stop_limit <= self.stop_trigger < self.reference_price):
                raise ProtectionPlanError("invalid fixed sell-stop geometry")
        elif self.mode == "TRAILING_OCO":
            if self.trailing_delta_bips is None or self.trailing_delta_bips <= 0:
                raise ProtectionPlanError("trailing OCO requires positive delta")
        else:
            raise ProtectionPlanError(f"unsupported protection mode {self.mode}")


def provisional_plan(*, intent_id: str, pair: str, quantity: Any, entry_limit: Any,
                     tick_size: Any, target_fraction: Any, stop_fraction: Any,
                     stop_limit_buffer_fraction: Any, estimated_exit_fee_fraction: Any = "0") -> ProtectionPlan:
    q=D(quantity); entry=D(entry_limit); tick=D(tick_size)
    target=D(target_fraction); stop=D(stop_fraction); buffer=D(stop_limit_buffer_fraction); fee=D(estimated_exit_fee_fraction)
    if not (Decimal("0") < stop < Decimal("1") and Decimal("0") < target < Decimal("1")):
        raise ProtectionPlanError("target/stop fractions must be in (0,1)")
    if not (Decimal("0") <= buffer < stop):
        raise ProtectionPlanError("stop-limit buffer must be >=0 and smaller than stop fraction")
    # TP is rounded upward so rounding cannot silently reduce the configured target.
    raw_tp = entry * (Decimal("1") + target + fee)
    raw_stop = entry * (Decimal("1") - stop)
    raw_limit = raw_stop * (Decimal("1") - buffer)
    plan=ProtectionPlan(intent_id,pair,1,"FIXED_OCO",q,entry,
                        round_tick(raw_tp,tick,up=True),
                        round_tick(raw_stop,tick,up=False),
                        round_tick(raw_limit,tick,up=False),None,target,stop,fee)
    plan.ensure_sell_geometry()
    return plan


def fill_adjusted(plan: ProtectionPlan, *, quantity: Any, average_fill: Any, tick_size: Any) -> ProtectionPlan:
    """Recompute prices from actual fill. Caller decides whether replacement is worth the gap."""
    q=D(quantity); avg=D(average_fill); tick=D(tick_size)
    raw_tp=avg*(Decimal("1")+plan.target_fraction+plan.estimated_exit_fee_fraction)
    raw_stop=avg*(Decimal("1")-plan.stop_fraction)
    # Preserve the same proportional stop-limit buffer observed in provisional plan.
    buffer=(plan.stop_trigger-plan.stop_limit)/plan.stop_trigger if plan.stop_trigger else Decimal("0")
    new=replace(plan,quantity=q,reference_price=avg,
                tp_price=round_tick(raw_tp,tick,up=True),
                stop_trigger=round_tick(raw_stop,tick,up=False),
                stop_limit=round_tick(raw_stop*(Decimal("1")-buffer),tick,up=False))
    new.ensure_sell_geometry(); return new


def trailing_plan(plan: ProtectionPlan, *, market_price: Any, trailing_delta_bips: int,
                  min_bips: int, max_bips: int, generation: int | None = None) -> ProtectionPlan:
    if not (int(min_bips) <= int(trailing_delta_bips) <= int(max_bips)):
        raise ProtectionPlanError("trailing delta violates symbol filter")
    market=D(market_price)
    indicative=market*(Decimal("1")-Decimal(int(trailing_delta_bips))/Decimal("10000"))
    # Never intentionally replace a fixed boundary with an immediately looser indicative boundary.
    if plan.stop_trigger is not None and indicative < plan.stop_trigger:
        raise ProtectionPlanError("promotion would loosen accepted stop boundary")
    new=replace(plan,generation=generation or plan.generation+1,mode="TRAILING_OCO",
                trailing_delta_bips=int(trailing_delta_bips),stop_trigger=None,stop_limit=None)
    new.ensure_sell_geometry(); return new
