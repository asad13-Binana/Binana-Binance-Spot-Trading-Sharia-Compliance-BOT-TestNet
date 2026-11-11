"""AWS-only host runner for non-matching order validation; keeps the owner paused."""
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
    if result['binana-owner.sqlite'][2] != [(0,)]:
        raise RuntimeError('OWNER_HAS_OPEN_POSITIONS')
    return result


def verified_result(receipt, report):
    return (receipt.get('exit_code') == 0 and report.get('passed') is True
            and all(receipt.get(key) is True for key in [
                'owner_container_unchanged', 'owner_image_unchanged',
                'trade_order_counts_and_open_incidents_unchanged']))


def main():
    raise SystemExit(64)  # Retired AWS wrapper; retained helpers are regression inputs only.


if __name__ == '__main__':
    raise SystemExit(main())
