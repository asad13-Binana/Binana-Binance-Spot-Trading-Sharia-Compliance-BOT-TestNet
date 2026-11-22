"""Fresh read-only absence proof for a replacement OCO, never an entry."""
import json
from math import isfinite
from time import time

from .recovery_contract import RecoveryBlocked
from .environment import package_environment


def explicit_absence(error, *, order_list=False):
    text = str(error)
    for index, character in enumerate(text):
        if character != '{':
            continue
        try:
            payload, _ = json.JSONDecoder().raw_decode(text[index:])
        except ValueError:
            continue
        expected = (-2018, 'Order list does not exist.') if order_list else (-2013, 'Order does not exist.')
        if isinstance(payload, dict) and (payload.get('code'), payload.get('msg')) == expected:
            return True
    return False


def _is_valid_open_orders(value):
    """Only a real JSON array from GET /api/v3/openOrders can prove absence.

    Binance returns an array of order objects; empty array means no open orders.
    None, an error envelope, or any non-list/non-dict-shape value is transient
    or malformed and cannot prove absence — fail closed.
    """
    if not isinstance(value, list):
        return None
    for item in value:
        if not isinstance(item, dict):
            return None
    return value


def _our_order_in_list(open_orders, client_ids):
    """True when any open order carries one of our client order IDs."""
    wanted = {str(cid) for cid in client_ids if cid}
    for order in open_orders:
        if not isinstance(order, dict):
            continue
        client_id = order.get('clientOrderId') or order.get('origClientOrderId')
        if client_id and str(client_id) in wanted:
            return True
        oid = order.get('id') or order.get('orderId')
        if oid and str(oid) in wanted:
            return True
    return False


def verify_absent_replacement(manager, generation, pair, *, require_record=True,
                              require_empty_symbol=True):
    if (str(manager.config.get('binana', {}).get('environment', '')).lower() != package_environment()
            or generation.get('mode') not in {'FIXED_OCO', 'TRAILING_OCO'}
            or int(generation.get('generation', 0)) <= 1 or generation.get('pair') != pair
            or generation.get('working_client_id')
            or any(generation.get(key) for key in ('order_list_id', 'working_order_id',
                                                   'tp_order_id', 'sl_order_id'))):
        raise RecoveryBlocked('REPLACEMENT_ABSENCE_IDENTITY_UNVERIFIED')
    ids = [generation.get(key) for key in ('list_client_id', 'tp_client_id', 'sl_client_id')]
    if not all(isinstance(cid, str) and cid.strip() for cid in ids) or len(set(ids)) != 3:
        raise RecoveryBlocked('REPLACEMENT_ABSENCE_CLIENT_IDS_INCOMPLETE')
    if require_record:
        payload = json.loads(generation.get('payload_json') or '{}')
        stamp = payload.get('verified_absent_at')
        if (type(stamp) not in (int, float) or not isfinite(stamp) or not 0 < stamp <= time() + 5
                or payload.get('list_client_id') != ids[0]):
            raise RecoveryBlocked('REPLACEMENT_ABSENCE_RECORD_UNVERIFIED')
    try:
        manager.order_lists.query_list(list_client_id=ids[0])
    except Exception as exc:
        if not explicit_absence(exc, order_list=True):
            raise RecoveryBlocked('REPLACEMENT_LIST_ABSENCE_UNVERIFIED') from exc
    else:
        raise RecoveryBlocked('REPLACEMENT_LIST_EXISTS')
    for cid in ids[1:]:
        try:
            manager.exchange._api.privateGetOrder({'symbol': pair.replace('/', ''),
                                                   'origClientOrderId': cid})
        except Exception as exc:
            if not explicit_absence(exc):
                raise RecoveryBlocked('REPLACEMENT_CHILD_ABSENCE_UNVERIFIED') from exc
        else:
            raise RecoveryBlocked('REPLACEMENT_CHILD_EXISTS')
    if require_empty_symbol:
        try:
            open_orders = manager.exchange._api.fetch_open_orders(pair)
        except Exception:
            # Transient API error/timeout: cannot prove absence. Fail closed.
            raise RecoveryBlocked('REPLACEMENT_OPEN_ORDER_ABSENCE_UNVERIFIED')
        valid = _is_valid_open_orders(open_orders)
        if valid is None:
            # None, error envelope, or non-list/non-dict shape: cannot prove absence.
            raise RecoveryBlocked('REPLACEMENT_OPEN_ORDER_ABSENCE_UNVERIFIED')
        if valid:
            # Non-empty list: our orders present means an open replacement exists;
            # foreign orders present means we cannot prove our absence.
            if _our_order_in_list(valid, ids):
                raise RecoveryBlocked('REPLACEMENT_OPEN_ORDER_EXISTS')
            raise RecoveryBlocked('REPLACEMENT_OPEN_ORDER_ABSENCE_UNVERIFIED')
        # Empty list [] proves absence of open orders on the symbol.
    return {'verified_absent_at': time(), 'scope_id': manager.scope_id,
            'generation': generation['generation'], 'list_client_id': ids[0],
            'tp_client_id': ids[1], 'sl_client_id': ids[2]}
