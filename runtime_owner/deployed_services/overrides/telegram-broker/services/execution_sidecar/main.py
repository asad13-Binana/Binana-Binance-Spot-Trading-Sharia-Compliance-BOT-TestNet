from __future__ import annotations
import json, logging, math, os, re, signal, shutil, threading, time, uuid
from datetime import datetime, timezone
from pathlib import Path
from services.common.paths import (
    SIGNAL_INBOX, SIGNAL_PROCESSED, SIGNAL_REJECTED, UNIVERSE_CURRENT,
    SHARIA_FILE, LEGACY_HALAL_FILE, RUNTIME, COMMAND_INBOX,
    TELEGRAM_ALERT_OUTBOX,
)
from services.common.models import ProtectionMode, ExecutionMode
from services.common.atomic import read_json, atomic_write_json
from services.common.audit import audit
from services.common.retention import prune_files
from services.common import envelope
from services.common.config_bounds import ConfigError, env_int
from services.execution_sidecar.state_store import StateStore
from services.execution_sidecar.risk_checks import FreshSignalGuard
from services.execution_sidecar.core_adapter import CoreAdapter, legacy
from services.execution_sidecar.live_evidence import LiveEvidenceError, verify_live_evidence
from services.execution_sidecar.order_manager import OrderManager
from services.execution_sidecar.package_mode import enforce_package_mode, enforce_sharia_gate_mode
from services.execution_sidecar.reconciler import reconcile
from services.execution_sidecar.simulation_adapter import SimulationAdapter
from services.universe_service.sharia_filter import ShariaFilter
from services.universe_service.snapshot_store import load_current

log = logging.getLogger('sidecar')
STOP = threading.Event()
RELEASE_HASH_FILE = Path(__file__).resolve().parents[2] / 'RELEASE_SHA256.txt'

def universe_state():
    try:
        max_age = env_int('MAX_UNIVERSE_AGE_SECONDS', 1800, 1, 86_400)
        data = load_current(UNIVERSE_CURRENT, max_age_seconds=max_age)
    except Exception:
        return set(), '', False
    pairs = {str(pair) for pair in data['pairs']}
    return pairs, str(data['snapshot_hash']), True

def universe_legacy():
    pairs, _, fresh = universe_state()
    return [p.replace('/', '') for p in pairs] if fresh else []

def notify(text, buttons=None, chat_id=None):
    """Persist an owner alert before recording its audit event.

    Telegram delivery is deliberately owned by the Telegram broker. The
    sidecar never performs network I/O here, and a failed outbox write remains
    visible as a critical audit event instead of being reported as delivered.
    """
    notification_id = uuid.uuid4().hex
    payload = {
        'schema': 1,
        'notification_id': notification_id,
        'created_at': time.time(),
        'text': str(text),
        'buttons': buttons,
        'chat_id': chat_id,
    }
    try:
        atomic_write_json(TELEGRAM_ALERT_OUTBOX / f'{notification_id}.json', payload)
    except Exception as exc:
        audit('sidecar_notification_outbox_failed', severity='CRITICAL', details={
            'notification_id': notification_id, 'error': str(exc),
        })
        return None
    audit('sidecar_notification', details={
        'notification_id': notification_id, 'text': str(text),
        'buttons': buttons, 'chat_id': chat_id, 'outbox': 'queued',
    })
    return notification_id

def _safe_command_id(value: object) -> str | None:
    cid = str(value or '')
    return cid if re.fullmatch(r'[A-Za-z0-9_-]{1,128}', cid) else None

def _safe_spot_symbol(value: object) -> str:
    symbol = str(value or '').upper()
    if not re.fullmatch(r'[A-Z0-9]{2,20}USDT', symbol) or symbol.startswith(('BNB', 'BTC')):
        raise ValueError('invalid or excluded Spot/USDT symbol')
    return symbol

def _disarm_execution(adapter, state: StateStore, reason: str) -> str:
    """Attempt both persistent and in-memory/core disarm before reporting errors."""
    errors = []
    try:
        state.set_entries(False, reason)
    except Exception as exc:
        errors.append('state persistence failed: ' + str(exc))
    result = 'OFF'
    try:
        trader = getattr(adapter, 'trader', None)
        running = bool(trader and trader.is_running())
        if not state.data.get('simulation', True) and running:
            result = adapter.set_enabled(False)
            if result != 'OFF':
                errors.append('execution core did not confirm OFF: ' + str(result))
    except Exception as exc:
        errors.append('execution core disarm failed: ' + str(exc))
    if errors:
        raise RuntimeError('; '.join(errors))
    return result

