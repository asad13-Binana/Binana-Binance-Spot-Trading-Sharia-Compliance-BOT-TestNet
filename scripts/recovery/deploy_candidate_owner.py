"""Profile-bound helpers for existing Oracle renewal and accounting checks.

The former AWS deployment CLI is retired and always refuses execution.
"""
import argparse
from contextlib import closing
from datetime import datetime,timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time

REPO=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('release_authority',REPO/'runtime_owner/deployment/release_authority.py')
authority=importlib.util.module_from_spec(spec);spec.loader.exec_module(authority)
HOST = None
SHARED = None
DB = None
CONFIG = None
OWNER = None
RELEASE = None
ISSUER = None
OVERLAY = None
IMAGES = None
EVIDENCE_ROOT = None
HOST_CONTEXT=None


def run(args,timeout=180):
    if HOST_CONTEXT is None:
        raise RuntimeError('HOST_PROFILE_REQUIRED')
    result=subprocess.run(args,capture_output=True,timeout=timeout)
    if result.returncode:
        # Keep diagnostics root-private; exception/public receipts contain only
        # executable and exit status, never environment or raw command output.
        diagnostic=EVIDENCE_ROOT/('rollout-command-failure-'+str(time.time_ns())+'.json')
        if os.geteuid()==0 and diagnostic.parent.exists():
            authority.atomic(diagnostic,json.dumps({'command':args,'exit_code':result.returncode,
                'stdout':result.stdout.decode(errors='replace'),'stderr':result.stderr.decode(errors='replace')}).encode(),mode=0o600)
        raise RuntimeError('COMMAND_FAILED:'+args[0]+':'+str(result.returncode))
    return result.stdout


def inspect(name=None):
    if HOST_CONTEXT is None:
        raise RuntimeError('HOST_PROFILE_REQUIRED')
    if name is None and HOST_CONTEXT is not None:
        return HOST_CONTEXT.inspect()
    return json.loads(run(['docker','inspect',name or OWNER]))[0]


def verify_flat_testnet():
    """Fresh signed GETs only; never infer exchange flatness from old receipts."""
    import hmac
    import urllib.parse
    import urllib.request
    config=json.loads(CONFIG.read_text())
    if (config.get('dry_run') is not False or config.get('trading_mode')!='spot'
            or config.get('binana',{}).get('environment')!='testnet'
            or config.get('exchange',{}).get('binana_testnet') is not True):
        raise RuntimeError('SPOT_TESTNET_CONFIGURATION_REQUIRED')
    env=dict(row.split('=',1) for row in inspect()['Config']['Env'])
    key=env['FREQTRADE__EXCHANGE__KEY'];secret=env['FREQTRADE__EXCHANGE__SECRET']
    for endpoint in ('openOrders','openOrderList'):
        query=urllib.parse.urlencode({'timestamp':int(time.time()*1000),'recvWindow':5000})
        signature=hmac.new(secret.encode(),query.encode(),hashlib.sha256).hexdigest()
        request=urllib.request.Request('https://testnet.binance.vision/api/v3/'+endpoint+'?'+query+'&signature='+signature,
            headers={'X-MBX-APIKEY':key},method='GET')
        try:
            with urllib.request.urlopen(request,timeout=15) as response: rows=json.load(response)
        except Exception as exc:raise RuntimeError('TESTNET_FLAT_CHECK_FAILED:'+type(exc).__name__) from None
        if not isinstance(rows,list) or rows:raise RuntimeError('TESTNET_EXCHANGE_NOT_FLAT')
    with sqlite3.connect('file:'+str(DB/'binana-owner.sqlite')+'?mode=ro',uri=True) as connection:
        if connection.execute('SELECT count(*) FROM trades WHERE is_open=1').fetchone()[0]:
            raise RuntimeError('CANONICAL_POSITIONS_NOT_FLAT')




def api_state():
    script="""import os,json,base64,urllib.request
v=os.environ
h=base64.b64encode((v['FREQTRADE__API_SERVER__USERNAME']+':'+v['FREQTRADE__API_SERVER__PASSWORD']).encode()).decode()
r=urllib.request.Request('http://127.0.0.1:8080/api/v1/show_config',headers={'Authorization':'Basic '+h})
d=json.load(urllib.request.urlopen(r,timeout=5))
print(json.dumps({k:d.get(k) for k in ['state','dry_run','trading_mode']}))
"""
    return json.loads(run(['docker','exec',OWNER,'python','-c',script],timeout=15))


def backup(root):
    for name in ('binana-owner.sqlite','binana-extension.sqlite'):
        with sqlite3.connect('file:'+str(DB/name)+'?mode=ro',uri=True) as src,sqlite3.connect(root/name) as dst:
            src.backup(dst)
            if dst.execute('PRAGMA integrity_check').fetchone()[0]!='ok': raise RuntimeError('BACKUP_CORRUPT')


