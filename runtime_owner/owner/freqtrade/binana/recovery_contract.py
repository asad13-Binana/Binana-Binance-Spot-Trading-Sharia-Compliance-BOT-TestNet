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

def _protection_geometry(tp, sl, plan, order_list_id):
    above, below = tp.get('info') or {}, sl.get('info') or {}
    if above.get('type') != 'LIMIT_MAKER' or below.get('type') not in {'STOP_LOSS','STOP_LOSS_LIMIT'}:
        raise RecoveryBlocked('PROTECTION_CHILD_TYPE_MISMATCH')
    if above.get('orderListId') is None or str(above['orderListId']) != str(below.get('orderListId')):
        raise RecoveryBlocked('PROTECTION_CHILD_LIST_MISMATCH')
    if order_list_id is not None and str(above['orderListId']) != str(order_list_id):
        raise RecoveryBlocked('PROTECTION_PLAN_LIST_MISMATCH')
    if not isinstance(plan, dict):
        raise RecoveryBlocked('DURABLE_PROTECTION_PLAN_UNAVAILABLE')
    if dec(above.get('price')) != dec(plan.get('tp_price')):
        raise RecoveryBlocked('PROTECTION_TP_PRICE_MISMATCH')
    if plan.get('mode') == 'FIXED_OCO':
        if (below.get('type') != 'STOP_LOSS_LIMIT' or below.get('timeInForce') != 'GTC'
                or dec(below.get('price')) != dec(plan.get('stop_limit'))
                or dec(below.get('stopPrice')) != dec(plan.get('stop_trigger'))):
            raise RecoveryBlocked('PROTECTION_STOP_GEOMETRY_MISMATCH')
    elif plan.get('mode') == 'TRAILING_OCO':
        if (below.get('type') != 'STOP_LOSS'
                or int(below.get('trailingDelta',0)) != int(plan.get('trailing_delta_bips',0))
                or dec(below.get('stopPrice',0)) != 0):
            raise RecoveryBlocked('PROTECTION_TRAILING_GEOMETRY_MISMATCH')
    else:
        raise RecoveryBlocked('PROTECTION_PLAN_MODE_UNKNOWN')


def native_exit_working(tp, sl, quantity, *, plan=None, order_list_id=None):
    """Recognize only an authenticated working exit after native OCO activation.

    A canceled sibling alone is not evidence of activation. Require raw working
    state plus a stop trigger or an actual TP fill, exact geometry and remaining
    ownership. Never cancel the surviving child to repair an ambiguous snapshot.
    """
    rows=[('tp',tp,sl),('sl',sl,tp)]
    matches=[(role,active,other) for role,active,other in rows
             if active.get('status')=='open' and other.get('status') in {'canceled','cancelled'}]
    if len(matches)!=1:
        return False
    role,active,other=matches[0]
    _protection_geometry(tp,sl,plan,order_list_id)
    raw=active.get('info') or {}; inactive=other.get('info') or {}
    if (raw.get('status') not in {'NEW','PARTIALLY_FILLED'} or inactive.get('status')!='CANCELED'
            or raw.get('isWorking') is not True or int(raw.get('workingTime',-1))<0
            or dec(other.get('filled',0))!=0):
        return False
    if role=='tp' and dec(active.get('filled',0))<=0:
        return False
    q=dec(quantity)
    return q>0 and dec(active.get('amount',0))-dec(active.get('filled',0))==q


def live_protection(tp, sl, quantity, *, plan=None, order_list_id=None):
    if tp.get("status") != "open" or sl.get("status") != "open":
        return False
    _protection_geometry(tp,sl,plan,order_list_id)
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
