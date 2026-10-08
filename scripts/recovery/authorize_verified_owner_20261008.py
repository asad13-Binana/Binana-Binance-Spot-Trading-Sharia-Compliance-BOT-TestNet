"""Record completed Testnet acceptance and resolve only its historical incident.

Requires the actual successful paused rollout receipt. Does not call /start.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time

import deploy_paused_owner_20261008 as deploy

BINDINGS=('code_sha256','config_sha256','exchange_epoch_id','account_key_sha256')
FLAGS=('verified','controlled_lifecycle_verified','restart_verified','backup_restore_verified',
       'critical_suite_10x_verified','one_slot_soak_verified','four_slot_soak_verified')


def certificate_from_status(status,evidence,rollout):
    for key in BINDINGS:
        value=status.get(key)
        if not isinstance(value,str) or not value or (key.endswith('sha256') and not re.fullmatch('[0-9a-f]{64}',value)):
            raise RuntimeError('RUNTIME_BINDING_UNAVAILABLE')
    if set(status.get('blockers',[]))!={'release_evidence_unavailable','RECONCILIATION_BLOCKER_OPEN'}:
        raise RuntimeError('UNEXPECTED_RELEASE_BLOCKERS')
    now=time.time()
    return {**{k:status[k] for k in BINDINGS},**{k:True for k in FLAGS},
            'generated_at':now,'valid_until':now+7*86400,'image':deploy.IMAGE,
            'acceptance_receipt_sha256':evidence,'deployment_receipt':str(rollout),
            'scope':'Binance Spot Testnet existing policy only',
            'limits':['Strategy profitability is not certified.',
                      'Retained aggregates remain gated; executable dust consolidation is not implemented.',
                      'Backup/restore acceptance used flat databases; no active-position restore is claimed.']}


def verified_boot(receipt,expected_container=None):
    before=deploy.inspect()
    fresh=deploy.wait_owner(deploy.IMAGE,candidate=True)
    after=deploy.inspect()
    keys=lambda d:(d['Id'],d['Image'],d['State']['StartedAt'])
    if keys(before)!=keys(after) or (expected_container is not None and keys(after)!=keys(expected_container)):
        raise RuntimeError('OWNER_BOOT_CHANGED')
    expected=receipt['stages'][-1]['readiness']
    if any(fresh['readiness'].get(k)!=expected.get(k) for k in BINDINGS):
        raise RuntimeError('ACCEPTED_RUNTIME_BINDINGS_CHANGED')
    return fresh,after


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--rollout',required=True);args=parser.parse_args()
    root=Path(args.rollout).resolve(strict=True)
    if root.parent!=deploy.BACKUPS or not re.fullmatch('owner-rollout-[0-9]{8}T[0-9]{6}Z',root.name):
        raise RuntimeError('UNEXPECTED_ROLLOUT_PATH')
    receipt_path=root/'receipt.json';receipt=json.loads(receipt_path.read_text())
    evidence=deploy.acceptance()
    for relative in ['controlled-owner-20261007T141715Z/receipt.json','controlled-owner-20261008T124602Z/receipt.json','controlled-owner-20261008T125807Z/receipt.json']:
        if json.loads((deploy.BACKUPS/relative).read_text()).get('source_config_sha256')!=deploy.CONFIG_SHA:
            raise RuntimeError('LIFECYCLE_CONFIGURATION_BINDING_CHANGED')
    if (receipt.get('passed') is not True or receipt.get('image')!=deploy.IMAGE
            or receipt.get('acceptance_receipts')!=evidence
            or [r.get('stage') for r in receipt.get('stages',[])]!=['candidate','rollback_rehearsal','candidate_final']
            or any(r.get('accounting_and_extension_unchanged') is not True for r in receipt['stages'])
            or receipt.get('other_six_containers_unchanged') is not True):
        raise RuntimeError('SUCCESSFUL_DEPLOYMENT_RECEIPT_REQUIRED')
    if hashlib.sha256(deploy.CONFIG.read_bytes()).hexdigest()!=deploy.CONFIG_SHA:
        raise RuntimeError('CONFIG_DRIFT')
    fresh,container=verified_boot(receipt)
    deploy.preserved_databases(root,receipt['database_backups'],receipt['started_at'])
    deploy.obligations()
    evidence={**evidence,str(receipt_path.relative_to(deploy.BACKUPS)):hashlib.sha256(receipt_path.read_bytes()).hexdigest()}
    certificate=certificate_from_status(fresh['readiness'],evidence,receipt_path)
    output=root/'release-authorization';output.mkdir(mode=0o700,exist_ok=False)
    paths=[deploy.DBROOT/'runtime/freqtrade_owner_patch_ready.json',deploy.SHARED/'runtime/freqtrade_owner_patch_ready.json']
    if any(p.is_symlink() or p.parent.is_symlink() for p in paths):
        raise RuntimeError('CERTIFICATE_SYMLINK_DENIED')
    originals={p:p.read_bytes() if p.exists() else None for p in paths}
    for index,(p,raw) in enumerate(originals.items()):
        if raw is not None:(output/('marker-'+str(index)+'.before')).write_bytes(raw)
    result={'image':deploy.IMAGE,'passed':False,'trading_resumed':False,'certificate_paths':[str(p) for p in paths]}
    db=sqlite3.connect(deploy.DBROOT/'binana-extension.sqlite',timeout=15)
    db.row_factory=sqlite3.Row
    committed=False
    try:
        db.execute('ATTACH DATABASE ? AS canonical',(str(deploy.DBROOT/'binana-owner.sqlite'),))
        db.execute('BEGIN IMMEDIATE')
        deploy.preserved_databases(root,receipt['database_backups'],receipt['started_at'])
        if db.execute('SELECT count(*) FROM canonical.trades WHERE is_open=1').fetchone()[0]:
            raise RuntimeError('CANONICAL_POSITION_APPEARED')
        incidents=db.execute("SELECT * FROM incidents WHERE status='OPEN' ORDER BY incident_id").fetchall()
        if len(incidents)!=1 or incidents[0]['incident_id']!=deploy.INCIDENT or incidents[0]['code']!='ECONOMIC_SETTLEMENT_REPLAY_REQUIRED':
            raise RuntimeError('INCIDENT_CHANGED')
        old=dict(incidents[0]);(output/'incident.before.json').write_text(json.dumps(old,indent=2))
        state=deploy.api_state()
        if state.get('state') not in {'stopped','paused'} or deploy.inspect()['Id']!=container['Id']:
            raise RuntimeError('OWNER_STATE_CHANGED')
        for name in ['binana-owner.sqlite','binana-extension.sqlite']:
            with sqlite3.connect('file:'+str(deploy.DBROOT/name)+'?mode=ro',uri=True) as src,sqlite3.connect(output/name) as dst:
                src.backup(dst)
            if deploy.logical(output/name)!=deploy.logical(deploy.DBROOT/name):
                raise RuntimeError('AUTHORIZATION_BACKUP_MISMATCH')
        fresh,_=verified_boot(receipt,container)
        certificate=certificate_from_status(fresh['readiness'],evidence,receipt_path)
        raw=(json.dumps(certificate,indent=2)+'\n').encode()
        for p in paths:deploy.atomic(p,raw)
        detail=('2026-10-08: historical fills, retained metadata and MINA identity repairs verified; '
                'candidate a90b7f66 passed protected entry/exit, repeated restarts, flat restore, 164 tests x10, '
                '601-second one/four-slot soaks and paused deployment/rollback. Fresh authenticated readiness '
                'confirms no exchange obligations and no executable retained aggregates. '
                'Retained verification and executable-aggregate blockers remain enforced. Evidence: '+str(receipt_path))
        changed=db.execute("UPDATE incidents SET status='CLOSED',detail=?,updated_ts=? WHERE incident_id=? AND status='OPEN' AND updated_ts=?",
                           (detail,time.time(),deploy.INCIDENT,old['updated_ts']))
        if changed.rowcount!=1:raise RuntimeError('INCIDENT_UPDATE_NOT_EXACT')
        # Check every other extension value against the backup within this same transaction.
        snapshot=sqlite3.connect(':memory:')
        try:
            # Serialize the current transaction without backup() locking against itself.
            snapshot.executescript('\n'.join(db.iterdump()))
            snapshot.execute('UPDATE incidents SET status=?,detail=?,updated_ts=? WHERE incident_id=?',
                             (old['status'],old['detail'],old['updated_ts'],deploy.INCIDENT));snapshot.commit()
            normalized=hashlib.sha256('\n'.join(snapshot.iterdump()).encode()).hexdigest()
            if normalized!=deploy.logical(output/'binana-extension.sqlite'):
                raise RuntimeError('AUTHORIZATION_CHANGED_OTHER_EXTENSION_DATA')
        finally:snapshot.close()
        # Same container boot and stopped state must persist through commit.
        current=deploy.inspect();state=deploy.api_state()
        if ((current['Id'],current['Image'],current['State']['StartedAt']) != (container['Id'],container['Image'],container['State']['StartedAt'])
                or state!={'state':state.get('state'),'dry_run':False,'trading_mode':'spot'}
                or state.get('state') not in {'stopped','paused'}):
            raise RuntimeError('OWNER_CHANGED_BEFORE_COMMIT')
        if time.time()-fresh['readiness']['generated_at']>=30 or any(time.time()-fresh['readiness'][k]['checked_at']>=35 for k in ['authenticated_reconciliation','retained_inventory']):
            raise RuntimeError('READINESS_EXPIRED_BEFORE_COMMIT')
        db.commit();committed=True
        result.update(passed=True,incident_closed=deploy.INCIDENT,certificate_sha256=hashlib.sha256(raw).hexdigest(),
                      valid_until=certificate['valid_until'],bindings={k:certificate[k] for k in BINDINGS})
    except BaseException as exc:
        if not committed:
            db.rollback()
            for p,raw in originals.items():
                if raw is None:p.unlink(missing_ok=True)
                else:deploy.atomic(p,raw)
        result['error_type']=type(exc).__name__
        if isinstance(exc,RuntimeError):result['error']=str(exc)
    finally:
        db.close();(output/'receipt.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))
    return 0 if result['passed'] else 1


if __name__=='__main__':
    os.umask(0o077)
    raise SystemExit(main())
