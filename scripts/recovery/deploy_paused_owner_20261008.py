"""Pinned, paused Testnet owner rollout with a real rollback rehearsal.

No certificate is created, no incident is cleared, and no /start is called.
"""
import datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time

HOST = Path('/home/ubuntu/binana-runtime-fixes')
SHARED = Path('/var/lib/binana-testnet/shared')
DBROOT = SHARED/'freqtrade'
OWNER = 'binana-testnet-freqtrade-1'
IMAGE = 'sha256:a90b7f661500f7d00b3da32ce4c22d987d41d6da01483affe849a0baccddb523'
OLD_IMAGE = 'sha256:87c25dfc5b3058891491b6fd0647c86fe9a71f96d8ba41a2693b42797fcada83'
TAG = 'binana-testnet-freqtrade:recovery-validation-20261007-r4'
CONFIG = Path('/home/ubuntu/binana-repair-20260912T130255Z/release-config/config.json')
CONFIG_SHA = 'eabd0e6b44b2fe9d74dd054e3a22077c96ce4bc37c5fefc2016c9d194b002123'
INCIDENT = 'repair-economic-settlement-replay-20260920'
BACKUPS = Path('/var/backups/binana-testnet')
FILES = {
    HOST/'compose.running-images-20261007.json':'9d209a4d5f8d3b473139be7691822c3c80472a11a523e3d1069e06ed3e09f97f',
    HOST/'local-images-20261007.json':'3f5d00a4ff31b9423ccd3fa32d7642436bebc6374f66c04bd29a507ad6be4a9f',
}
EXPECTED_SCHEMA = {"unique_intent_trade":{"type":"index","sql_sha256":"80b23fd1c3f6b8d08c11c47a17906c9e99a785dc4756d9e7648a7d74f0b856c0"},"immutable_intent_trade":{"type":"trigger","sql_sha256":"28d664fe49f716de55f1c61723cf255ab60b89d49a3ba3bfe7350c9e4f0954a4"},"immutable_protection_identity":{"type":"trigger","sql_sha256":"54706a907caec910c286f2f9ecb4bf03adf27a3e6ff6eeb37dfb8d6bb4450e0f"},"immutable_order_identity":{"type":"trigger","sql_sha256":"793ff2df092f5043357a3d63dd1ee26ab8d7dd120ed0065b8a2e376b5e0db6a4"}}
OTHERS = ('universe','telegram-broker','execution-sidecar','sharia-screener','sharia-research','sharia-egress-proxy')


def run(args, timeout=180):
    result = subprocess.run(args, capture_output=True, timeout=timeout)
    if result.returncode:
        # Never expose Compose interpolation or private environment output.
        raise RuntimeError('COMMAND_FAILED:'+args[0]+':'+str(result.returncode))
    return result.stdout


def inspect(name=OWNER):
    return json.loads(run(['docker','inspect',name]))[0]


def identity(name):
    d = inspect(name)
    return {k:d[k] for k in ['Id','Image']} | {'StartedAt':d['State']['StartedAt']}


def stop_owner():
    names=run(['docker','container','ls','-a','--filter','name=^/'+OWNER+'$','--format','{{.Names}}']).decode().splitlines()
    if not names:
        return
    if names!=[OWNER] or inspect()['Image'] not in {IMAGE,OLD_IMAGE}:
        raise RuntimeError('UNEXPECTED_OWNER_DURING_STOP')
    run(['docker','stop','--time','30',OWNER],timeout=45)
    if inspect()['State'].get('Running') is not False:
        raise RuntimeError('OWNER_STOP_NOT_CONFIRMED')


def api_state():
    code = '''import os,json,base64,urllib.request
v=os.environ
h=base64.b64encode((v['FREQTRADE__API_SERVER__USERNAME']+':'+v['FREQTRADE__API_SERVER__PASSWORD']).encode()).decode()
r=urllib.request.Request('http://127.0.0.1:8080/api/v1/show_config',headers={'Authorization':'Basic '+h})
x=json.load(urllib.request.urlopen(r,timeout=5))
print(json.dumps({k:x.get(k) for k in ['state','dry_run','trading_mode']}))
'''
    return json.loads(run(['docker','exec',OWNER,'python','-c',code],timeout=15))


def logical(path):
    with sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True) as db:
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise RuntimeError('DATABASE_INTEGRITY_FAILED')
        return hashlib.sha256('\n'.join(db.iterdump()).encode()).hexdigest()


def db_hashes():
    return {n:logical(DBROOT/n) for n in ['binana-owner.sqlite','binana-extension.sqlite']}


