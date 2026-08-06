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
from services.execution_sidecar.protection_modes import OrderRequestFactory
from services.execution_sidecar.risk_checks import FreshSignalGuard
from services.execution_sidecar.state_store import StateStore
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


class ReleaseIntegrityTests(unittest.TestCase):
    def test_legacy_core_is_byte_preserved(self):
        path = ROOT / 'legacy_core/binance_bot_V4.9.16_ALL_IN_ONE.py'
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), CORE_SHA256)

    def test_docker_build_context_includes_every_copied_path(self):
        """DOCKERCTX-001: .dockerignore is deny-all ('*') plus an allow-list, so
        any path the Dockerfile COPYs must be re-included or the image build
        dies with 'failed to compute cache key: ... not found'. RELEASE_MODE,
        RELEASE_VERSION and freqtrade/user_data/strategies were missing, which
        broke the artifact and integration-simulation jobs on the first CI run
        that ever built the image. Checked statically, so it needs no Docker."""
        import re as _re
        ignore = (ROOT / '.dockerignore').read_text(encoding='utf-8').splitlines()
        allowed = {line[1:].strip().rstrip('/').replace('/**', '')
                   for line in ignore if line.startswith('!')}
        sources = []
        for line in (ROOT / 'Dockerfile.services').read_text(encoding='utf-8').splitlines():
            match = _re.match(r'^COPY\s+(?:--\S+\s+)*(.+)$', line.strip())
            if match:
                parts = match.group(1).split()
                sources.extend(parts[:-1])       # the last token is the destination
        self.assertTrue(sources, 'no COPY instructions parsed from Dockerfile.services')
        missing = [src for src in sorted(set(sources))
                   if not any(src == a or src.startswith(a + '/') for a in allowed)]
        self.assertEqual(
            missing, [],
            msg='Dockerfile.services COPYs paths that .dockerignore excludes; '
                'add a matching "!" line for each')
        for src in sorted(set(sources)):
            self.assertTrue((ROOT / src).exists(),
                            msg=f'Dockerfile.services COPYs {src}, absent from the tree')

    def test_signal_strategy_methods_are_unchanged(self):
        path = ROOT / 'freqtrade/user_data/strategies/IctSmcStrategy.py'
        for method, expected in STRATEGY_SOURCE_SHA256.items():
            self.assertEqual(source_hash(path, 'IctSmcStrategy', method), expected, method)
        for method, expected in STRATEGY_TOKEN_SHA256.items():
            self.assertEqual(token_hash(path, 'IctSmcStrategy', method), expected, method)

    def test_freqtrade_is_signal_only(self):
        path = ROOT / 'freqtrade/user_data/strategies/IctSmcStrategy.py'
        tree = ast.parse(path.read_text(encoding='utf-8'))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'IctSmcStrategy')
        fn = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'confirm_trade_entry')
        returns = [n for n in ast.walk(fn) if isinstance(n, ast.Return)]
        self.assertTrue(returns)
        self.assertTrue(all(isinstance(r.value, ast.Constant) and r.value.value is False for r in returns))

    def test_freqtrade_config_uses_dynamic_top50_and_never_bnb(self):
        cfg = json.loads((ROOT / 'freqtrade/user_data/config.json').read_text())
        self.assertTrue(cfg['dry_run'])
        self.assertEqual(cfg['pairlists'][0]['method'], 'RemotePairList')
        self.assertEqual(cfg['pairlists'][0]['number_assets'], 50)
        self.assertIn('BNB/USDT', cfg['exchange']['pair_blacklist'])
        self.assertNotIn('BNB/USDT', cfg['exchange']['pair_whitelist'])
        self.assertTrue(cfg['api_server']['enabled'])

    def test_break_even_and_profit_lock_use_fixed_oco(self):
        source = (ROOT / 'services/execution_sidecar/main.py').read_text(encoding='utf-8')
        self.assertIn("ProtectionMode.FIXED_OCO, break_even=True", source)
        self.assertIn("_safe_spot_symbol(args['symbol']), ProtectionMode.FIXED_OCO,", source)

    def test_compose_is_persistent_and_secrets_are_least_privilege(self):
        import yaml
        compose = yaml.safe_load((ROOT / 'docker-compose.yml').read_text(encoding='utf-8'))
        services = compose['services']
        self.assertTrue(all('env_file' not in service for service in services.values()))
        self.assertIn('BINANCE_API_SECRET', services['execution-sidecar']['environment'])
        for name in ('universe', 'freqtrade', 'telegram-broker'):
            self.assertNotIn('BINANCE_API_SECRET', services[name].get('environment', {}))
        self.assertIn('TELEGRAM_BOT_TOKEN', services['telegram-broker']['environment'])
        for name in ('universe', 'freqtrade', 'execution-sidecar'):
            self.assertNotIn('TELEGRAM_BOT_TOKEN', services[name].get('environment', {}))
        cfg = json.loads((ROOT / 'freqtrade/user_data/config.json').read_text())
        self.assertIn('/freqtrade/shared/freqtrade/', cfg['db_url'])
        self.assertNotIn('env_file', (ROOT / 'docker-compose.yml').read_text())
        dockerignore = (ROOT / '.dockerignore').read_text()
        self.assertTrue(dockerignore.startswith('*\n'))


