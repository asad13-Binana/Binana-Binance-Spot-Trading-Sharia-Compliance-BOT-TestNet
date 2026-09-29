"""Fresh read-only absence proof for a replacement OCO, never an entry."""
import json
from math import isfinite
from time import time

from .recovery_contract import RecoveryBlocked


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


def verify_absent_replacement(manager, generation, pair, *, require_record=True,
                              require_empty_symbol=True):
    if (str(manager.config.get('binana', {}).get('environment', '')).lower() != 'testnet'
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
    if require_empty_symbol and manager.exchange._api.fetch_open_orders(pair):
        raise RecoveryBlocked('REPLACEMENT_OPEN_ORDER_EXISTS')
    return {'verified_absent_at': time(), 'scope_id': manager.scope_id,
            'generation': generation['generation'], 'list_client_id': ids[0],
            'tp_client_id': ids[1], 'sl_client_id': ids[2]}
