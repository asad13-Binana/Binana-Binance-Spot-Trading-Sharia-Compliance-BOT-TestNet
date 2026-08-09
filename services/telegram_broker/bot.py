from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests
from requests.auth import HTTPBasicAuth

from services.common import envelope
from services.common.atomic import atomic_write_json, read_json
from services.common.audit import audit
from services.common.config_bounds import env_int
from services.common.paths import (
    AUDIT_DIR,
    COMMAND_INBOX,
    RUNTIME,
    SHARIA_DECISION_INBOX,
    SHARIA_FILE,
    SHARIA_QUEUE_INBOX,
    SHARIA_REPORTS_DIR,
    SHARIA_RESULTS_DIR,
    SHARIA_RUNTIME_DIR,
    SIGNAL_INBOX,
    SIGNAL_PROCESSED,
    SIGNAL_REJECTED,
    TELEGRAM_ALERT_OUTBOX,
    UNIVERSE_CURRENT,
)
from services.common.sharia_attestation import RESULT_PURPOSE, verify_attached
from services.sharia_screener.approval import SCOPE_CONFIRMATION
from services.telegram_broker.authorization import is_owner
from services.telegram_broker.callbacks import CallbackStore
from services.universe_service.sharia_filter import ShariaFilter
from services.universe_service.snapshot_store import load_current

_TOKEN_PATTERN = re.compile(r'bot\d{4,}:[A-Za-z0-9_\-]{20,}')


def _redact_secrets(value: object, limit: int = 500) -> str:
    """H-004: strip the Telegram bot token from any text before it is logged
    or persisted.

    ``requests`` puts the full attempted URL into its exception text, and the
    API base embeds the token, so an unredacted exception discloses the
    control-channel credential to anyone who can read the log or the shared
    telegram_health.json. Redaction is applied twice: the exact configured
    token, then a defensive pattern match for any bot<id>:<secret> shape that
    might arrive from a nested or third-party exception.
    """
    text = f'{type(value).__name__}: {value}' if isinstance(value, BaseException) else str(value)
    if TOKEN:
        text = text.replace(TOKEN, '***REDACTED***')
    text = _TOKEN_PATTERN.sub('bot***REDACTED***', text)
    return text[:limit]


def _release_label() -> str:
    """DOC-001: report the ACTUAL packaged release, not a hardcoded V10.1
    string. Operator-facing status must never claim an older release than the
    one running. Falls back to a neutral label if the metadata is unreadable."""
    try:
        return (Path(__file__).resolve().parents[2] / 'RELEASE_VERSION').read_text(
            encoding='utf-8').strip() or 'unknown'
    except OSError:
        return 'unknown'

log = logging.getLogger('telegram-broker')
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
OWNER = os.getenv('TELEGRAM_OWNER_CHAT_ID', '')
BASE = f'https://api.telegram.org/bot{TOKEN}'
CB = CallbackStore(ttl=int(os.getenv('CALLBACK_TTL_SECONDS', '120')))
FT_BASE = os.getenv('FREQTRADE_API_URL', 'http://freqtrade:8080/api/v1').rstrip('/')
FT_USER = os.getenv('FREQTRADE_API_USERNAME', 'freqtrade')
FT_PASS = os.getenv('FREQTRADE_API_PASSWORD', '')
OFFSET_PATH = RUNTIME / 'telegram_offset.json'
ALERT_DELIVERY_STATE = RUNTIME / 'telegram_alert_delivery.json'
PAIR_RE = re.compile(r'^([A-Z0-9]{2,20})(?:/USDT|USDT)$')
NOT_FATWA = 'Research screening only — not a fatwa and not financial advice.'


def send(text, chat_id=None, buttons=None):
    if not TOKEN:
        return
    data = {'chat_id': chat_id or OWNER, 'text': str(text)[:4000]}
    if buttons:
        data['reply_markup'] = json.dumps({'inline_keyboard': buttons})
    response = requests.post(BASE + '/sendMessage', data=data, timeout=15)
    response.raise_for_status()


def deliver_sidecar_notifications(limit: int = 20) -> int:
    """Deliver durable sidecar alerts with local replay deduplication."""
    state = read_json(ALERT_DELIVERY_STATE, {}) or {}
    delivered = state.get('delivered', {})
    if not isinstance(delivered, dict):
        delivered = {}
    processed = 0
    for path in sorted(TELEGRAM_ALERT_OUTBOX.glob('*.json'))[:max(0, int(limit))]:
        payload = read_json(path, None)
        if not isinstance(payload, dict):
            audit('telegram_alert_invalid', severity='ERROR', details={'file': path.name})
            continue
        notification_id = str(payload.get('notification_id') or '')
        if not re.fullmatch(r'[0-9a-f]{32}', notification_id) or path.stem != notification_id:
            audit('telegram_alert_invalid', severity='ERROR', details={'file': path.name})
            continue
        if notification_id in delivered:
            path.unlink(missing_ok=True)
            continue
        try:
            # Execution alerts are owner-only; never honor a producer-supplied
            # destination for privileged account state.
            send(_redact_secrets(payload.get('text', '')), OWNER)
        except Exception as exc:
            audit('telegram_alert_delivery_failed', severity='ERROR', details={
                'notification_id': notification_id, 'error': _redact_secrets(exc),
            })
            break
        delivered[notification_id] = time.time()
        max_ids = max(100, int(os.getenv('TELEGRAM_ALERT_DEDUPE_MAX', '5000')))
        if len(delivered) > max_ids:
            delivered = dict(sorted(delivered.items(), key=lambda item: item[1])[-max_ids:])
        try:
            atomic_write_json(ALERT_DELIVERY_STATE, {
                'delivered': delivered, 'updated_at': time.time(),
            })
        except Exception as exc:
            # Retain the item. At-least-once delivery is safer than losing an
            # emergency alert when the local dedupe journal cannot be saved.
            audit('telegram_alert_dedupe_persist_failed', severity='CRITICAL', details={
                'notification_id': notification_id, 'error': _redact_secrets(exc),
            })
            break
        path.unlink(missing_ok=True)
        audit('telegram_alert_delivered', details={'notification_id': notification_id})
        processed += 1
    return processed


