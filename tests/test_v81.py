from __future__ import annotations
import ast, hashlib, json, os, subprocess, sys, tempfile, time, unittest
from unittest import mock
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # make _harness importable in any run mode

import _harness  # sets bus keys + release binding; import before service modules

from services.common.models import ProtectionMode, LifecycleState
from services.common.audit import audit
from services.common.retention import prune_files
from services.telegram_broker.callbacks import CallbackStore
from services.telegram_broker import authorization
from services.universe_service.sharia_filter import ShariaFilter
from services.universe_service.snapshot_store import store as store_snapshot
from services.universe_service.validate_sharia import validate

from services.common.strategy_fingerprint import source_hash, token_hash

CORE_SHA256 = '70b1d67cc0092b5b8db4a68b343cf893641bde1aae580e9ef51e2adec1062459'
# Canonical source/token fingerprints of the four protected signal methods.
# V10.1 fix: the previous ast.dump() hashes changed between interpreter
# versions (Python 3.12 AST fields) even when the source did not; these
# hashes depend only on the source text and are stable on Python 3.10-3.13.
# The methods themselves are byte-identical to the original reviewed
# strategy; the strategy file must never be edited to satisfy this test.
STRATEGY_SOURCE_SHA256 = {
    'populate_indicators_5m': '3c01ceda9807efbcf63b32297879c04af6cc65744387dd4821a2ed1328025969',
    'populate_indicators': '11c39597e4c7f535808e36db290f36f8908dc0e3b98578d9d92dd1e8abd93526',
    'populate_entry_trend': '277b321d40430842a0462182df665ca1e4f0a1546ced7bf2ffc75eb5b7b8c11e',
    'populate_exit_trend': 'fdd2c099edf44b4db4408a5aec183f2c493c95db34d66975697dc6a50c50c196',
}
STRATEGY_TOKEN_SHA256 = {
    'populate_indicators_5m': '071a394a70a2370b05700ba8e58bfbbeec6d66fb48ca78995dc8fc5dd98e265b',
    'populate_indicators': 'ab8b017314652ed1d08f9c23813eade8a79d5a67c0831a624095f315ef6ecd59',
    'populate_entry_trend': '954d39c9f92fc3a7ce744cc2ef0269e38d32949612fea4ba6baed07049e0c8ad',
    'populate_exit_trend': 'dc79eb4e19e4c68db8c3e877c671a115edb11f87f40cf1a9b34fbb0c457ccd7f',
}


class FakeLegacy:
    CFG = SimpleNamespace(
        EXIT_FEE_SHAVE=True, FEE_PCT_PER_SIDE=0.1, FIXED_STOP_PCT=2.0,
        LIMIT_FILL_BUFFER_BIPS=10, OTOCO_TP_PCT=4.0, INITIAL_TRAIL_DELTA_BIPS=100
    )
    _counter = 0

    @staticmethod
    def round_down(value, step):
        value, step = Decimal(value), Decimal(step)
        return (value / step).to_integral_value(rounding=ROUND_DOWN) * step

    @staticmethod
    def dstr(value):
        return format(Decimal(value), 'f')

    @staticmethod
    def bips_mult(bips):
        return Decimal(1) + Decimal(bips) / Decimal(10000)

    @classmethod
    def _new_coid(cls, prefix):
        cls._counter += 1
        return f'{prefix}{cls._counter:08d}'


SYM = SimpleNamespace(
    symbol='ADAUSDT', base='ADA', tick=Decimal('0.0001'), step=Decimal('0.1'),
    trail_min=10, trail_max=2000
)






