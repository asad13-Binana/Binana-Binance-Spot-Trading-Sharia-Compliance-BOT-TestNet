from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent))  # make _harness importable in any run mode
_sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _harness  # sets bus keys + release binding; import before service modules

_TEST_SHARED = Path(tempfile.mkdtemp(prefix='v81_safety_import_'))
_OLD_ENV = {key: os.environ.get(key) for key in ('SHARED_ROOT', 'LEGACY_RUNTIME_DIR', 'AUDIT_LOG')}
os.environ['SHARED_ROOT'] = str(_TEST_SHARED)
os.environ['LEGACY_RUNTIME_DIR'] = str(_TEST_SHARED / 'legacy_runtime')
os.environ['AUDIT_LOG'] = str(_TEST_SHARED / 'audit/events.jsonl')
_ORIGINAL_CWD = Path.cwd()

from services.common.audit import audit
from services.execution_sidecar.core_adapter import CoreAdapter
from services.execution_sidecar.main import process_command, _disarm_execution
from services.execution_sidecar.order_manager import OrderManager
from services.execution_sidecar.risk_checks import FreshSignalGuard
from services.execution_sidecar.state_store import StateStore
from services.telegram_broker import bot

os.chdir(_ORIGINAL_CWD)
for _key, _value in _OLD_ENV.items():
    if _value is None:
        os.environ.pop(_key, None)
    else:
        os.environ[_key] = _value


class _Trader:
    def __init__(self, running=True):
        self._running = running

    def is_running(self):
        return self._running


class _CommandAdapter:
    def __init__(self, *, crash=False):
        self.trader = _Trader(True)
        self.crash = crash
        self.emergency_calls = 0
        self.enabled = []

    def set_enabled(self, value):
        self.enabled.append(bool(value))
        return 'ON' if value else 'OFF'

    def emergency_exit(self, symbol):
        self.emergency_calls += 1
        if self.crash:
            raise SystemExit('simulated process death after exchange action')
        return f'exit {symbol}'


class _SignalAdapter:
    def __init__(self, *, crash=False):
        self.crash = crash
        self.submit_calls = 0

    def submit(self, symbol, reason):
        self.submit_calls += 1
        if self.crash:
            raise SystemExit('simulated process death after accepted submission')
        return True, 'accepted'

    def mirror_positions(self, status):
        return 0