def shutdown_adapter(adapter, state: StateStore) -> bool:
    """Best-effort disarm AND portfolio flush on normal stop or loop failure."""
    errors = []
    try:
        _disarm_execution(adapter, state, 'sidecar-shutdown')
    except Exception as exc:
        errors.append(type(exc).__name__)
    try:
        portfolio = getattr(getattr(adapter, 'trader', None), 'pf', None)
        if portfolio is not None:
            portfolio.save()
    except Exception as exc:
        errors.append(type(exc).__name__)
    try:
        audit('sidecar_stopped', severity='ERROR' if errors else 'INFO',
              details={'clean_shutdown': not errors, 'errors': errors})
    except Exception:
        log.error('shutdown audit unavailable')
        errors.append('audit-unavailable')
    return not errors


def disk_pressure_reason() -> str:
    """Read actual runtime volume pressure, even when no status file can be written."""
    try:
        threshold = env_int('DISK_CRITICAL_PERCENT', 90, 50, 99)
        usage = shutil.disk_usage(RUNTIME)
        if usage.total <= 0:
            return 'disk-capacity-unknown'
        if usage.used * 100 >= usage.total * threshold:
            return 'critical-disk-pressure'
    except Exception:
        return 'disk-capacity-unknown'
    return ''


def _write_command_result(cid: str, cmd: str, ok: bool, result: object, state: StateStore):
    payload = {'ok': bool(ok), 'result': str(result), 'command_id': cid, 'command': cmd}
    # M-008 fix: SQLite is authoritative — commit the final result FIRST, then
    # materialize the derived file consumers poll. A crash between the two can
    # no longer leave the database IN_PROGRESS behind a final result file.
    state.record_command(cid, cmd, json.dumps(payload, sort_keys=True))
    atomic_write_json(RUNTIME / f'command_result_{cid}.json', payload)
    audit('command_result', severity='INFO' if ok else 'ERROR', details=payload)
    return payload

