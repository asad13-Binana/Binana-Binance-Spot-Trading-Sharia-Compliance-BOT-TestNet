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


def load_certificate(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