class ProtectionRequestTests(unittest.TestCase):
    def setUp(self):
        self.factory = OrderRequestFactory(FakeLegacy)
        self.args = (SYM, Decimal('100'), Decimal('0.50'), Decimal('0.54'), 120)

    def test_fixed_oco_entry(self):
        endpoint, p = self.factory.entry(ProtectionMode.FIXED_OCO, *self.args)
        self.assertEqual(endpoint, 'orderList/otoco')
        self.assertIn('pendingBelowStopPrice', p)
        self.assertNotIn('pendingBelowTrailingDelta', p)

    def test_trailing_only_entry(self):
        endpoint, p = self.factory.entry(ProtectionMode.TRAILING_ONLY, *self.args)
        self.assertEqual(endpoint, 'orderList/oto')
        self.assertEqual(p['pendingType'], 'STOP_LOSS_LIMIT')
        self.assertIn('pendingTrailingDelta', p)
        self.assertNotIn('pendingAbovePrice', p)

    def test_oco_trailing_entry(self):
        endpoint, p = self.factory.entry(ProtectionMode.OCO_TRAILING, *self.args)
        self.assertEqual(endpoint, 'orderList/otoco')
        self.assertIn('pendingBelowTrailingDelta', p)
        self.assertNotIn('pendingBelowStopPrice', p)

    def test_existing_fixed_oco_tp_is_above_current(self):
        endpoint, p = self.factory.existing(
            ProtectionMode.FIXED_OCO, SYM, Decimal('100'), Decimal('0.50'), Decimal('0.60'),
            4.0, 2.0, 100
        )
        self.assertEqual(endpoint, 'orderList/oco')
        self.assertGreater(Decimal(p['abovePrice']), Decimal('0.60'))
        self.assertLess(Decimal(p['belowStopPrice']), Decimal('0.60'))


class ShariaTests(unittest.TestCase):
    def test_only_current_halal_passes(self):
        # V19.1: only GREEN / GREEN_AVOID_OPTIONAL are trade-eligible; the
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
            # Valid V19.1 wrapper so the inner duplicate/source checks are exercised.
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


