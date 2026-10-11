"""Owner-side release evidence checks; UI booleans cannot authorize trading."""
from hashlib import sha256
import json
import math
from pathlib import Path
from time import time


def runtime_bindings(config):
    root = Path(__file__).resolve().parents[1]
    paths = sorted((root / 'binana').glob('*.py'))
    paths += [root / name for name in ['freqtradebot.py', 'exchange/binance.py',
                                      'rpc/rpc.py', 'rpc/api_server/deps.py',
                                      'resolvers/exchange_resolver.py', 'persistence/trade_model.py']]
    paths += [root.parent / 'binana-strategies/BinanaNfiSpot.py']
    paths += [root.parent / 'binana-strategies/binana_vendor/NostalgiaForInfinityX8.py']
    digest = sha256()
    for path in paths:
        digest.update(str(path.relative_to(root.parent)).encode() + b'\0' + path.read_bytes())
    keys = ['binana', 'stake_amount', 'stake_currency', 'max_open_trades', 'timeframe',
            'trading_mode', 'margin_mode', 'dry_run', 'force_entry_enable',
            'position_adjustment_enable', 'order_types', 'order_time_in_force']
    policy = {key:config.get(key) for key in keys}
    return {'code_sha256':digest.hexdigest(),
            'config_sha256':sha256(json.dumps(policy, sort_keys=True, default=str).encode()).hexdigest(),
            'exchange_epoch_id':config['binana']['testnet_epoch_id']}


def certificate_blockers(certificate, bindings, *, now=None):
    now = time() if now is None else now
    if not isinstance(certificate, dict):
        return ['release_evidence_unavailable']
    blockers = []
    for flag in ['verified', 'controlled_lifecycle_verified', 'restart_verified',
                 'backup_restore_verified', 'critical_suite_10x_verified',
                 'one_slot_soak_verified', 'four_slot_soak_verified']:
        if certificate.get(flag) is not True:
            blockers.append(flag + '_missing')
    try:
        generated = float(certificate['generated_at'])
        expires = float(certificate['valid_until'])
        if not all(math.isfinite(v) for v in [generated, expires]) or not generated <= now < expires:
            raise ValueError('expired evidence')
        if expires - generated > 86400 * 7:
            raise ValueError('evidence TTL exceeds seven days')
    except (ValueError, TypeError, KeyError, OverflowError):
        blockers.append('release_evidence_expired_or_invalid')
    for key, expected in bindings.items():
        if not expected or certificate.get(key) != expected:
            blockers.append(key + '_mismatch')
    return blockers


def _unique_object(pairs):
    result={}
    for key,value in pairs:
        if key in result:
            raise ValueError('duplicate certificate member')
        result[key]=value
    return result


def load_certificate(path, *, authority_path='/freqtrade/binana-config/release-authority.ed25519.pub'):
    """Authenticate issuer bytes before considering any acceptance claims.

    The raw 32-byte public key is supplied through the owner-read-only config
    mount. It is never selected by the certificate itself. The private issuer
    key must remain outside the trading container. Missing/legacy certificates
    fail closed; this verifier does not issue or renew release evidence.
    """
    try:
        envelope=json.loads(Path(path).read_text(),object_pairs_hook=_unique_object)
        if (not isinstance(envelope,dict) or set(envelope)!={'schema','payload','signature'}
                or envelope['schema']!='binana-release-ed25519-v1'
                or not isinstance(envelope['payload'],dict)):
            return None
        import base64
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        signature=base64.b64decode(envelope['signature'],validate=True)
        if len(signature)!=64: return None
        canonical=json.dumps(envelope['payload'],sort_keys=True,separators=(',',':'),allow_nan=False).encode()
        authority=Ed25519PublicKey.from_public_bytes(Path(authority_path).read_bytes())
        try:
            authority.verify(signature,canonical)
        except InvalidSignature:
            return None
        return envelope['payload']
    except (OSError, ValueError, TypeError, ImportError):
        return None