def sidecar_command(name, args=None, wait=False):
    """Enqueue one HMAC-signed owner command for the execution sidecar.

    V101-NEW-001: commands are signed envelopes; the sidecar rejects any file
    that does not verify against the shared COMMAND_HMAC_KEY.
    """
    cid = uuid.uuid4().hex
    payload = {'command_id': cid, 'command': name, 'args': args or {}, 'created_at': time.time()}
    signed = envelope.sign_envelope(
        producer='telegram-broker', purpose=envelope.BUS_COMMAND, payload=payload,
        ttl_seconds=int(os.getenv('COMMAND_MAX_AGE_SECONDS', '120')) + 30)
    atomic_write_json(COMMAND_INBOX / f'{cid}.json', signed)
    audit('telegram_command_enqueued', actor='telegram-owner', details={'command_id': cid, 'command': name})
    if not wait:
        return {'ok': True, 'result': 'queued', 'command_id': cid}
    result_path = RUNTIME / f'command_result_{cid}.json'
    deadline = time.time() + 8
    while time.time() < deadline:
        result = read_json(result_path, None)
        if result is not None:
            result_path.unlink(missing_ok=True)
            return result
        time.sleep(0.2)
    return {'ok': False, 'result': 'sidecar command outcome is uncertain; check status/reconciliation and do not repeat blindly', 'command_id': cid}


def ft_call(method, endpoint):
    if not FT_PASS:
        return {'ok': False, 'error': 'Freqtrade API password not configured'}
    try:
        response = requests.request(
            method, FT_BASE + endpoint, auth=HTTPBasicAuth(FT_USER, FT_PASS), timeout=12
        )
        response.raise_for_status()
        try: payload = response.json()
        except Exception: payload = {'text': response.text}
        return {'ok': True, 'data': payload}
    except Exception as exc:
        return {'ok': False, 'error': str(exc)}


def confirm_button(label, action, args=None):
    token = CB.issue(action, args)
    audit('telegram_confirmation_issued', actor='telegram-owner', details={'action': action})
    return {'text': label, 'callback_data': 'confirm|' + token}


# ---- V19.1 Sharia screening controls (master protocol 8.7) ----
def normalize_pair_input(text: str) -> tuple[str | None, str]:
    """Normalize 'BTC/USDT' or 'BTCUSDT' to the base asset; reject the rest."""
    candidate = str(text or '').strip().upper()
    if '/' in candidate and not candidate.endswith('/USDT'):
        return None, 'only USDT-quoted Binance Spot pairs are supported (e.g. BTC/USDT)'
    match = PAIR_RE.match(candidate)
    if not match:
        return None, ('unrecognized pair format; use BASE/USDT (e.g. BTC/USDT). '
                      'Futures, margin and non-USDT symbols are rejected.')
    base = match.group(1)
    if base.endswith(('UP', 'DOWN', 'BULL', 'BEAR')) and len(base) > 4:
        return None, 'leveraged tokens are excluded'
    if base in {'BNB'}:
        return None, 'BNB is excluded by policy; the bot never buys or depends on BNB'
    return base, 'ok'


def sharia_scan_request(base: str, priority: str = 'manual') -> dict:
    if priority == 'bulk':
        request_id = f'bulk-all-{uuid.uuid4().hex[:8]}'
        payload = {'request_id': request_id, 'pair': '*/USDT', 'base': '*',
                   'priority': 'bulk', 'requested_by': 'telegram-owner'}
    else:
        request_id = f'manual-{base}-{uuid.uuid4().hex[:8]}'
        payload = {'request_id': request_id, 'pair': f'{base}/USDT', 'base': base,
                   'priority': 'manual', 'requested_by': 'telegram-owner'}
    signed = envelope.sign_envelope(
        producer='telegram-broker', purpose=envelope.BUS_SHARIA_REQUEST,
        payload=payload, ttl_seconds=600)
    SHARIA_QUEUE_INBOX.mkdir(parents=True, exist_ok=True)
    atomic_write_json(SHARIA_QUEUE_INBOX / f'request_{request_id}.json', signed)
    audit('telegram_sharia_scan_requested', actor='telegram-owner',
          details={'request_id': request_id, 'base': base, 'priority': priority})
    return {'request_id': request_id}


