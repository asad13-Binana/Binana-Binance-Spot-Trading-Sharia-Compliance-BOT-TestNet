"""AWS-only stopped rollout with backups, rollback rehearsal and immutable code.

No certificate is issued and no trading is started by this command.
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
HOST=Path('/home/ubuntu/binana-runtime-fixes')
SHARED=Path('/var/lib/binana-testnet/shared')
DB=SHARED/'freqtrade'
CONFIG=Path('/home/ubuntu/binana-repair-20260912T130255Z/release-config/config.json')
OWNER='binana-testnet-freqtrade-1'
RELEASE=Path('/etc/binana-testnet/release')
ISSUER=Path('/etc/binana-testnet/release-authority/issuer.ed25519')
OVERLAY=HOST/'compose.running-images-20261007.json'
IMAGES=HOST/'local-images-20261007.json'


def run(args,timeout=180):
    result=subprocess.run(args,capture_output=True,timeout=timeout)
    if result.returncode:
        # Keep diagnostics root-private; exception/public receipts contain only
        # executable and exit status, never environment or raw command output.
        diagnostic=Path('/var/backups/binana-testnet')/('rollout-command-failure-'+str(time.time_ns())+'.json')
        if os.geteuid()==0 and diagnostic.parent.exists():
            authority.atomic(diagnostic,json.dumps({'command':args,'exit_code':result.returncode,
                'stdout':result.stdout.decode(errors='replace'),'stderr':result.stderr.decode(errors='replace')}).encode(),mode=0o600)
        raise RuntimeError('COMMAND_FAILED:'+args[0]+':'+str(result.returncode))
    return result.stdout


def inspect(name=OWNER): return json.loads(run(['docker','inspect',name]))[0]


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


def compose():
    result=['docker','compose','-p','binana-testnet','--env-file','/etc/binana-testnet/.env']
    for name in ('/opt/binana-testnet/current/docker-compose.yml',str(HOST/'compose.runtime-fixes.yml'),
                 str(HOST/'compose.freqtrade-owner.yml'),str(HOST/'compose.sharia-v193.yml'),
                 str(OVERLAY),str(HOST/'compose.market-recovery-20261007.json')):
        result+=['-f',name]
    return result


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
    p=argparse.ArgumentParser();p.add_argument('--image',required=True);p.add_argument('--tag',required=True)
    p.add_argument('--evidence',type=Path,required=True);args=p.parse_args()
    os.umask(0o077);os.environ['RELEASE_TAG']='v101-4dd60e295d145e62'
    if inspect()['State']['Running']:raise RuntimeError('MAINTENANCE_STOP_REQUIRED')
    old=inspect()['Image']
    if json.loads(run(['docker','image','inspect',args.tag]))[0]['Id']!=args.image:raise RuntimeError('CANDIDATE_IMAGE_MISMATCH')
    for name in ('lifecycle','one_slot','four_slot','restore','offline'):
        receipt=json.loads((args.evidence/(name+'.json')).read_text())
        if receipt.get('image')!=args.image or receipt.get('passed') is not True:
            raise RuntimeError('ACCEPTANCE_REQUIRED:'+name)
        if name in {'lifecycle','one_slot','four_slot'} and (receipt.get('source_preserved') is not True
                or receipt.get('open_exchange_orders')!=0 or receipt.get('open_exchange_lists')!=0
                or receipt.get('source_config_sha256')!=hashlib.sha256(CONFIG.read_bytes()).hexdigest()):
            raise RuntimeError('ACCEPTANCE_SCOPE_CHANGED:'+name)
    verify_flat_testnet()
    root=Path('/var/backups/binana-testnet')/('completion-rollout-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    root.mkdir(mode=0o700);backup(root)
    originals={path:path.read_bytes() for path in (OVERLAY,IMAGES)}
    for path,raw in originals.items():(root/path.name).write_bytes(raw)
    (root/'private-config.json').write_bytes(CONFIG.read_bytes())
    for path in (RELEASE,ISSUER.parent):path.mkdir(parents=True,exist_ok=True)
    RELEASE.chmod(0o755);ISSUER.parent.chmod(0o700)
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    if not ISSUER.exists():
        key=Ed25519PrivateKey.generate()
        authority.atomic(ISSUER,key.private_bytes(serialization.Encoding.Raw,serialization.PrivateFormat.Raw,serialization.NoEncryption()),mode=0o600)
    key=Ed25519PrivateKey.from_private_bytes(ISSUER.read_bytes())
    authority.atomic(CONFIG.parent/'release-authority.ed25519.pub',key.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw))
    overlay=json.loads(originals[OVERLAY]);owner=overlay['services']['freqtrade']
    # The base Compose already enforces no-new-privileges. Repeating it in
    # this JSON overlay produces a normalized duplicate rejected by Compose.
    owner.pop('security_opt',None)
    owner.update(image=args.tag,read_only=True,init=True,cap_drop=['ALL'],
        volumes=[str(SHARED)+':/freqtrade/shared:ro',str(DB)+':/freqtrade/shared/freqtrade:rw',str(RELEASE)+':/freqtrade/binana-release:ro'])
    images=json.loads(originals[IMAGES]);images['freqtrade']={'reference':args.tag,'image_id':args.image}
    report={'image':args.image,'rollback_image':old,'root':str(root),'passed':False,'stages':[],
        'source_config_sha256':hashlib.sha256(CONFIG.read_bytes()).hexdigest(),'read_only':True,'init':True,
        'rollback_verified':False,'accounting_preserved':False,'trading_resumed':False}
    try:
        for label,candidate in [('candidate',True),('rollback',False),('candidate_final',True)]:
            for path,value in ((OVERLAY,overlay),(IMAGES,images)):
                authority.atomic(path,(json.dumps(value,indent=2)+'\n').encode() if candidate else originals[path])
            run(compose()+['config','--quiet'])
            run(compose()+['up','--no-start','--no-deps','--no-build','--pull','never','freqtrade'])
            if candidate:attest(args.image)
            run(['docker','start',OWNER])
            result=wait(args.image if candidate else old,candidate)
            result['accounting_changes']=preserve_accounting(root)
            report['stages'].append({'stage':label,**result})
            authority.atomic(root/'receipt.json',(json.dumps(report,indent=2)+'\n').encode(),mode=0o600)
            if label!='candidate_final':run(['docker','stop','--timeout','30',OWNER],timeout=45)
        report.update(passed=True,rollback_verified=True,accounting_preserved=True)
    except BaseException as exc:
        report['error']=str(exc) if isinstance(exc,RuntimeError) else type(exc).__name__
        try:
            run(['docker','stop','--timeout','30',OWNER],timeout=45)
            failed=root/'failed-state';failed.mkdir(mode=0o700,exist_ok=True);backup(failed)
            for name in ('binana-owner.sqlite','binana-extension.sqlite'):
                with sqlite3.connect(root/name) as src,sqlite3.connect(DB/name) as dst:src.backup(dst)
            for path,raw in originals.items():authority.atomic(path,raw)
            run(compose()+['up','-d','--no-deps','--no-build','--pull','never','freqtrade'])
            report['rollback_after_error']=wait(old,False)
        except Exception as rollback:report['rollback_error']=type(rollback).__name__
    finally:
        authority.atomic(root/'receipt.json',(json.dumps(report,indent=2)+'\n').encode(),mode=0o600)
        print(json.dumps({k:v for k,v in report.items() if k!='stages'}))
    return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
