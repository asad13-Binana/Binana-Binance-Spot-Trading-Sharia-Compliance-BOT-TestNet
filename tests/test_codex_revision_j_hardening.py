from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_WORKSPACE = Path.cwd()
_TEMP = tempfile.TemporaryDirectory(prefix='codex-j-hardening-')
_ROOT = Path(_TEMP.name)
os.environ.setdefault('SHARED_ROOT', str(_ROOT))
os.environ.setdefault('LEGACY_RUNTIME_DIR', str(_ROOT / 'legacy'))
os.environ.setdefault('RUNTIME_DIR', str(_ROOT / 'runtime'))
os.environ.setdefault('AUDIT_LOG', str(_ROOT / 'audit' / 'events.jsonl'))

import _harness  # noqa: E402
from services.common.models import LifecycleState  # noqa: E402
from services.common.sharia_v19 import ResultValidationError, validate_result  # noqa: E402
from services.telegram_broker import bot as telegram_bot  # noqa: E402
from services.universe_service.snapshot_store import (  # noqa: E402
    UniverseSnapshotError, load_current, store,
)

os.chdir(_WORKSPACE)






def _filters(*, percent: dict | None = None, trailing: dict | None = None,
             capacity: dict | None = None) -> list[dict]:
    rows = [
        {'filterType': 'PRICE_FILTER', 'tickSize': '0.01',
         'minPrice': '0.01', 'maxPrice': '100000'},
        {'filterType': 'LOT_SIZE', 'stepSize': '0.001',
         'minQty': '0.001', 'maxQty': '1000'},
        {'filterType': 'MARKET_LOT_SIZE', 'stepSize': '0',
         'minQty': '0', 'maxQty': '1000'},
        {'filterType': 'NOTIONAL', 'minNotional': '1', 'maxNotional': '1000000',
         'applyMinToMarket': False, 'applyMaxToMarket': False},
    ]
    if percent is not None:
        rows.append(dict({'filterType': 'PERCENT_PRICE_BY_SIDE'}, **percent))
    if trailing is not None:
        rows.append(dict({'filterType': 'TRAILING_DELTA'}, **trailing))
    if capacity is not None:
        rows.append(capacity)
    return rows


class _Public:
    NO_REFERENCE_PRICE = object()

    def __init__(self, filters, *, last='100', reference='100', entry_overrides=None):
        self.filters = filters
        self.last = last
        self.reference = reference
        self.entry_overrides = entry_overrides or {}

    def exchange_info(self, symbol):
        entry = {
            'symbol': symbol, 'status': 'TRADING', 'isSpotTradingAllowed': True,
            'baseAsset': 'ETH', 'quoteAsset': 'USDT', 'filters': self.filters,
        }
        entry.update(self.entry_overrides)
        return {'symbols': [entry]}

    def reference_price(self, _symbol):
        return self.reference

    def ticker_price(self, _symbol):
        return {'price': self.last}

    def get(self, _path, _params=None):
        return {'price': self.last}


