"""Regressions for the actual AWS collector and startup image contracts."""
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
CANDIDATE = ROOT / 'runtime_owner' / 'market_context_repair'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# Keep the preserved archival package and root service imports separate.
import types
package = types.ModuleType('repaired_market')
package.__path__ = [str(CANDIDATE)]
sys.modules['repaired_market'] = package
analytics = load('repaired_market.analytics', CANDIDATE / 'analytics.py')
stream = load('repaired_market.spot_stream', CANDIDATE / 'spot_stream.py')
service = load('repaired_market.service', CANDIDATE / 'service.py')
images = load('verified_local_images', ROOT / 'runtime_owner/deployment/verify_local_images.py')


def test_one_letter_assets_do_not_poison_whole_universe():
    collector = analytics.SpotMicrostructureAnalytics()
    connection = stream.SpotMarketStream(collector, endpoint=stream.PUBLIC_MARKET_ENDPOINT)
    connection.update_symbols({'LTCUSDT', 'SUSDT', 'UUSDT'})
    assert connection.status()['desired_symbol_count'] == 3
    assert collector.snapshot()['symbol_count'] == 3


@pytest.mark.parametrize('invalid', ['USDT', '/USDT', 'S/USDT', 'susdt', 'S USDT', 'SUSDT/evil'])
def test_malformed_symbols_still_rejected_atomically(invalid):
    collector = analytics.SpotMicrostructureAnalytics()
    collector.set_symbols({'LTCUSDT'})
    with pytest.raises(analytics.MarketDataError):
        collector.set_symbols({'SUSDT', invalid})
    assert set(collector.snapshot()['symbols']) == {'LTCUSDT'}


def test_stream_capacity_matches_bounded_symbol_capacity():
    collector = analytics.SpotMicrostructureAnalytics()
    connection = stream.SpotMarketStream(collector, endpoint=stream.PUBLIC_MARKET_ENDPOINT)
    connection.update_symbols({f'X{i}USDT' for i in range(200)})
    assert connection.status()['desired_stream_count'] == 600
    with pytest.raises(ValueError):
        connection.update_symbols({f'X{i}USDT' for i in range(201)})
    assert connection.status()['desired_stream_count'] == 600
    assert collector.snapshot()['symbol_count'] == 200


def test_universe_failure_cannot_publish_old_symbols_as_fresh(tmp_path):
    collector = service.MarketContextService.__new__(service.MarketContextService)
    snapshot = {'symbols': {'LTCUSDT': {'status': 'fresh'}}, 'symbol_count': 1,
                'fresh_symbol_count': 1, 'statistics': {}}
    collector.analytics = SimpleNamespace(snapshot=lambda: snapshot, depth=SimpleNamespace(requests=0, errors=0))
    collector.stream = SimpleNamespace(status=lambda: {'subscription_ready': True})
    collector.mode = 'testnet'
    collector._last_universe_error = 'MarketDataError: invalid identity'
    collector._universe_snapshot_hash = 'old'
    collector.snapshot_path = tmp_path / 'snapshot.json'
    collector.health_path = tmp_path / 'health.json'
    collector._publish()
    result = json.loads(collector.snapshot_path.read_text())
    assert result['fresh_symbol_count'] == 0
    assert result['universe_ready'] is False
    assert result['symbols']['LTCUSDT']['status'] == 'stale'
    assert json.loads(collector.health_path.read_text())['ok'] is False
    owner = load('owner_market_contract', ROOT / 'runtime_owner/owner/freqtrade/binana/market_data.py')
    consumer = owner.MarketDataService(None, collector.snapshot_path, {})
    assert consumer._context('LTC/USDT') == ({}, 'universe_unavailable')


def test_local_image_identity_missing_or_retagged_blocks_startup():
    manifest = {'owner': {'reference': 'local:retained', 'image_id': 'sha256:' + 'a' * 64}}
    compose = {'services': {'owner': {'image': 'local:retained'}}}
    assert images.verify(manifest, compose, lambda _: [{'Id': 'sha256:' + 'a' * 64}])
    with pytest.raises(ValueError, match='identity mismatch'):
        images.verify(manifest, compose, lambda _: [{'Id': 'sha256:' + 'b' * 64}])
    with pytest.raises(ValueError, match='identity mismatch'):
        images.verify(manifest, compose, lambda _: [])
    compose['services']['owner']['image'] = 'local:unexpected'
    with pytest.raises(ValueError, match='reference mismatch'):
        images.verify(manifest, compose, lambda _: [{'Id': 'sha256:' + 'a' * 64}])
