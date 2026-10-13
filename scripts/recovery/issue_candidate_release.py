"""Issue/renew from retained exact-image acceptance and fresh stopped readiness.

One atomic certificate is the activation point. Never calls /start or changes
strategy, reconciliation incidents, test receipts or acceptance flags in place.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import time
import base64
import subprocess
import deploy_candidate_owner as deploy


def previous_authority(raw,status,image):
    """Renewal cannot approve different code, policy, account or deployment."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    envelope=json.loads(raw)
    if envelope.get('schema')!='binana-release-ed25519-v1':raise RuntimeError('SIGNED_RELEASE_REQUIRED')
    payload=envelope['payload']
    canonical=json.dumps(payload,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
    public=Ed25519PublicKey.from_public_bytes((deploy.CONFIG.parent/'release-authority.ed25519.pub').read_bytes())
    public.verify(base64.b64decode(envelope['signature'],validate=True),canonical)
    if payload.get('image')!=image or any(payload.get(k)!=status.get(k) for k in deploy.authority.BINDINGS):
        raise RuntimeError('RENEWAL_BINDING_DRIFT')
    if any(payload.get(k) is not True for k in deploy.authority.FLAGS):raise RuntimeError('PREVIOUS_ACCEPTANCE_INCOMPLETE')
    return payload


def refresh_offline(evidence,image):
    root=evidence/('offline-renewal-'+str(int(time.time())));root.mkdir(mode=0o700)
    report={'image':image,'passed':False,'runs':[]}
    for number in range(1,11):
        result=subprocess.run(['docker','run','--rm','--network','none','--read-only','--cap-drop=ALL',
            '--security-opt=no-new-privileges','--memory=1g','--cpus=0.75','--pids-limit=256',
            '--tmpfs','/tmp:rw,nosuid,nodev,size=128m,mode=1777','--entrypoint','python',image,
            '/freqtrade/recovery_validation/validate_image.py'],capture_output=True,timeout=180)
        (root/(str(number)+'.stdout')).write_bytes(result.stdout)
        (root/(str(number)+'.stderr')).write_bytes(result.stderr)
        summaries=[json.loads(line) for line in result.stdout.decode().splitlines() if line.startswith('{"tests":')]
        if len(summaries)!=1:raise RuntimeError('OFFLINE_RESULT_UNAVAILABLE')
        row=summaries[0]
        if result.returncode or row.get('successful') is not True or row.get('skipped')!=[] or row.get('tests',0)<218:
            raise RuntimeError('OFFLINE_RENEWAL_FAILED')
        row.update(exit_code=result.returncode,stdout_sha256=hashlib.sha256(result.stdout).hexdigest(),
            stderr_sha256=hashlib.sha256(result.stderr).hexdigest());report['runs'].append(row)
    report.update(passed=True,completed_at=time.time())
    deploy.authority.atomic(root/'receipt.json',(json.dumps(report,indent=2)+'\n').encode(),mode=0o600)
    deploy.authority.atomic(evidence/'offline.json',(json.dumps(report,indent=2)+'\n').encode(),mode=0o600)
    return report


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--evidence',type=Path,required=True)
    parser.add_argument('--rollout',type=Path,required=True)
    parser.add_argument('--renew',action='store_true')
    parser.add_argument('--force-renewal',action='store_true')
    args=parser.parse_args();os.umask(0o077)
    receipts={name:json.loads((args.evidence/(name+'.json')).read_text())
              for name in ('lifecycle','one_slot','four_slot','restore','offline')}
    receipts['rollout']=json.loads(args.rollout.read_text())
    image=receipts['rollout']['image'];container=deploy.inspect()
    if container['Image']!=image:raise RuntimeError('IMAGE_CHANGED')
    if hashlib.sha256(deploy.CONFIG.read_bytes()).hexdigest()!=receipts['rollout']['source_config_sha256']:
        raise RuntimeError('CONFIGURATION_CHANGED')
    record=json.loads((deploy.RELEASE/'deployment.json').read_text())
    actual={k:container['HostConfig'].get(k) for k in ('ReadonlyRootfs','Init','CapDrop')}
    if actual!={'ReadonlyRootfs':True,'Init':True,'CapDrop':['ALL']}:
        raise RuntimeError('IMMUTABLE_RUNTIME_REQUIRED')
    env_sha=hashlib.sha256(json.dumps(sorted(container['Config']['Env'])).encode()).hexdigest()
    mounts=sorted([{k:m[k] for k in ('Type','Source','Destination','RW')} for m in container['Mounts']],key=lambda m:m['Destination'])
    if (record.get('image')!=image or record.get('environment_sha256')!=env_sha
            or record.get('security_opt')!=container['HostConfig'].get('SecurityOpt')
            or record.get('mounts')!=mounts or record.get('command')!=container['Config']['Cmd']
            or record.get('entrypoint')!=container['Config']['Entrypoint']):
        raise RuntimeError('DEPLOYMENT_DRIFT')
    destination=deploy.RELEASE/'certificate.json'
    previous=destination.read_bytes() if destination.exists() else None
    if args.force_renewal and not args.renew:raise RuntimeError('RENEW_REQUIRED')
    if args.renew:
        if previous is None:raise RuntimeError('FIRST_ISSUANCE_REQUIRES_STOPPED_ROLLOUT')
        status=json.loads((deploy.DB/'runtime/owner_readiness.json').read_text())
        old=previous_authority(previous,status,image)
        if old['valid_until']-time.time()>2*86400 and not args.force_renewal:
            print(json.dumps({'renewal_due':False,'valid_until':old['valid_until']}));return 0
        receipts['offline']=refresh_offline(args.evidence,image)
    if not 0<=time.time()-receipts['offline'].get('completed_at',0)<86400:
        raise RuntimeError('FRESH_OFFLINE_EVIDENCE_REQUIRED')
    if args.renew:
        status=json.loads((deploy.DB/'runtime/owner_readiness.json').read_text())
        previous_authority(previous,status,image)
    else:status=deploy.wait(image,True)['readiness']
    for name in ('lifecycle','one_slot','four_slot'):
        if receipts[name].get('account_key_sha256')!=status['account_key_sha256']:
            raise RuntimeError('ACCEPTED_ACCOUNT_KEY_MISMATCH')
    payload=deploy.authority.build_payload(status,image,receipts)
    current=deploy.inspect()
    if (current['Id'],current['State']['StartedAt'])!=(container['Id'],container['State']['StartedAt']):
        raise RuntimeError('OWNER_CHANGED_DURING_ISSUANCE')
    archive=args.evidence/('issuance-'+str(int(time.time())))
    archive.mkdir(mode=0o700,exist_ok=False)
    if previous:deploy.authority.atomic(archive/'previous-certificate.json',previous,mode=0o600)
    signed=deploy.authority.sign(payload,deploy.ISSUER)
    deploy.authority.atomic(destination,signed)
    result={'image':image,'passed':True,'certificate_sha256':hashlib.sha256(signed).hexdigest(),
        'generated_at':payload['generated_at'],'valid_until':payload['valid_until'],
        'bindings':{k:payload[k] for k in deploy.authority.BINDINGS},'trading_resumed':False}
    deploy.authority.atomic(archive/'receipt.json',(json.dumps(result,indent=2)+'\n').encode(),mode=0o600)
    print(json.dumps(result))
    return 0


if __name__=='__main__':raise SystemExit(main())