class PersistenceAndRiskTests(unittest.TestCase):
    def test_state_store_deduplicates_events_and_signals(self):
        with tempfile.TemporaryDirectory() as td:
            store = StateStore(Path(td) / 'state.json', Path(td) / 'state.sqlite')
            sig = {'signal_id': 's1', 'pair': 'ETH/USDT', 'candle_time': '2026-07-15T00:00:00+00:00'}
            store.record_signal(sig, 'simulated')
            self.assertTrue(store.signal_seen('s1'))
            event = {'e': 'executionReport', 'I': 77, 's': 'ETHUSDT', 'i': 5, 'N': 'USDT', 'n': '0.01', 'E': 1}
            self.assertTrue(store.record_exchange_event(event))
            self.assertFalse(store.record_exchange_event(event))

    def test_exchange_events_advance_and_backfill_trade_record(self):
        with tempfile.TemporaryDirectory() as td:
            store = StateStore(Path(td) / 'state.json', Path(td) / 'state.sqlite')
            store.upsert_trade('sig-1', 'ETH/USDT', lifecycle_state=LifecycleState.ENTRY_SUBMITTED.value)

            # Simulate a user-stream fill arriving before the REST response has
            # been mirrored into the local position row.
            early = {
                'e': 'executionReport', 'I': 101, 's': 'ETHUSDT', 'i': 500,
                'g': 900, 'S': 'BUY', 'X': 'FILLED', 'o': 'LIMIT',
                'z': '1.0', 'Z': '2500.0', 'N': 'ETH', 'n': '0.001', 'E': 1000,
            }
            self.assertTrue(store.record_exchange_event(early))
            self.assertFalse(store.record_exchange_event(early))

            position = SimpleNamespace(
                entry_order_id=500, order_list_id=900, tp_order_id=501,
                sl_order_id=502, exit_order_id=None, entry_client_id='entry-1',
                state=SimpleNamespace(name='ARMED_TRAIL'), filled_qty=Decimal('1.0'),
                entry_price=Decimal('2500'), trail_delta=100,
            )
            store.mirror_legacy_position('ETHUSDT', position)
            with store._connect() as con:
                rows = con.execute('SELECT * FROM trade_records').fetchall()
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row['trade_id'], 'sig-1')
            self.assertEqual(row['entry_order_id'], 500)
            self.assertEqual(row['commission_asset'], 'ETH')
            self.assertEqual(row['last_exchange_event_id'], '101')
            self.assertEqual(row['lifecycle_state'], LifecycleState.TRAILING_ACTIVE.value)

            stop_fill = {
                'e': 'executionReport', 'I': 102, 's': 'ETHUSDT', 'i': 502,
                'g': 900, 'S': 'SELL', 'X': 'FILLED', 'o': 'STOP_LOSS_LIMIT',
                'z': '1.0', 'Z': '2450.0', 'N': 'USDT', 'n': '2.45', 'E': 2000,
            }
            self.assertTrue(store.record_exchange_event(stop_fill))
            with store._connect() as con:
                row = con.execute('SELECT * FROM trade_records WHERE trade_id=?', ('sig-1',)).fetchone()
            self.assertEqual(row['lifecycle_state'], LifecycleState.EXIT_FILLED.value)
            self.assertEqual(row['commission_asset'], 'USDT')
            self.assertEqual(row['last_exchange_event_id'], '102')

    def test_periodic_mirror_preserves_advanced_lifecycle_mode_and_quantity(self):
        with tempfile.TemporaryDirectory() as td:
            store = StateStore(Path(td) / 'state.json', Path(td) / 'state.sqlite')
            store.upsert_trade(
                'sig-advanced', 'ADA/USDT',
                lifecycle_state=LifecycleState.PROFIT_LOCKED.value,
                protection_mode=ProtectionMode.FIXED_OCO.value,
                protected_quantity='99.9',
            )
            position = SimpleNamespace(
                entry_order_id=700, order_list_id=701, tp_order_id=702,
                sl_order_id=703, exit_order_id=None, entry_client_id='entry-advanced',
                state=SimpleNamespace(name='ARMED'), filled_qty=Decimal('100.0'),
                entry_price=Decimal('0.50'), trail_delta=100, bracket=True, uncapped=False,
            )
            store.mirror_legacy_position('ADAUSDT', position)
            with store._connect() as con:
                row = con.execute(
                    'SELECT * FROM trade_records WHERE trade_id=?', ('sig-advanced',)
                ).fetchone()
            self.assertEqual(row['lifecycle_state'], LifecycleState.PROFIT_LOCKED.value)
            self.assertEqual(row['protection_mode'], ProtectionMode.FIXED_OCO.value)
            self.assertEqual(row['protected_quantity'], '99.9')

    def test_fresh_signal_and_stopout_rules(self):
        with tempfile.TemporaryDirectory() as td:
            os.environ['PAIR_COOLDOWN_SECONDS'] = '0'
            guard = FreshSignalGuard(Path(td) / 'risk.json')
            now = datetime.now(timezone.utc) - timedelta(minutes=1)
            sig = {
                'signal_id': 'a', 'pair': 'ETH/USDT', 'candle_time': now.isoformat(),
                'generated_at': datetime.now(timezone.utc).isoformat(), 'universe_hash': 'h'
            }
            self.assertTrue(guard.allow(sig, {'ETH/USDT'}, 'h')[0])
            guard.record(sig, 'simulated')
            self.assertFalse(guard.allow(sig, {'ETH/USDT'}, 'h')[0])
            guard.record_stopout('ETHUSDT', now.isoformat())
            newer = dict(sig, signal_id='b', candle_time=(now + timedelta(minutes=1)).isoformat(),
                         generated_at=datetime.now(timezone.utc).isoformat())
            self.assertTrue(guard.allow(newer, {'ETH/USDT'}, 'h')[0])
            stale = dict(newer, signal_id='c', candle_time=(now - timedelta(minutes=10)).isoformat())
            self.assertEqual(guard.allow(stale, {'ETH/USDT'}, 'h')[1], 'stale signal candle')


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


