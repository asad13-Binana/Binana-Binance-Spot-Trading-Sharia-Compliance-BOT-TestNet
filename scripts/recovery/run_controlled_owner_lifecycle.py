"""AWS-only runner for one bounded matching-engine Testnet OTOCO rehearsal."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time


IMAGE = None
SOURCE = None
OWNER = None
CONFIG = None
ROOT = None


def inspect_owner():
    return json.loads(subprocess.check_output(['docker', 'inspect', OWNER]))[0]


def owner_state():
    # Execute locally inside the owner; do not serialize its API credentials.
    code = """import os,json,base64,urllib.request
v=os.environ
auth=base64.b64encode((v['FREQTRADE__API_SERVER__USERNAME']+':'+v['FREQTRADE__API_SERVER__PASSWORD']).encode()).decode()
r=urllib.request.Request('http://127.0.0.1:8080/api/v1/show_config',headers={'Authorization':'Basic '+auth})
x=json.load(urllib.request.urlopen(r,timeout=10))
print(json.dumps({k:x.get(k) for k in ['state','dry_run','trading_mode']}))
"""
    state = json.loads(subprocess.check_output(['docker', 'exec', OWNER, 'python', '-c', code], timeout=20))
    if state != {'state': 'paused', 'dry_run': False, 'trading_mode': 'spot'}:
        raise RuntimeError('OWNER_NOT_PAUSED_SPOT_TESTNET')
    return state


def database_summary():
    result = {}
    for name, queries in {
        'binana-owner.sqlite': ['SELECT count(*) FROM trades', 'SELECT count(*) FROM orders',
                               'SELECT count(*) FROM trades WHERE is_open=1'],
        'binana-extension.sqlite': ["SELECT incident_id,code,status FROM incidents WHERE status='OPEN' ORDER BY incident_id"]
    }.items():
        with sqlite3.connect('file:'+str(SOURCE/'freqtrade'/name)+'?mode=ro', uri=True) as connection:
            result[name] = [connection.execute(query).fetchall() for query in queries]
            result[name+'_logical_sha256'] = hashlib.sha256('\n'.join(connection.iterdump()).encode()).hexdigest()
    if result['binana-owner.sqlite'][2] != [(0,)]:
        raise RuntimeError('OWNER_HAS_OPEN_POSITIONS')
    return result



def stop_rehearsal(container):
    try:
        result = subprocess.run(['docker','rm','-f',container], capture_output=True, text=True, timeout=20)
        if result.returncode == 0 or 'No such container' in result.stderr:
            return None
        return 'DockerCleanupFailed'
    except Exception as exc:
        return type(exc).__name__


def container_command(data, credentials, container):
    command = ['docker','run','--rm','--name',container,'--read-only','--cap-drop=ALL',
        '--security-opt=no-new-privileges','--memory=1536m','--cpus=1','--pids-limit=256',
        '--tmpfs','/tmp:rw,nosuid,nodev,size=128m,mode=1777','--env-file',str(credentials),
        '-e','SHARED_ROOT=/freqtrade/shared','-e','FREQTRADE__INITIAL_STATE=stopped',
        '-e','PYTHONPATH=/freqtrade:/freqtrade/services_src',
        '-v',str(data)+':/probe','-v',str(data/'shared')+':/freqtrade/shared']
    for name in ['sharia','universe','market_context']:
        command += ['-v',str(SOURCE/name)+':/freqtrade/shared/'+name+':ro']
    command += ['-v','/opt/binana-testnet/current/services:/freqtrade/services_src/services:ro',
        '-v','/opt/binana-testnet/current/RELEASE_SHA256.txt:/freqtrade/services_src/RELEASE_SHA256.txt:ro',
        '--entrypoint','python',IMAGE,'/probe/probe.py']
    return command


def main():
    raise SystemExit(64)  # Retired AWS wrapper; retained helpers are regression inputs only.


if __name__ == '__main__':
    raise SystemExit(main())
