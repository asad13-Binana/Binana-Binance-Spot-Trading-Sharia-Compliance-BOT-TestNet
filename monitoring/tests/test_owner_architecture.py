"""Owner monitoring must never present archived sidecar data as live execution."""
from __future__ import annotations

from contextlib import closing
import json
import os
import sqlite3
import time

import pytest

from monitoring.api import metrics
from monitoring.api.configuration import CONFIG, Config
from monitoring.tests.test_monitoring import AUTH, client, isolated_config  # noqa: F401


def _write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding='utf-8')


def _owner():
    CONFIG.execution_owner = 'FREQTRADE'


def _readiness(**changes):
    now = time.time()
    payload = {'order_owner': 'FREQTRADE', 'execution_environment': 'TESTNET',
               'generated_at': now, 'valid_until': now + 30,
               'release_approved': True, 'blockers': []}
    payload.update(changes)
    _write(CONFIG.owner_readiness_path, payload)


def _legacy_health():
    for path in (CONFIG.sidecar_health_path, CONFIG.telegram_health_path,
                 CONFIG.sharia_health_path, CONFIG.universe_health_path,
                 CONFIG.market_context_health_path):
        _write(path, {'ok': True, 'ts': time.time()})


@pytest.mark.parametrize('mode', ['testnet', 'live'])
def test_default_architecture_remains_sidecar(monkeypatch, mode):
    monkeypatch.delenv('MONITOR_EXECUTION_OWNER', raising=False)
    monkeypatch.setenv('BOT_MODE', mode)
    assert Config().execution_owner == 'SIDECAR'


def test_owner_architecture_and_paths_are_explicit_configuration(monkeypatch, tmp_path):
    monkeypatch.setenv('MONITOR_EXECUTION_OWNER', 'freqtrade')
    for variable, name in [('OWNER_DATABASE_PATH', 'owner.sqlite'),
                           ('OWNER_EXTENSION_DATABASE_PATH', 'extension.sqlite'),
                           ('OWNER_READINESS_PATH', 'readiness.json')]:
        monkeypatch.setenv(variable, str(tmp_path / name))
    config = Config()
    assert config.execution_owner == 'FREQTRADE'
    assert config.owner_db_path == tmp_path / 'owner.sqlite'
    assert config.owner_extension_db_path == tmp_path / 'extension.sqlite'
    assert config.owner_readiness_path == tmp_path / 'readiness.json'


def test_unknown_execution_owner_is_a_configuration_error(monkeypatch):
    monkeypatch.setenv('MONITOR_EXECUTION_OWNER', 'freqtrdae')
    assert any('MONITOR_EXECUTION_OWNER' in error for error in Config().runtime_errors())


def test_owner_database_labels_and_paths_ignore_stale_legacy_files(tmp_path):
    _owner()
    _readiness()
    for path, schema in [
        (CONFIG.execution_db_path, 'CREATE TABLE trade_records(trade_id TEXT)'),
        (CONFIG.signal_db_path, 'CREATE TABLE trades(id INTEGER)'),
        (CONFIG.owner_db_path, 'CREATE TABLE trades(id INTEGER); CREATE TABLE orders(id INTEGER)'),
        (CONFIG.owner_extension_db_path,
         'CREATE TABLE intents(intent_id TEXT); CREATE TABLE protection(intent_id TEXT); '
         'CREATE TABLE fill_ledger(fill_key TEXT)'),
    ]:
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.executescript(schema)
        path.chmod(0o600)
    for path in (CONFIG.execution_db_path, CONFIG.signal_db_path):
        os.utime(path, (time.time() - 30 * 86400,) * 2)
    result = metrics.databases()
    assert result['execution_owner'] == 'FREQTRADE'
    assert result['execution']['path'] == str(CONFIG.owner_db_path)
    assert result['execution']['role'] == 'authoritative_freqtrade_owner'
    assert result['extension']['path'] == str(CONFIG.owner_extension_db_path)
    assert result['extension']['role'] == 'canonical_order_protection_and_fill_state'
    assert result['execution']['integrity'] == result['extension']['integrity'] == 'ok'
    assert 'signal_engine' not in result
    assert result['owner_readiness']['fresh'] is True
    assert CONFIG.owner_db_path.stat().st_mode & 0o777 == 0o600
    assert CONFIG.owner_extension_db_path.stat().st_mode & 0o777 == 0o600


