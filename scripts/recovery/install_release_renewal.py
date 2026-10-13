"""Install root-owned issuer tooling and a daily unchanged-release renewal."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import deploy_candidate_owner as deploy


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--evidence',type=Path,required=True)
    parser.add_argument('--rollout',type=Path,required=True);args=parser.parse_args()
    if os.geteuid()!=0:raise RuntimeError('ROOT_REQUIRED')
    evidence=args.evidence.resolve(strict=True);rollout=args.rollout.resolve(strict=True)
    boundary=Path('/var/backups/binana-testnet')
    if not evidence.is_relative_to(boundary) or not rollout.is_relative_to(boundary):
        raise RuntimeError('PRIVATE_EVIDENCE_BOUNDARY_REQUIRED')
    if any(c in str(evidence)+str(rollout) for c in (' ','\n','\r','%','"')):
        raise RuntimeError('UNEXPECTED_SYSTEMD_PATH')
    if json.loads(rollout.read_text()).get('passed') is not True:raise RuntimeError('SUCCESSFUL_ROLLOUT_REQUIRED')
    destination=Path('/usr/local/lib/binana-release-maintenance')
    files=('scripts/recovery/issue_candidate_release.py','scripts/recovery/deploy_candidate_owner.py',
           'runtime_owner/deployment/release_authority.py')
    hashes={}
    for relative in files:
        target=destination/relative;target.parent.mkdir(mode=0o755,parents=True,exist_ok=True)
        if target.is_symlink():raise RuntimeError('ISSUER_DESTINATION_SYMLINK')
        raw=(deploy.REPO/relative).read_bytes();deploy.authority.atomic(target,raw)
        hashes[relative]=hashlib.sha256(raw).hexdigest()
    service='''[Unit]
Description=Renew unchanged qualified BINANA Spot Testnet release
After=docker.service network-online.target
Requires=docker.service

[Service]
Type=oneshot
User=root
Group=root
UMask=0077
TimeoutStartSec=600
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/etc/binana-testnet/release /var/backups/binana-testnet
ExecStart=/usr/bin/python3 '''+str(destination/'scripts/recovery/issue_candidate_release.py')+' --evidence '+str(evidence)+' --rollout '+str(rollout)+' --renew\n'
    timer='''[Unit]
Description=Check BINANA release renewal daily before expiry

[Timer]
OnCalendar=*-*-* 03:45:00 UTC
RandomizedDelaySec=10m
Persistent=true
Unit=binana-testnet-release-renewal.service

[Install]
WantedBy=timers.target
'''
    for suffix,raw in (('service',service),('timer',timer)):
        deploy.authority.atomic(Path('/etc/systemd/system/binana-testnet-release-renewal.'+suffix),raw.encode())
    for command in (['systemctl','daemon-reload'],['systemctl','start','binana-testnet-release-renewal.service'],
                    ['systemctl','enable','--now','binana-testnet-release-renewal.timer']):
        deploy.run(command,timeout=610)
    report={'passed':True,'source_sha256':hashes,'evidence':str(evidence),'rollout':str(rollout),
        'timer':deploy.run(['systemctl','show','binana-testnet-release-renewal.timer',
                           '--property=ActiveState,NextElapseUSecRealtime']).decode().strip()}
    deploy.authority.atomic(evidence/'renewal-installation.json',(json.dumps(report,indent=2)+'\n').encode(),mode=0o600)
    print(json.dumps(report))


if __name__=='__main__':main()