class UniversePointerTests(unittest.TestCase):
    def test_full_snapshot_is_reopened_rehashed_and_pointer_bound(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            published = store(
                root, [{'pair': 'ETH/USDT', 'rank': 1}], {'limit': 1}, 900)
            loaded = load_current(root / 'current_pairlist.json')
            self.assertEqual(loaded['snapshot_hash'], published['snapshot_hash'])
            pointer_path = root / 'current_pairlist.json'
            pointer = json.loads(pointer_path.read_text())
            pointer['pairs'] = ['SOL/USDT']
            pointer_path.write_text(json.dumps(pointer))
            with self.assertRaisesRegex(UniverseSnapshotError, 'pointer/full pairs'):
                load_current(pointer_path)

    def test_tampered_full_configuration_and_unsafe_filename_fail_closed(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            store(root, [{'pair': 'ETH/USDT', 'rank': 1}], {'limit': 1}, 900)
            pointer_path = root / 'current_pairlist.json'
            pointer = json.loads(pointer_path.read_text())
            full_path = root / 'snapshots' / pointer['snapshot_file']
            full = json.loads(full_path.read_text())
            full['configuration']['limit'] = 2
            full_path.write_text(json.dumps(full))
            with self.assertRaisesRegex(
                    UniverseSnapshotError, 'selection state|configuration hash'):
                load_current(pointer_path)
            pointer['snapshot_file'] = '../outside.json'
            pointer_path.write_text(json.dumps(pointer))
            with self.assertRaisesRegex(UniverseSnapshotError, 'unsafe'):
                load_current(pointer_path)

    def test_retention_never_deletes_the_pointed_snapshot(self):
        with tempfile.TemporaryDirectory() as raw, mock.patch.dict(
                os.environ, {'UNIVERSE_SNAPSHOT_MAX_FILES': '1'}):
            root = Path(raw)
            store(root, [{'pair': 'ETH/USDT', 'rank': 1}], {'limit': 1}, 900)
            store(root, [{'pair': 'SOL/USDT', 'rank': 1}], {'limit': 1}, 900)
            pointer = json.loads((root / 'current_pairlist.json').read_text())
            history = list((root / 'snapshots').glob('universe_*.json'))
            self.assertEqual([path.name for path in history], [pointer['snapshot_file']])
            self.assertEqual(load_current(root / 'current_pairlist.json')['pairs'], ['SOL/USDT'])



class ShariaProvenanceAndTelegramTests(unittest.TestCase):
    def test_search_result_url_alone_is_not_opened_evidence(self):
        report = _harness.green_report('ETH')
        report['tool_evidence']['url_citations'] = []
        with self.assertRaisesRegex(ResultValidationError, 'citation/open-page evidence'):
            validate_result(report, expected_base='ETH')

    def test_haram_proof_url_must_be_provider_evidence_bound(self):
        report = _harness.green_report('ETH')
        report.update({
            'final_code': 'HARAM', 'direct_result': 'HARAM',
            'haram_narrative_code': 'N1',
            'haram_narrative_name': 'RIBA_LENDING',
            'haram_proof_card': {
                'C1': True, 'C2': True, 'C3': True, 'C4': True, 'C5': True,
                'quote': ('The official protocol explicitly charges guaranteed interest '
                          'on every lending balance through its core contract.'),
                'url': 'https://example.org/whitepaper',
                'tier': 'TIER_1_OFFICIAL',
            },
        })
        # Hosted-provider citations alone cannot establish a v19.3 HARAM
        # path. Exact local bytes and the complete economic graph are needed.
        with self.assertRaisesRegex(ResultValidationError, 'replayable local evidence'):
            validate_result(report, expected_base='ETH')
        report['tool_evidence']['url_citations'] = []
        with self.assertRaisesRegex(ResultValidationError, 'replayable local evidence'):
            validate_result(report, expected_base='ETH')

    def test_telegram_never_labels_a_forged_record_eligible(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            status = root / 'sharia' / 'sharia_status.json'
            _harness.write_attested_status(status, [('ETH', 'GREEN')])
            runtime = root / 'runtime'
            runtime.mkdir()
            (runtime / 'health.json').write_text(json.dumps({
                'ok': True, 'ready_for_screening': True, 'ts': time.time(),
                'queue': {}, 'completed_today': 0, 'cost_events_today': 0,
                'daily_quota': 10, 'api': {'available': True, 'reason': ''},
            }))
            with mock.patch.object(telegram_bot, 'SHARIA_FILE', status), \
                    mock.patch.object(telegram_bot, 'SHARIA_RUNTIME_DIR', runtime):
                text = telegram_bot._sharia_service_status()
                self.assertIn('trade-eligible now: ETH', text)
                forged = json.loads(status.read_text())
                forged['records'][0]['status'] = 'HARAM'
                forged['records'][0]['final_code'] = 'HARAM'
                status.write_text(json.dumps(forged))
                text = telegram_bot._sharia_service_status()
            self.assertIn('trade-eligible now: NONE (fail-closed)', text)
            self.assertNotIn('trade-eligible now: ETH', text)

    def test_telegram_health_requires_ok_and_ready_flags(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            status = root / 'sharia' / 'sharia_status.json'
            _harness.write_attested_status(status, [('ETH', 'GREEN')])
            runtime = root / 'runtime'; runtime.mkdir()
            (runtime / 'health.json').write_text(json.dumps({
                'ok': True, 'ready_for_screening': False, 'ts': time.time(),
                'queue': {}, 'api': {},
            }))
            with mock.patch.object(telegram_bot, 'SHARIA_FILE', status), \
                    mock.patch.object(telegram_bot, 'SHARIA_RUNTIME_DIR', runtime):
                text = telegram_bot._sharia_service_status()
            self.assertIn('health: NOT READY', text)








if __name__ == '__main__':
    unittest.main(verbosity=2)