def sharia_owner_decision(action: str, args: dict) -> dict:
    """Send one owner decision bound to the exact report bytes shown."""
    decision_id = uuid.uuid4().hex
    normalized = str(action).upper().strip()
    if normalized not in {'APPROVE', 'REJECT'}:
        raise ValueError('invalid Sharia owner decision')
    payload = {
        'decision_id': decision_id,
        'action': normalized,
        'base': str(args['base']).upper(),
        'pair': f'{str(args["base"]).upper()}/USDT',
        'proposal_request_id': str(args['proposal_request_id']),
        'report_file': str(args['report_file']),
        'report_sha256': str(args['report_sha256']).lower(),
        'decided_at': datetime.now(timezone.utc).isoformat(),
    }
    if normalized == 'APPROVE' and args.get('scope_review_only') is True:
        payload['scope_confirmation'] = SCOPE_CONFIRMATION
    signed = envelope.sign_envelope(
        producer='telegram-broker', purpose=envelope.BUS_SHARIA_DECISION,
        payload=payload, ttl_seconds=600)
    SHARIA_DECISION_INBOX.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        SHARIA_DECISION_INBOX / f'decision_{decision_id}.json', signed)
    audit('telegram_sharia_owner_decision', actor='telegram-owner', details={
        'decision_id': decision_id, 'base': payload['base'],
        'action': normalized, 'report_sha256': payload['report_sha256']})
    return {'decision_id': decision_id, 'action': normalized,
            'base': payload['base']}


def _sharia_service_status() -> str:
    health = read_json(SHARIA_RUNTIME_DIR / 'health.json', None)
    lines = ['V19.1 Sharia screening service']
    health_ready = False
    if not isinstance(health, dict):
        lines.append('health: NOT READY — no heartbeat file (fail-closed)')
    else:
        try:
            age = time.time() - float(health.get('ts'))
            heartbeat_fresh = -30 <= age < 120
        except (TypeError, ValueError, OverflowError):
            age = float('inf')
            heartbeat_fresh = False
        health_ready = bool(
            heartbeat_fresh
            and health.get('ok') is True
            and health.get('ready_for_screening') is True
        )
        detail = 'READY' if health_ready else (
            f'NOT READY (heartbeat age={age:.0f}s, ok={health.get("ok")}, '
            f'ready_for_screening={health.get("ready_for_screening")})')
        lines.append('health: ' + detail)
        lines.append('queue: ' + json.dumps(health.get('queue', {}), sort_keys=True))
        lines.append(
            f'completed today: {health.get("completed_today")}; '
            f'cost attempts: {health.get("cost_events_today")} / quota '
            f'{health.get("daily_quota")}')
        backend = health.get('backend', {})
        lines.append(
            f'local backend: {backend.get("name", "unknown")}; '
            f'available={backend.get("available")}; '
            f'external AI={backend.get("external_ai")} '
            f'({backend.get("reason", "")})')
        last_done = health.get('last_done') or {}
        last_failed = health.get('last_failed') or {}
        if last_done:
            lines.append(f'last completed: {last_done.get("base")} -> {last_done.get("final_code")} at {last_done.get("finished_at")}')
        if last_failed:
            lines.append(f'last failed: {last_failed.get("base")} ({str(last_failed.get("error"))[:120]}) at {last_failed.get("finished_at")}')
        lines.append(f'idle scanning: {health.get("idle_scanning")}')
    try:
        gate = ShariaFilter(SHARIA_FILE)
        eligible = gate.current_halal_symbols()
        verified = sum(1 for base in gate.records if gate.is_record_verified(base))
        lines.append(
            f'verified status cache: {verified}/{len(gate.records)} records '
            '(signature, report hash, controller and expiry checked)')
        lines.append(
            'trade-eligible now: ' +
            (', '.join(eligible) or 'NONE (fail-closed)'))
    except Exception as exc:
        lines.append('verified status cache: REJECTED (fail-closed): ' + str(exc)[:240])
        lines.append('trade-eligible now: NONE (fail-closed)')
    if not health_ready:
        lines.append('new screening requests are NOT READY; cached labels are not treated as service health')
    lines.append(NOT_FATWA)
    return '\n'.join(lines)


def _universe_status() -> str:
    try:
        snapshot = load_current(
            UNIVERSE_CURRENT,
            max_age_seconds=env_int(
                'MAX_UNIVERSE_AGE_SECONDS', 1800, 1, 86_400),
        )
    except Exception as exc:
        return 'Universe FAIL-CLOSED: pointer/snapshot validation failed: ' + str(exc)[:500]
    return json.dumps({
        'validated': True,
        'snapshot_hash': snapshot['snapshot_hash'],
        'generated_at': snapshot['generated_at'],
        'snapshot_file': snapshot['snapshot_file'],
        'selection': snapshot['selection'],
        'pairs': snapshot['pairs'],
    }, indent=2)


def _latest_sharia_report(base: str) -> str:
    files = sorted(SHARIA_RESULTS_DIR.glob('result_*.json'),
                   key=lambda p: p.stat().st_mtime, reverse=True)[:300]
    for path in files:
        try:
            payload = envelope.read_verified_file(
                path, purpose=envelope.BUS_SHARIA_RESULT,
                expected_producers={'sharia-screener'})
            payload = verify_attached(payload, purpose=RESULT_PURPOSE)
        except Exception:
            continue
        if str(payload.get('base', '')).upper() != base:
            continue
        return '\n'.join([
            f'V19.1 screening — {base}/USDT',
            f'request: {payload.get("request_id")}',
            f'final_code: {payload.get("final_code")}',
            f'direct result: {payload.get("direct_result")}',
            f'validated: {payload.get("validated")}',
            f'confidence: {payload.get("confidence_level")}',
            f'human escalation required: {payload.get("human_escalation_required")}',
            f'next rescreen: {payload.get("next_rescreen_date")}',
            f'completed: {payload.get("completed_at")}',
            f'report file: {payload.get("report_file")}',
            (f'error: {payload.get("error")}' if payload.get('error') else ''),
            NOT_FATWA,
        ])
    return f'No verified V19.1 screening result found for {base}/USDT yet.'


