"""Preserve active support contracts while retiring old engine entrypoints."""
from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import pytest
import yaml

from services.common import envelope
from services.common.strategy_fingerprint import method_source_segment, source_hash, token_hash
from scripts import verify_four_bot_cohost

ROOT = Path(__file__).resolve().parents[1]


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    previous = sys.path[:]
    try:
        sys.path.insert(0, str(path.parent))
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = previous
    return module


def topology():
    builder = load(ROOT / 'runtime_owner/deployment/materialize_oracle_bundle.py',
                   'retirement_bundle')
    return builder.topology('a' * 64, (ROOT / 'RELEASE_MODE').read_text().strip())


def test_active_consumers_require_explicit_spot_permission():
    # The earlier four-consumer regression also covered a now-retired sidecar.
    for relative in ('services/common/binance_public.py', 'services/sharia_screener/service.py',
                     'services/universe_service/scanner.py'):
        source = (ROOT / relative).read_text()
        assert "isSpotTradingAllowed', False" in source, relative
        assert "isSpotTradingAllowed', True" not in source, relative


def test_current_manual_projector_receives_only_the_required_bus_key_names():
    service = topology()['services']['sharia-screener']
    assert service['network_mode'] == 'none'
    assert 'FREQTRADE__EXCHANGE__SECRET' not in service['environment']
    with patch.dict('os.environ', {key: 't' * 48 for key in service['environment']}, clear=True):
        for purpose in (envelope.BUS_SHARIA_DECISION, envelope.BUS_SHARIA_RESULT):
            assert len(envelope.load_key(purpose)) == 48


def test_market_context_has_no_strategy_or_order_engine_import():
    for path in (ROOT / 'services/market_context').glob('*.py'):
        tree = ast.parse(path.read_text())
        imports = [node.module or '' for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        imports += [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]
        assert not any(marker in module for module in imports
                       for marker in ('legacy_core', 'execution_sidecar', 'IctSmcStrategy', 'BinanaNfiSpot')), path
    assert 'start_background()' in (ROOT / 'services/market_context/universe_main.py').read_text()


def test_fingerprint_source_and_decorator_contract_survives_engine_retirement(tmp_path):
    source = 'class Fixture:\n    @decorator("5m")\n    def signal(self):\n        return 1\n'
    path = tmp_path / 'fixture.py'
    path.write_text(source)
    segment = method_source_segment(path, 'Fixture', 'signal')
    assert segment == '    @decorator("5m")\n    def signal(self):\n        return 1\n'
    assert segment in source


def test_fingerprint_cosmetic_changes_and_logic_changes_are_distinguished(tmp_path):
    paths = [tmp_path / name for name in ('original.py', 'cosmetic.py', 'logic.py')]
    for path, return_value in zip(paths, ('1', '1  # comment only', '2')):
        path.write_text('class Fixture:\n    def signal(self):\n        return ' + return_value + '\n')
    hashes = [token_hash(path, 'Fixture', 'signal') for path in paths]
    assert hashes[0] == hashes[1]
    assert hashes[0] != hashes[2]
    assert source_hash(paths[0], 'Fixture', 'signal') != source_hash(paths[1], 'Fixture', 'signal')


@pytest.mark.parametrize('filename', [
    'deploy_candidate_owner.py', 'deploy_paused_owner_20261008.py',
    'authorize_verified_owner_20261008.py', 'run_candidate_acceptance.py',
    'run_controlled_owner_lifecycle.py', 'run_testnet_order_validation.py',
    'run_testnet_otoco_no_fill.py',
])
def test_retired_recovery_command_cannot_activate_a_host(filename):
    path = ROOT / 'scripts/recovery' / filename
    environment = (ROOT / 'RELEASE_MODE').read_text().strip()
    assert environment in {'testnet', 'live'}
    if environment == 'live' and filename != 'deploy_candidate_owner.py':
        # These Testnet-only regression libraries were never shipped by LIVE.
        assert not path.exists(), 'Testnet-only recovery helper leaked into LIVE'
        return
    module = load(path, 'retired_' + Path(filename).stem)
    with patch.object(subprocess, 'run', side_effect=AssertionError('PROCESS_MUST_NOT_START')):
        with pytest.raises(SystemExit) as error:
            module.main()
    assert error.value.code == 64


def test_historical_cohost_mode_cannot_read_host_credentials_or_qualify_new_bundles():
    with patch.object(verify_four_bot_cohost, 'parse_env', side_effect=AssertionError('HOST_READ')):
        with pytest.raises(verify_four_bot_cohost.ContractError, match='LEGACY_HOST_TOPOLOGY_RETIRED'):
            verify_four_bot_cohost.check_host({})


def test_root_ci_keeps_full_coverage_security_and_host_fixture_gates():
    jobs = yaml.safe_load((ROOT / '.github/workflows/ci.yml').read_text())['jobs']
    assert {'verify', 'sast', 'quality-evidence', 'host-fixtures', 'dependency-audit', 'owner-build'} <= set(jobs['artifact']['needs'])
    coverage = '\n'.join(step.get('run', '') for step in jobs['quality-evidence']['steps'])
    assert '--branch --source=services,monitoring' in coverage
    assert '-m pytest -q -p no:cacheprovider tests monitoring/tests' in coverage
    assert '--minimum 68 --target 85' in coverage
    assert 'continue-on-error' not in json.dumps(jobs)
    audits = '\n'.join(step.get('run', '') for step in jobs['dependency-audit']['steps'])
    for lock in ('requirements.services.lock', 'requirements.host.lock', 'requirements.renewal.lock',
                 'monitoring/requirements-monitoring.lock', 'runtime_owner/deployment/requirements.owner.lock',
                 'runtime_owner/deployment/requirements.build.lock'):
        assert lock in audits
    host = '\n'.join(step.get('run', '') for step in jobs['host-fixtures']['steps'])
    assert 'sudo env PYTHONDONTWRITEBYTECODE=1' in host
    assert 'test_owner_monitor_installer.py' in host
    assert 'test_release_host_portability.py' in host
    if (ROOT / 'RELEASE_MODE').read_text().strip() == 'testnet':
        assert 'test_owner_release_authorization.py' in host
    assert 'test_owner_snapshot_publication.py' in host
    for name in ('test_runtime_health_permissions.py', 'test_sharia_health_permissions.py',
                 'test_telegram_health_permissions.py'):
        assert name in host
