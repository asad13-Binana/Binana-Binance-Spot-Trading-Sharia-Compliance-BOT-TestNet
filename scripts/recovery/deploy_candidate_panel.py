"""Deploy the reviewed panel/outbox while the order owner is stopped."""
import hashlib
import json
import os
from pathlib import Path
import time
import deploy_candidate_owner as deploy


def main():
    os.umask(0o077);os.environ['RELEASE_TAG']='v101-4dd60e295d145e62'
    owner=deploy.inspect()
    if owner['State']['Running'] and deploy.api_state()['state'] not in {'stopped','paused'}:
        raise RuntimeError('OWNER_MUST_BE_STOPPED')
    source=deploy.REPO/'runtime_owner/runtime-fixes/telegram'
    target=deploy.HOST/'telegram';name='binana-testnet-telegram-broker-1'
    root=Path('/var/backups/binana-testnet')/('panel-completion-'+str(int(time.time())))
    root.mkdir(mode=0o700)
    old=deploy.OVERLAY.read_bytes();originals={}
    for filename in ('runtime_stable_panel.py','durable_outbox.py'):
        path=target/filename
        if path.is_symlink():raise RuntimeError('PANEL_SYMLINK')
        originals[path]=path.read_bytes() if path.exists() else None
        if originals[path] is not None:(root/filename).write_bytes(originals[path])
        compile((source/filename).read_bytes(),filename,'exec')
    (root/deploy.OVERLAY.name).write_bytes(old)
    overlay=json.loads(old);service=overlay['services']['telegram-broker']
    service.update(read_only=True,init=True)
    environment=service.setdefault('environment',{})
    if not isinstance(environment,dict):raise RuntimeError('ENVIRONMENT_MAPPING_REQUIRED')
    environment['PYTHONDONTWRITEBYTECODE']='1'
    volumes=service.setdefault('volumes',[])
    for mount in (str(target/'durable_outbox.py')+':/app/durable_outbox.py:ro',str(deploy.RELEASE)+':/app/binana-release:ro'):
        if mount not in volumes:volumes.append(mount)
    deploy.RELEASE.mkdir(parents=True,exist_ok=True);deploy.RELEASE.chmod(0o755)
    report={'passed':False,'root':str(root),'files':{n:hashlib.sha256((source/n).read_bytes()).hexdigest() for n in ('runtime_stable_panel.py','durable_outbox.py')}}
    def healthy():
        deadline=time.monotonic()+150
        while time.monotonic()<deadline:
            d=deploy.inspect(name)
            if d['State'].get('Health',{}).get('Status')=='healthy':return d
            time.sleep(3)
        raise RuntimeError('PANEL_HEALTH_FAILED')
    try:
        for path in originals:deploy.authority.atomic(path,(source/path.name).read_bytes())
        deploy.authority.atomic(deploy.OVERLAY,(json.dumps(overlay,indent=2)+'\n').encode())
        deploy.run(deploy.compose()+['up','-d','--force-recreate','--no-deps','--no-build','--pull','never','telegram-broker'])
        d=healthy()
        if not d['HostConfig']['ReadonlyRootfs'] or not d['HostConfig']['Init']:raise RuntimeError('PANEL_HARDENING_FAILED')
        for filename,expected in report['files'].items():
            actual=deploy.run(['docker','exec',name,'python','-c',"import hashlib;print(hashlib.sha256(open('/app/"+filename+"','rb').read()).hexdigest())"]).decode().strip()
            if actual!=expected:raise RuntimeError('PANEL_SOURCE_MISMATCH')
        report.update(passed=True,image=d['Image'],read_only=True,init=True)
    except Exception as exc:
        report['error']=str(exc) if isinstance(exc,RuntimeError) else type(exc).__name__
        for path,raw in originals.items():
            if raw is not None:deploy.authority.atomic(path,raw)
        deploy.authority.atomic(deploy.OVERLAY,old)
        deploy.run(deploy.compose()+['up','-d','--force-recreate','--no-deps','--no-build','--pull','never','telegram-broker'])
        healthy();report['rollback_healthy']=True
    deploy.authority.atomic(root/'receipt.json',(json.dumps(report,indent=2)+'\n').encode(),mode=0o600)
    print(json.dumps(report));return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