def _latest_local_review_card(base: str) -> tuple[str, list[list[dict]] | None]:
    """Render the newest attested local proposal and hash-bound decisions."""
    files = sorted(SHARIA_RESULTS_DIR.glob('result_*.json'),
                   key=lambda p: p.stat().st_mtime, reverse=True)[:300]
    for path in files:
        try:
            payload = envelope.read_verified_file(
                path, purpose=envelope.BUS_SHARIA_RESULT,
                expected_producers={'sharia-screener'})
            payload = verify_attached(payload, purpose=RESULT_PURPOSE)
            if str(payload.get('base', '')).upper() != base:
                continue
            report_name = str(payload.get('report_file', ''))
            report_path = (SHARIA_REPORTS_DIR / report_name).resolve()
            reports_root = SHARIA_REPORTS_DIR.resolve()
            if reports_root not in report_path.parents or not report_path.is_file():
                continue
            raw = report_path.read_bytes()
            report_sha = hashlib.sha256(raw).hexdigest()
            if report_sha != str(payload.get('report_sha256', '')).lower():
                continue
            report = json.loads(raw)
            review = report.get('local_review')
            if not isinstance(review, dict):
                return _latest_sharia_report(base), None
            if (report.get('final_code') != 'NO_TRADE_INFO' or
                    review.get('owner_decision_required') is not True):
                return _latest_sharia_report(base), None
            failed = sorted(
                name for name, passed in (review.get('green_checks') or {}).items()
                if passed is not True)
            hits = [
                item for item in [*(review.get('hits') or []),
                                  *(review.get('clean_hits') or [])]
                if isinstance(item, dict)
            ]
            lines = [
                f'V19.1 local evidence review - {base}/USDT',
                f'disposition: {review.get("disposition")}',
                f'promotable: {review.get("promotable") is True}',
                f'scope review only: {review.get("scope_review_only") is True}',
                (f'proof checks: {len(review.get("green_checks") or {}) - len(failed)} '
                 f'passed; failed: {", ".join(failed) or "none"}'),
                f'report SHA-256: {report_sha}',
            ]
            for index, hit in enumerate(hits[:4], start=1):
                lines.append(
                    f'quote {index} [{hit.get("narrative", "")}; '
                    f'negated={hit.get("negated")}]: '
                    f'{str(hit.get("quote", ""))[:500]}')
            if len(hits) > 4:
                lines.append(f'{len(hits) - 4} additional quote(s) are in {report_name}.')
            lines.extend([
                ('Approval is an owner operational decision over these exact '
                 'stored bytes; it is research screening, not a fatwa.'),
                NOT_FATWA,
            ])
            args = {
                'base': base,
                'proposal_request_id': str(payload.get('request_id', '')),
                'report_file': report_name,
                'report_sha256': report_sha,
                'scope_review_only': review.get('scope_review_only') is True,
            }
            buttons = [[confirm_button(
                'REJECT - keep blocked', 'sharia_reject', args)]]
            if review.get('promotable') is True:
                buttons.insert(0, [confirm_button(
                    'APPROVE exact evidence', 'sharia_approve', args)])
            return '\n'.join(lines), buttons
        except Exception:
            continue
    return f'No verified local review proposal found for {base}/USDT yet.', None


def menu():
    # V10.1 owner controls: original V4.9.16 set, V8.1 execution/universe
    # controls, and the V19.1 Sharia screening controls.
    return [
        [{'text': '▶️ Resume Signals', 'callback_data': 'do|entries_on_confirm'},
         {'text': '⛔ Stop Auto-Trade', 'callback_data': 'do|entries_off'}],
        [{'text': '⏸ Pause New Signals', 'callback_data': 'do|entries_off'},
         {'text': '📊 Private Status', 'callback_data': 'do|status'}],
        [{'text': '🛑 Emergency Sell Coin', 'callback_data': 'do|emergency_help'},
         {'text': '📈 Last Signal', 'callback_data': 'do|last_signal'}],
        [{'text': '💰 Profit Report', 'callback_data': 'do|profit'},
         {'text': '💵 Balance', 'callback_data': 'do|balance'}],
        [{'text': '⚠️ Error / Logs', 'callback_data': 'do|logs'},
         {'text': '🔁 Restart WebSocket', 'callback_data': 'do|restart_ws_confirm'}],
        [{'text': '⚙️ Settings', 'callback_data': 'do|settings'},
         {'text': '🧪 Self-Test Status', 'callback_data': 'do|selftest'}],
        [{'text': '💵 Trade Size & Slots', 'callback_data': 'do|sizing_help'},
         {'text': '📉 Backtest Status', 'callback_data': 'do|backtest'}],
        [{'text': '🔄 Reload Configuration', 'callback_data': 'do|reload_confirm'}],
        [{'text': '🛡 Fixed OCO', 'callback_data': 'do|mode_fixed'},
         {'text': '📈 Trailing only', 'callback_data': 'do|mode_trailing'}],
        [{'text': '🔗 OCO + trailing', 'callback_data': 'do|mode_oco_trailing'},
         {'text': '🔄 Convert position', 'callback_data': 'do|convert_help'}],
        [{'text': '⚖️ Move stop to break-even', 'callback_data': 'do|be_help'},
         {'text': '🔒 Lock profit', 'callback_data': 'do|profit_help'}],
        [{'text': '🧾 Protection status', 'callback_data': 'do|status'},
         {'text': '🔁 Reconcile orders', 'callback_data': 'do|reconcile'}],
        [{'text': '🌐 Universe status', 'callback_data': 'do|universe'},
         {'text': '🕌 Sharia service', 'callback_data': 'do|sharia_service'}],
        [{'text': '🔍 Scan one coin', 'callback_data': 'do|scan_help'},
         {'text': '🕌 Scan ALL Spot/USDT', 'callback_data': 'do|scanall_confirm'}],
        [{'text': '📜 Sharia report', 'callback_data': 'do|sharia_report_help'},
         {'text': '🕌 Sharia cache', 'callback_data': 'do|sharia'}],
        [{'text': '🚀 Deployment status', 'callback_data': 'do|deploy'},
         {'text': '❓ Help', 'callback_data': 'do|help'}],
    ]