def process_command(adapter, state: StateStore, guard: FreshSignalGuard, path: Path):
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except Exception as exc:
        audit('command_malformed', severity='ERROR', details={'file': path.name, 'error': str(exc)})
        path.unlink(missing_ok=True)
        return
    # V101-NEW-001 fix: only HMAC-authenticated envelopes from the Telegram
    # broker or the deployment installer may drive the order-owning sidecar.
    # A forged or unsigned file from any other container is rejected unread.
    try:
        data = envelope.verify_envelope(
            raw, purpose=envelope.BUS_COMMAND,
            expected_producers={'telegram-broker', 'deploy-installer'})
    except envelope.EnvelopeError as exc:
        audit('command_rejected_unauthenticated', severity='CRITICAL',
              details={'file': path.name, 'error': str(exc)})
        path.unlink(missing_ok=True)
        return
    try:
        cmd = str(data['command'])
        args = data.get('args', {})
        cid = _safe_command_id(data.get('command_id', path.stem))
        if not cid:
            raise ValueError('invalid command_id')
    except Exception as exc:
        audit('command_malformed', severity='ERROR', details={'file': path.name, 'error': str(exc)})
        path.unlink(missing_ok=True)
        return
    try:
        created_at = float(data['created_at'])
        age = time.time() - created_at
        max_age = int(os.getenv('COMMAND_MAX_AGE_SECONDS', '120'))
        if age > max_age or age < -30:
            raise ValueError(f'command timestamp outside allowed window (age={age:.1f}s)')
    except Exception as exc:
        if state.claim_command(cid, cmd):
            _write_command_result(cid, cmd, False, 'expired or invalid command: ' + str(exc), state)
        audit('command_expired_or_invalid', severity='WARNING', details={'command_id': cid, 'command': cmd})
        path.unlink(missing_ok=True)
        return

    if not state.claim_command(cid, cmd):
        prior = state.command_result(cid) or 'UNKNOWN'
        audit('duplicate_command_rejected', severity='WARNING', details={
            'command_id': cid, 'command': cmd, 'prior_result': prior,
        })
        # IN_PROGRESS after restart means an exchange side effect may already have
        # occurred. Never replay automatically; require status/reconciliation.
        result_path = RUNTIME / f'command_result_{cid}.json'
        if prior == 'IN_PROGRESS' and not result_path.exists():
            atomic_write_json(result_path, {
                'ok': False, 'result': 'prior execution outcome is uncertain; reconcile before retrying',
                'command_id': cid, 'command': cmd,
            })
            state.set_entries(False, 'uncertain-prior-command-reconcile-required')
        elif prior not in ('UNKNOWN', 'IN_PROGRESS') and not result_path.exists():
            # M-008 recovery: the database committed a final result but the
            # derived file was lost (crash between DB write and file write).
            # Rematerialize the file deterministically from the database.
            try:
                atomic_write_json(result_path, json.loads(prior))
            except Exception:
                atomic_write_json(result_path, {'ok': False, 'result': prior,
                                                'command_id': cid, 'command': cmd})
        path.unlink(missing_ok=True)
        return

    result, ok = 'unknown command', True
    try:
        if cmd == 'entries':
            enabled = args.get('enabled')
            if not isinstance(enabled, bool):
                raise ValueError('entries.enabled must be a JSON boolean')
            if enabled:
                safety_halts = state.safety_halts()
                if safety_halts:
                    raise ValueError(
                        'entries remain paused: safety reconciliation pending for ' +
                        ', '.join(sorted(safety_halts)))
                _, _, fresh = universe_state()
                if not fresh:
                    raise ValueError('entries remain paused: universe snapshot is stale or missing')
                pause = str(guard.state.get('global_pause', ''))
                if pause == 'stale-or-missing-universe':
                    guard.clear_global_pause()
                elif pause:
                    raise ValueError('entries remain paused: ' + pause)
                if state.data.get('simulation', True):
                    result = 'ON'
                else:
                    result = adapter.set_enabled(True)
                    if result != 'ON':
                        raise RuntimeError('execution core did not arm entries: ' + str(result))
                state.set_entries(True)
            else:
                result = _disarm_execution(adapter, state, 'operator-paused')
        elif cmd == 'mode':
            state.set_mode(args['mode']); result = 'mode ' + state.get_mode()
        elif cmd == 'simulation':
            if args.get('enabled') is not True:
                raise ValueError('simulation cannot be disabled at runtime; restart with an explicit EXECUTION_MODE')
            state.data['simulation'] = True; state.set_entries(False, 'simulation-enabled'); result = 'simulation=true'
        elif cmd == 'status':
            result = adapter.status() if adapter.trader.is_running() else json.dumps(state.data, sort_keys=True)
        elif cmd == 'orders':
            outcome = adapter.open_orders_snapshot()
            if not isinstance(outcome, dict) or outcome.get('ok') is not True:
                ok = False
            result = json.dumps(outcome, sort_keys=True, default=str)
        elif cmd == 'balance':
            result = json.dumps(adapter.balance(), sort_keys=True)
        elif cmd == 'profit':
            result = json.dumps(adapter.profit(), sort_keys=True)
        elif cmd == 'restart_stream':
            result = adapter.restart_user_stream()
            ok = str(result).strip().lower() == 'restarted'
        elif cmd == 'reload_sharia':
            # Read-only reload: the canonical Sharia directory is written only
            # by the sharia-screener service. This command revalidates the
            # V19.1 projection and asks the preserved core to re-read the
            # generated compatibility whitelist.
            gate = ShariaFilter(SHARIA_FILE)
            eligible = gate.current_halal_symbols()
            result = (f'V19.1 status reloaded: {len(gate.records)} records, '
                      f'{len(eligible)} trade-eligible')
            if adapter.trader.is_running():
                adapter.reload_sharia()
        elif cmd == 'set_size':
            value = float(args['usdt'])
            if not math.isfinite(value) or value <= 0:
                raise ValueError('trade size must be a positive finite number')
            if not adapter.trader.is_running():
                raise RuntimeError('execution core not started; size was not changed')
            result = adapter.set_size(value)
            if not str(result).lower().startswith('trade size ='):
                raise ValueError(str(result))
        elif cmd == 'set_max':
            value = int(args['count'])
            ceiling = int(legacy.FORTRESS_CFG.MAX_POSITIONS_CEILING)
            if value < 1 or value > ceiling:
                raise ValueError(f'maximum positions must be within 1–{ceiling}')
            if not adapter.trader.is_running():
                raise RuntimeError('execution core not started; maximum positions were not changed')
            result = adapter.set_max(value)
            if not str(result).lower().startswith('max concurrent positions ='):
                raise ValueError(str(result))
        elif cmd == 'reconcile':
            result = reconcile(adapter)
            ok = not str(result).strip().lower().startswith(('not started', 'error', 'failed'))
        elif cmd == 'emergency_exit':
            _disarm_execution(adapter, state, 'emergency-exit-owner-resume-required')
            outcome = adapter.emergency_exit(_safe_spot_symbol(args['symbol']))
            # C-002 fix: success is only a VERIFIED fully-exited result from
            # the structured adapter contract — never a success-prefix string.
            if isinstance(outcome, dict):
                ok = outcome.get('ok') is True
                result = json.dumps(outcome, sort_keys=True, default=str)
            else:
                ok, result = False, str(outcome)
        elif cmd == 'convert':
            _disarm_execution(adapter, state, 'protection-conversion-owner-resume-required')
            ok, result = adapter.convert(_safe_spot_symbol(args['symbol']), ProtectionMode(args['mode']))
        elif cmd == 'break_even':
            _disarm_execution(adapter, state, 'protection-conversion-owner-resume-required')
            ok, result = adapter.convert(_safe_spot_symbol(args['symbol']), ProtectionMode.FIXED_OCO, break_even=True)
        elif cmd == 'lock_profit':
            pct = float(args.get('profit_pct', 0.2))
            if not math.isfinite(pct) or pct <= 0 or pct > 100:
                raise ValueError('profit percentage must be finite and within (0, 100]')
            _disarm_execution(adapter, state, 'protection-conversion-owner-resume-required')
            ok, result = adapter.convert(
                _safe_spot_symbol(args['symbol']), ProtectionMode.FIXED_OCO, lock_profit_pct=pct
            )
        elif cmd == 'clear_risk_pause':
            guard.clear_global_pause(); result = 'risk pause cleared'
        else:
            ok = False
    except Exception as exc:
        ok, result = False, str(exc)
    _write_command_result(cid, cmd, ok, result, state)
    path.unlink(missing_ok=True)

