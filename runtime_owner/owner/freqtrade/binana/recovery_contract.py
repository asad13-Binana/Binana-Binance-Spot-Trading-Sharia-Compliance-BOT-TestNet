"""Verified residual arithmetic and exchange protection evidence. No order transport."""
from decimal import Decimal
from dataclasses import dataclass

TERMINAL = frozenset({"closed", "canceled", "cancelled", "expired", "rejected"})
LIVE = frozenset({"open"})
class RecoveryBlocked(RuntimeError):
    pass

def dec(value):
    x = Decimal(str(value))
    if not x.is_finite():
        raise RecoveryBlocked("NON_FINITE_QUANTITY")
    return x

def residual_quantity(original, sold, canonical, step):
    original, sold, canonical, step = map(dec, (original, sold, canonical, step))
    if min(original, sold, canonical) < 0 or step <= 0:
        raise RecoveryBlocked("INVALID_RESIDUAL_BASIS")
    if sold > original + canonical:
        raise RecoveryBlocked("SELL_EXCEEDS_OWNERSHIP")
    residual = min(max(Decimal(0), original - sold), canonical)
    return (residual // step) * step

def recovery_boundary_action(price, stop_trigger, tp_price):
    """Classify recovery when a position has no surviving protection."""
    px, stop, tp = dec(price), dec(stop_trigger), dec(tp_price)
    if px <= stop:
        return "STOP_EXIT"
    if px >= tp:
        return "TARGET_EXIT"
    return "REPROTECT"

def recovery_protection_quantity(original, sold, canonical, price, filters):
    """Return the safest executable recovery quantity after ownership is verified.

    The immutable protection basis may intentionally reserve one quantity step below
    canonical ownership.  If that reserve-derived residual is no longer executable
    while the verified canonical remainder is executable, protect the canonical
    rounded remainder instead of leaving material owned assets unprotected.
    """
    f = {r["filterType"]: r for r in filters}
    if "LOT_SIZE" not in f:
        raise RecoveryBlocked("LOT_SIZE_UNAVAILABLE")
    step = dec(f["LOT_SIZE"]["stepSize"])
    canonical_ok, canonical_rounded = executable_quantity(canonical, price, filters)
    if not canonical_ok:
        return None
    reserved = residual_quantity(original, sold, canonical, step)
    reserved_ok, reserved_rounded = executable_quantity(reserved, price, filters)
    return reserved_rounded if reserved_ok else canonical_rounded

def verify_child(order, *, order_id, client_id, pair, side="sell"):
    if not isinstance(order, dict):
        raise RecoveryBlocked("CHILD_UNAVAILABLE")
    info = order.get("info") or {}
    if str(order.get("id")) != str(order_id):
        raise RecoveryBlocked("CHILD_ID_MISMATCH")
    actual_client = order.get("clientOrderId") or info.get("clientOrderId")
    if not actual_client or actual_client != client_id:
        raise RecoveryBlocked("CHILD_CLIENT_MISMATCH")
    if order.get("symbol") != pair or order.get("side") != side:
        raise RecoveryBlocked("CHILD_SCOPE_MISMATCH")
    if order.get("status") not in TERMINAL | LIVE:
        raise RecoveryBlocked("CHILD_STATUS_UNKNOWN")
    filled, amount = dec(order.get("filled", 0)), dec(order.get("amount", 0))
    if filled < 0 or amount < filled:
        raise RecoveryBlocked("CHILD_QUANTITY_INVALID")
    return order

def live_protection(tp, sl, quantity):
    if tp.get("status") != "open" or sl.get("status") != "open":
        return False
    q = dec(quantity)
    # Fresh protection must be complete; any simultaneous fill is reconciled next.
    return (dec(tp.get("amount", 0)) - dec(tp.get("filled", 0)) == q
        and dec(sl.get("amount", 0)) - dec(sl.get("filled", 0)) == q)


def classify_executability(quantity, price, filters):
    """Return an explicit execution classification plus rounded quantity."""
    f = {r["filterType"]: r for r in filters}
    if "LOT_SIZE" not in f or not ({"NOTIONAL", "MIN_NOTIONAL"} & f.keys()):
        raise RecoveryBlocked("EXECUTION_FILTERS_UNAVAILABLE")
    lot = f["LOT_SIZE"]; q, px = dec(quantity), dec(price)
    step = dec(lot["stepSize"])
    if step <= 0 or px <= 0 or q < 0:
        raise RecoveryBlocked("INVALID_EXECUTION_FILTER")
    rounded = (q // step) * step
    if rounded < dec(lot["minQty"]):
        return "NON_EXECUTABLE_DUST_MIN_QTY", rounded
    minimum = dec((f.get("NOTIONAL") or f["MIN_NOTIONAL"])["minNotional"])
    if rounded * px < minimum:
        return "NON_EXECUTABLE_DUST_MIN_NOTIONAL", rounded
    if rounded > dec(lot["maxQty"]):
        raise RecoveryBlocked("QUANTITY_ABOVE_MAX")
    if "NOTIONAL" in f and rounded * px > dec(f["NOTIONAL"]["maxNotional"]):
        raise RecoveryBlocked("NOTIONAL_ABOVE_MAX")
    return "EXECUTABLE", rounded

def protectable_quantity(original, sold, canonical, price, filters):
    """Protection quantity is reserve-aware and distinct from canonical ownership."""
    return recovery_protection_quantity(original, sold, canonical, price, filters)

def order_history_state(error_text, *, testnet):
    """Map TestNet's missing historical order response to an explicit recovery state."""
    text=str(error_text or '')
    if testnet and ('-2013' in text or 'NO_SUCH_ORDER' in text or 'Order does not exist' in text):
        return "ORDER_HISTORY_UNAVAILABLE"
    return None

def executable_quantity(quantity, price, filters):
    """Compatibility wrapper around explicit execution classification."""
    state, rounded = classify_executability(quantity, price, filters)
    return state == "EXECUTABLE", rounded
