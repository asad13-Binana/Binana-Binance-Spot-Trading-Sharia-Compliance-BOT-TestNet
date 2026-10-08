"""Verify restoration of a completed, flat controlled lifecycle database pair."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess

from run_controlled_owner_lifecycle import IMAGE, container_command, database_summary, inspect_owner, owner_state, stop_rehearsal

SOURCE_RUN = Path('/var/backups/binana-testnet/controlled-owner-20261007T141715Z')
PROBE_SHA256 = '8b245edffaad31d5ded38250c23b21b0455bf729703a3d8d7ccbc79ff1fefed9'


def logical(connection):
    return hashlib.sha256('\n'.join(connection.iterdump()).encode()).hexdigest()


def main():
    os.umask(0o077)
    before = inspect_owner()
    state_before, databases_before = owner_state(), database_summary()
    previous = json.loads((SOURCE_RUN/'receipt.json').read_text())
    closed = json.loads((SOURCE_RUN/'data/closed_restart.json').read_text())
    if (previous.get('passed') is not True or closed.get('passed') is not True
            or closed.get('open_exchange_orders') or closed.get('open_exchange_lists')
            or previous['image'] != IMAGE
            or hashlib.sha256((SOURCE_RUN/'data/probe.py').read_bytes()).hexdigest() != PROBE_SHA256):
        raise RuntimeError('COMPLETED_FLAT_REHEARSAL_REQUIRED')
    root = SOURCE_RUN/'backup-restore'
    root.mkdir(mode=0o700, exist_ok=False)
    data, backup = root/'data', root/'backup'
    (data/'shared/freqtrade').mkdir(parents=True)
    backup.mkdir()
    for name in ['userdata','shared/cache','shared/runtime']:
        (data/name).mkdir()
    receipt = {'source_run':str(SOURCE_RUN),'image':IMAGE,'probe_sha256':PROBE_SHA256,
               'passed':False,'databases':{},'read_only_exchange':True}
    for name in ['binana-owner.sqlite','binana-extension.sqlite']:
        source = SOURCE_RUN/'data/shared/freqtrade'/name
        with sqlite3.connect('file:'+str(source)+'?mode=ro',uri=True) as src:
            with sqlite3.connect(backup/name) as target:
                src.backup(target)
                if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise RuntimeError('BACKUP_INTEGRITY_FAILED')
                original_hash, backup_hash = logical(src), logical(target)
        with sqlite3.connect('file:'+str(backup/name)+'?mode=ro',uri=True) as src:
            with sqlite3.connect(data/'shared/freqtrade'/name) as target:
                src.backup(target)
                restored_hash = logical(target)
                if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise RuntimeError('RESTORE_INTEGRITY_FAILED')
        if len({original_hash,backup_hash,restored_hash}) != 1:
            raise RuntimeError('RESTORE_LOGICAL_MISMATCH')
        receipt['databases'][name] = {'source':original_hash,'backup':backup_hash,'restored':restored_hash}
    for name in ['probe.py','config.json','run_id','transport.json','entry.json','exit.json']:
        shutil.copyfile(SOURCE_RUN/'data'/name,data/name)
    (data/'restore_probe.py').write_text('''import runpy,sys
scope=runpy.run_path('/probe/probe.py',run_name='restore_support')
original=scope['TransportGuard'].validate
def reads_only(self,url,method,headers=None,body=None):
 if method != 'GET': raise RuntimeError('RESTORE_MUTATION_DENIED')
 return original(self,url,method,headers,body)
scope['TransportGuard'].validate=reads_only
sys.argv=['/probe/probe.py','closed_restart']
raise SystemExit(scope['main']())
''')
    credentials = root/'credentials.env'
    env = dict(row.split('=',1) for row in before['Config']['Env'] if '=' in row)
    names = ['FREQTRADE__EXCHANGE__KEY','FREQTRADE__EXCHANGE__SECRET']
    if any(not env.get(key) or '\n' in env[key] or '\r' in env[key] for key in names):
        raise RuntimeError('CREDENTIALS_UNAVAILABLE')
    container = 'binana-testnet-restore-20261007'
    try:
        credentials.write_text(''.join(key+'='+env[key]+'\n' for key in names))
        credentials.chmod(0o600)
        for path in [data,*data.rglob('*')]:
            os.chown(path,999,987)
        command = container_command(data,credentials,container)
        command[-1] = '/probe/restore_probe.py'
        run = subprocess.run(command,capture_output=True,text=True,timeout=300)
        # Keep raw output private; only structured report fields are published.
        (root/'stdout.log').write_text(run.stdout)
        (root/'stderr.log').write_text(run.stderr)
        report = json.loads((data/'closed_restart.json').read_text())
        receipt.update(exit_code=run.returncode,restored_owner_report=report,
                       passed=run.returncode == 0 and report.get('passed') is True)
    except Exception as exc:
        receipt['error_type'] = type(exc).__name__
    finally:
        try:
            credentials.unlink(missing_ok=True)
        except Exception as exc:
            receipt['credential_cleanup_error'] = type(exc).__name__
            receipt['passed'] = False
        error = stop_rehearsal(container)
        if error:
            receipt['container_cleanup_error'] = error
            receipt['passed'] = False
        checks = {'source_databases_unchanged':lambda:database_summary() == databases_before,
                  'source_state_unchanged':lambda:owner_state() == state_before,
                  'source_container_unchanged':lambda:inspect_owner()['Id'] == before['Id'],
                  'source_image_unchanged':lambda:inspect_owner()['Image'] == before['Image']}
        for key,check in checks.items():
            try:
                receipt[key] = check()
            except Exception as exc:
                receipt[key] = False
                receipt[key+'_error'] = type(exc).__name__
        receipt['passed'] = receipt['passed'] and all(receipt.get(key) is True for key in checks)
        (root/'receipt.json').write_text(json.dumps(receipt,indent=2,default=str)+'\n')
        print(json.dumps({key:value for key,value in receipt.items() if key != 'restored_owner_report'}))
    return 0 if receipt['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