def help_text():
    return (
        'Commands: /menu /status /orders /start /stop /pause /balance /profit /logs /reload '
        '/fixed_oco /trailing_only /oco_trailing /convert SYMBOL MODE /breakeven SYMBOL '
        '/lockprofit SYMBOL PCT /reconcile /universe /sharia /shariastatus '
        '/scan BASE/USDT /scanall /shariareport BASE /deploy /lastsignal '
        '/restartws /setsize USDT /setmax N /selftest /backtest. '
        'Typing a pair like BTC/USDT (or BTCUSDT) queues a V19.1 scan for it. '
        'Start, reload, WebSocket restart, protection conversion, break-even, profit-lock, '
        'scan-all, and emergency exit require one-time confirmation.'
    )


def _latest_signal():
    files = []
    for folder in (SIGNAL_INBOX, SIGNAL_PROCESSED, SIGNAL_REJECTED):
        files.extend(folder.glob('*.json'))
    if not files:
        return 'No signal file has been recorded.'
    path = max(files, key=lambda p: p.stat().st_mtime)
    return json.dumps({'location': path.parent.name, 'signal': read_json(path, {})}, indent=2)[:3800]


def _tail_audit(lines=30):
    path = AUDIT_DIR / 'events.jsonl'
    if not path.exists():
        return 'No audit events recorded.'
    rows = path.read_text(encoding='utf-8', errors='replace').splitlines()[-lines:]
    return '\n'.join(rows)[-3800:]


def _settings():
    health = read_json(RUNTIME / 'sidecar_health.json', {}) or {}
    return json.dumps({
        'release': _release_label(),
        'release_hash': envelope.installed_release_hash(),
        'sidecar': health,
        'configured': {
            'execution_mode': os.getenv('EXECUTION_MODE', 'simulation'),
            'universe_limit': os.getenv('UNIVERSE_LIMIT', '50'),
            'min_listing_age_days': os.getenv('MIN_LISTING_AGE_DAYS', '30'),
            'min_quote_volume_usdt': os.getenv('MIN_QUOTE_VOLUME_USDT', '1000000'),
            'max_spread_ratio': os.getenv('MAX_SPREAD_RATIO', '0.005'),
            'sharia_signal_gate_mode': os.getenv('SHARIA_SIGNAL_GATE_MODE', 'cached'),
            'telegram_owner_configured': bool(OWNER),
            'freqtrade_api_password_configured': bool(FT_PASS),
        }
    }, indent=2)


def _validation_status():
    candidates = [RUNTIME / 'validation_status.json', Path('/app/VALIDATION_STATUS.json')]
    for path in candidates:
        if path.exists():
            return path.read_text(encoding='utf-8')[:3800]
    return 'No installed validation status file. Run deploy/verify_release.sh.'


def _backtest_status():
    """H-001 fix: never present the deterministic self-test artifact as a
    production backtest. A verified artifact must be installed explicitly."""
    verified = RUNTIME / 'verified_backtest.json'
    if verified.exists():
        return ('VERIFIED PRODUCTION BACKTEST:\n'
                + verified.read_text(encoding='utf-8')[:3500])
    selftest = Path('/app/legacy_core/logs/backtest_results_selftest.json')
    lines = ['NO VERIFIED PRODUCTION BACKTEST is installed.',
             'The strategy has not proven an edge; previous real backtests lost after fees.',
             'Run the exact-strategy Freqtrade backtest and install its verified artifact.']
    if selftest.exists():
        lines.append('\n[Deterministic unit self-test artifact — NOT performance evidence:]')
        lines.append(selftest.read_text(encoding='utf-8')[:1500])
    return '\n'.join(lines)[:3900]