def preserved_databases(root, expected, started_at):
    """Permit only Freqtrade's observed startup_time bookkeeping field to advance."""
    bookkeeping={}
    for name,digest in expected.items():
        if logical(DBROOT/name)==digest:
            continue
        if name=='binana-extension.sqlite':
            with sqlite3.connect('file:'+str(DBROOT/name)+'?mode=ro',uri=True) as src,sqlite3.connect(':memory:') as clone:
                src.backup(clone)
                for object_name,spec in EXPECTED_SCHEMA.items():
                    row=clone.execute('SELECT type,sql FROM sqlite_master WHERE name=?',(object_name,)).fetchone()
                    if not row or row[0]!=spec['type'] or hashlib.sha256(row[1].encode()).hexdigest()!=spec['sql_sha256']:
                        raise RuntimeError('IDENTITY_SCHEMA_MIGRATION_MISMATCH')
                    clone.execute('DROP '+spec['type'].upper()+' "'+object_name+'"')
                normalized=hashlib.sha256('\n'.join(clone.iterdump()).encode()).hexdigest()
                if normalized!=digest:
                    raise RuntimeError('EXTENSION_CONTENT_CHANGED_BEYOND_IDENTITY_CONSTRAINTS')
                bookkeeping[name]={'added_identity_constraints':EXPECTED_SCHEMA}
            continue
        if name!='binana-owner.sqlite':
            raise RuntimeError('UNEXPECTED_DATABASE')
        with sqlite3.connect('file:'+str(root/name)+'?mode=ro',uri=True) as old, sqlite3.connect('file:'+str(DBROOT/name)+'?mode=ro',uri=True) as src, sqlite3.connect(':memory:') as clone:
            src.backup(clone)
            before=old.execute("SELECT * FROM KeyValueStore WHERE key='startup_time'").fetchall()
            after=clone.execute("SELECT * FROM KeyValueStore WHERE key='startup_time'").fetchall()
            columns=[r[1] for r in clone.execute('PRAGMA table_info(KeyValueStore)')]
            index=columns.index('datetime_value')
            if len(before)!=1 or len(after)!=1 or any(a!=b for i,(a,b) in enumerate(zip(before[0],after[0])) if i!=index):
                raise RuntimeError('STARTUP_METADATA_SHAPE_CHANGED')
            observed=datetime.datetime.fromisoformat(after[0][index]).replace(tzinfo=datetime.timezone.utc).timestamp()
            if not started_at<=observed<=time.time()+5:
                raise RuntimeError('STARTUP_METADATA_TIME_INVALID')
            clone.execute("UPDATE KeyValueStore SET datetime_value=? WHERE key='startup_time'",(before[0][index],));clone.commit()
            normalized=hashlib.sha256('\n'.join(clone.iterdump()).encode()).hexdigest()
            if normalized!=digest:
                raise RuntimeError('CANONICAL_CONTENT_CHANGED_BEYOND_STARTUP_TIME')
            bookkeeping[name]={'key':'startup_time','before':before[0][index],'after':after[0][index]}
    return bookkeeping


def obligations():
    with sqlite3.connect('file:'+str(DBROOT/'binana-owner.sqlite')+'?mode=ro',uri=True) as db:
        if db.execute('SELECT count(*) FROM trades WHERE is_open=1').fetchone()[0]:
            raise RuntimeError('OPEN_CANONICAL_POSITIONS')
    with sqlite3.connect('file:'+str(DBROOT/'binana-extension.sqlite')+'?mode=ro',uri=True) as db:
        rows = db.execute("SELECT incident_id,code FROM incidents WHERE status='OPEN' ORDER BY incident_id").fetchall()
        if rows != [(INCIDENT,'ECONOMIC_SETTLEMENT_REPLAY_REQUIRED')]:
            raise RuntimeError('INCIDENT_SET_CHANGED')


