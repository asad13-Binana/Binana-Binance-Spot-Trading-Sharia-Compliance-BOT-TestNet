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
ROOT = Path('/var/backups/binana-testnet/otoco-no-fill-' + time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()))


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
    if result['binana-owner.sqlite'][2] != [(0,)]:
        raise RuntimeError('OWNER_HAS_OPEN_POSITIONS')
    return result


def verified_result(receipt, report):
    return (receipt.get('exit_code') == 0 and report.get('passed') is True
            and all(receipt.get(key) is True for key in [
                'owner_container_unchanged', 'owner_image_unchanged',
                'trade_order_counts_and_open_incidents_unchanged']))


def main():
    os.umask(0o077)
    before = inspect_owner()
    state_before = owner_state()
    databases_before = database_summary()
    config = json.loads(CONFIG.read_text())
    if (config.get('initial_state') != 'stopped'
            or config['exchange'].get('binana_testnet') is not True):
        raise RuntimeError('EXPECTED_TESTNET_CONFIGURATION')
    keys = ['binana', 'stake_amount', 'max_open_trades', 'stake_currency', 'trading_mode',
            'margin_mode', 'timeframe', 'force_entry_enable', 'position_adjustment_enable']
    policy = {key: config.get(key) for key in keys}
    policy['exchange'] = {'name': config['exchange']['name']}
    ROOT.mkdir(mode=0o700, exist_ok=False)
    data = ROOT/'data'
    data.mkdir()
    (data/'policy.json').write_text(json.dumps(policy))
    (data/'run_id').write_text(ROOT.name)
    for source, target in [('sharia/halal_coins.json', 'halal_coins.json'),
                           ('universe/current_pairlist.json', 'pairlist.json')]:
        shutil.copyfile(SOURCE/source, data/target)
    script = Path(__file__).with_name('probe_testnet_otoco_no_fill.py')
    shutil.copyfile(script, data/'probe.py')
    env = dict(row.split('=', 1) for row in before['Config']['Env'] if '=' in row)
    names = ['FREQTRADE__EXCHANGE__KEY', 'FREQTRADE__EXCHANGE__SECRET']
    if any(not env.get(k) or '\n' in env[k] or '\r' in env[k] for k in names):
        raise RuntimeError('CREDENTIALS_UNAVAILABLE')
    credentials = ROOT/'credentials.env'
    name = 'binana-testnet-otoco-no-fill-' + ROOT.name.rsplit('-', 1)[-1].lower()
    try:
        credentials.write_text(''.join(k+'='+env[k]+'\n' for k in names))
        credentials.chmod(0o600)
        for path in [data, *data.iterdir()]:
            os.chown(path, 999, 987)
        command = ['docker', 'run', '--rm', '--name', name, '--read-only', '--cap-drop=ALL',
                   '--security-opt=no-new-privileges', '--memory=512m', '--cpus=1', '--pids-limit=128',
                   '--tmpfs', '/tmp:rw,nosuid,nodev,size=64m,mode=1777', '--env-file', str(credentials),
                   '-v', str(data)+':/probe', '--entrypoint', 'python', IMAGE, '/probe/probe.py']
        execution_error = None
        try:
            run = subprocess.run(command, capture_output=True, text=True, timeout=180)
            (ROOT/'stdout.log').write_text(run.stdout)
            (ROOT/'stderr.log').write_text(run.stderr)
            exit_code = run.returncode
        except Exception as exc:
            execution_error = type(exc).__name__
            exit_code = None
            # Killing the client alone does not stop the container. Stop only
            # this rehearsal before reading its durable ambiguous-attempt journal.
            try:
                subprocess.run(['docker', 'rm', '-f', name], capture_output=True, timeout=20)
            except Exception as stop_exc:
                execution_error += ':container_stop_' + type(stop_exc).__name__
        receipt = {'image': IMAGE, 'probe_sha256': hashlib.sha256(script.read_bytes()).hexdigest(),
                   'exit_code': exit_code, 'execution_error': execution_error,
                   'owner_state_before': state_before, 'live_database_mounted': False,
                   'authenticated_lifecycle_acceptance': False}
        checks = {
            'owner_container_unchanged': lambda: before['Id'] == inspect_owner()['Id'],
            'owner_image_unchanged': lambda: before['Image'] == inspect_owner()['Image'],
            'trade_order_counts_and_open_incidents_unchanged': lambda: databases_before == database_summary(),
            'owner_state_unchanged': lambda: owner_state() == state_before,
        }
        for key, check in checks.items():
            try:
                receipt[key] = check()
            except Exception as exc:
                receipt[key] = False
                receipt[key + '_error'] = type(exc).__name__
        report = {}
        try:
            report = json.loads((data/'report.json').read_text())
        except (OSError, ValueError):
            receipt['report_unavailable'] = True
        receipt['passed'] = verified_result(receipt, report) and receipt['owner_state_unchanged']
        receipt['requires_exchange_reconciliation'] = not receipt['passed']
        (ROOT/'receipt.json').write_text(json.dumps(receipt, indent=2)+'\n')
        print(json.dumps({'root': str(ROOT), **receipt,
                         'probe_error': report.get('error'),
                         'matching_engine_post_attempts': report.get('matching_engine_post_attempts'),
                         'unexpected_fill_requires_reconciliation': report.get('unexpected_fill_requires_reconciliation')}))
        return 0 if receipt['passed'] else 1
    finally:
        credentials.unlink(missing_ok=True)
        subprocess.run(['docker', 'rm', '-f', name], capture_output=True, timeout=20)


if __name__ == '__main__':
    raise SystemExit(main())
