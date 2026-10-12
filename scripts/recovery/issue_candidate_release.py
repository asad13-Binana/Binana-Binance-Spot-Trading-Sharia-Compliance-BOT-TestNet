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
import deploy_candidate_owner as deploy


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--evidence',type=Path,required=True)
    parser.add_argument('--rollout',type=Path,required=True)
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
            or record.get('mounts')!=mounts or record.get('command')!=container['Config']['Cmd']
            or record.get('entrypoint')!=container['Config']['Entrypoint']):
        raise RuntimeError('DEPLOYMENT_DRIFT')
    if not 0<=time.time()-receipts['offline'].get('completed_at',0)<86400:
        raise RuntimeError('FRESH_OFFLINE_EVIDENCE_REQUIRED')
    fresh=deploy.wait(image,True)
    status=fresh['readiness']
    for name in ('lifecycle','one_slot','four_slot'):
        if receipts[name].get('account_key_sha256')!=status['account_key_sha256']:
            raise RuntimeError('ACCEPTED_ACCOUNT_KEY_MISMATCH')
    payload=deploy.authority.build_payload(status,image,receipts)
    current=deploy.inspect()
    if (current['Id'],current['State']['StartedAt'])!=(container['Id'],container['State']['StartedAt']):
        raise RuntimeError('OWNER_CHANGED_DURING_ISSUANCE')
    destination=deploy.RELEASE/'certificate.json'
    previous=destination.read_bytes() if destination.exists() else None
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