USER_STREAM_MAX_AGE_SECONDS = 180


def user_stream_state(mode: ExecutionMode) -> tuple[bool, dict]:
    """H-001: report whether the authoritative Binance user-data stream is
    actually usable, so container health cannot be green while the sidecar is
    connected-but-unsubscribed (or silently disconnected).

    Simulation has no exchange stream and is always considered healthy. For
    testnet/live the stream must be connected AND subscribed AND its health
    record must be fresh; anything else is unhealthy and, because Compose
    health gates on this file, will surface instead of being hidden.
    """
    if mode is ExecutionMode.SIMULATION:
        return True, {'mode': 'simulation', 'required': False}
    health = read_json(RUNTIME / 'user_stream_health.json', {}) or {}
    ts = health.get('ts')
    age = None
    if isinstance(ts, (int, float)) and ts > 0:
        age = max(0.0, time.time() - float(ts))
    connected = bool(health.get('connected'))
    subscribed = bool(health.get('subscribed'))
    fresh = age is not None and age <= USER_STREAM_MAX_AGE_SECONDS
    ok = connected and subscribed and fresh
    return ok, {
        'required': True, 'connected': connected, 'subscribed': subscribed,
        'age_seconds': round(age, 1) if age is not None else None,
        'fresh': fresh, 'last_error': health.get('last_error'),
        'max_age_seconds': USER_STREAM_MAX_AGE_SECONDS,
    }