def test_owner_database_integrity_can_be_unavailable_without_legacy_fallback(monkeypatch):
    _owner()
    _readiness()
    consulted = []

    def unavailable(path):
        consulted.append(path)
        return {'present': True, 'integrity': 'unavailable', 'reason': 'PermissionError'}

    monkeypatch.setattr(metrics, 'database_status', unavailable)
    result = metrics.databases()
    assert consulted == [CONFIG.owner_db_path, CONFIG.owner_extension_db_path]
    assert result['execution']['integrity'] == 'unavailable'
    assert result['extension']['integrity'] == 'unavailable'
    assert result['execution']['role'] == 'authoritative_freqtrade_owner'


def test_owner_blockers_degrade_health_even_with_healthy_legacy_components():
    _owner()
    _legacy_health()
    _readiness(release_approved=False, blockers=['AUTHENTICATED_RECONCILIATION_UNAVAILABLE'])
    result = metrics.runtime_health()
    assert result['status'] == 'degraded'
    assert result['components']['freqtrade_owner']['fresh'] is True
    assert result['components']['freqtrade_owner']['release_approved'] is False
    assert result['components']['execution_sidecar']['role'] == 'advisory_only'


def test_fresh_unblocked_owner_is_healthy_despite_stale_advisory_sidecar():
    _owner()
    _legacy_health()
    _write(CONFIG.sidecar_health_path, {'ok': True, 'ts': time.time() - 86400})
    _readiness()
    result = metrics.runtime_health()
    assert result['status'] == 'healthy'
    assert result['components']['freqtrade_owner']['order_owner'] == 'FREQTRADE'


@pytest.mark.parametrize('change', [
    {'generated_at': 1, 'valid_until': 31},
    {'order_owner': 'SIDECAR'},
    {'execution_environment': 'LIVE'},
    {'release_approved': 'true'},
    {'blockers': 'none'},
    {'blockers': [False]},
    {'generated_at': float('nan')},
    {'generated_at': time.time() + 100, 'valid_until': time.time() + 130},
    {'valid_until': time.time() + 86400},
    {'blockers': ['RECONCILIATION_BLOCKER_OPEN']},
])
def test_invalid_or_expired_owner_readiness_cannot_report_healthy(change):
    _owner()
    _legacy_health()
    _readiness(**change)
    assert metrics.runtime_health()['status'] != 'healthy'


def test_missing_owner_snapshot_does_not_fall_back_to_healthy_sidecar():
    _owner()
    _legacy_health()
    assert metrics.runtime_health()['status'] != 'healthy'


@pytest.mark.parametrize('collector', ['execution_state', 'performance', 'recent_trades',
                                       'signal_performance', 'order_quality'])
def test_legacy_collectors_are_explicitly_unsupported_in_owner_mode(monkeypatch, collector):
    _owner()

    def forbidden(*args, **kwargs):
        raise AssertionError('owner monitor read a legacy execution source')

    monkeypatch.setattr(metrics, 'query', forbidden)
    monkeypatch.setattr(metrics, '_read_jsonl_tail', forbidden)
    result = getattr(metrics, collector)()
    assert result['supported'] is False
    assert result['execution_owner'] == 'FREQTRADE'
    assert result['reason'] == 'unsupported_for_execution_owner'
    assert result.get('closed_trades') is None
    assert result.get('net_pnl_pct') is None


def test_authenticated_health_reports_configured_owner_and_current_blockers():
    _owner()
    _legacy_health()
    _readiness(release_approved=False, blockers=['RECONCILIATION_BLOCKER_OPEN'])
    response = client.get('/api/v1/health', headers=AUTH)
    assert response.status_code == 200
    payload = response.json()
    assert payload['bot']['status'] == 'degraded'
    assert payload['databases']['execution_owner'] == 'FREQTRADE'
    assert payload['databases']['execution']['path'] == str(CONFIG.owner_db_path)
    assert 'signal_engine' not in payload['databases']


@pytest.mark.parametrize('route,sections', [
    ('performance', ['execution_performance', 'signal_engine_performance', 'order_quality']),
    ('trades', ['recent_execution_trades', 'execution_state']),
    ('status', ['execution', 'order_quality']),
])
def test_authenticated_legacy_analytics_are_unsupported_for_owner(route, sections):
    _owner()
    response = client.get('/api/v1/' + route, headers=AUTH)
    assert response.status_code == 200
    for section in sections:
        assert response.json()[section]['supported'] is False
        assert response.json()[section]['reason'] == 'unsupported_for_execution_owner'