class DurableClaimTests(unittest.TestCase):
    def _store_and_guard(self, root: Path):
        store = StateStore(root / 'state.json', root / 'state.sqlite')
        store.data['simulation'] = False
        store.set_entries(True)
        guard = FreshSignalGuard(root / 'risk.json')
        return store, guard

    def test_dangerous_command_is_claimed_before_side_effect_and_not_replayed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store, guard = self._store_and_guard(root)
            command_id = 'cmd_crash_001'
            command = root / f'{command_id}.json'
            command.write_text(json.dumps(_harness.sign_command(
                'emergency_exit', {'symbol': 'ETHUSDT'}, command_id=command_id)))
            first = _CommandAdapter(crash=True)
            with mock.patch('services.execution_sidecar.main.RUNTIME', root / 'runtime'):
                with self.assertRaises(SystemExit):
                    process_command(first, store, guard, command)
                self.assertEqual(first.emergency_calls, 1)
                self.assertEqual(store.command_result(command_id), 'IN_PROGRESS')

                second = _CommandAdapter(crash=False)
                process_command(second, store, guard, command)
                self.assertEqual(second.emergency_calls, 0)
                result = json.loads((root / 'runtime' / f'command_result_{command_id}.json').read_text())
                self.assertFalse(result['ok'])
                self.assertIn('uncertain', result['result'])
                self.assertFalse(store.entries())


    def test_string_false_cannot_enable_entries(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = StateStore(root / 'state.json', root / 'state.sqlite')
            guard = FreshSignalGuard(root / 'risk.json')
            command_id = 'strict_bool_001'
            command = root / 'command.json'
            command.write_text(json.dumps(_harness.sign_command(
                'entries', {'enabled': 'false'}, command_id=command_id)))
            adapter = _CommandAdapter()
            with mock.patch('services.execution_sidecar.main.RUNTIME', root / 'runtime'):
                process_command(adapter, store, guard, command)
            payload = json.loads((root / 'runtime' / f'command_result_{command_id}.json').read_text())
            self.assertFalse(payload['ok'])
            self.assertIn('JSON boolean', payload['result'])
            self.assertFalse(store.entries())

    def test_core_disarm_is_attempted_even_when_state_persistence_fails(self):
        class BrokenState:
            data = {'simulation': False}
            def set_entries(self, value, reason):
                raise OSError('disk full')

        adapter = _CommandAdapter()
        with self.assertRaises(RuntimeError):
            _disarm_execution(adapter, BrokenState(), 'test')
        self.assertEqual(adapter.enabled, [False])

    def test_entry_signal_is_claimed_before_submit_and_not_replayed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store, guard = self._store_and_guard(root)
            sharia = root / 'sharia.json'
            _harness.write_attested_status(sharia, [('ETH', 'GREEN')])
            inbox = root / 'inbox'; processed = root / 'processed'; rejected = root / 'rejected'
            for folder in (inbox, processed, rejected):
                folder.mkdir()
            now = datetime.now(timezone.utc)
            signal = {
                'signal_id': 'signal_crash_001', 'pair': 'ETH/USDT', 'symbol': 'ETHUSDT',
                'candle_time': now.isoformat(), 'generated_at': now.isoformat(),
                'strategy': 'IctSmcStrategy', 'universe_hash': 'hash-1',
                'sharia_status': 'GREEN',
            }
            path = inbox / 'signal.json'
            path.write_text(json.dumps(_harness.sign_signal(signal)))
            # cached gate: durable claim/submit path without the fresh-screening seam.
            os.environ['SHARIA_SIGNAL_GATE_MODE'] = 'cached'
            self.addCleanup(os.environ.pop, 'SHARIA_SIGNAL_GATE_MODE', None)
            mode_patch = mock.patch(
                'services.execution_sidecar.order_manager.load_package_mode',
                return_value='testnet')
            mode_patch.start()
            self.addCleanup(mode_patch.stop)
            first = _SignalAdapter(crash=True)
            manager = OrderManager(first, store, guard, store, sharia,
                                   {'processed': processed, 'rejected': rejected})
            with self.assertRaises(SystemExit):
                manager.process_signal(path, {'ETH/USDT'}, 'hash-1')
            self.assertEqual(first.submit_calls, 1)
            self.assertEqual(store.signal_result(signal['signal_id']), 'IN_PROGRESS')

            second = _SignalAdapter(crash=False)
            manager = OrderManager(second, store, guard, store, sharia,
                                   {'processed': processed, 'rejected': rejected})
            ok, reason = manager.process_signal(path, {'ETH/USDT'}, 'hash-1')
            self.assertFalse(ok)
            self.assertEqual(second.submit_calls, 0)
            self.assertIn('uncertain', reason)
            self.assertFalse(store.entries())
            self.assertTrue(any(rejected.glob('*.json')))


class EventAndInputSafetyTests(unittest.TestCase):
    def test_duplicate_exchange_event_does_not_double_count_stopout_sink(self):
        class Trader:
            def __init__(self):
                self.order_calls = 0
                self.list_calls = 0

            def _uds_order(self, report):
                self.order_calls += 1

            def _uds_list(self, report):
                self.list_calls += 1

        class Store:
            def __init__(self):
                self.calls = 0

            def record_exchange_event(self, report):
                self.calls += 1
                return self.calls == 1

        adapter = CoreAdapter.__new__(CoreAdapter)
        adapter.trader = Trader()
        adapter.state_store = Store()
        seen = []
        adapter.event_sink = lambda event: seen.append(event)
        adapter._install_event_wrappers()
        event = {'e': 'executionReport', 'I': 1, 's': 'ETHUSDT', 'X': 'FILLED'}
        adapter.trader._uds_order(event)
        adapter.trader._uds_order(event)
        self.assertEqual(len(seen), 1)
        self.assertEqual(adapter.trader.order_calls, 2)

    def test_future_generated_at_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            guard = FreshSignalGuard(Path(td) / 'risk.json')
            future = datetime.now(timezone.utc) + timedelta(minutes=5)
            sig = {
                'signal_id': 'future_001', 'pair': 'ETH/USDT',
                'candle_time': datetime.now(timezone.utc).isoformat(),
                'generated_at': future.isoformat(), 'universe_hash': 'h',
            }
            ok, reason = guard.allow(sig, {'ETH/USDT'}, 'h')
            self.assertFalse(ok)
            self.assertIn('future', reason)

    def test_invalid_command_id_cannot_escape_runtime_directory(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = StateStore(root / 'state.json', root / 'state.sqlite')
            guard = FreshSignalGuard(root / 'risk.json')
            command = root / 'malicious.json'
            command.write_text(json.dumps({
                'command_id': '../../outside', 'command': 'status', 'args': {},
                'created_at': time.time(),
            }))
            adapter = SimpleNamespace(trader=_Trader(False))
            with mock.patch('services.execution_sidecar.main.RUNTIME', root / 'runtime'):
                process_command(adapter, store, guard, command)
            self.assertFalse((root.parent / 'outside.json').exists())
            self.assertFalse(command.exists())

    def test_audit_write_failure_does_not_raise(self):
        # Cross-platform unwritable target: a regular FILE stands where a parent
        # directory is required, so the audit's mkdir raises OSError on any OS
        # (the previous '/proc/...' path is writable as D:\proc\... on Windows).
        with tempfile.TemporaryDirectory() as td:
            blocker = Path(td) / 'blocker'
            blocker.write_text('x')
            target = blocker / 'sub' / 'events.jsonl'
            row = audit('expected-write-failure', path=str(target))
        self.assertIn('audit_write_error', row)

    def test_runtime_dependency_matches_preserved_core(self):
        root = Path(__file__).resolve().parents[1]
        service = next(line for line in (root / 'requirements.services.txt').read_text().splitlines()
                       if line.startswith('python-binance=='))
        legacy = next(line for line in (root / 'legacy_core/requirements.txt').read_text().splitlines()
                      if line.startswith('python-binance=='))
        self.assertEqual(service, legacy)


class DeploymentGateTests(unittest.TestCase):
    def test_oracle_setup_hard_fails_before_install_on_1gib_host(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as td:
            meminfo = Path(td) / 'meminfo'
            meminfo.write_text('MemTotal:        1048576 kB\nSwapTotal:             0 kB\n')
            env = os.environ.copy()
            env['MEMINFO_PATH'] = str(meminfo)
            # Resolve a real POSIX bash (the bare 'bash' name is the WSL stub on
            # a Windows host without WSL); the deployment guard is genuinely run.
            bash = _harness.posix_bash()
            if not bash:
                self.skipTest('no POSIX bash available to execute the deployment shell guard')
            proc = __import__('subprocess').run(
                [bash, str(root / 'deploy/oracle_setup.sh')],
                cwd=root, env=env, text=True, capture_output=True, timeout=15,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn('unsupported', proc.stderr.lower())
            self.assertNotIn('apt-get', proc.stdout + proc.stderr)


class TelegramFailClosedOrderingTests(unittest.TestCase):
    def test_pause_disarms_execution_sidecar_before_freqtrade(self):
        calls = []
        with mock.patch.object(bot, 'sidecar_command', side_effect=lambda *a, **k: calls.append('sidecar') or {'ok': True}), \
             mock.patch.object(bot, 'ft_call', side_effect=lambda *a, **k: calls.append('freqtrade') or {'ok': True}), \
             mock.patch.object(bot, 'send'):
            bot.route('entries_off', '123')
        self.assertEqual(calls, ['sidecar', 'freqtrade'])

    def test_nan_trade_size_is_rejected_before_confirmation(self):
        message = {'from': {'id': 1}, 'chat': {'id': 1, 'type': 'private'}, 'text': '/setsize nan'}
        with mock.patch.object(bot, 'is_owner', return_value=True), \
             mock.patch.object(bot, '_ask_confirm') as confirm, \
             mock.patch.object(bot, 'send') as send:
            bot.handle_message(message)
        confirm.assert_not_called()
        self.assertIn('finite', send.call_args.args[0])

    def test_nan_profit_lock_is_rejected_before_confirmation(self):
        message = {'from': {'id': 1}, 'chat': {'id': 1, 'type': 'private'}, 'text': '/lockprofit ETHUSDT nan'}
        with mock.patch.object(bot, 'is_owner', return_value=True), \
             mock.patch.object(bot, '_ask_confirm') as confirm, \
             mock.patch.object(bot, 'send') as send:
            bot.handle_message(message)
        confirm.assert_not_called()
        self.assertIn('finite', send.call_args.args[0])


if __name__ == '__main__':
    unittest.main(verbosity=2)


class ProtectionConversionSafetyTests(unittest.TestCase):
    @staticmethod
    def _fixture(*, min_notional='5', fail_after_accept=False):
        import threading
        from decimal import Decimal
        from services.common.models import ProtectionMode

        sym = SimpleNamespace(
            symbol='ETHUSDT', base='ETH', quote='USDT', step='0.001', tick='0.01',
            min_notional=min_notional, trail_min=10, trail_max=2000,
            min_qty='0.001', max_qty='1000',
        )
        position = SimpleNamespace(
            sym=sym, entry_price=Decimal('8'), filled_qty=Decimal('1'), trail_delta=100,
            order_list_id=111, exit_order_id=None, tp_order_id=112, sl_order_id=113,
            list_client_id='old-list', bracket=True, uncapped=False,
            replacing_protection=False,
        )

        class Portfolio:
            def __init__(self):
                self.positions = {'ETHUSDT': position}
                self.lock = threading.RLock()
                self.autotrade_on = True
                self.protection_halt = ''
                self.save_calls = 0

            def save(self):
                self.save_calls += 1
                if fail_after_accept and self.save_calls >= 2:
                    raise OSError('simulated persistence failure after accepted replacement')

        class Client:
            def _post(self, endpoint, signed, data):
                return {
                    'orderListId': 222, 'listClientOrderId': data['listClientOrderId'],
                    'orderReports': [],
                }

        class Broker:
            def __init__(self):
                self.c = Client()
                self.cancel_calls = 0

            def price(self, symbol):
                return Decimal('10')

            def free(self, base):
                return Decimal('1')

            def _check_lot(self, symbol, qty):
                if qty < Decimal(symbol.min_qty):
                    raise ValueError('below min qty')

            def cancel_order_list(self, symbol, order_list_id):
                self.cancel_calls += 1
                return {'orderListId': order_list_id}

            def cancel(self, symbol, order_id):
                self.cancel_calls += 1
                return {'orderId': order_id}

            def invalidate_balance_cache(self):
                return None

            def _sync_weight(self):
                return None

            def _find_list(self, coid):
                return None

            def _find_order(self, symbol, coid):
                return None

            def _place_idempotent(self, send, lookup, what):
                return send()

            def parse_otoco_ids(self, out):
                return None, 223, 224

        class Factory:
            def existing(self, mode, symbol, qty, entry, current, tp_pct, stop_pct, trail_bips):
                return 'orderList/oco', {
                    'symbol': symbol.symbol, 'side': 'SELL', 'quantity': str(qty),
                    'listClientOrderId': 'replacement-001',
                    'aboveType': 'LIMIT_MAKER', 'abovePrice': '12.00',
                    'belowType': 'STOP_LOSS_LIMIT', 'belowPrice': '7.80',
                    'belowStopPrice': '7.84', 'belowTimeInForce': 'GTC',
                    'newOrderRespType': 'FULL',
                }

        class Exit:
            def __init__(self):
                self.reprotect_calls = 0

            def reprotect_if_naked(self, pos, current):
                self.reprotect_calls += 1

        pf = Portfolio()
        broker = Broker()
        exit_engine = Exit()
        trader = SimpleNamespace(
            pf=pf, broker=broker, exit=exit_engine, _uds_resync_calls=0,
        )

        def resync():
            trader._uds_resync_calls += 1

        trader._uds_resync = resync
        class StubFilterValidator:
            # V101-NEW-002 preflight is exercised in its own dedicated tests;
            # here it is a no-op so the conversion unit test stays offline.
            def validate_replacement(self, *a, **k):
                return {}

        adapter = CoreAdapter.__new__(CoreAdapter)
        adapter.trader = trader
        adapter.factory = Factory()
        adapter.state_store = None
        adapter.filter_validator = StubFilterValidator()
        adapter.mirror_positions = lambda status: 0
        return adapter, trader, position, ProtectionMode

    def test_replacement_filters_are_checked_before_active_order_is_cancelled(self):
        adapter, trader, _position, ProtectionMode = self._fixture(min_notional='50')
        with self.assertRaisesRegex(ValueError, 'notional'):
            adapter.convert('ETHUSDT', ProtectionMode.FIXED_OCO)
        self.assertEqual(trader.broker.cancel_calls, 0)
        self.assertEqual(trader.pf.save_calls, 0)

    def test_accepted_replacement_persistence_failure_does_not_blindly_reprotect(self):
        adapter, trader, position, ProtectionMode = self._fixture(fail_after_accept=True)
        ok, message = adapter.convert('ETHUSDT', ProtectionMode.FIXED_OCO)
        self.assertFalse(ok)
        self.assertIn('accepted', message)
        self.assertEqual(trader.broker.cancel_calls, 1)
        self.assertEqual(trader.exit.reprotect_calls, 0)
        self.assertEqual(trader._uds_resync_calls, 1)
        self.assertTrue(position.replacing_protection)


class BinanceListStatusSemanticsTests(unittest.TestCase):
    def _store_with_trade(self, root: Path):
        store = StateStore(root / 'state.json', root / 'state.sqlite')
        store.upsert_trade(
            'trade-1', 'ETH/USDT', lifecycle_state='ENTRY_FILLED',
            order_list_id=9001, protection_mode='FIXED_OCO',
            reconciliation_status='BEFORE_EVENT',
        )
        return store

    @staticmethod
    def _row(store):
        with store._connect() as con:
            return dict(con.execute(
                "SELECT lifecycle_state,reconciliation_status FROM trade_records WHERE trade_id='trade-1'"
            ).fetchone())

    def test_all_done_is_terminal_and_requires_reconciliation_not_active_protection(self):
        with tempfile.TemporaryDirectory() as td:
            store = self._store_with_trade(Path(td))
            store.record_exchange_event({
                'e': 'listStatus', 'E': 1000, 's': 'ETHUSDT', 'g': 9001,
                'l': 'ALL_DONE', 'L': 'ALL_DONE', 'r': 'NONE', 'C': 'list-1',
            })
            row = self._row(store)
            self.assertEqual(row['lifecycle_state'], 'ENTRY_FILLED')
            self.assertEqual(row['reconciliation_status'], 'ORDER_LIST_TERMINAL_RECONCILE_REQUIRED')

    def test_failed_list_action_does_not_falsely_mark_entire_trade_error(self):
        with tempfile.TemporaryDirectory() as td:
            store = self._store_with_trade(Path(td))
            store.record_exchange_event({
                'e': 'listStatus', 'E': 1001, 's': 'ETHUSDT', 'g': 9001,
                'l': 'RESPONSE', 'L': 'REJECT', 'r': 'UNKNOWN_ORDER', 'C': 'list-1',
            })
            row = self._row(store)
            self.assertEqual(row['lifecycle_state'], 'ENTRY_FILLED')
            self.assertEqual(row['reconciliation_status'], 'ORDER_LIST_ACTION_REJECTED_RECONCILE_REQUIRED')


class ArchivedDeploymentIsolationTests(unittest.TestCase):
    def test_legacy_pull_and_standalone_start_paths_are_fail_closed(self):
        root = Path(__file__).resolve().parents[1]
        update = (root / 'freqtrade/deploy/update.sh').read_text(encoding='utf-8')
        start = (root / 'freqtrade/scripts/start.sh').read_text(encoding='utf-8')
        self.assertIn('BLOCKED', update)
        self.assertNotIn('git pull', update)
        self.assertNotIn('docker compose up', update)
        self.assertIn('BLOCKED', start)
        self.assertNotIn('docker compose up', start)

    def test_archived_compose_is_offline_profile_and_has_no_secret_env_file(self):
        import yaml
        root = Path(__file__).resolve().parents[1]
        compose = yaml.safe_load((root / 'freqtrade/docker-compose.yml').read_text(encoding='utf-8'))
        service = compose['services']['freqtrade']
        self.assertEqual(service.get('profiles'), ['offline-audit'])
        self.assertNotIn('env_file', service)
        self.assertEqual(service.get('restart'), 'no')


class CommandOutcomeTruthfulnessTests(unittest.TestCase):
    @staticmethod
    def _run(root: Path, adapter, command: str, args=None):
        store = StateStore(root / 'state.json', root / 'state.sqlite')
        store.data['simulation'] = False
        store.set_entries(True)
        guard = FreshSignalGuard(root / 'risk.json')
        cid = 'truth_' + command
        path = root / (cid + '.json')
        path.write_text(json.dumps(_harness.sign_command(command, args or {}, command_id=cid)))
        with mock.patch('services.execution_sidecar.main.RUNTIME', root / 'runtime'):
            process_command(adapter, store, guard, path)
        return json.loads((root / 'runtime' / f'command_result_{cid}.json').read_text())

    def test_failed_emergency_exit_is_not_reported_as_success(self):
        class Adapter(_CommandAdapter):
            def emergency_exit(self, symbol):
                return 'no such open position (already closed?)'
        with tempfile.TemporaryDirectory() as td:
            result = self._run(Path(td), Adapter(), 'emergency_exit', {'symbol': 'ETHUSDT'})
            self.assertFalse(result['ok'])
            self.assertIn('no such open position', result['result'])

    def test_failed_stream_restart_is_not_reported_as_success(self):
        class Adapter(_CommandAdapter):
            def restart_user_stream(self):
                return 'restart failed: websocket unavailable'
        with tempfile.TemporaryDirectory() as td:
            result = self._run(Path(td), Adapter(), 'restart_stream')
            self.assertFalse(result['ok'])
            self.assertIn('restart failed', result['result'])


class SecretScannerRegressionTests(unittest.TestCase):
    def test_sensitive_assignment_is_detected_even_when_variable_name_contains_api_key(self):
        import importlib.util
        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location('release_secret_scan', root / 'tests/secret_scan.py')
        module = importlib.util.module_from_spec(spec)
        assert spec.loader
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as td:
            test_root = Path(td)
            (test_root / '.env').write_text('BINANCE_API_KEY=REALISTIC_NON_PLACEHOLDER_VALUE_123456789\n')
            findings = module.scan(test_root)
            self.assertTrue(any('BINANCE_API_KEY' in item for item in findings))

    def test_blank_and_explicit_placeholders_are_allowed(self):
        import importlib.util
        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location('release_secret_scan_placeholders', root / 'tests/secret_scan.py')
        module = importlib.util.module_from_spec(spec)
        assert spec.loader
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as td:
            test_root = Path(td)
            (test_root / '.env.example').write_text(
                'BINANCE_API_KEY=\nTELEGRAM_BOT_TOKEN=REPLACE_WITH_REAL_TELEGRAM_TOKEN\n'
            )
            self.assertEqual(module.scan(test_root), [])


class DeterministicReleaseManifestTests(unittest.TestCase):
    def test_manifest_and_release_hash_are_stable_for_unchanged_tree(self):
        import subprocess
        source = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'release'
            shutil.copytree(source, root, ignore=shutil.ignore_patterns(
                '.git', '__pycache__', '.pytest_cache', '.ruff_cache',
                'RELEASE_MANIFEST.json', 'RELEASE_SHA256.txt',
            ))
            subprocess.run([_sys.executable, 'scripts/build_manifest.py'], cwd=root, check=True, capture_output=True, text=True)
            first_manifest = (root / 'RELEASE_MANIFEST.json').read_bytes()
            first_hash = (root / 'RELEASE_SHA256.txt').read_bytes()
            time.sleep(0.02)
            subprocess.run([_sys.executable, 'scripts/build_manifest.py'], cwd=root, check=True, capture_output=True, text=True)
            self.assertEqual((root / 'RELEASE_MANIFEST.json').read_bytes(), first_manifest)
            self.assertEqual((root / 'RELEASE_SHA256.txt').read_bytes(), first_hash)
        payload = json.loads(first_manifest)
        self.assertNotIn('generated_at', payload)
        # manifest_format 2: V10.1 replaced interpreter-dependent AST hashes
        # with canonical source/token strategy fingerprints (V101-001).
        self.assertEqual(payload.get('manifest_format'), 2)
        self.assertIn(payload.get('package_mode'), {'testnet', 'live'})
        # V102-REM-009: the release label derives from RELEASE_VERSION
        # metadata plus the package mode — never a hardcoded string.
        release_version = (source / 'RELEASE_VERSION').read_text(encoding='utf-8').strip()
        self.assertEqual(
            payload.get('release'),
            f"{release_version}-{payload['package_mode'].upper()}",
        )
        preservation = payload.get('preservation', {})
        self.assertEqual(preservation.get('strategy_signal_fingerprints'),
                         preservation.get('expected_strategy_signal_fingerprints'))


class AtomicDeploymentControlFileTests(unittest.TestCase):
    def test_installer_uses_atomic_fsynced_replacement_for_watched_json_files(self):
        root = Path(__file__).resolve().parents[1]
        installer = (root / 'deploy/install_artifact.sh').read_text(encoding='utf-8')
        self.assertGreaterEqual(installer.count('os.replace(tmp,'), 3)
        self.assertGreaterEqual(installer.count('os.fsync(handle.fileno())'), 3)
        self.assertNotIn("(inbox/f'{cid}.json').write_text", installer)
        self.assertNotIn("deployment_status.json').write_text", installer)


class DeploymentMonitoringTransactionTests(unittest.TestCase):
    def test_monitoring_activation_and_rollback_share_one_transaction(self):
        root = Path(__file__).resolve().parents[1]
        installer = (root / 'deploy/install_artifact.sh').read_text(encoding='utf-8')

        def assert_transactional(source: str):
            activation = (
                'install_monitoring_for "$DEST" "$PACKAGE_MODE" '
                '"$RELEASE_HASH" || rollback'
            )
            success_record = (
                'python3 - "$PERSIST" "$RELEASE_HASH" "$DEST" "$NEW_TAG" <<\'PY\''
            )
            self.assertIn(activation, source)
            self.assertLess(source.index(activation), source.index(success_record),
                            'DEPLOYED must not be recorded before monitoring succeeds')
            rollback_start = source.index('rollback(){')
            rollback_end = source.index(
                '\nif [[ -n "$OLD" && -d "$OLD" ]]; then\n  compose_for "$OLD"',
                rollback_start,
            )
            rollback = source[rollback_start:rollback_end]
            self.assertIn('if restore_monitoring; then', rollback)
            self.assertIn('_MONITORING_UNHEALTHY_CRITICAL', rollback)
            self.assertIn('install_monitoring_for "$OLD" "$OLD_MODE"', source)
            self.assertIn('disable_monitoring_after_failed_first_install', source)

        assert_transactional(installer)
        mutant = installer.replace('  if restore_monitoring; then\n',
                                   '  if true; then\n', 1)
        with self.assertRaises(AssertionError):
            assert_transactional(mutant)


class SizingCommandTruthfulnessTests(unittest.TestCase):
    @staticmethod
    def _run(root: Path, adapter, command: str, args: dict):
        store = StateStore(root / 'state.json', root / 'state.sqlite')
        store.data['simulation'] = False
        store.set_entries(True)
        guard = FreshSignalGuard(root / 'risk.json')
        cid = 'sizing_' + command
        path = root / f'{cid}.json'
        path.write_text(json.dumps(_harness.sign_command(command, args, command_id=cid)))
        with mock.patch('services.execution_sidecar.main.RUNTIME', root / 'runtime'):
            process_command(adapter, store, guard, path)
        return json.loads((root / 'runtime' / f'command_result_{cid}.json').read_text())

    def test_out_of_range_max_positions_is_reported_as_failure(self):
        class Adapter(_CommandAdapter):
            def set_max(self, value):
                return 'max positions 1–5'
        with tempfile.TemporaryDirectory() as td:
            result = self._run(Path(td), Adapter(), 'set_max', {'count': 6})
        self.assertFalse(result['ok'])
        self.assertIn('within 1–5', result['result'])

    def test_size_change_is_not_reported_success_when_core_is_stopped(self):
        class Adapter(_CommandAdapter):
            def __init__(self):
                super().__init__()
                self.trader = _Trader(False)
            def set_size(self, value):
                raise AssertionError('must not be called')
        with tempfile.TemporaryDirectory() as td:
            result = self._run(Path(td), Adapter(), 'set_size', {'usdt': 100})
        self.assertFalse(result['ok'])
        self.assertIn('not started', result['result'])


class ExecutionReportTerminalSemanticsTests(unittest.TestCase):
    @staticmethod
    def _row(store, trade_id='trade-x'):
        with store._connect() as con:
            return dict(con.execute(
                'SELECT lifecycle_state,reconciliation_status FROM trade_records WHERE trade_id=?',
                (trade_id,),
            ).fetchone())

    def test_rejected_protective_sell_does_not_make_open_trade_terminal(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = StateStore(root / 'state.json', root / 'state.sqlite')
            store.upsert_trade('trade-x', 'ETH/USDT', lifecycle_state='PROTECTION_ACTIVE', stop_order_id=77)
            store.record_exchange_event({
                'e': 'executionReport', 'E': 1, 'I': 1001, 's': 'ETHUSDT', 'i': 77,
                'S': 'SELL', 'o': 'STOP_LOSS_LIMIT', 'X': 'REJECTED', 'z': '0', 'q': '1',
            })
            row = self._row(store)
        self.assertEqual(row['lifecycle_state'], 'PROTECTION_ACTIVE')
        self.assertEqual(row['reconciliation_status'], 'SELL_PROTECTION_TERMINAL_RECONCILE_REQUIRED')

    def test_canceled_partial_entry_remains_an_open_partial_position(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = StateStore(root / 'state.json', root / 'state.sqlite')
            store.upsert_trade('trade-x', 'ETH/USDT', lifecycle_state='ENTRY_SUBMITTED', entry_order_id=88)
            store.record_exchange_event({
                'e': 'executionReport', 'E': 2, 'I': 1002, 's': 'ETHUSDT', 'i': 88,
                'S': 'BUY', 'o': 'LIMIT', 'X': 'CANCELED', 'z': '0.25', 'Z': '500', 'q': '1',
            })
            row = self._row(store)
        self.assertEqual(row['lifecycle_state'], 'ENTRY_PARTIALLY_FILLED')
        self.assertEqual(row['reconciliation_status'], 'ENTRY_TERMINAL_PARTIAL_FILL_RECONCILE_REQUIRED')

    def test_rejected_zero_fill_entry_is_terminal_error(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = StateStore(root / 'state.json', root / 'state.sqlite')
            store.upsert_trade('trade-x', 'ETH/USDT', lifecycle_state='ENTRY_SUBMITTED', entry_order_id=89)
            store.record_exchange_event({
                'e': 'executionReport', 'E': 3, 'I': 1003, 's': 'ETHUSDT', 'i': 89,
                'S': 'BUY', 'o': 'LIMIT', 'X': 'REJECTED', 'z': '0', 'Z': '0', 'q': '1',
            })
            row = self._row(store)
        self.assertEqual(row['lifecycle_state'], 'ERROR')
        self.assertEqual(row['reconciliation_status'], 'ENTRY_TERMINAL_NO_FILL')


def _audit_process_worker(path: str, worker: int, count: int) -> None:
    os.environ['AUDIT_LOG_MAX_BYTES'] = '2048'
    os.environ['AUDIT_LOG_BACKUPS'] = '20'
    for index in range(count):
        audit('multiprocess-audit', path=path, details={'worker': worker, 'index': index, 'padding': 'x' * 80})


class CrossProcessAuditLockTests(unittest.TestCase):
    def test_concurrent_container_style_writers_leave_only_valid_json_lines(self):
        import multiprocessing
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / 'events.jsonl')
            ctx = multiprocessing.get_context('spawn')
            # Windows can transiently refuse handle duplication under load
            # (WinError 5) during spawn; retry the spawn a few times so a real
            # cross-process run happens on the audit host as it does on Linux.
            procs = None
            for attempt in range(5):
                try:
                    procs = [ctx.Process(target=_audit_process_worker, args=(path, n, 40)) for n in range(4)]
                    for proc in procs:
                        proc.start()
                    break
                except PermissionError:
                    for proc in procs or []:
                        try:
                            proc.terminate()
                        except Exception:
                            pass
                    procs = None
                    time.sleep(1.0)
            self.assertIsNotNone(procs, 'the OS refused to spawn worker processes after retries')
            for proc in procs:
                proc.join(15)
                self.assertEqual(proc.exitcode, 0)
            files = sorted(Path(td).glob('events.jsonl*'))
            data_files = [p for p in files if not p.name.endswith('.lock')]
            self.assertTrue(data_files)
            rows = []
            for file in data_files:
                for line in file.read_text(encoding='utf-8').splitlines():
                    rows.append(json.loads(line))
            self.assertTrue(rows)
            self.assertTrue(all(row.get('event') == 'multiprocess-audit' for row in rows))
            self.assertTrue((Path(path + '.lock')).exists())


class UniversePublicationOrderingTests(unittest.TestCase):
    def test_historical_snapshot_exists_before_current_pointer_is_published(self):
        from services.universe_service.snapshot_store import store
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            result = store(root, [{'pair': 'ETH/USDT', 'rank': 1}], {'limit': 50}, 900)
            current = json.loads((root / 'current_pairlist.json').read_text())
            history = root / 'snapshots' / current['snapshot_file']
            self.assertTrue(history.is_file())
            self.assertEqual(json.loads(history.read_text())['snapshot_hash'], result['snapshot_hash'])
            self.assertIn(result['snapshot_hash'][:12], history.name)
            self.assertIn('.', history.name)


class UniverseFreshnessClockSkewTests(unittest.TestCase):
    def test_future_universe_snapshot_is_not_fresh(self):
        from services.execution_sidecar import main as sidecar_main
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'current_pairlist.json'
            path.write_text(json.dumps({
                'pairs': ['ETH/USDT'], 'snapshot_hash': 'abc',
                'generated_at': (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
            }))
            with mock.patch.object(sidecar_main, 'UNIVERSE_CURRENT', path):
                pairs, config_hash, fresh = sidecar_main.universe_state()
        self.assertFalse(fresh)
        self.assertEqual(pairs, set())
        self.assertEqual(config_hash, '')

    def test_compose_healthcheck_rejects_large_future_skew(self):
        root = Path(__file__).resolve().parents[1]
        text = (root / 'docker-compose.yml').read_text(encoding='utf-8')
        self.assertIn('-30<=a<1800', text)


class UniverseRuntimeConfigurationTests(unittest.TestCase):
    def test_rate_limit_hostile_refresh_and_oversized_universe_are_rejected(self):
        from services.universe_service import scanner
        with mock.patch.object(scanner, 'REFRESH', 0), mock.patch.object(scanner, 'LIMIT', 51):
            with self.assertRaisesRegex(ValueError, 'UNIVERSE_LIMIT.*UNIVERSE_REFRESH_SECONDS'):
                scanner.validate_runtime_settings()

    def test_nonfinite_timeout_is_rejected(self):
        from services.universe_service import scanner
        with mock.patch.object(scanner, 'TIMEOUT', float('nan')):
            with self.assertRaisesRegex(ValueError, 'HTTP_TIMEOUT_SECONDS'):
                scanner.validate_runtime_settings()


class InstalledReleaseLiveGateTests(unittest.TestCase):
    def setUp(self):
        # V102-REM-001: these tests pin the DEEPER live-evidence gates, which
        # now sit behind the runtime package-mode interlock. Exercise them as
        # the live-capable package in BOTH trees so the evidence-gate logic
        # stays fully tested everywhere; the testnet package lock itself is
        # pinned by tests/test_package_mode.py.
        from services.execution_sidecar import package_mode
        self._mode_dir = tempfile.TemporaryDirectory()
        mode_file = Path(self._mode_dir.name) / 'RELEASE_MODE'
        mode_file.write_text('live', encoding='utf-8')
        self._mode_patch = mock.patch.object(
            package_mode, 'PACKAGE_MODE_FILE', mode_file)
        self._mode_patch.start()

    def tearDown(self):
        self._mode_patch.stop()
        self._mode_dir.cleanup()

    def _state(self, root: Path):
        return StateStore(root / 'state.json', root / 'state.sqlite')

    def test_old_matching_env_and_marker_cannot_authorize_different_installed_release(self):
        from services.execution_sidecar import main as sidecar_main
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runtime = root / 'runtime'; runtime.mkdir()
            old_hash = 'a' * 64
            new_hash = 'b' * 64
            (runtime / 'SIDECAR_LIVE_OK').write_text(old_hash)
            release_file = root / 'RELEASE_SHA256.txt'; release_file.write_text(new_hash + '  RELEASE_MANIFEST.json\n')
            env = {'EXECUTION_MODE': 'live', 'SIDECAR_RELEASE_HASH': old_hash, 'AUTO_CONFIRM': 'false'}
            with mock.patch.dict(os.environ, env, clear=False), \
                 mock.patch.object(sidecar_main, 'RUNTIME', runtime), \
                 mock.patch.object(sidecar_main, 'RELEASE_HASH_FILE', release_file), \
                 mock.patch.object(sidecar_main.legacy, 'assert_live_ready', return_value=None):
                with self.assertRaisesRegex(SystemExit, 'must all match'):
                    sidecar_main.live_interlock(self._state(root))

    def test_markers_alone_cannot_unlock_live_without_signed_evidence(self):
        # C-003/H-007: matching markers are necessary but NOT sufficient; the
        # signed live-evidence envelope must also be present and valid.
        from services.execution_sidecar import main as sidecar_main
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runtime = root / 'runtime'; runtime.mkdir()
            release_hash = 'c' * 64
            (runtime / 'SIDECAR_LIVE_OK').write_text(release_hash)
            release_file = root / 'RELEASE_SHA256.txt'; release_file.write_text(release_hash + '  RELEASE_MANIFEST.json\n')
            env = {'EXECUTION_MODE': 'live', 'SIDECAR_RELEASE_HASH': release_hash,
                   'ENVELOPE_RELEASE_HASH': release_hash, 'AUTO_CONFIRM': 'false'}
            with mock.patch.dict(os.environ, env, clear=False), \
                 mock.patch.object(sidecar_main, 'RUNTIME', runtime), \
                 mock.patch.object(sidecar_main, 'RELEASE_HASH_FILE', release_file), \
                 mock.patch.object(sidecar_main.legacy, 'assert_live_ready', return_value=None):
                with self.assertRaisesRegex(SystemExit, 'LIVE BLOCKED'):
                    sidecar_main.live_interlock(self._state(root))

    def test_exact_environment_marker_and_signed_evidence_can_pass_sidecar_gate(self):
        from services.execution_sidecar import main as sidecar_main
        strategy = Path(__file__).resolve().parents[1] / 'freqtrade/user_data/strategies/IctSmcStrategy.py'
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runtime = root / 'runtime'; runtime.mkdir()
            release_hash = 'c' * 64
            (runtime / 'SIDECAR_LIVE_OK').write_text(release_hash)
            release_file = root / 'RELEASE_SHA256.txt'; release_file.write_text(release_hash + '  RELEASE_MANIFEST.json\n')
            env = {'EXECUTION_MODE': 'live', 'SIDECAR_RELEASE_HASH': release_hash,
                   'ENVELOPE_RELEASE_HASH': release_hash, 'AUTO_CONFIRM': 'false',
                   'LIVE_EVIDENCE_FILE': str(runtime / 'LIVE_EVIDENCE.json')}
            with mock.patch.dict(os.environ, env, clear=False), \
                 mock.patch.object(sidecar_main, 'RUNTIME', runtime), \
                 mock.patch.object(sidecar_main, 'RELEASE_HASH_FILE', release_file), \
                 mock.patch.object(sidecar_main.legacy, 'assert_live_ready', return_value=None):
                _harness.write_live_evidence(runtime, release_hash, strategy)
                mode = sidecar_main.live_interlock(self._state(root))
        self.assertEqual(mode.value, 'live')


class DockerReleaseHashAvailabilityTests(unittest.TestCase):
    def test_service_image_contains_installed_release_hash_file(self):
        root = Path(__file__).resolve().parents[1]
        dockerfile = (root / 'Dockerfile.services').read_text(encoding='utf-8')
        dockerignore = (root / '.dockerignore').read_text(encoding='utf-8')
        self.assertIn('RELEASE_SHA256.txt', dockerfile)
        self.assertIn('!RELEASE_SHA256.txt', dockerignore)


class PackagedArtifactCIParityTests(unittest.TestCase):
    def test_ci_uses_deterministic_tar_and_retests_extracted_artifact(self):
        root = Path(__file__).resolve().parents[1]
        workflow = (root / '.github/workflows/ci.yml').read_text(encoding='utf-8')
        self.assertIn("--sort=name --mtime='UTC 2020-01-01'", workflow)
        self.assertIn('gzip -n -9', workflow)
        self.assertIn('Verify freshly extracted release artifact', workflow)
        self.assertIn('./deploy/verify_release.sh', workflow)
        self.assertIn('docker compose --env-file .env.example build universe', workflow)
        self.assertIn('cmp RELEASE_MANIFEST.json', workflow)


class MalformedExchangeNumericTests(unittest.TestCase):
    def test_malformed_fill_numeric_forces_reconciliation_status(self):
        from services.execution_sidecar.state_store import StateStore
        with tempfile.TemporaryDirectory() as td:
            state = StateStore(Path(td) / 'state.json', Path(td) / 'state.sqlite')
            state.upsert_trade('t1', 'ETH/USDT', entry_order_id=10, lifecycle_state='ENTRY_SUBMITTED')
            event = {
                'e': 'executionReport', 's': 'ETHUSDT', 'i': 10, 'S': 'BUY',
                'X': 'PARTIALLY_FILLED', 'z': 'not-a-number', 'Z': '5', 'q': '1',
                'E': 1, 'I': 99,
            }
            self.assertTrue(state.record_exchange_event(event))
            import sqlite3
            con = sqlite3.connect(Path(td) / 'state.sqlite')
            row = con.execute('SELECT reconciliation_status FROM trade_records WHERE trade_id=?', ('t1',)).fetchone()
            con.close()
            self.assertEqual(row[0], 'MALFORMED_FILL_NUMERIC_RECONCILE_REQUIRED')