def live_interlock(state: StateStore):
    try:
        mode = ExecutionMode(os.getenv('EXECUTION_MODE', 'simulation').lower())
    except ValueError as exc:
        raise SystemExit('Invalid EXECUTION_MODE; use simulation, testnet, or live') from exc
    # V102-REM-001: the shipped package mode is runtime law. This runs before
    # any authenticated client, stream, reconciliation, order, or evidence
    # code, and cannot be overridden by .env (the mode comes from the
    # read-only RELEASE_MODE file baked into the image).
    package = enforce_package_mode(mode.value)
    gate_mode = enforce_sharia_gate_mode(
        package, os.getenv('SHARIA_SIGNAL_GATE_MODE', 'cached'))
    audit('package_mode_enforced', details={
        'package_mode': package, 'execution_mode': mode.value,
        'sharia_signal_gate_mode': gate_mode})
    testnet = mode is not ExecutionMode.LIVE
    os.environ['BINANCE_TESTNET'] = 'true' if testnet else 'false'
    legacy.CFG.TESTNET = testnet
    legacy.FORTRESS_CFG.TESTNET = testnet
    if mode is ExecutionMode.SIMULATION:
        state.data['simulation'] = True
        state.set_entries(False, 'simulation-safe-default')
        return mode
    state.data['simulation'] = False
    state.save()
    if mode is ExecutionMode.TESTNET:
        return mode

    # Preserved core release gates, then a sidecar-specific immutable release marker.
    legacy.assert_live_ready(False)
    marker = RUNTIME / 'SIDECAR_LIVE_OK'
    expected = os.getenv('SIDECAR_RELEASE_HASH', '').strip()
    try:
        installed = RELEASE_HASH_FILE.read_text(encoding='utf-8').split()[0]
    except Exception as exc:
        raise SystemExit('LIVE BLOCKED: installed release hash is unreadable') from exc
    approved = marker.read_text(encoding='utf-8').strip() if marker.exists() else ''
    if not re.fullmatch(r'[0-9a-f]{64}', installed):
        raise SystemExit('LIVE BLOCKED: installed release hash is invalid')
    if not expected or expected != installed or approved != installed:
        raise SystemExit(
            'LIVE BLOCKED: installed release hash, SIDECAR_RELEASE_HASH, and SIDECAR_LIVE_OK must all match'
        )
    if os.getenv('AUTO_CONFIRM', 'false').lower() == 'true':
        raise SystemExit('LIVE BLOCKED: AUTO_CONFIRM must be false')
    # C-003 / H-007 fix: presence markers and the legacy backtest gate can no
    # longer unlock live mode on their own. Live additionally requires the
    # HMAC-signed evidence envelope binding this exact release, the protected
    # strategy fingerprints, the immutable V19.1 controller, an exact-strategy
    # Freqtrade backtest artifact, and Testnet/Oracle/clean-pass assertions.
    try:
        from services.common.strategy_fingerprint import fingerprints
        strategy_path = Path(__file__).resolve().parents[2] / 'freqtrade/user_data/strategies/IctSmcStrategy.py'
        prints = fingerprints(strategy_path, 'IctSmcStrategy', [
            'populate_indicators_5m', 'populate_indicators',
            'populate_entry_trend', 'populate_exit_trend'])
        verify_live_evidence(release_hash=installed, strategy_fingerprints=prints)
    except (LiveEvidenceError, envelope.EnvelopeError) as exc:
        raise SystemExit(f'LIVE BLOCKED: {exc}') from exc
    except Exception as exc:
        raise SystemExit(f'LIVE BLOCKED: evidence verification failed: {exc}') from exc
    return mode