def atomic(path, raw, mode=0o644):
    if path.is_symlink():
        raise RuntimeError('SYMLINK_TARGET')
    fd,tmp = tempfile.mkstemp(prefix=path.name+'.',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as f:
            f.write(raw); f.flush(); os.fchmod(f.fileno(),mode); os.fsync(f.fileno())
        Path(tmp).replace(path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def compose():
    command = ['docker','compose','-p','binana-testnet','--env-file','/etc/binana-testnet/.env']
    for name in ['/opt/binana-testnet/current/docker-compose.yml',str(HOST/'compose.runtime-fixes.yml'),
                 str(HOST/'compose.freqtrade-owner.yml'),str(HOST/'compose.sharia-v193.yml'),
                 str(HOST/'compose.running-images-20261007.json'),str(HOST/'compose.market-recovery-20261007.json')]:
        command += ['-f',name]
    return command


def acceptance():
    checked = {}
    for relative in ['controlled-owner-20261007T141715Z/receipt.json',
                     'controlled-owner-20261007T141715Z/backup-restore/receipt.json',
                     'controlled-owner-20261008T124602Z/receipt.json',
                     'controlled-owner-20261008T125807Z/receipt.json']:
        path=BACKUPS/relative; d=json.loads(path.read_text())
        if d.get('passed') is not True or d.get('image') != IMAGE:
            raise RuntimeError('ACCEPTANCE_EVIDENCE_FAILED:'+relative)
        if d.get('slot_soak'):
            root=path.parent
            entry=json.loads((root/'data/entry.json').read_text())
            closed=json.loads((root/'data/closed_restart.json').read_text())
            if (entry.get('soak_seconds_observed',0)<600 or closed.get('passed') is not True
                    or closed.get('open_exchange_orders') or closed.get('open_exchange_lists')
                    or closed.get('account_delta_matches_receipts') is not True):
                raise RuntimeError('SOAK_EVIDENCE_INCOMPLETE')
        checked[relative]=hashlib.sha256(path.read_bytes()).hexdigest()
    critical=BACKUPS/'owner-r4-10x-20261007/report.json'; d=json.loads(critical.read_text())
    if (d.get('image')!=IMAGE or d.get('all_passed') is not True or len(d.get('results',[]))!=10
            or any(r.get('tests')!=164 or r.get('exit_code')!=0 or r.get('successful') is not True for r in d['results'])):
        raise RuntimeError('CRITICAL_SUITE_EVIDENCE_INCOMPLETE')
    checked[str(critical.relative_to(BACKUPS))]=hashlib.sha256(critical.read_bytes()).hexdigest()
    return checked


def wait_owner(image, *, candidate):
    deadline=time.monotonic()+180
    last='unavailable'
    while time.monotonic()<deadline:
        try:
            d=inspect()
            if d['Image']!=image:
                raise RuntimeError('OWNER_IMAGE_MISMATCH')
            if d['State'].get('Health',{}).get('Status') != 'healthy':
                raise ValueError('health_pending')
            state=api_state()
            if state.get('state') not in {'stopped','paused'} or state.get('dry_run') is not False or state.get('trading_mode')!='spot':
                raise RuntimeError('OWNER_NOT_STOPPED_TESTNET')
            if d['State'].get('Health',{}).get('Status') != 'healthy':
                raise ValueError('health_pending')
            after=inspect()
            if (after['Id'],after['State']['StartedAt']) != (d['Id'],d['State']['StartedAt']):
                raise RuntimeError('OWNER_CHANGED_DURING_PROBE')
            if not candidate:
                return {'state':state,'image':image}
            status=json.loads((DBROOT/'runtime/owner_readiness.json').read_text())
            allowed={'verified_missing','controlled_lifecycle_verified_missing','restart_verified_missing',
                'backup_restore_verified_missing','critical_suite_10x_verified_missing','one_slot_soak_verified_missing',
                'four_slot_soak_verified_missing','release_evidence_expired_or_invalid','release_evidence_unavailable',
                'code_sha256_mismatch','config_sha256_mismatch','exchange_epoch_id_mismatch','account_key_sha256_mismatch',
                'RECONCILIATION_BLOCKER_OPEN'}
            booted=datetime.datetime.fromisoformat(d['State']['StartedAt'].replace('Z','+00:00')).timestamp()
            now=time.time()
            components=[status['authenticated_reconciliation'],status['retained_inventory']]
            if any(c.get('ok') is not True or not 0<=now-c.get('checked_at',0)<35 for c in components):
                raise ValueError('component_freshness_pending')
            if (not booted<=status['generated_at']<=time.time() or not 0<=time.time()-status['generated_at']<30
                    or status['authenticated_reconciliation'].get('checked_at',0)<booted
                    or status['retained_inventory'].get('checked_at',0)<booted
                    or status.get('execution_environment')!='TESTNET'
                    or status.get('release_approved') is not False or set(status.get('blockers',[]))-allowed
                    or 'RECONCILIATION_BLOCKER_OPEN' not in status.get('blockers',[])):
                raise ValueError('readiness_pending')
            if status['authenticated_reconciliation'].get('open_orders') or status['authenticated_reconciliation'].get('open_lists'):
                raise RuntimeError('OPEN_EXCHANGE_OBLIGATIONS')
            if status['retained_inventory'].get('executable_pairs'):
                raise RuntimeError('RETAINED_AGGREGATE_EXECUTABLE')
            after=inspect()
            if (after['Id'],after['State']['StartedAt']) != (d['Id'],d['State']['StartedAt']):
                raise RuntimeError('OWNER_CHANGED_DURING_PROBE')
            return {'state':state,'image':image,'readiness':status}
        except RuntimeError as exc:
            if not str(exc).startswith('COMMAND_FAILED:docker:'):
                raise
            last='api_or_container_probe_pending'
        except Exception as exc:
            last=type(exc).__name__
        time.sleep(3)
    raise RuntimeError('OWNER_STARTUP_NOT_VERIFIED:'+last)


def main():
    raise SystemExit(64)  # Retired AWS wrapper; retained helpers are regression inputs only.


if __name__=='__main__':
    raise SystemExit(main())