def route(action, chat):
    if action == 'entries_on_confirm':
        button = confirm_button('✅ CONFIRM resume entries', 'resume_entries')
        send('Confirm enabling new strategy signals and sidecar entries.', chat, [[button], [{'text': '❌ Cancel', 'callback_data': 'do|help'}]])
    elif action == 'entries_off':
        # Disarm the only order-owning component first. Freqtrade may stall or
        # fail, but a delayed signal must not remain executable in that window.
        result = sidecar_command('entries', {'enabled': False}, wait=True)
        ft = ft_call('POST', '/pause')
        send(json.dumps({'sidecar': result, 'freqtrade': ft}, indent=2), chat)
    elif action.startswith('mode_'):
        mode = {'mode_fixed': 'FIXED_OCO', 'mode_trailing': 'TRAILING_ONLY', 'mode_oco_trailing': 'OCO_TRAILING'}[action]
        _ask_confirm(chat, '✅ CONFIRM default protection mode', 'set_mode',
                     {'mode': mode}, f'Confirm new-entry protection mode: {mode}.')
    elif action == 'status':
        sidecar = sidecar_command('status', wait=True)
        freqtrade = ft_call('GET', '/status')
        send(json.dumps({'release_hash': envelope.installed_release_hash(),
                         'sidecar': sidecar, 'freqtrade': freqtrade}, indent=2)[:3900], chat)
    elif action == 'orders':
        send(json.dumps(sidecar_command('orders', wait=True), indent=2)[:3900], chat)
    elif action == 'balance':
        send(json.dumps(sidecar_command('balance', wait=True), indent=2), chat)
    elif action == 'profit':
        send(json.dumps(sidecar_command('profit', wait=True), indent=2), chat)
    elif action == 'logs':
        ft = ft_call('GET', '/logs?limit=25')
        send((json.dumps(ft, indent=2) + '\n\nAUDIT:\n' + _tail_audit(20))[-3900:], chat)
    elif action == 'restart_ws_confirm':
        button = confirm_button('✅ CONFIRM WebSocket restart', 'restart_stream')
        send('Confirm restarting the Binance user-data stream.', chat, [[button], [{'text': '❌ Cancel', 'callback_data': 'do|help'}]])
    elif action == 'reload_confirm':
        button = confirm_button('✅ CONFIRM configuration reload', 'reload_config')
        send('Confirm Freqtrade configuration and Sharia status reload.', chat, [[button], [{'text': '❌ Cancel', 'callback_data': 'do|help'}]])
    elif action == 'reconcile':
        send(json.dumps(sidecar_command('reconcile', wait=True), indent=2), chat)
    elif action == 'universe':
        send(_universe_status()[:3900], chat)
    elif action == 'sharia':
        send(_sharia_service_status()[:3900], chat)
    elif action == 'sharia_service':
        send(_sharia_service_status(), chat)
    elif action == 'scan_help':
        send('Use /scan BTC/USDT (or just type BTC/USDT). BTCUSDT is normalized. '
             'Non-USDT, futures and leveraged symbols are rejected; the screener '
             'verifies live Binance listing before scanning.', chat)
    elif action == 'scanall_confirm':
        button = confirm_button('✅ CONFIRM scan ALL Spot/USDT pairs', 'scan_all')
        send('Scan ALL current Binance Spot/USDT pairs under V19.1?\n'
             '⚠️ This queues local source retrieval and evidence review for '
             'hundreds of assets. No paid AI API is used. Bulk scans run at '
             'low priority behind signal and manual scans.', chat,
             [[button], [{'text': '❌ Cancel', 'callback_data': 'do|help'}]])
    elif action == 'sharia_report_help':
        send('Use /shariareport BASE (e.g. /shariareport ETH) for the latest verified V19.1 result.', chat)
    elif action == 'deploy':
        send(json.dumps(read_json(RUNTIME / 'deployment_status.json', {}), indent=2), chat)
    elif action == 'last_signal':
        send(_latest_signal(), chat)
    elif action == 'settings':
        send(_settings(), chat)
    elif action == 'selftest':
        send(_validation_status(), chat)
    elif action == 'backtest':
        send(_backtest_status(), chat)
    elif action == 'emergency_help':
        send('Use /emergency ADAUSDT. A separate one-time confirmation is required.', chat)
    elif action == 'convert_help':
        send('Use /convert ADAUSDT TRAILING_ONLY (or FIXED_OCO / OCO_TRAILING).', chat)
    elif action == 'be_help':
        send('Use /breakeven ADAUSDT.', chat)
    elif action == 'profit_help':
        send('Use /lockprofit ADAUSDT 0.2', chat)
    elif action == 'sizing_help':
        send('Use /setsize 100 and /setmax 2. Values affect the preserved execution core.', chat)
    else:
        send(help_text(), chat)


def _confirm_action(action, args, chat):
    if action == 'resume_entries':
        ft = ft_call('POST', '/start')
        sidecar = sidecar_command('entries', {'enabled': True}, wait=True)
        send(json.dumps({'sidecar': sidecar, 'freqtrade': ft}, indent=2), chat)
    elif action == 'restart_stream':
        send(json.dumps(sidecar_command('restart_stream', wait=True), indent=2), chat)
    elif action == 'reload_config':
        ft = ft_call('POST', '/reload_config')
        sidecar = sidecar_command('reload_sharia', wait=True)
        send(json.dumps({'sidecar': sidecar, 'freqtrade': ft}, indent=2), chat)
    elif action == 'set_mode':
        send(json.dumps(sidecar_command('mode', args, wait=True), indent=2), chat)
    elif action == 'scan_all':
        outcome = sharia_scan_request('*', priority='bulk')
        send('Queued V19.1 scan of ALL current Binance Spot/USDT pairs '
             f'(request {outcome["request_id"]}). Progress: 🕌 Sharia service.\n' + NOT_FATWA, chat)
    elif action in {'sharia_approve', 'sharia_reject'}:
        decision = sharia_owner_decision(
            'APPROVE' if action == 'sharia_approve' else 'REJECT', args)
        send(
            f'Queued {decision["action"]} decision for '
            f'{decision["base"]}/USDT (decision {decision["decision_id"]}). '
            'The screener will re-hash the exact evidence and fail closed on '
            'any mismatch. Check /shariareport again for the signed outcome.\n'
            + NOT_FATWA,
            chat)
    elif action in {'convert', 'break_even', 'lock_profit', 'emergency_exit', 'set_size', 'set_max'}:
        send(json.dumps(sidecar_command(action, args, wait=True), indent=2), chat)
    else:
        send('Unsupported confirmation action.', chat)