def preserve_accounting(root):
    changes={}
    for name in ('binana-owner.sqlite','binana-extension.sqlite'):
        with closing(sqlite3.connect(root/name)) as before,closing(sqlite3.connect('file:'+str(DB/name)+'?mode=ro',uri=True)) as after:
            tables=[r[0] for r in before.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            for table in tables:
                old=before.execute('SELECT * FROM "'+table+'" ORDER BY rowid').fetchall()
                new=after.execute('SELECT * FROM "'+table+'" ORDER BY rowid').fetchall()
                if old==new: continue
                if table=='KeyValueStore':
                    columns=[c[1] for c in before.execute('PRAGMA table_info(KeyValueStore)')]
                    key=columns.index('key');stamp=columns.index('datetime_value')
                    if len(old)!=len(new): raise RuntimeError('KEY_VALUE_ROWS_CHANGED')
                    for a,b in zip(old,new):
                        if a==b: continue
                        if a[key]!='startup_time' or any(x!=y for i,(x,y) in enumerate(zip(a,b)) if i!=stamp):
                            raise RuntimeError('NON_STARTUP_METADATA_CHANGED')
                elif table=='wallet_history' and len(new)>=len(old) and new[:len(old)]==old:
                    pass  # New telemetry snapshots never change existing trade economics.
                elif table=='incidents' and len(old)==len(new):
                    columns=[c[1] for c in before.execute('PRAGMA table_info(incidents)')]
                    stamp=columns.index('updated_ts');status=columns.index('status')
                    for a,b in zip(old,new):
                        if a==b:continue
                        if (a[status]!='CLOSED' or b[status]!='CLOSED' or b[stamp]<a[stamp]
                                or any(x!=y for i,(x,y) in enumerate(zip(a,b)) if i!=stamp)):
                            raise RuntimeError('INCIDENT_CONTENT_CHANGED')
                else: raise RuntimeError('ACCOUNTING_CHANGED:'+name+':'+table)
                changes[name+':'+table]={'before_rows':len(old),'after_rows':len(new)}
            added={r[0] for r in after.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}-set(tables)
            if added-{'protection_plan_revisions'}: raise RuntimeError('UNEXPECTED_NEW_STATE_TABLE')
    return changes


def attest(image):
    d=inspect();h=d['HostConfig']
    if (d['Image']!=image or not h['ReadonlyRootfs'] or not h.get('Init') or h.get('CapDrop')!=['ALL']
            or not set(h.get('SecurityOpt') or []) & {'no-new-privileges','no-new-privileges:true','no-new-privileges=true'}):
        raise RuntimeError('IMMUTABLE_OWNER_CONTRACT_FAILED')
    mounts=[{k:m[k] for k in ('Type','Source','Destination','RW')} for m in d['Mounts']]
    for target in ('/freqtrade/binana-config','/freqtrade/binana-release','/freqtrade/shared'):
        if not any(m['Destination']==target and m['RW'] is False for m in mounts):
            raise RuntimeError('READONLY_MOUNT_MISSING:'+target)
    record={'schema':'binana-deployment-v1','image':image,'read_only':True,'init':True,'cap_drop':['ALL'],
        'mounts':sorted(mounts,key=lambda m:m['Destination']),
        'environment_sha256':hashlib.sha256(json.dumps(sorted(d['Config']['Env'])).encode()).hexdigest(),
        'entrypoint':d['Config']['Entrypoint'],'command':d['Config']['Cmd'],'user':d['Config']['User'],
        'security_opt':h['SecurityOpt']}
    authority.atomic(RELEASE/'deployment.json',(json.dumps(record,sort_keys=True)+'\n').encode())
    return record


def wait(image,candidate):
    deadline=time.monotonic()+240;last='STARTING'
    while time.monotonic()<deadline:
        try:
            d=inspect()
            if d['Image']!=image: raise RuntimeError('WRONG_RUNNING_IMAGE')
            s=api_state()
            if s.get('state') not in {'paused','stopped'} or s.get('dry_run') is not False or s.get('trading_mode')!='spot':
                raise RuntimeError('OWNER_NOT_STOPPED_SPOT_TESTNET')
            if d['State'].get('Health',{}).get('Status')!='healthy': raise ValueError('HEALTH_PENDING')
            if not candidate:return {'state':s,'image':image}
            status=json.loads((DB/'runtime/owner_readiness.json').read_text())
            boot=datetime.fromisoformat(d['State']['StartedAt'].replace('Z','+00:00')).timestamp()
            allowed={'release_evidence_unavailable','release_evidence_expired_or_invalid'}|{k+'_mismatch' for k in authority.BINDINGS}
            if status.get('generated_at',0)<boot or not 0<=time.time()-status['generated_at']<30:
                raise ValueError('STALE_READINESS')
            if set(status.get('blockers',[]))-allowed: raise ValueError(','.join(status['blockers']))
            for part in ('authenticated_reconciliation','retained_inventory'):
                row=status.get(part,{})
                if row.get('ok') is not True or not 0<=time.time()-row.get('checked_at',0)<35: raise ValueError(part)
            if status['authenticated_reconciliation'].get('open_orders') or status['authenticated_reconciliation'].get('open_lists'):
                raise RuntimeError('EXCHANGE_OBLIGATIONS_NOT_EMPTY')
            if not status.get('deployment_sha256') or not status.get('dependency_sha256'):
                raise ValueError('DEPLOYMENT_BINDING_MISSING')
            return {'state':s,'image':image,'readiness':status}
        except RuntimeError as exc:
            if not str(exc).startswith('COMMAND_FAILED:'):raise
            last=str(exc)
        except (ValueError,OSError,KeyError) as exc:last=str(exc)
        time.sleep(3)
    raise RuntimeError('OWNER_BOOT_NOT_QUALIFIED:'+last)


def main():
    raise SystemExit(64)  # AWS rollout retired; the Oracle bundle is the supported route.


if __name__=='__main__':raise SystemExit(main())
