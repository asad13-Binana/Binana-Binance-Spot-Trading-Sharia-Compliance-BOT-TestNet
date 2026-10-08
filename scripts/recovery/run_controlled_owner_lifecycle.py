"""AWS-only runner for one bounded matching-engine Testnet OTOCO rehearsal."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time


IMAGE = 'sha256:a90b7f661500f7d00b3da32ce4c22d987d41d6da01483affe849a0baccddb523'
SOURCE = Path('/var/lib/binana-testnet/shared')
OWNER = 'binana-testnet-freqtrade-1'
CONFIG = Path('/home/ubuntu/binana-repair-20260912T130255Z/release-config/config.json')
ROOT = Path('/var/backups/binana-testnet/controlled-owner-' + time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()))


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
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--slot-soak', type=int, choices=[1,4])
    args = parser.parse_args()
    os.umask(0o077)
    before = inspect_owner()
    state_before = owner_state()
    databases_before = database_summary()
    expected_incident = [('repair-economic-settlement-replay-20260920','ECONOMIC_SETTLEMENT_REPLAY_REQUIRED','OPEN')]
    if databases_before['binana-extension.sqlite'][0] != expected_incident:
        raise RuntimeError('ORIGINAL_RECOVERY_INCIDENT_NOT_PRESERVED')
    config_sha256 = hashlib.sha256(CONFIG.read_bytes()).hexdigest()
    original = json.loads(CONFIG.read_text())
    if (original.get('initial_state') != 'stopped' or original.get('dry_run') is not False
            or original['exchange'].get('binana_testnet') is not True
            or original.get('cancel_open_orders_on_exit') is not True):
        raise RuntimeError('EXPECTED_STOPPED_TESTNET_CONFIGURATION')
    ROOT.mkdir(mode=0o700, exist_ok=False)
    data = ROOT/'data'
    clone = data/'shared/freqtrade'
    clone.mkdir(mode=0o700, parents=True)
    for name in ['userdata', 'shared/runtime', 'shared/cache']:
        (data/name).mkdir()
    with sqlite3.connect(SOURCE/'freqtrade/binana-owner.sqlite', timeout=10) as lock:
        lock.execute('ATTACH DATABASE ? AS ext', (str(SOURCE/'freqtrade/binana-extension.sqlite'),))
        lock.execute('BEGIN IMMEDIATE')
        if lock.execute('SELECT count(*) FROM trades WHERE is_open=1').fetchone()[0]:
            raise RuntimeError('CANONICAL_POSITIONS_NOT_EMPTY')
        for name in ['binana-owner.sqlite','binana-extension.sqlite']:
            with sqlite3.connect('file:'+str(SOURCE/'freqtrade'/name)+'?mode=ro', uri=True) as src:
                with sqlite3.connect(clone/name) as dst:
                    src.backup(dst)
                    if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                        raise RuntimeError('CLONE_INTEGRITY_FAILURE')
        lock.rollback()
    config = original
    config['db_url'] = 'sqlite:////freqtrade/shared/freqtrade/binana-owner.sqlite'
    config['user_data_dir'] = '/probe/userdata'
    config['datadir'] = '/probe/userdata/data'
    for name in ['telegram','api_server','webhook','external_message_consumer']:
        config.pop(name, None)
    for key in ['key','secret','password','uid','apiKey']:
        config['exchange'].pop(key, None)
    config['bot_name'] = 'BINANA controlled Testnet lifecycle clone'
    (data/'config.json').write_text(json.dumps(config))
    (data/'run_id').write_text(ROOT.name)
    script = Path(__file__).with_name('probe_owner_slot_soak.py' if args.slot_soak else 'probe_controlled_owner_lifecycle.py')
    if args.slot_soak:
        shutil.copyfile(Path(__file__).with_name('probe_controlled_owner_lifecycle.py'), data/'probe_base.py')
        (data/'soak.json').write_text(json.dumps({'slots':args.slot_soak,'seconds':600}))
    shutil.copyfile(script, data/'probe.py')
    env = dict(row.split('=', 1) for row in before['Config']['Env'] if '=' in row)
    names = ['FREQTRADE__EXCHANGE__KEY','FREQTRADE__EXCHANGE__SECRET']
    if any(not env.get(k) or '\n' in env[k] or '\r' in env[k] for k in names):
        raise RuntimeError('CREDENTIALS_UNAVAILABLE')
    credentials = ROOT/'credentials.env'
    container = 'binana-testnet-controlled-' + ROOT.name.rsplit('-',1)[-1].lower()
    receipt = {'root': str(ROOT), 'image': IMAGE, 'probe_sha256':hashlib.sha256(script.read_bytes()).hexdigest(),
               'source_config_sha256':config_sha256, 'source_databases':databases_before, 'phases': [], 'live_writable_mounts':False, 'release_certificate_created':False,
               'strategy_admission_verified':False, 'slot_soak':args.slot_soak, 'passed':False}
    if args.slot_soak:
        receipt['base_probe_sha256'] = hashlib.sha256((data/'probe_base.py').read_bytes()).hexdigest()
    try:
        credentials.write_text(''.join(k+'='+env[k]+'\n' for k in names))
        credentials.chmod(0o600)
        for path in [data, *data.rglob('*')]:
            os.chown(path, 999, 987)
        command = container_command(data, credentials, container)
        def phase(name):
            outcome = {'phase':name,'passed':False}
            try:
                run = subprocess.run(command+[name], capture_output=True, text=True, timeout=900 if args.slot_soak else 300)
                (ROOT/(name+'-stdout.log')).write_text(run.stdout)
                (ROOT/(name+'-stderr.log')).write_text(run.stderr)
                outcome['exit_code'] = run.returncode
                report = json.loads((data/(name+'.json')).read_text())
                outcome['passed'] = run.returncode == 0 and report.get('passed') is True
                outcome['error'] = report.get('error')
                outcome['canonical'] = report.get('canonical')
            except Exception as exc:
                outcome['error'] = {'type':type(exc).__name__}
            finally:
                cleanup_error = stop_rehearsal(container)
                if cleanup_error:
                    outcome['cleanup_error'] = cleanup_error
                    outcome['passed'] = False
            receipt['phases'].append(outcome)
            (ROOT/'receipt.json').write_text(json.dumps(receipt,indent=2,default=str))
            print(json.dumps({k:outcome.get(k) for k in ['phase','passed','exit_code','error']}),flush=True)
            return outcome['passed']
        phases = ['entry','restart','exit','closed_restart'] if args.slot_soak else ['entry','restart','restart_again','exit','closed_restart']
        for name in phases:
            if not phase(name):
                break
        # Cleanup uses the same durable candidate owner and only its known intent.
        # It never resubmits the entry and never sells pre-existing inventory.
        if not any(row['phase'] == 'exit' for row in receipt['phases']) and (data/'transport.json').exists() and not any(row.get('cleanup_error') for row in receipt['phases']):
            phase('exit')
        receipt['passed'] = [row['phase'] for row in receipt['phases']] == phases and all(row['passed'] for row in receipt['phases'])
    finally:
        try:
            credentials.unlink(missing_ok=True)
        except Exception as exc:
            receipt['credential_cleanup_error'] = type(exc).__name__
            receipt['passed'] = False
        cleanup_error = stop_rehearsal(container)
        if cleanup_error:
            receipt['container_cleanup_error'] = cleanup_error
            receipt['passed'] = False
        checks = {
            'owner_config_unchanged':lambda:hashlib.sha256(CONFIG.read_bytes()).hexdigest() == config_sha256,
            'owner_container_unchanged':lambda:inspect_owner()['Id'] == before['Id'],
            'owner_image_unchanged':lambda:inspect_owner()['Image'] == before['Image'],
            'owner_state_unchanged':lambda:owner_state() == state_before,
            'trade_order_counts_and_open_incidents_unchanged':lambda:database_summary() == databases_before,
        }
        for key, check in checks.items():
            try:
                receipt[key] = check()
            except Exception as exc:
                receipt[key] = False
                receipt[key+'_error'] = type(exc).__name__
        receipt['passed'] = receipt['passed'] and all(receipt.get(key) is True for key in checks)
        receipt['requires_exchange_reconciliation'] = not receipt['passed']
        (ROOT/'receipt.json').write_text(json.dumps(receipt,indent=2,default=str)+'\n')
        print(json.dumps({k:v for k,v in receipt.items() if k != 'phases'}))
    return 0 if receipt['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