def _ask_confirm(chat, label, action, args=None, text='Confirm action.'):
    button = confirm_button(label, action, args)
    send(text, chat, [[button], [{'text': '❌ Cancel', 'callback_data': 'do|help'}]])


def handle_message(message):
    chat = str(message.get('chat', {}).get('id', ''))
    user_id = message.get('from', {}).get('id')
    text = str(message.get('text', '')).strip()
    if not is_owner(user_id, chat):
        audit('telegram_unauthorized', severity='WARNING', details={'user_id': user_id})
        return
    parts = text.split()
    cmd = parts[0].lower() if parts else ''
    audit('telegram_message', actor='telegram-owner', details={'command': cmd})
    if cmd in ('/menu', '/owner'):
        send(f'{_release_label()} owner control panel', chat, menu())
    elif cmd == '/start': route('entries_on_confirm', chat)
    elif cmd in ('/stop', '/pause'): route('entries_off', chat)
    elif cmd == '/status': route('status', chat)
    elif cmd == '/orders': route('orders', chat)
    elif cmd == '/balance': route('balance', chat)
    elif cmd == '/profit': route('profit', chat)
    elif cmd == '/logs': route('logs', chat)
    elif cmd == '/reload': route('reload_confirm', chat)
    elif cmd == '/fixed_oco': route('mode_fixed', chat)
    elif cmd == '/trailing_only': route('mode_trailing', chat)
    elif cmd == '/oco_trailing': route('mode_oco_trailing', chat)
    elif cmd == '/reconcile': route('reconcile', chat)
    elif cmd == '/universe': route('universe', chat)
    elif cmd in ('/sharia', '/halal'): route('sharia', chat)
    elif cmd in ('/shariastatus', '/shariaservice'): route('sharia_service', chat)
    elif cmd == '/scanall': route('scanall_confirm', chat)
    elif cmd == '/scan' and len(parts) == 2:
        base, why = normalize_pair_input(parts[1])
        if not base:
            send('Scan rejected: ' + why, chat); return
        outcome = sharia_scan_request(base, priority='manual')
        send(f'Queued V19.1 scan for {base}/USDT (request {outcome["request_id"]}). '
             'Result: /shariareport ' + base + '\n' + NOT_FATWA, chat)
    elif cmd == '/shariareport' and len(parts) == 2:
        base, why = normalize_pair_input(parts[1])
        if not base:
            send('Rejected: ' + why, chat); return
        card, buttons = _latest_local_review_card(base)
        send(card, chat, buttons)
    elif cmd == '/deploy': route('deploy', chat)
    elif cmd == '/lastsignal': route('last_signal', chat)
    elif cmd == '/settings': route('settings', chat)
    elif cmd == '/selftest': route('selftest', chat)
    elif cmd == '/backtest': route('backtest', chat)
    elif cmd == '/restartws': route('restart_ws_confirm', chat)
    elif cmd == '/convert' and len(parts) == 3:
        mode = parts[2].upper()
        if mode not in {'FIXED_OCO', 'TRAILING_ONLY', 'OCO_TRAILING'}:
            send('Invalid mode.', chat); return
        _ask_confirm(chat, '✅ CONFIRM protection conversion', 'convert',
                     {'symbol': parts[1].upper(), 'mode': mode},
                     f'Confirm conversion of {parts[1].upper()} to {mode}.')
    elif cmd == '/breakeven' and len(parts) == 2:
        _ask_confirm(chat, '✅ CONFIRM break-even move', 'break_even',
                     {'symbol': parts[1].upper()}, f'Confirm break-even replacement for {parts[1].upper()}.')
    elif cmd == '/lockprofit' and len(parts) >= 2:
        try: pct = float(parts[2]) if len(parts) > 2 else 0.2
        except ValueError: send('Invalid profit percentage.', chat); return
        if not math.isfinite(pct) or pct <= 0 or pct > 100:
            send('Profit percentage must be finite and within (0, 100].', chat); return
        _ask_confirm(chat, '✅ CONFIRM profit lock', 'lock_profit',
                     {'symbol': parts[1].upper(), 'profit_pct': pct},
                     f'Confirm locking at least {pct}% profit for {parts[1].upper()}.')
    elif cmd == '/emergency' and len(parts) == 2:
        _ask_confirm(chat, '✅ CONFIRM emergency exit', 'emergency_exit',
                     {'symbol': parts[1].upper()}, f'Confirm emergency exit for {parts[1].upper()}.')
    elif cmd == '/setsize' and len(parts) == 2:
        try: value = float(parts[1])
        except ValueError: send('Invalid USDT amount.', chat); return
        if not math.isfinite(value) or value <= 0:
            send('USDT amount must be a positive finite number.', chat); return
        _ask_confirm(chat, '✅ CONFIRM trade-size change', 'set_size', {'usdt': value}, f'Confirm trade size {value} USDT.')
    elif cmd == '/setmax' and len(parts) == 2:
        try: value = int(parts[1])
        except ValueError: send('Invalid slot count.', chat); return
        _ask_confirm(chat, '✅ CONFIRM slot change', 'set_max', {'count': value}, f'Confirm maximum {value} positions.')
    elif len(parts) == 1 and normalize_pair_input(parts[0])[0]:
        # Direct owner message like "DGB/USDT" or "DGBUSDT" queues a manual scan.
        base, _ = normalize_pair_input(parts[0])
        outcome = sharia_scan_request(base, priority='manual')
        send(f'Queued V19.1 scan for {base}/USDT (request {outcome["request_id"]}). '
             'Result: /shariareport ' + base + '\n' + NOT_FATWA, chat)
    else:
        send(help_text(), chat)