def main():
    logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO'), format='%(asctime)s %(levelname)s %(name)s %(message)s')
    # V101-NEW-001: every bus key must be present before anything runs; a
    # missing or short key is a fail-closed startup error, never a silent
    # fallback to unauthenticated files.
    try:
        for purpose in (envelope.BUS_COMMAND, envelope.BUS_SIGNAL, envelope.BUS_SHARIA_REQUEST):
            envelope.load_key(purpose)
    except envelope.EnvelopeError as exc:
        raise SystemExit(f'BUS KEYS MISSING: {exc}') from exc
    if not envelope.installed_release_hash():
        raise SystemExit('RELEASE BINDING MISSING: set ENVELOPE_RELEASE_HASH or install RELEASE_SHA256.txt')
    state = StateStore(RUNTIME / 'sidecar_state.json', RUNTIME / 'execution_state.sqlite')
    mode = live_interlock(state)
    # Fail-closed V19.1 gate load. The canonical Sharia directory is written
    # only by the sharia-screener service; the sidecar validates read-only.
    sharia_gate = ShariaFilter(SHARIA_FILE)
    audit('sharia_gate_loaded', details={
        'records': len(sharia_gate.records),
        'trade_eligible': len(sharia_gate.current_halal_symbols()),
        'controller_sha256': sharia_gate.controller_sha256,
    })
    try:
        guard = FreshSignalGuard(RUNTIME / 'fresh_signal_guard.json', state_store=state)
    except ConfigError as exc:
        raise SystemExit(f'RISK CONFIGURATION INVALID: {exc}') from exc
    if mode is ExecutionMode.SIMULATION:
        # C-004 fix: simulation runs the same claim/submit/protection/event
        # lifecycle through a deterministic simulator instead of bypassing the
        # execution path entirely.
        adapter = SimulationAdapter(state, guard, notify,
                                    fault_path=RUNTIME / 'simulation_faults.json')
        adapter.start()
    else:
        adapter = CoreAdapter(
            state.get_mode, notify, LEGACY_HALAL_FILE, universe_legacy,
            event_sink=guard.on_exchange_event, state_store=state
        )
        # Disarm the preserved broker chokepoint before any portfolio/thread startup.
        legacy._set_entries_armed(False)
        if not adapter.start():
            raise SystemExit('sidecar core failed to start')
        # Startup reconciliation must complete while entries remain paused.
        # Never restore a previous armed state automatically: every process or
        # host restart requires a fresh owner confirmation through Telegram.
        was_requested = state.entries()
        state.set_entries(False, 'startup-reconciliation-complete-owner-resume-required')
        # H-005 fix: startup reconciliation is endpoint-complete; a failed
        # enumeration latches a global pause instead of looking successful.
        startup_reconciliation = adapter.verified_reconcile()
        if not startup_reconciliation.get('ok'):
            guard.set_global_pause('startup-reconciliation-failed')
            audit('startup_reconciliation_failed', severity='CRITICAL',
                  details=startup_reconciliation)
        adapter.set_enabled(False)
        audit('startup_entries_left_paused', severity='WARNING', details={
            'previous_entries_enabled': was_requested,
            'reason': 'owner confirmation required after restart',
        })
    manager = OrderManager(
        adapter, state, guard, state, SHARIA_FILE,
        {'processed': SIGNAL_PROCESSED, 'rejected': SIGNAL_REJECTED}
    )
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    audit('sidecar_started', details={'mode': mode.value, 'protection_mode': state.get_mode()})
    next_maintenance = 0.0
    next_backup = 0.0

    try:
        while not STOP.is_set():
            for path in sorted(COMMAND_INBOX.glob('*.json')):
                try:
                    process_command(adapter, state, guard, path)
                except Exception as exc:
                    log.exception('command processing failed for %s', path.name)
                    try:
                        _disarm_execution(adapter, state, 'command-processing-failure')
                    except Exception as disarm_exc:
                        log.critical('fail-closed command disarm also failed: %s', disarm_exc)
                    audit('command_processing_failed', severity='CRITICAL', details={
                        'file': path.name, 'error': str(exc),
                    })
            pressure = disk_pressure_reason()
            if pressure:
                _disarm_execution(adapter, state, pressure)
            if time.time() >= next_maintenance:
                prune_files(
                    RUNTIME, 'command_result_*.json',
                    max_files=max(0, int(os.getenv('COMMAND_RESULT_MAX_FILES', '1000'))),
                    max_age_seconds=max(0, int(os.getenv('COMMAND_RESULT_MAX_AGE_SECONDS', '86400'))),
                )
                # H-006: periodic verified online backup of the authoritative DB.
                try:
                    backup_interval = int(os.getenv('SQLITE_BACKUP_INTERVAL_SECONDS', '86400'))
                    if backup_interval > 0 and time.time() >= next_backup:
                        destination = state.backup(RUNTIME / 'db_backups',
                                                   retain=int(os.getenv('SQLITE_BACKUP_RETAIN', '14')))
                        audit('sqlite_backup_verified', details={'path': str(destination)})
                        next_backup = time.time() + backup_interval
                except Exception as exc:
                    audit('sqlite_backup_failed', severity='ERROR', details={'error': str(exc)})
                    next_backup = time.time() + 3600
                next_maintenance = time.time() + 60
            pairs, universe_hash, fresh = universe_state()
            if not fresh and state.entries():
                disarm_error = None
                try:
                    _disarm_execution(adapter, state, 'stale-or-missing-universe')
                except Exception as exc:
                    disarm_error = str(exc)
                    log.critical('stale-universe fail-closed disarm was incomplete: %s', exc)
                try:
                    guard.set_global_pause('stale-or-missing-universe')
                except Exception as exc:
                    log.critical('could not persist stale-universe global pause: %s', exc)
                    disarm_error = (disarm_error + '; ' if disarm_error else '') + str(exc)
                audit('entries_paused_universe_stale', severity='CRITICAL' if disarm_error else 'ERROR',
                      details={'disarm_error': disarm_error or ''})
            for path in sorted(SIGNAL_INBOX.glob('*.json')):
                try:
                    manager.process_signal(path, pairs, universe_hash)
                except Exception as exc:
                    log.exception('signal processing failed for %s', path.name)
                    pause_error = None
                    try:
                        _disarm_execution(adapter, state, 'signal-processing-failure')
                    except Exception as disarm_exc:
                        log.critical('fail-closed signal disarm also failed: %s', disarm_exc)
                    # V101-NEW-003 fix: pause persistence can itself fail (disk
                    # full, read-only filesystem). That failure must never kill
                    # the supervision loop during exactly the incident where the
                    # loop matters most.
                    try:
                        guard.set_global_pause('signal-processing-failure-reconcile-required')
                    except Exception as pause_exc:
                        pause_error = str(pause_exc)
                        log.critical('could not persist signal-failure global pause: %s', pause_exc)
                        try:
                            guard.state['global_pause'] = 'signal-processing-failure-reconcile-required'
                        except Exception:
                            pass
                    audit('signal_processing_failed', severity='CRITICAL', details={
                        'file': path.name, 'error': str(exc),
                        'pause_persist_error': pause_error or '',
                    })
            if adapter.trader.is_running():
                adapter.mirror_positions('PERIODIC_RECONCILIATION')
            # H-001: 'ok' was hardcoded True, so Docker reported the sidecar healthy
            # even when the user-data stream was connected-but-unsubscribed and no
            # order/account events were arriving. Health must reflect the stream
            # that authoritative order state depends on. Simulation has no exchange
            # stream, so it is exempt.
            stream_ok, stream_detail = user_stream_state(mode)
            runtime_faults = (
                adapter.runtime_safety_faults()
                if callable(getattr(adapter, 'runtime_safety_faults', None)) else {})
            atomic_write_json(RUNTIME / 'sidecar_health.json', {
                'ok': bool(stream_ok and not runtime_faults),
                'ts': time.time(), 'execution_mode': mode.value,
                'user_stream_ok': stream_ok, 'user_stream': stream_detail,
                'entries_enabled': state.entries(), 'pause_reason': state.data.get('pause_reason', ''),
                'simulation': state.data.get('simulation', True), 'protection_mode': state.get_mode(),
                'universe_count': len(pairs), 'universe_fresh': fresh,
                'trading_ready': bool(stream_ok and fresh and pairs and state.entries()
                                      and not runtime_faults and not state.safety_halts()),
                'readiness_blockers': (["NO_ELIGIBLE_PAIRS"] if not pairs else []) +
                    (["UNIVERSE_STALE"] if not fresh else []) +
                    (["ENTRIES_DISARMED"] if not state.entries() else []) +
                    (["USER_STREAM_UNHEALTHY"] if not stream_ok else []),
                'sharia_signal_gate_mode': os.getenv('SHARIA_SIGNAL_GATE_MODE', 'cached'),
                'last_reconciliation_status': state.data.get('last_reconciliation_status', 'NOT_RUN'),
                'safety_halts': state.data.get('safety_halts', {}),
                'runtime_safety_faults': runtime_faults,
            })
            STOP.wait(1)

    finally:
        shutdown_adapter(adapter, state)

if __name__ == '__main__':
    main()