class ShariaTests(unittest.TestCase):
    def test_only_current_halal_passes(self):
        # V19.3: only GREEN / GREEN_AVOID_OPTIONAL are trade-eligible; the
        # legacy HALAL vocabulary and old schema are rejected.
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 's.json'
            past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
            _harness.write_attested_status(path, [
                ('ETH', 'GREEN'),
                ('ADA', 'GREEN_AVOID_OPTIONAL'),
                ('SOL', 'DOUBTFUL'),
                ('DOG', 'NO_TRADE_INFO'),
                ('XRP', 'GREEN', past),
            ])
            gate = ShariaFilter(path)
            self.assertTrue(gate.decision('ETH').allowed)
            self.assertTrue(gate.decision('ADA').allowed)
            self.assertFalse(gate.decision('SOL').allowed)
            self.assertFalse(gate.decision('DOG').allowed)
            self.assertEqual(gate.decision('XRP').status, 'STALE')       # expired GREEN
            self.assertEqual(gate.decision('ZZZ').status, 'NO_TRADE_INFO')  # unlisted

    def test_legacy_schema_is_rejected_fail_closed(self):
        # The previous Sharia definition must never load again.
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'legacy.json'
            future = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
            path.write_text(json.dumps({'schema_version': 1, 'records': [
                {'symbol': 'ETH', 'status': 'HALAL', 'source': 'x',
                 'reviewed_at': '2026-07-14', 'expires_at': future},
            ]}))
            with self.assertRaises(ValueError):
                ShariaFilter(path)

    def test_release_dataset_schema(self):
        self.assertEqual(validate(ROOT / 'shared/sharia/sharia_status.json'), [])

    def test_malformed_or_duplicate_records_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'bad.json'
            # Valid V19.3 wrapper so the inner duplicate/source checks are exercised.
            _harness.write_attested_status(path, [('ETH', 'GREEN'), ('ETH', 'GREEN')])
            with self.assertRaises(ValueError):
                ShariaFilter(path)
            _harness.write_attested_status(path, [('SOL', 'GREEN')])
            missing_source = json.loads(path.read_text())
            missing_source['records'][0]['source'] = ''
            path.write_text(json.dumps(missing_source))
            with self.assertRaises(ValueError):
                ShariaFilter(path)


class UniverseSnapshotTests(unittest.TestCase):
    def test_snapshot_hash_identifies_exact_ranked_universe(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = {'limit': 50, 'sharia_dataset_sha256': 'abc'}
            first = store_snapshot(root, [{'pair': 'ETH/USDT', 'rank': 1}], config, 900)
            second = store_snapshot(root, [{'pair': 'SOL/USDT', 'rank': 1}], config, 900)
            self.assertEqual(first['configuration_hash'], second['configuration_hash'])
            self.assertNotEqual(first['snapshot_hash'], second['snapshot_hash'])
            current = json.loads((root / 'current_pairlist.json').read_text())
            self.assertEqual(current['snapshot_hash'], second['snapshot_hash'])
            self.assertEqual(current['pairs'], ['SOL/USDT'])




class RetentionTests(unittest.TestCase):
    def test_audit_log_rotates_with_bounded_backups(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.dict(os.environ, {
            'AUDIT_LOG_MAX_BYTES': '220', 'AUDIT_LOG_BACKUPS': '2'
        }, clear=False):
            path = Path(td) / 'events.jsonl'
            for index in range(12):
                audit('rotation-test', details={'index': index, 'payload': 'x' * 80}, path=str(path))
            self.assertTrue(path.exists())
            self.assertTrue(path.with_name('events.jsonl.1').exists())
            self.assertFalse(path.with_name('events.jsonl.3').exists())

    def test_file_retention_keeps_newest_records(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = []
            for index in range(5):
                path = root / f'{index}.json'
                path.write_text('{}')
                os.utime(path, (100 + index, 100 + index))
                paths.append(path)
            self.assertEqual(prune_files(root, '*.json', max_files=2), 3)
            self.assertEqual(sorted(path.name for path in root.glob('*.json')), ['3.json', '4.json'])


class TelegramSafetyTests(unittest.TestCase):
    def test_callback_is_one_time(self):
        store = CallbackStore(ttl=10)
        token = store.issue('emergency_exit', {'symbol': 'ETHUSDT'})
        item, reason = store.consume(token)
        self.assertEqual(reason, 'ok')
        self.assertEqual(item['action'], 'emergency_exit')
        item2, reason2 = store.consume(token)
        self.assertIsNone(item2)
        self.assertEqual(reason2, 'duplicate callback')

    def test_callback_expiry(self):
        store = CallbackStore(ttl=-1)
        token = store.issue('x')
        item, reason = store.consume(token)
        self.assertIsNone(item)
        self.assertEqual(reason, 'expired callback')

    def test_owner_must_match_sender_and_private_chat(self):
        with mock.patch.object(authorization, 'OWNER', '12345'):
            self.assertTrue(authorization.is_owner(12345, 12345))
            self.assertFalse(authorization.is_owner(12345, -100987654321))
            self.assertFalse(authorization.is_owner(99999, 12345))




if __name__ == '__main__':
    unittest.main(verbosity=2)
