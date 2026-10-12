"""Host-only evidence validation and atomic Ed25519 release issuance.

This module cannot submit orders or mark unexecuted checks as passed. The issuer
key must be outside every runtime mount. Inputs are private host test receipts.
"""
from hashlib import sha256
import base64
import json
import os
from pathlib import Path
import re
import tempfile
import time

BINDINGS=('code_sha256','config_sha256','exchange_epoch_id','account_key_sha256',
          'deployment_sha256','dependency_sha256')
FLAGS=('verified','controlled_lifecycle_verified','restart_verified','backup_restore_verified',
       'critical_suite_10x_verified','one_slot_soak_verified','four_slot_soak_verified')


def validate_receipts(image,receipts):
    expected={'lifecycle','one_slot','four_slot','restore','offline','rollout'}
    if set(receipts)!=expected: raise ValueError('ACCEPTANCE_RECEIPTS_INCOMPLETE')
    for name,receipt in receipts.items():
        if receipt.get('image')!=image or receipt.get('passed') is not True:
            raise ValueError('RECEIPT_FAILED_OR_WRONG_IMAGE:'+name)
    for name,slots in [('lifecycle',None),('one_slot',1),('four_slot',4)]:
        receipt=receipts[name]
        phases=['entry','restart','exit','closed_restart'] if slots else ['entry','restart','restart_again','exit','closed_restart']
        if ([p.get('phase') for p in receipt.get('phases',[])]!=phases
                or any(p.get('passed') is not True for p in receipt['phases'])
                or receipt.get('slot_soak')!=slots
                or receipt.get('live_writable_mounts') is not False
                or receipt.get('source_preserved') is not True
                or (slots and receipt.get('soak_seconds_observed',0)<600)
                or receipt.get('open_exchange_orders')!=0 or receipt.get('open_exchange_lists')!=0):
            raise ValueError('LIFECYCLE_RECEIPT_INCOMPLETE:'+name)
    runs=receipts['offline'].get('runs',[])
    if len(runs)!=10 or any(r.get('successful') is not True or r.get('skipped')!=[]
                           or r.get('tests',0)<212 for r in runs):
        raise ValueError('TEN_COMPLETE_OFFLINE_RUNS_REQUIRED')
    if receipts['restore'].get('logical_equality') is not True:
        raise ValueError('RESTORE_IDENTITY_REQUIRED')
    rollout=receipts['rollout']
    if any(rollout.get(k) is not True for k in ('read_only','init','rollback_verified','accounting_preserved')):
        raise ValueError('ROLLOUT_INCOMPLETE')
    config={receipts[n].get('source_config_sha256') for n in ('lifecycle','one_slot','four_slot','rollout')}
    if len(config)!=1 or not re.fullmatch('[0-9a-f]{64}',next(iter(config)) or ''):
        raise ValueError('ACCEPTANCE_CONFIG_MISMATCH')


def build_payload(status,image,receipts,*,now=None):
    now=time.time() if now is None else now
    validate_receipts(image,receipts)
    for key in BINDINGS:
        if not isinstance(status.get(key),str) or not status[key]:
            raise ValueError('BINDING_MISSING:'+key)
        if key.endswith('sha256') and not re.fullmatch('[0-9a-f]{64}',status[key]):
            raise ValueError('BINDING_INVALID:'+key)
    allowed={'release_evidence_unavailable','release_evidence_expired_or_invalid'} | {k+'_mismatch' for k in BINDINGS}
    if set(status.get('blockers',[]))-allowed:
        raise ValueError('RUNTIME_NOT_READY')
    if not 0<=now-status.get('generated_at',0)<30:
        raise ValueError('RUNTIME_EVIDENCE_STALE')
    for name in ('authenticated_reconciliation','retained_inventory'):
        row=status.get(name,{})
        if row.get('ok') is not True or not 0<=now-row.get('checked_at',0)<35:
            raise ValueError('RECONCILIATION_NOT_FRESH')
    return {**{k:status[k] for k in BINDINGS},**{k:True for k in FLAGS},'image':image,
        'generated_at':now,'valid_until':now+7*86400,
        'acceptance_sha256':{name:sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()
                             for name,value in receipts.items()},
        'scope':'Binance Spot Testnet; existing strategy and allocation',
        'limits':['No strategy profitability claim.','Telegram delivery is at least once, not exactly once.']}


def atomic(path,raw,*,mode=0o644):
    path=Path(path)
    if path.is_symlink() or path.parent.is_symlink(): raise ValueError('SYMLINK_DESTINATION')
    fd,name=tempfile.mkstemp(prefix=path.name+'.',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as stream:
            os.fchmod(stream.fileno(),mode);stream.write(raw);stream.flush();os.fsync(stream.fileno())
        os.replace(name,path)
        directory=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


def sign(payload,private_key):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key=Ed25519PrivateKey.from_private_bytes(Path(private_key).read_bytes())
    canonical=json.dumps(payload,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
    return (json.dumps({'schema':'binana-release-ed25519-v1','payload':payload,
        'signature':base64.b64encode(key.sign(canonical)).decode()},sort_keys=True)+'\n').encode()
