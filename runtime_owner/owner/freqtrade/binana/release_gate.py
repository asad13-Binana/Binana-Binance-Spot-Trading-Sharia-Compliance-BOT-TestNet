"""Owner-side release evidence checks; UI booleans cannot authorize trading."""
from hashlib import sha256
import json
import math
import os
from pathlib import Path
from time import time


def _public_policy(value):
    """Hash effective policy, excluding credentials and transient parser state."""
    private={'key','secret','password','apikey','api_key','jwt_secret_key','ws_token','token'}
    if isinstance(value,dict):
        return {str(k):_public_policy(v) for k,v in value.items()
                if str(k).lower() not in private and not str(k).startswith('_')}
    if isinstance(value,(list,tuple)):
        return [_public_policy(v) for v in value]
    return value


def deployment_binding(path=Path('/freqtrade/binana-release/deployment.json')):
    """Bind independently captured Docker identity through a read-only mount."""
    try:
        raw=path.read_bytes(); record=json.loads(raw)
        if (record.get('schema')!='binana-deployment-v1' or record.get('read_only') is not True
                or record.get('init') is not True or record.get('cap_drop')!=['ALL']
                or not str(record.get('image','')).startswith('sha256:')
                or not record.get('mounts') or not record.get('environment_sha256')):
            return ''
        if not os.statvfs(path).f_flag & os.ST_RDONLY:
            return ''
        return sha256(raw).hexdigest()
    except (OSError,ValueError,TypeError,AttributeError):
        return ''


def dependency_binding():
    from importlib.metadata import distributions
    rows=sorted((d.metadata.get('Name','').lower(),d.version,
                 sha256((d.read_text('RECORD') or '').encode()).hexdigest()) for d in distributions())
    return sha256(json.dumps(rows,separators=(',',':')).encode()).hexdigest()


def runtime_bindings(config):
    root = Path(__file__).resolve().parents[1]
    paths = sorted(root.rglob('*.py'))
    paths += sorted((root.parent / 'binana-strategies').rglob('*.py'))
    digest = sha256()
    for path in paths:
        digest.update(str(path.relative_to(root.parent)).encode() + b'\0' + path.read_bytes())
    policy = _public_policy(config)
    return {'code_sha256':digest.hexdigest(),
            'config_sha256':sha256(json.dumps(policy, sort_keys=True, default=str).encode()).hexdigest(),
            'exchange_epoch_id':config['binana']['testnet_epoch_id'],
            'deployment_sha256':deployment_binding(),
            'dependency_sha256':dependency_binding()}


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
