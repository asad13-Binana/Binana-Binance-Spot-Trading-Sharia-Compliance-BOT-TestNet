import json
import os
import sqlite3
import time
from pathlib import Path

ROOT = Path(os.getenv('SHARED_ROOT', '/app/shared'))
RUNTIME = ROOT / 'runtime'
HEALTH = RUNTIME / 'sidecar_health.json'
STATE = RUNTIME / 'sidecar_state.json'
INTERVAL = max(5, int(os.getenv('MONITOR_INTERVAL_SECONDS', '10')))


def read_json(path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return None


def count_open_incidents():
    db = ROOT / 'freqtrade' / 'binana-extension.sqlite'
    if not db.exists():
        return None
    try:
        con = sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=2)
        try:
            return con.execute("select count(*) from incidents where status='OPEN'").fetchone()[0]
        finally:
            con.close()
    except Exception:
        return None


def snapshot():
    sharia = read_json(ROOT / 'sharia' / 'sharia_status.json') or {}
    registry = read_json(ROOT / 'sharia' / 'halal_coins.json')
    universe = read_json(ROOT / 'universe' / 'current_pairlist.json') or {}
    return {
        'ok': True,
        'ts': time.time(),
        'role': 'monitor-only',
        'order_authority': False,
        'trading_ready': False,
        'entries_enabled': False,
        'execution_mode': 'monitor-only',
        'readiness_blockers': ['MONITOR_ONLY_NO_ORDER_AUTHORITY'],
        'exchange_credentials_present': bool(os.getenv('BINANCE_API_KEY') or os.getenv('BINANCE_API_SECRET')),
        'sharia_trade_authority': False,
        'sharia_research_status': sharia.get('status') or sharia.get('state') or 'unknown',
        'halal_registry_readable': isinstance(registry, (dict, list)),
        'universe_readable': bool(universe),
        'open_canonical_incidents': count_open_incidents(),
    }


def state_payload(now: float):
    return {
        'schema_version': 2,
        'ts': now,
        'mode': 'testnet',
        'execution_mode': 'monitor-only',
        'simulation': True,
        'entries_enabled': False,
        'trading_ready': False,
        'pause_reason': 'MONITOR_ONLY_NO_ORDER_AUTHORITY',
        'last_reconciliation_status': 'NOT_APPLICABLE_MONITOR_ONLY',
        'order_authority': False,
    }


def atomic_write(path, payload):
    RUNTIME.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(payload, sort_keys=True) + '\n', encoding='utf-8')
    tmp.replace(path)


def main():
    while True:
        now=time.time()
        health=snapshot(); health['ts']=now
        atomic_write(HEALTH, health)
        atomic_write(STATE, state_payload(now))
        time.sleep(INTERVAL)


if __name__ == '__main__':
    main()