class SimulationIntegrationTests(unittest.TestCase):
    def test_sidecar_processes_signal_without_network_or_order(self):
        if (ROOT / 'RELEASE_MODE').read_text(encoding='utf-8').strip() == 'live':
            self.skipTest(
                'cached offline simulation is testnet-only; live cached-gate rejection is tested separately'
            )
        with tempfile.TemporaryDirectory() as td:
            shared = Path(td) / 'shared'
            for rel in ['signals/inbox','signals/processed','signals/rejected','universe','runtime','audit','sharia','commands/inbox']:
                (shared / rel).mkdir(parents=True, exist_ok=True)
            # V19.1: use the same controller/report/Ed25519-bound projection
            # required by the runtime gate. Only the screener writes this in
            # production; the harness creates it for this offline simulation.
            _harness.write_attested_status(
                shared / 'sharia/sharia_status.json', [('ETH', 'GREEN')]
            )
            (shared / 'sharia/halal_coins.json').write_text(json.dumps({'symbols':['ETHUSDT']}))
            now = datetime.now(timezone.utc)
            universe = store_snapshot(
                shared / 'universe',
                [{'pair': 'ETH/USDT', 'rank': 1}],
                {'limit': 1, 'source': 'offline-simulation'},
                900,
            )
            (shared / 'runtime/fresh_signal_guard.json').write_text(json.dumps({
                'pairs': {}, 'daily': {}, 'global_pause': 'stale-or-missing-universe'
            }))
            env = os.environ.copy()
            env.update({
                'PYTHONPATH': str(ROOT), 'SHARED_ROOT': str(shared), 'EXECUTION_MODE':'simulation',
                # Override path variables that other collected test modules
                # intentionally set at import time. The subprocess must be
                # entirely bound to this disposable integration root.
                'RUNTIME_DIR': str(shared/'runtime'),
                'LEGACY_RUNTIME_DIR': str(shared/'legacy_runtime'),
                'SHARIA_FILE': str(shared/'sharia/sharia_status.json'),
                'LEGACY_HALAL_FILE': str(shared/'sharia/halal_coins.json'),
                'UNIVERSE_FILE': str(shared/'universe/current_pairlist.json'),
                'SIGNAL_INBOX': str(shared/'signals/inbox'),
                'AUDIT_LOG': str(shared/'audit/events.jsonl'),
                # Signed buses + cached V19.1 gate (fresh-screening seam covered
                # by dedicated tests) for a deterministic offline integration run.
                'SHARIA_SIGNAL_GATE_MODE': 'cached',
                **_harness.TEST_BUS_KEYS,
            })
            proc = subprocess.Popen([sys.executable, '-m', 'services.execution_sidecar.main'], cwd=ROOT, env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                health = shared / 'runtime/sidecar_health.json'
                # Windows CI and heavily loaded audit hosts can take longer to
                # import the preserved all-in-one core. This is a startup
                # allowance, not a network wait; simulation remains offline.
                deadline = time.time() + 45
                while time.time() < deadline and not health.exists(): time.sleep(0.1)
                self.assertTrue(health.exists(), proc.stderr.read() if proc.poll() is not None else 'health timeout')
                self.assertTrue((shared / 'legacy_runtime/logs').is_dir())
                self.assertTrue((shared / 'legacy_runtime/data').is_dir())
                cid = 'cmd1'
                (shared / f'commands/inbox/{cid}.json').write_text(json.dumps(
                    _harness.sign_command('entries', {'enabled': True}, command_id=cid)))
                result = shared / f'runtime/command_result_{cid}.json'
                deadline = time.time() + 10
                while time.time() < deadline and not result.exists(): time.sleep(0.1)
                self.assertTrue(result.exists())
                self.assertTrue(json.loads(result.read_text())['ok'])
                guard_state = json.loads((shared / 'runtime/fresh_signal_guard.json').read_text())
                self.assertEqual(guard_state.get('global_pause'), '')
                signal = {
                    'signal_id':'sim1','pair':'ETH/USDT','symbol':'ETHUSDT',
                    'candle_time':now.isoformat(),'generated_at':datetime.now(timezone.utc).isoformat(),
                    'strategy':'IctSmcStrategy','entry_tag':'test',
                    'universe_hash':universe['snapshot_hash'],
                    'sharia_status':'GREEN','payload':{}
                }
                (shared / 'signals/inbox/sim1.json').write_text(json.dumps(_harness.sign_signal(signal)))
                processed = shared / 'signals/processed/sim1.json'
                deadline = time.time() + 10
                while time.time() < deadline and not processed.exists(): time.sleep(0.1)
                self.assertTrue(processed.exists())
                self.assertFalse((shared / 'signals/rejected/sim1.json').exists())

                # A pair/symbol mismatch must be rejected before it can reach
                # the preserved execution engine. This explicitly blocks a
                # forged BNB symbol behind an otherwise eligible pair.
                bad = dict(signal, signal_id='sim-bad-symbol', symbol='BNBUSDT',
                           generated_at=datetime.now(timezone.utc).isoformat())
                (shared / 'signals/inbox/sim-bad-symbol.json').write_text(json.dumps(_harness.sign_signal(bad)))
                rejected = shared / 'signals/rejected/sim-bad-symbol.json'
                deadline = time.time() + 10
                while time.time() < deadline and not rejected.exists(): time.sleep(0.1)
                self.assertTrue(rejected.exists())
                self.assertFalse((shared / 'signals/processed/sim-bad-symbol.json').exists())

                stale_id = 'stale-command'
                (shared / f'commands/inbox/{stale_id}.json').write_text(json.dumps(
                    _harness.sign_command('mode', {'mode': 'FIXED_OCO'}, command_id=stale_id,
                                          created_at=time.time() - 999, ttl=2000)))
                stale_result = shared / f'runtime/command_result_{stale_id}.json'
                deadline = time.time() + 10
                while time.time() < deadline and not stale_result.exists(): time.sleep(0.1)
                self.assertTrue(stale_result.exists())
                self.assertFalse(json.loads(stale_result.read_text())['ok'])
            finally:
                proc.terminate()
                try:
                    proc.communicate(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.communicate(timeout=3)


if __name__ == '__main__':
    unittest.main(verbosity=2)