def handle_callback(callback):
    user_id = callback.get('from', {}).get('id')
    chat = str(callback.get('message', {}).get('chat', {}).get('id', ''))
    data = str(callback.get('data', ''))
    callback_id = callback.get('id')
    try:
        requests.post(BASE + '/answerCallbackQuery', data={'callback_query_id': callback_id}, timeout=10)
    except Exception as exc:
        # Callback acknowledgement is best-effort; never stop control supervision.
        log.debug('answerCallbackQuery failed: %s', _redact_secrets(exc))
    if not is_owner(user_id, chat):
        audit('telegram_callback_unauthorized', severity='WARNING', details={'user_id': user_id})
        return
    if data.startswith('do|'):
        route(data.split('|', 1)[1], chat)
        return
    if data.startswith('confirm|'):
        item, reason = CB.consume(data.split('|', 1)[1])
        if not item:
            audit('telegram_confirmation_rejected', severity='WARNING', details={'reason': reason})
            send('Confirmation rejected: ' + reason, chat)
            return
        audit('telegram_confirmation_consumed', actor='telegram-owner', details={'action': item['action']})
        _confirm_action(item['action'], item['args'], chat)


def _load_offset() -> int:
    """V101-NEW-009: restore the committed getUpdates offset across restarts."""
    data = read_json(OFFSET_PATH, {}) or {}
    try:
        return max(0, int(data.get('offset', 0)))
    except Exception:
        return 0


def _store_offset(offset: int):
    atomic_write_json(OFFSET_PATH, {'offset': int(offset), 'ts': time.time()})


def _claim_update(update_id: object, offset: int) -> tuple[int, bool]:
    """Durably claim one Telegram update before any command side effect.

    Telegram confirms an update once a later ``getUpdates`` request supplies an
    offset above its ID. Persisting that next offset before dispatch gives the
    broker at-most-once command execution across process or host crashes. If
    the durable write fails, the caller raises before executing the command and
    Telegram can safely retry it. Replayed or out-of-order updates below the
    committed offset are ignored.
    """
    if isinstance(update_id, bool) or not isinstance(update_id, int) or update_id < 0:
        raise ValueError('Telegram update_id must be a non-negative integer')
    committed = max(0, int(offset))
    if update_id < committed:
        audit('telegram_update_replay_skipped', details={
            'update_id': update_id, 'committed_offset': committed,
        })
        return committed, False
    next_offset = update_id + 1
    _store_offset(next_offset)
    return next_offset, True


def main():
    logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO'), format='%(asctime)s %(levelname)s %(name)s %(message)s')
    if not TOKEN or not OWNER:
        raise SystemExit('TELEGRAM_BOT_TOKEN and TELEGRAM_OWNER_CHAT_ID required')
    try:
        envelope.load_key(envelope.BUS_COMMAND)
        envelope.load_key(envelope.BUS_SHARIA_REQUEST)
        envelope.load_key(envelope.BUS_SHARIA_DECISION)
    except envelope.EnvelopeError as exc:
        raise SystemExit(f'BUS KEYS MISSING: {exc}') from exc
    offset = _load_offset()
    while True:
        try:
            deliver_sidecar_notifications()
            response = requests.get(
                BASE + '/getUpdates',
                params={'offset': offset, 'timeout': 25, 'allowed_updates': json.dumps(['message', 'callback_query'])},
                timeout=35,
            ).json()
            if not response.get('ok', False):
                raise RuntimeError('Telegram getUpdates returned an error: ' + str(response)[:500])
            atomic_write_json(RUNTIME / 'telegram_health.json', {
                'ok': True, 'ts': time.time(), 'offset': offset,
            })
            for update in response.get('result', []):
                # Claim and fsync each update before dispatch. Assignment occurs
                # before a handler can fail, and the persisted offset survives
                # a restart, so side effects such as /scan are not replayed.
                offset, claimed = _claim_update(update.get('update_id'), offset)
                if not claimed:
                    continue
                if 'message' in update: handle_message(update['message'])
                if 'callback_query' in update: handle_callback(update['callback_query'])
            deliver_sidecar_notifications()
        except Exception as exc:
            # H-004: requests embeds the full attempted URL in its exception
            # text, and BASE contains the bot token. Writing the raw exception
            # to the shared health file and the container log leaked the token
            # to anyone who could read either. Never persist a raw exception
            # from a tokenized URL.
            safe = _redact_secrets(exc)
            atomic_write_json(RUNTIME / 'telegram_health.json', {
                'ok': False, 'ts': time.time(), 'error': safe,
                'error_type': type(exc).__name__,
            })
            log.warning('poll failed: %s', safe)
            time.sleep(3)

if __name__ == '__main__':
    main()
