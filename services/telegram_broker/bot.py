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
    SHARIA_DISCOVERY_CURRENT_DIR,
    SHARIA_FILE,
    SHARIA_QUEUE_INBOX,
    SHARIA_REPORTS_DIR,
    SHARIA_RESULTS_DIR,
    SHARIA_RUNTIME_DIR,
    SHARIA_SOURCE_REGISTRY,
    SIGNAL_INBOX,
    SIGNAL_PROCESSED,
    SIGNAL_REJECTED,
    TELEGRAM_ALERT_OUTBOX,
    UNIVERSE_CURRENT,
)
from services.common.sharia_attestation import RESULT_PURPOSE, verify_attached
from services.sharia_operator.source_review import candidate_bases
from services.sharia_screener.approval import SCOPE_CONFIRMATION
from services.sharia_screener.source_registry import SourceRegistry
from services.telegram_broker.authorization import is_owner
from services.telegram_broker.callbacks import CallbackStore
from services.universe_service.sharia_gate import load_sharia_gate
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
BOT_PRODUCT = os.getenv('BOT_PRODUCT', 'BINANA').strip().upper()
BOT_ENVIRONMENT = os.getenv('BOT_ENVIRONMENT', 'TESTNET').strip().upper()
BOT_INSTANCE_ID = os.getenv('BOT_INSTANCE_ID', 'BINANA-TN-TYO-01').strip().upper()
BASE = f'https://api.telegram.org/bot{TOKEN}'
CB = CallbackStore(ttl=int(os.getenv('CALLBACK_TTL_SECONDS', '120')))
FT_BASE = os.getenv('FREQTRADE_API_URL', 'http://freqtrade:8080/api/v1').rstrip('/')
FT_USER = os.getenv('FREQTRADE_API_USERNAME', 'freqtrade')
FT_PASS = os.getenv('FREQTRADE_API_PASSWORD', '')
OFFSET_PATH = RUNTIME / 'telegram_offset.json'
ALERT_DELIVERY_STATE = RUNTIME / 'telegram_alert_delivery.json'
ALERT_QUARANTINE = RUNTIME / 'telegram_alert_quarantine'
ALERT_OUTBOX_HEALTH = RUNTIME / 'telegram_alert_outbox_health.json'
SIGNAL_NOTIFICATION_STATE = RUNTIME / 'telegram_signal_notification_state.json'
PAIR_RE = re.compile(r'^([A-Z0-9]{2,20}?)(?:/USDT|USDT)?$')
NOT_FATWA = 'Research screening only — not a fatwa and not financial advice.'
BULK_SCAN_LIMITS = frozenset({10, 25, 50, 100})
BULK_SCAN_MIN = 1
BULK_SCAN_MAX = 100
MARKET_CONTEXT_FILE = Path(os.getenv(
    'MARKET_CONTEXT_FILE', RUNTIME.parent / 'market_context/current.json'))
MARKET_CONTEXT_HEALTH_FILE = Path(os.getenv(
    'MARKET_CONTEXT_HEALTH_FILE', RUNTIME / 'market_context/health.json'))
EXTERNAL_SIGNALS_STATUS = Path(os.getenv(
    'EXTERNAL_SIGNALS_STATUS',
    RUNTIME.parent / 'universe/external' / ('external_' + 'signals.json')))
API_READINESS_STATUS = RUNTIME / 'api_readiness_status.json'


def _telegram_message_data(text, chat_id=None, buttons=None) -> dict:
    identity = f'[{BOT_PRODUCT} | {BOT_ENVIRONMENT} | {BOT_INSTANCE_ID}]'
    rendered = str(text)
    if not rendered.startswith(identity):
        rendered = identity + '\n' + rendered
    data = {'chat_id': chat_id or OWNER, 'text': rendered[:4000]}
    if buttons:
        data['reply_markup'] = json.dumps({'inline_keyboard': buttons})
    return data


def send(text, chat_id=None, buttons=None):
    if not TOKEN:
        return
    data = _telegram_message_data(text, chat_id, buttons)
    response = requests.post(BASE + '/sendMessage', data=data, timeout=15)
    response.raise_for_status()


def edit_or_send(text, chat_id, message_id=None, buttons=None):
    """Edit a callback menu in place, falling back to a normal message.

    This is presentation-only.  A failed Telegram edit never retries or
    repeats a trading command; it merely sends the same owner-facing screen as
    a new message.
    """
    if not TOKEN:
        return
    if message_id is None:
        send(text, chat_id, buttons)
        return
    data = _telegram_message_data(text, chat_id, buttons)
    data['message_id'] = message_id
    try:
        response = requests.post(BASE + '/editMessageText', data=data, timeout=15)
        if (response.status_code == 400 and
                'message is not modified' in response.text.lower()):
            return
        response.raise_for_status()
    except Exception as exc:
        log.debug('editMessageText failed; sending a new menu: %s',
                  _redact_secrets(exc))
        send(text, chat_id, buttons)


def _load_alert_delivery_state() -> dict:
    """Load the alert dedupe journal without silently accepting corruption."""
    if not ALERT_DELIVERY_STATE.exists():
        return {}
    try:
        state = json.loads(ALERT_DELIVERY_STATE.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError('Telegram alert dedupe state is unreadable; delivery paused') from exc
    if not isinstance(state, dict) or not isinstance(state.get('delivered', {}), dict):
        raise RuntimeError('Telegram alert dedupe state has an invalid schema; delivery paused')
    return state


def _quarantine_alert(path: Path, reason: str) -> None:
    """Atomically remove an invalid alert from the live queue for inspection."""
    ALERT_QUARANTINE.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    payload_path = ALERT_QUARANTINE / f'{token}.payload'
    metadata_path = ALERT_QUARANTINE / f'{token}.json'
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        os.replace(path, payload_path)
        atomic_write_json(metadata_path, {
            'quarantine_id': token,
            'original_name': path.name[:255],
            'reason': str(reason)[:300],
            'sha256': digest,
            'quarantined_at': time.time(),
            'payload_file': payload_path.name,
        })
    except Exception:
        if payload_path.exists() and not path.exists():
            try:
                os.replace(payload_path, path)
            except OSError:
                pass
        raise


def _alert_outbox_health(*, blocked_reason: str = '') -> dict:
    now = time.time()
    pending = list(TELEGRAM_ALERT_OUTBOX.glob('*.json'))
    ages = []
    for path in pending:
        try:
            ages.append(max(0.0, now - path.stat().st_mtime))
        except OSError:
            continue
    dead_letters = list(ALERT_QUARANTINE.glob('*.json')) if ALERT_QUARANTINE.exists() else []
    payload = {
        'ok': not blocked_reason,
        'ts': now,
        'pending_alert_count': len(pending),
        'oldest_pending_alert_age_seconds': round(max(ages), 3) if ages else 0.0,
        'dead_letter_count': len(dead_letters),
        'blocked_reason': str(blocked_reason)[:300] or None,
    }
    atomic_write_json(ALERT_OUTBOX_HEALTH, payload)
    return payload


def deliver_sidecar_notifications(limit: int = 20) -> int:
    """Deliver durable sidecar alerts without malformed-file starvation."""
    try:
        state = _load_alert_delivery_state()
    except RuntimeError as exc:
        audit('telegram_alert_dedupe_state_invalid', severity='CRITICAL',
              details={'error': _redact_secrets(exc)})
        _alert_outbox_health(blocked_reason=str(exc))
        return 0
    delivered = state.get('delivered', {})
    processed = 0
    blocked_reason = ''
    scan_max = env_int('TELEGRAM_ALERT_SCAN_MAX', 1000, 20, 10000)
    for path in sorted(TELEGRAM_ALERT_OUTBOX.glob('*.json'))[:scan_max]:
        if processed >= max(0, int(limit)):
            break
        payload = read_json(path, None)
        if not isinstance(payload, dict):
            audit('telegram_alert_invalid', severity='ERROR', details={'file': path.name})
            try:
                _quarantine_alert(path, 'payload is not a JSON object')
            except Exception as exc:
                reason = 'failed to quarantine malformed alert: ' + _redact_secrets(exc)
                audit('telegram_alert_quarantine_failed', severity='CRITICAL',
                      details={'file': path.name, 'error': _redact_secrets(exc)})
                _alert_outbox_health(blocked_reason=reason)
                return processed
            continue
        notification_id = str(payload.get('notification_id') or '')
        if not re.fullmatch(r'[0-9a-f]{32}', notification_id) or path.stem != notification_id:
            audit('telegram_alert_invalid', severity='ERROR', details={'file': path.name})
            try:
                _quarantine_alert(path, 'notification_id or filename is invalid')
            except Exception as exc:
                reason = 'failed to quarantine invalid alert identifier: ' + _redact_secrets(exc)
                audit('telegram_alert_quarantine_failed', severity='CRITICAL',
                      details={'file': path.name, 'error': _redact_secrets(exc)})
                _alert_outbox_health(blocked_reason=reason)
                return processed
            continue
        if notification_id in delivered:
            path.unlink(missing_ok=True)
            continue
        try:
            # Execution alerts are owner-only; never honor a producer-supplied
            # destination for privileged account state.
            send(_redact_secrets(payload.get('text', '')), OWNER)
        except Exception as exc:
            blocked_reason = 'Telegram alert delivery failed: ' + _redact_secrets(exc)
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
            blocked_reason = 'Telegram alert dedupe persistence failed: ' + _redact_secrets(exc)
            audit('telegram_alert_dedupe_persist_failed', severity='CRITICAL', details={
                'notification_id': notification_id, 'error': _redact_secrets(exc),
            })
            break
        path.unlink(missing_ok=True)
        audit('telegram_alert_delivered', details={'notification_id': notification_id})
        processed += 1
    _alert_outbox_health(blocked_reason=blocked_reason)
    return processed


def _load_signal_notification_state() -> dict:
    """Load the durable signal-notification journal or fail closed.

    Replaying an archive after a corrupt journal could flood Telegram with old
    signal messages. Corruption therefore pauses this advisory notifier; it
    never changes entries, orders, the universe, or any execution state.
    """
    if not SIGNAL_NOTIFICATION_STATE.exists():
        return {'schema_version': 1, 'handled': {}}
    try:
        state = json.loads(SIGNAL_NOTIFICATION_STATE.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            'Telegram signal-notification state is unreadable; notifications paused'
        ) from exc
    if (not isinstance(state, dict) or state.get('schema_version') != 1 or
            not isinstance(state.get('handled'), dict)):
        raise RuntimeError(
            'Telegram signal-notification state has an invalid schema; '
            'notifications paused')
    return state


def _persist_signal_notification_state(handled: dict) -> None:
    max_ids = env_int('TELEGRAM_SIGNAL_NOTICE_DEDUPE_MAX', 5000, 100, 50000)
    if len(handled) > max_ids:
        handled = dict(sorted(
            handled.items(), key=lambda item: float(item[1]))[-max_ids:])
    atomic_write_json(SIGNAL_NOTIFICATION_STATE, {
        'schema_version': 1,
        'handled': handled,
        'updated_at': time.time(),
    })


def deliver_signal_notifications(limit: int = 20) -> int:
    """Notify the owner about recent authenticated terminal strategy signals.

    This observes only files already archived by the protected execution
    sidecar. It verifies the original Freqtrade HMAC envelope and renders a
    strict metadata allow-list; it cannot submit, repeat, approve, or alter an
    order. Processed means entry submission was accepted, not that a fill has
    been confirmed. Rejected means the signal remained blocked.
    """
    try:
        state = _load_signal_notification_state()
    except RuntimeError as exc:
        audit('telegram_signal_notification_state_invalid', severity='CRITICAL',
              details={'error': _redact_secrets(exc)})
        return 0

    handled = state['handled']
    now = time.time()
    max_age = env_int('TELEGRAM_SIGNAL_NOTICE_MAX_AGE_SECONDS', 300, 60, 3600)
    max_bytes = env_int('TELEGRAM_SIGNAL_NOTICE_MAX_BYTES', 65536, 1024, 1048576)
    scan_max = env_int('TELEGRAM_SIGNAL_NOTICE_SCAN_MAX', 1000, 20, 10000)
    candidates = []
    for folder, outcome in (
            (SIGNAL_PROCESSED, 'ENTRY SUBMISSION ACCEPTED'),
            (SIGNAL_REJECTED, 'BLOCKED / REJECTED')):
        for path in folder.glob('*.json'):
            try:
                modified = path.stat().st_mtime
            except OSError:
                continue
            if 0 <= now - modified <= max_age:
                candidates.append((modified, path, outcome))
    candidates.sort(key=lambda row: (row[0], row[1].name))

    notified = 0
    for _, path, outcome in candidates[:scan_max]:
        if notified >= max(0, int(limit)):
            break
        try:
            stat = path.stat()
            identity = hashlib.sha256(
                (outcome + '\0' + path.name + '\0' + str(stat.st_mtime_ns) +
                 '\0' + str(stat.st_size)).encode('utf-8')).hexdigest()
        except OSError:
            continue
        if identity in handled:
            continue
        try:
            if stat.st_size > max_bytes:
                raise ValueError('archived signal exceeds the notification size limit')
            raw_bytes = path.read_bytes()
            identity = hashlib.sha256(
                outcome.encode('utf-8') + b'\0' + raw_bytes).hexdigest()
            if identity in handled:
                continue
            raw = json.loads(raw_bytes.decode('utf-8'))
            payload = envelope.verify_envelope(
                raw, purpose=envelope.BUS_SIGNAL,
                expected_producers={'freqtrade-strategy'})
            signal_id = str(payload.get('signal_id', ''))
            pair = str(payload.get('pair', '')).upper()
            strategy = str(payload.get('strategy', ''))
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', signal_id):
                raise ValueError('invalid signal identifier')
            if not re.fullmatch(r'[A-Z0-9]{2,20}/USDT', pair):
                raise ValueError('invalid Spot/USDT pair')
            if strategy != 'IctSmcStrategy':
                raise ValueError('unexpected strategy identity')
            candle_time = _redact_secrets(payload.get('candle_time', ''), 80)
            entry_tag = _redact_secrets(payload.get('entry_tag', ''), 80)
        except Exception as exc:
            audit('telegram_signal_notification_invalid', severity='ERROR', details={
                'file': path.name[:255],
                'archive': path.parent.name[:80],
                'error_type': type(exc).__name__,
                'error': _redact_secrets(exc),
            })
            # Record this exact invalid archive as handled so one bad file
            # cannot generate an audit event on every poll cycle.
            try:
                handled[identity] = now
                _persist_signal_notification_state(handled)
            except Exception as persist_exc:
                audit('telegram_signal_notification_state_write_failed',
                      severity='CRITICAL', details={
                          'error_type': type(persist_exc).__name__,
                      })
                return notified
            continue

        lines = [
            'Automatic strategy signal result',
            f'Pair: {pair}',
            f'Signal ID: {signal_id}',
            f'Result: {outcome}',
        ]
        if candle_time:
            lines.append(f'Candle: {candle_time}')
        if entry_tag:
            lines.append(f'Entry tag: {entry_tag}')
        if outcome == 'ENTRY SUBMISSION ACCEPTED':
            lines.append('This is not a fill confirmation; use Open Trades for live state.')
        else:
            lines.append('No entry was authorised by this archived signal result.')
        lines.append(
            'Strategy, halal registry, universe, risk and execution gates remained authoritative.')
        try:
            send('\n'.join(lines), OWNER)
        except Exception as exc:
            audit('telegram_signal_notification_delivery_failed', severity='ERROR',
                  details={'signal_id': signal_id,
                           'error_type': type(exc).__name__})
            break
        handled[identity] = time.time()
        try:
            _persist_signal_notification_state(handled)
        except Exception as exc:
            # The terminal signal file remains untouched. At-least-once
            # notification is preferable to silently losing a signal notice.
            audit('telegram_signal_notification_state_write_failed',
                  severity='CRITICAL', details={
                      'signal_id': signal_id,
                      'error_type': type(exc).__name__,
                  })
            break
        audit('telegram_signal_notification_delivered', details={
            'signal_id': signal_id, 'pair': pair, 'outcome': outcome,
        })
        notified += 1
    return notified


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


def confirm_button(label, action, args=None, *, style='danger'):
    token = CB.issue(action, args)
    audit('telegram_confirmation_issued', actor='telegram-owner', details={'action': action})
    button = {'text': label, 'callback_data': 'confirm|' + token}
    if style in {'danger', 'success', 'primary'}:
        button['style'] = style
    return button


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
    normalized_priority = str(priority).lower().strip()
    if normalized_priority not in {'manual', 'bulk'}:
        raise ValueError('Sharia scan priority must be manual or bulk')
    if normalized_priority == 'bulk' and base == '*':
        request_id = f'bulk-all-{uuid.uuid4().hex[:8]}'
        payload = {'request_id': request_id, 'pair': '*/USDT', 'base': '*',
                   'priority': 'bulk', 'requested_by': 'telegram-owner'}
    else:
        normalized, reason = normalize_pair_input(base)
        if not normalized:
            raise ValueError('invalid Sharia scan pair: ' + reason)
        request_id = (
            f'{normalized_priority}-{normalized}-{uuid.uuid4().hex[:8]}')
        base = normalized
        payload = {'request_id': request_id, 'pair': f'{base}/USDT', 'base': base,
                   'priority': normalized_priority,
                   'requested_by': 'telegram-owner'}
    signed = envelope.sign_envelope(
        producer='telegram-broker', purpose=envelope.BUS_SHARIA_REQUEST,
        payload=payload, ttl_seconds=600)
    SHARIA_QUEUE_INBOX.mkdir(parents=True, exist_ok=True)
    atomic_write_json(SHARIA_QUEUE_INBOX / f'request_{request_id}.json', signed)
    audit('telegram_sharia_scan_requested', actor='telegram-owner',
          details={'request_id': request_id, 'base': base, 'priority': priority})
    return {'request_id': request_id}


def sharia_bounded_scan_requests(limit: int) -> dict:
    """Queue a bounded batch without changing the protected screener service.

    Each exact base is sent through the existing signed request bus as ordinary
    low-priority bulk work.  The current, hash-validated universe snapshot is
    the only source; malformed or stale snapshots fail before any request is
    written.
    """
    if (isinstance(limit, bool) or not isinstance(limit, int) or
            not BULK_SCAN_MIN <= limit <= BULK_SCAN_MAX):
        raise ValueError(
            f'bounded scan size must be {BULK_SCAN_MIN}-{BULK_SCAN_MAX}')
    snapshot = load_current(
        UNIVERSE_CURRENT,
        max_age_seconds=env_int(
            'MAX_UNIVERSE_AGE_SECONDS', 1800, 1, 86_400),
    )
    bases: list[str] = []
    seen: set[str] = set()
    for row in snapshot.get('pairs', []):
        pair = row.get('pair') if isinstance(row, dict) else row
        base, _ = normalize_pair_input(str(pair or ''))
        if base and base not in seen:
            seen.add(base)
            bases.append(base)
        if len(bases) >= limit:
            break
    if not bases:
        raise ValueError('validated universe has no eligible Spot/USDT pairs')
    outcomes = [
        sharia_scan_request(base, priority='bulk') for base in bases]
    return {
        'requested_limit': limit,
        'queued_count': len(outcomes),
        'bases': bases,
        'request_ids': [item['request_id'] for item in outcomes],
        'snapshot_hash': snapshot['snapshot_hash'],
    }


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


def _sharia_registry_summary() -> dict:
    """Read administrative source readiness without changing Sharia core."""
    registry_valid = True
    registry_error = ''
    valid_bases: set[str] = set()
    registered_count = 0
    try:
        registry = SourceRegistry(SHARIA_SOURCE_REGISTRY)
        assets = registry.load().get('assets', {})
        registered_count = len(assets)
        for base in sorted(assets):
            try:
                registry.asset(base)
            except Exception as exc:
                registry_valid = False
                if not registry_error:
                    registry_error = f'{base}: {type(exc).__name__}: {exc}'
            else:
                valid_bases.add(str(base).upper())
    except Exception as exc:
        registry_valid = False
        registry_error = f'{type(exc).__name__}: {exc}'
    discovery_valid = True
    discovery_error = ''
    try:
        candidates = candidate_bases(SHARIA_DISCOVERY_CURRENT_DIR)
    except Exception as exc:
        candidates = set()
        discovery_valid = False
        discovery_error = f'{type(exc).__name__}: {exc}'
    return {
        'registry_valid': registry_valid,
        'registry_error': registry_error[:300],
        'registered_asset_count': registered_count,
        'screenable_registered_count': len(valid_bases),
        'source_registry_ready': bool(registry_valid and valid_bases),
        'discovery_candidate_count': len(candidates),
        'owner_review_pending': len(candidates - valid_bases),
        'discovery_index_valid': discovery_valid,
        'discovery_error': discovery_error[:300],
    }


def _sharia_service_status() -> str:
    health = read_json(SHARIA_RUNTIME_DIR / 'health.json', None)
    if isinstance(health, dict) and health.get('registry_mode') == 'manual-registry/v1':
        try:
            age = time.time() - float(health.get('ts'))
            heartbeat_fresh = -30 <= age < 120
        except (TypeError, ValueError, OverflowError):
            age = float('inf')
            heartbeat_fresh = False
        lines = [
            'Manual Sharia-approved asset registry',
            ('service: READY' if heartbeat_fresh and health.get('ok') is True
             else f'service: NOT READY (heartbeat age={age:.0f}s; '
                  f'error={str(health.get("degraded_reason", ""))[:180]})'),
            f'registry version: {health.get("registry_version") or "unknown"}',
            f'approved assets: {health.get("eligible_assets", 0)}',
            f'trade ready: {health.get("sharia_trade_ready") is True}',
            f'blocker: {health.get("eligibility_blocker") or "none"}',
            'automatic Sharia research: DISABLED',
        ]
        try:
            gate = load_sharia_gate(SHARIA_FILE)
            eligible = gate.current_halal_symbols()
            lines.append('approved now: ' + (', '.join(eligible) or 'NONE (fail-closed)'))
        except Exception as exc:
            lines.append('registry projection: REJECTED (fail-closed): ' + str(exc)[:240])
        lines.extend([
            'To change the list, edit shared/sharia/halal_coins.json with '
            'current review dates, sorted symbols and a new version.',
            NOT_FATWA,
        ])
        return '\n'.join(lines)
    lines = ['V19.1 Sharia screening service']
    health_ready = False
    operational_ready = False
    operational_known = False
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
    registry = _sharia_registry_summary()
    operational_known = True
    registry_valid = registry['registry_valid'] is True
    registered = registry['registered_asset_count']
    operational_ready = bool(
        health_ready and registry['source_registry_ready'] is True)
    lines.append(
        f'source registry: valid={registry_valid}; registered={registered}; '
        f'screenable={registry["screenable_registered_count"]}')
    lines.append(
        f'discovery candidates={registry["discovery_candidate_count"]}; '
        f'owner review pending={registry["owner_review_pending"]}')
    if not registry['discovery_index_valid']:
        lines.append(
            'discovery index: UNAVAILABLE — ' +
            str(registry['discovery_error'])[:180])
    if operational_ready:
        lines.append('operational screening: READY')
    elif registry_valid and registered == 0:
        lines.append(
            'operational screening: BLOCKED — registry is empty; '
            'discovery candidates cannot self-authorize')
    else:
        reason = str(registry['registry_error']).strip()
        lines.append(
            'operational screening: BLOCKED' +
            (f' — {reason[:180]}' if reason else ''))
    try:
        gate = load_sharia_gate(SHARIA_FILE)
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
    elif operational_known and not operational_ready:
        lines.append(
            'backend is live, but new evidence-backed screening is not '
            'operationally ready until owner-reviewed sources exist')
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
            bindings = [
                item for item in (review.get('evidence_bindings') or [])
                if isinstance(item, dict)
            ]
            for index, item in enumerate(bindings, start=1):
                context = ' '.join(str(item.get('context', '')).split())
                lines.append(
                    f'evidence {index}/{len(bindings)} '
                    f'[{item.get("name", "")}; '
                    f'context-sha={str(item.get("context_sha256", ""))[:16]}]: '
                    f'{context[:220]}')
            if bindings:
                lines.append(
                    'All evidence blocks and surrounding contexts above are '
                    'offset-bound to the exact extracted-text and response hashes.')
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


def _manual_registry_coin_status(base: str) -> str:
    try:
        gate = load_sharia_gate(SHARIA_FILE)
        decision = gate.decision(base)
        return '\n'.join([
            f'Manual registry status — {base}/USDT',
            f'approved: {decision.allowed}',
            f'status: {decision.status}',
            f'reason: {decision.reason}',
            f'registry mode: {gate.projection_mode}',
            NOT_FATWA,
        ])
    except Exception as exc:
        return '\n'.join([
            f'Manual registry status — {base}/USDT',
            'approved: False',
            'status: NO_TRADE_INFO',
            'reason: registry projection rejected (fail-closed): ' + str(exc)[:300],
            NOT_FATWA,
        ])


def menu():
    """Safe monitoring and limited-control panel for the bot owner.

    Protection, strategy, risk, credential and coin-list mutations are
    intentionally absent.  Existing low-level command handlers remain for
    backwards compatibility, but the normal Telegram workflow cannot expose
    them accidentally.
    """
    return [
        [{'text': '📊 Dashboard', 'callback_data': 'do|menu_dashboard',
          'style': 'primary'}],
        [{'text': '📈 Trading Status', 'callback_data': 'do|status'},
         {'text': '💰 Balance', 'callback_data': 'do|balance'}],
        [{'text': '📋 Open Trades', 'callback_data': 'do|open_trades'},
         {'text': '📜 Trade History', 'callback_data': 'do|trade_history'}],
        [{'text': '🔍 Market Scanner', 'callback_data': 'do|menu_market_scanner'},
         {'text': '🕌 Sharia Status', 'callback_data': 'do|menu_sharia'}],
        [{'text': '🛡 Safety & Health', 'callback_data': 'do|menu_health'},
         {'text': '📢 Alerts', 'callback_data': 'do|menu_alerts'}],
        [{'text': '⚙️ Bot Controls', 'callback_data': 'do|menu_controls'},
         {'text': '🚨 Emergency Stop',
          'callback_data': 'do|emergency_stop_confirm', 'style': 'danger'},
         {'text': 'ℹ️ Help', 'callback_data': 'do|menu_help'}],
    ]


def dashboard_menu():
    return [
        [{'text': '📈 Trading Status', 'callback_data': 'do|status'},
         {'text': '💰 Balance', 'callback_data': 'do|balance'}],
        [{'text': '📋 Open Trades', 'callback_data': 'do|open_trades'},
         {'text': '📜 Trade History', 'callback_data': 'do|trade_history'}],
        [{'text': '📅 Daily Report', 'callback_data': 'do|daily_report'},
         {'text': '📈 Last Signal', 'callback_data': 'do|last_signal'}],
        [{'text': '🌐 Pair Management', 'callback_data': 'do|universe'},
         {'text': '🚀 Deployment', 'callback_data': 'do|deploy'}],
        [{'text': '🏠 Home', 'callback_data': 'do|home'}],
    ]


def sharia_menu():
    return [
        [{'text': '📄 View Halal List', 'callback_data': 'do|sharia',
          'style': 'primary'},
         {'text': '🩺 Registry Health', 'callback_data': 'do|sharia_service'}],
        [{'text': '🔍 Check Coin', 'callback_data': 'do|sharia_report_help'},
         {'text': '✏️ Update Instructions',
          'callback_data': 'do|manual_registry_help'}],
        [{'text': '⚠️ Latest Failure', 'callback_data': 'do|sharia_failures'},
         {'text': '🏠 Home', 'callback_data': 'do|home'}],
    ]


def market_scanner_menu():
    """Read-only Binance Spot observations with no trade authority."""
    return [
        [{'text': '🆕 Current Spot Universe', 'callback_data': 'do|universe'},
         {'text': '🔥 Top Volume & Movers',
          'callback_data': 'do|universe_movers'}],
        [{'text': '🌊 Spot Flow & Liquidity',
          'callback_data': 'do|market_context'},
         {'text': '🕌 Check Sharia',
          'callback_data': 'do|sharia_report_help'}],
        [{'text': '🦎 CoinGecko', 'callback_data': 'do|provider_coingecko'},
         {'text': '🪙 CoinMarketCap',
          'callback_data': 'do|provider_coinmarketcap'}],
        [{'text': '✅ Data Freshness', 'callback_data': 'do|data_readiness'},
         {'text': '🏠 Home', 'callback_data': 'do|home'}],
    ]


def health_menu():
    return [
        [{'text': '🩺 System Health', 'callback_data': 'do|system_health'},
         {'text': '✅ API Readiness', 'callback_data': 'do|data_readiness'}],
        [{'text': '🧪 Release Validation', 'callback_data': 'do|selftest'},
         {'text': '🚀 Deployment Info', 'callback_data': 'do|deploy'}],
        [{'text': '📉 Recent Audit', 'callback_data': 'do|activity'},
         {'text': '🏠 Home', 'callback_data': 'do|home'}],
    ]


def controls_menu():
    """Only reversible entry controls are exposed on the normal menu."""
    return [
        [{'text': '▶️ Resume Entries',
          'callback_data': 'do|entries_on_confirm', 'style': 'success'},
         {'text': '⏸ Pause Entries',
          'callback_data': 'do|entries_off_confirm', 'style': 'danger'}],
        [{'text': '📡 Test Telegram', 'callback_data': 'do|test_telegram'},
         {'text': '🔄 Restart Guidance',
          'callback_data': 'do|restart_services_info'}],
        [{'text': '🚀 Deployment Info', 'callback_data': 'do|deploy'},
         {'text': '🏠 Home', 'callback_data': 'do|home'}],
    ]


def signals_menu():
    return [
        [{'text': '📈 Latest signal', 'callback_data': 'do|last_signal'},
         {'text': '🗂 Recent signals', 'callback_data': 'do|signal_history'}],
        [{'text': '🚫 Rejected signals', 'callback_data': 'do|signal_rejected'},
         {'text': '🌊 Spot context', 'callback_data': 'do|market_context'}],
        [{'text': '🩺 Signal health', 'callback_data': 'do|data_readiness'},
         {'text': '📊 Performance', 'callback_data': 'do|profit'}],
        [{'text': '🏠 Home', 'callback_data': 'do|home'}],
    ]


def research_menu():
    """Read-only Spot evidence; never creates or authorizes a trade."""
    return [
        [{'text': '🌊 Spot flow & CVD', 'callback_data': 'do|market_context'},
         {'text': '📚 Spot liquidity', 'callback_data': 'do|market_context'}],
        [{'text': '🔥 Binance Spot movers', 'callback_data': 'do|universe_movers'},
         {'text': '🌐 Coin data providers', 'callback_data': 'do|provider_status'}],
        [{'text': '🦎 CoinGecko', 'callback_data': 'do|provider_coingecko'},
         {'text': '🪙 CoinMarketCap',
          'callback_data': 'do|provider_coinmarketcap'}],
        [{'text': '🔗 Data sources', 'callback_data': 'do|data_sources'},
         {'text': '✅ Freshness', 'callback_data': 'do|data_readiness'}],
        [{'text': '🏠 Home', 'callback_data': 'do|home'}],
    ]


def trading_menu():
    return [
        [{'text': '▶️ Resume signals', 'callback_data': 'do|entries_on_confirm',
          'style': 'success'},
         {'text': '⛔ Stop entries', 'callback_data': 'do|entries_off',
          'style': 'danger'}],
        [{'text': '📊 Private status', 'callback_data': 'do|status'},
         {'text': '📋 Open orders', 'callback_data': 'do|orders'}],
        [{'text': '💵 Balance', 'callback_data': 'do|balance'},
         {'text': '💰 Profit', 'callback_data': 'do|profit'}],
        [{'text': '🔁 Reconcile orders', 'callback_data': 'do|reconcile'},
         {'text': '💵 Size & slots', 'callback_data': 'do|sizing_help'}],
        [{'text': '🏠 Home', 'callback_data': 'do|home'}],
    ]


def protection_menu():
    return [
        [{'text': '🛡 Fixed OCO', 'callback_data': 'do|mode_fixed'},
         {'text': '📈 Trailing only', 'callback_data': 'do|mode_trailing'}],
        [{'text': '🔗 OCO + trailing', 'callback_data': 'do|mode_oco_trailing'},
         {'text': '🔄 Convert position', 'callback_data': 'do|convert_help'}],
        [{'text': '⚖️ Break-even', 'callback_data': 'do|be_help'},
         {'text': '🔒 Lock profit', 'callback_data': 'do|profit_help'}],
        [{'text': '📋 Open orders', 'callback_data': 'do|orders'},
         {'text': '🔁 Reconcile', 'callback_data': 'do|reconcile'}],
        [{'text': '🏠 Home', 'callback_data': 'do|home'}],
    ]


def alerts_menu():
    return [
        [{'text': '🔔 Delivery status', 'callback_data': 'do|alert_status'},
         {'text': '☑️ Alert policy', 'callback_data': 'do|alert_policy'}],
        [{'text': '⚠️ Recent audit', 'callback_data': 'do|activity'},
         {'text': '🕌 Sharia failure', 'callback_data': 'do|sharia_failures'}],
        [{'text': '🏠 Home', 'callback_data': 'do|home'}],
    ]


def system_menu():
    return [
        [{'text': '⚙️ Settings', 'callback_data': 'do|settings'},
         {'text': '⚠️ Logs', 'callback_data': 'do|logs'}],
        [{'text': '🔁 Restart WebSocket', 'callback_data': 'do|restart_ws_confirm'},
         {'text': '🔄 Reload config', 'callback_data': 'do|reload_confirm'}],
        [{'text': '🧪 Self-test', 'callback_data': 'do|selftest'},
         {'text': '📉 Backtest', 'callback_data': 'do|backtest'}],
        [{'text': '✅ API readiness', 'callback_data': 'do|data_readiness'},
         {'text': '🌐 Providers', 'callback_data': 'do|provider_status'}],
        [{'text': '🚀 Deployment', 'callback_data': 'do|deploy'},
         {'text': '🏠 Home', 'callback_data': 'do|home'}],
    ]


def emergency_menu():
    return [
        [{'text': '🚨 STOP NEW ENTRIES',
          'callback_data': 'do|emergency_stop_confirm',
          'style': 'danger'}],
        [{'text': '📊 Check status', 'callback_data': 'do|status'},
         {'text': '🏠 Home', 'callback_data': 'do|home'}],
    ]


def help_text():
    return (
        'Safe commands: /menu /status /orders /history /daily /balance '
        '/profit /stop /pause '
        '/universe /sharia /shariastatus /shariareport BASE '
        '/deploy /lastsignal /signals /rejected '
        '/spotcontext /providers /readiness /alerts /selftest. '
        'Typing a pair like BTC/USDT (or BTCUSDT) checks the manual registry. '
        'The menu never changes strategy, risk, API keys, protection mode, '
        'pair rules or LIVE state. Automatic Sharia scanning is disabled.'
    )


def _latest_signal():
    files = []
    for folder in (SIGNAL_INBOX, SIGNAL_PROCESSED, SIGNAL_REJECTED):
        files.extend(folder.glob('*.json'))
    if not files:
        return 'No signal file has been recorded.'
    path = max(files, key=lambda p: p.stat().st_mtime)
    return json.dumps({'location': path.parent.name, 'signal': read_json(path, {})}, indent=2)[:3800]


def _signal_history(folder: Path | None = None, limit: int = 10) -> str:
    """Render bounded, identity-only signal history without leaking payloads."""
    folders = (folder,) if folder is not None else (
        SIGNAL_INBOX, SIGNAL_PROCESSED, SIGNAL_REJECTED)
    files: list[Path] = []
    for current in folders:
        files.extend(current.glob('*.json'))
    try:
        files.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    except OSError:
        return 'Signal history is temporarily unreadable.'
    rows = []
    for path in files[:max(1, min(int(limit), 20))]:
        raw = read_json(path, {}) or {}
        if not isinstance(raw, dict):
            raw = {}
        payload = raw.get('payload')
        if not isinstance(payload, dict):
            payload = raw
        rows.append({
            'queue': path.parent.name,
            'file': path.name,
            'signal_id': payload.get('signal_id'),
            'pair': payload.get('pair'),
            'side': payload.get('side'),
            'reason': payload.get('reason') or raw.get('reason'),
            'created_at': payload.get('created_at') or payload.get('timestamp'),
        })
    return json.dumps({'count': len(rows), 'signals': rows}, indent=2)[:3800]


def _market_context_status() -> str:
    """Show the existing Spot-only advisory layer; it cannot authorize trades."""
    context = read_json(MARKET_CONTEXT_FILE, None)
    health = read_json(MARKET_CONTEXT_HEALTH_FILE, None)
    if not isinstance(context, dict):
        return 'Spot market context: UNAVAILABLE — no valid current snapshot.'
    symbols = context.get('symbols') if isinstance(context.get('symbols'), dict) else {}
    fresh = sorted(
        symbol for symbol, row in symbols.items()
        if isinstance(row, dict) and row.get('status') == 'fresh')
    health_row = health if isinstance(health, dict) else {}
    stream = health_row.get('stream')
    if not isinstance(stream, dict):
        stream = {}
    return '\n'.join([
        'Spot market context (read-only evidence)',
        (f'status={health_row.get("status", "unknown")}; '
         f'health_ok={health_row.get("ok")}; '
         f'subscription_ready={stream.get("subscription_ready")}'),
        f'generated_at={context.get("generated_at")}',
        (f'fresh symbols={context.get("fresh_symbol_count", len(fresh))}/'
         f'{context.get("symbol_count", len(symbols))}'),
        (f'advisory_only={context.get("advisory_only")}; '
         f'spot_only={context.get("spot_only")}; '
         f'can_trade={context.get("can_trade")}'),
        'fresh sample=' + (', '.join(fresh[:12]) or 'NONE'),
        'features=' + ', '.join(str(item) for item in
                               (context.get('features') or [])[:8]),
        ('This screen observes flow, CVD, spread and liquidity; it does not '
         'approve, reject or alter the protected strategy.'),
    ])[:3900]


def _external_provider_status(selected: str | None = None) -> str:
    """Render a strict allow-list of non-secret provider health fields."""
    provider_labels = {
        'coingecko': 'CoinGecko',
        'cmc': 'CoinMarketCap',
    }
    if selected is not None and selected not in provider_labels:
        raise ValueError('unsupported external provider')
    status = read_json(EXTERNAL_SIGNALS_STATUS, None)
    if not isinstance(status, dict):
        label = provider_labels.get(selected, 'Coin data providers')
        return json.dumps({
            'provider': label,
            'status': 'unavailable',
            'reason': 'no valid advisory-provider status snapshot',
            'required_for_trading': False,
            'trade_authority': False,
        }, indent=2)

    def provider(name: str) -> dict:
        row = status.get(name)
        if not isinstance(row, dict):
            return {'enabled': False, 'status': 'unavailable'}
        breaker = row.get('breaker')
        if not isinstance(breaker, dict):
            breaker = {}
        return {
            'enabled': row.get('enabled'),
            'keyless': row.get('keyless') if name == 'coingecko' else None,
            'missing_key': row.get('requested_but_missing_key'),
            'cache_age_seconds': row.get('cache_age_seconds'),
            'breaker_state': breaker.get('state'),
            'ambiguous_symbols_excluded':
                row.get('ambiguous_symbols_excluded'),
        }

    role = status.get('config')
    role = role.get('role') if isinstance(role, dict) else None
    payload = {
        'generated_at': status.get('generated_at'),
        'role': role,
        'CoinGecko': provider('coingecko'),
        'CoinMarketCap': provider('cmc'),
        'trade_authority': False,
    }
    if selected is not None:
        label = provider_labels[selected]
        payload = {
            'generated_at': status.get('generated_at'),
            'provider': label,
            'role': role,
            'health': provider(selected),
            'required_for_trading': False,
            'trade_authority': False,
            'note': (
                'Advisory market metadata only. This provider cannot add a '
                'halal coin, create a strategy signal, or authorize an order.'),
        }
    return json.dumps(payload, indent=2)[:3800]


def _universe_movers() -> str:
    try:
        snapshot = load_current(
            UNIVERSE_CURRENT,
            max_age_seconds=env_int(
                'MAX_UNIVERSE_AGE_SECONDS', 1800, 1, 86_400),
        )
    except Exception as exc:
        return 'Binance Spot movers unavailable (fail-closed universe): ' + str(exc)[:300]
    rows = []
    for item in snapshot.get('ranking', [])[:10]:
        if not isinstance(item, dict):
            continue
        rows.append({
            'rank': item.get('rank'), 'pair': item.get('pair'),
            'change_pct': item.get('change_pct'),
            'quote_volume': item.get('quote_volume'),
            'spread_ratio': item.get('spread_ratio'),
        })
    return json.dumps({
        'source': 'validated Binance Spot universe ranking',
        'generated_at': snapshot.get('generated_at'),
        'snapshot_hash': snapshot.get('snapshot_hash'),
        'movers': rows,
        'trade_authority': False,
    }, indent=2)[:3800]


def _latest_signal_summary() -> dict:
    files: list[Path] = []
    for folder in (SIGNAL_INBOX, SIGNAL_PROCESSED, SIGNAL_REJECTED):
        files.extend(folder.glob('*.json'))
    if not files:
        return {'status': 'not-recorded'}
    try:
        path = max(files, key=lambda item: item.stat().st_mtime)
    except OSError:
        return {'status': 'unreadable'}
    raw = read_json(path, {}) or {}
    if not isinstance(raw, dict):
        raw = {}
    payload = raw.get('payload')
    if not isinstance(payload, dict):
        payload = raw
    return {
        'status': 'recorded',
        'queue': path.parent.name,
        'pair': payload.get('pair'),
        'side': payload.get('side'),
        'created_at': payload.get('created_at') or payload.get('timestamp'),
    }


def _dashboard_status() -> str:
    """Aggregate bounded, read-only snapshots without inventing host data."""
    sidecar = read_json(RUNTIME / 'sidecar_health.json', {}) or {}
    telegram = read_json(RUNTIME / 'telegram_health.json', {}) or {}
    sharia = read_json(SHARIA_RUNTIME_DIR / 'health.json', {}) or {}
    deployment = read_json(RUNTIME / 'deployment_status.json', {}) or {}
    sidecar = sidecar if isinstance(sidecar, dict) else {}
    telegram = telegram if isinstance(telegram, dict) else {}
    sharia = sharia if isinstance(sharia, dict) else {}
    deployment = deployment if isinstance(deployment, dict) else {}
    approved = 0
    selected = 0
    try:
        approved = len(load_sharia_gate(SHARIA_FILE).current_halal_symbols())
    except Exception:
        pass
    try:
        snapshot = load_current(
            UNIVERSE_CURRENT,
            max_age_seconds=env_int(
                'MAX_UNIVERSE_AGE_SECONDS', 1800, 1, 86_400),
        )
        pairs = snapshot.get('pairs')
        if isinstance(pairs, (dict, list)):
            selected = len(pairs)
    except Exception:
        pass
    running = bool(
        sidecar.get('ok') is True
        and telegram.get('ok') is True
        and sharia.get('ok') is True
    )
    return json.dumps({
        'product': BOT_PRODUCT,
        'environment': BOT_ENVIRONMENT,
        'instance': BOT_INSTANCE_ID,
        'status': 'RUNNING' if running else 'DEGRADED_OR_UNKNOWN',
        'execution_mode': os.getenv('EXECUTION_MODE', 'simulation'),
        'release': _release_label(),
        'release_hash': envelope.installed_release_hash(),
        'deployment_ok': deployment.get('ok'),
        'pairs': {'approved_registry': approved, 'selected_universe': selected},
        'last_signal': _latest_signal_summary(),
        'host_metrics': (
            'not exposed to the Telegram container; use the AWS/Oracle host '
            'monitor instead of displaying guessed CPU, RAM or uptime'),
    }, indent=2)[:3800]


def _trade_history() -> str:
    """Read the Freqtrade trade ledger; never creates or alters a trade."""
    return json.dumps(
        ft_call('GET', '/trades?limit=10&offset=0'), indent=2)[:3900]


def _open_trades() -> str:
    """Combine active Freqtrade positions with protective-order evidence."""
    return json.dumps({
        'freqtrade_open_trades': ft_call('GET', '/status'),
        'sidecar_protective_orders': sidecar_command('orders', wait=True),
        'scope': 'read-only; no order or position mutation was requested',
    }, indent=2)[:3900]


def _daily_report() -> str:
    """Show raw Freqtrade daily/account evidence without recomputing PnL."""
    return json.dumps({
        'scope': 'read-only Freqtrade API snapshot',
        'profit': ft_call('GET', '/profit'),
        'recent_trades': ft_call('GET', '/trades?limit=10&offset=0'),
        'note': 'No figure is treated as validated if its API call failed.',
    }, indent=2)[:3900]


def _system_health() -> str:
    """Report only health evidence visible to the Telegram container."""
    def snapshot(path: Path, fields: tuple[str, ...]) -> dict:
        value = read_json(path, {}) or {}
        if not isinstance(value, dict):
            return {'status': 'invalid'}
        return {field: value.get(field) for field in fields}

    try:
        universe_snapshot = load_current(
            UNIVERSE_CURRENT,
            max_age_seconds=env_int(
                'MAX_UNIVERSE_AGE_SECONDS', 1800, 1, 86_400),
        )
        universe = {
            'ok': True,
            'generated_at': universe_snapshot.get('generated_at'),
            'snapshot_hash': universe_snapshot.get('snapshot_hash'),
        }
    except Exception as exc:
        universe = {'ok': False, 'error': str(exc)[:300]}

    return json.dumps({
        'freqtrade': ft_call('GET', '/ping'),
        'execution_sidecar': snapshot(
            RUNTIME / 'sidecar_health.json', ('ok', 'ts', 'stream_status')),
        'universe': universe,
        'sharia_registry': snapshot(
            SHARIA_RUNTIME_DIR / 'health.json',
            ('ok', 'ts', 'registry_mode', 'eligible_assets',
             'eligibility_blocker')),
        'telegram': snapshot(
            RUNTIME / 'telegram_health.json', ('ok', 'ts', 'last_error')),
        'alerts': snapshot(
            ALERT_OUTBOX_HEALTH,
            ('ok', 'ts', 'pending_alert_count', 'dead_letter_count')),
        'host_docker_cpu_ram_disk_uptime': (
            'not directly observable here; consult the host monitor'),
        'last_backup': 'not reported unless a signed host backup status exists',
        'scope': 'read-only service health; not deployment or LIVE certification',
    }, indent=2)[:3900]


def _alert_policy() -> str:
    return json.dumps({
        'delivery': _alert_status(),
        'configuration': 'deployment-managed; Telegram cannot weaken alerts',
        'durability': 'outbox with deduplication and dead-letter reporting',
        'note': 'This screen does not change notification policy.',
    }, indent=2)[:3900]


def _pause_entries() -> dict:
    """Disarm the order owner before pausing Freqtrade."""
    sidecar = sidecar_command('entries', {'enabled': False}, wait=True)
    freqtrade = ft_call('POST', '/pause')
    return {'sidecar': sidecar, 'freqtrade': freqtrade}


def _data_readiness() -> str:
    api = read_json(API_READINESS_STATUS, {}) or {}
    market = read_json(MARKET_CONTEXT_HEALTH_FILE, {}) or {}
    telegram = read_json(RUNTIME / 'telegram_health.json', {}) or {}
    sharia = read_json(SHARIA_RUNTIME_DIR / 'health.json', {}) or {}
    api = api if isinstance(api, dict) else {}
    market = market if isinstance(market, dict) else {}
    telegram = telegram if isinstance(telegram, dict) else {}
    sharia = sharia if isinstance(sharia, dict) else {}
    providers = api.get('providers')
    if not isinstance(providers, dict):
        providers = {}
    api_ok = api.get('ok')
    api_status = ('PASS' if api_ok is True else
                  'FAIL' if api_ok is False else 'UNKNOWN')
    return json.dumps({
        'api_preflight': {
            'status': api_status,
            'generated_at': api.get('generated_at'),
            'providers': {
                str(name): (row.get('status') if isinstance(row, dict) else None)
                for name, row in providers.items()
            },
        },
        'spot_market_context': {
            'ok': market.get('ok'), 'status': market.get('status'),
            'fresh_symbol_count': market.get('fresh_symbol_count'),
            'ts': market.get('ts'),
        },
        'sharia_screener': {
            'ok': sharia.get('ok'),
            'ready_for_screening': sharia.get('ready_for_screening'),
            'sharia_trade_ready': sharia.get('sharia_trade_ready'),
            'eligible_assets': sharia.get('eligible_assets'),
            'eligibility_blocker': sharia.get('eligibility_blocker'),
            'ts': sharia.get('ts'),
        },
        'telegram': {'ok': telegram.get('ok'), 'ts': telegram.get('ts')},
        'note': 'GET-only/read-only readiness; this is not LIVE certification.',
    }, indent=2)[:3800]


def _sharia_review_queue() -> str:
    return json.dumps({
        'mode': 'manual-registry/v1',
        'automatic_research': False,
        'automatic_approval': False,
        'owner_action': 'edit shared/sharia/halal_coins.json after manual review',
    }, indent=2)[:3800]


def _sharia_failure_status() -> str:
    health = read_json(SHARIA_RUNTIME_DIR / 'health.json', {}) or {}
    last_failed = health.get('last_failed') if isinstance(health, dict) else {}
    if not isinstance(last_failed, dict) or not last_failed:
        return 'No Sharia failure is recorded in the current health snapshot.'
    return json.dumps({
        'base': last_failed.get('base'),
        'error': str(last_failed.get('error', ''))[:500],
        'finished_at': last_failed.get('finished_at'),
        'fail_closed': True,
    }, indent=2)


def _alert_status() -> str:
    health = read_json(ALERT_OUTBOX_HEALTH, {}) or {}
    if not isinstance(health, dict):
        health = {}
    return json.dumps({
        'delivery_ok': health.get('ok'),
        'pending_alert_count': health.get('pending_alert_count'),
        'oldest_pending_alert_age_seconds':
            health.get('oldest_pending_alert_age_seconds'),
        'dead_letter_count': health.get('dead_letter_count'),
        'blocked_reason': str(health.get('blocked_reason') or '')[:300] or None,
        'ts': health.get('ts'),
    }, indent=2)


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


def route(action, chat, message_id=None):
    if action == 'home':
        edit_or_send(f'{_release_label()} safe monitoring panel', chat,
                     message_id, menu())
    elif action == 'menu_dashboard':
        edit_or_send(_dashboard_status(), chat,
                     message_id, dashboard_menu())
    elif action == 'menu_sharia':
        edit_or_send(
            'Manual Sharia registry — the trading bot only permits coins in '
            'your current owner-maintained halal_coins.json file. Automatic '
            'research and automatic coin approval are disabled.\n' + NOT_FATWA,
            chat, message_id, sharia_menu())
    elif action == 'menu_signals':
        edit_or_send(
            'Signal centre — read-only strategy output and rejection evidence.',
            chat, message_id, signals_menu())
    elif action in {'menu_market_scanner', 'menu_research'}:
        edit_or_send(
            'Market scanner — read-only Binance Spot universe, volume, movers '
            'and liquidity evidence. It cannot add a coin or authorize a trade.',
            chat, message_id, market_scanner_menu())
    elif action in {'menu_controls', 'menu_trading'}:
        edit_or_send(
            'Limited controls — resume or pause new entries only. Strategy, '
            'risk, pair rules, credentials and LIVE state are unavailable.',
            chat, message_id, controls_menu())
    elif action == 'menu_protection':
        edit_or_send(
            'Protection controls — every position mutation retains its '
            'existing one-time confirmation.',
            chat, message_id, protection_menu())
    elif action == 'menu_alerts':
        edit_or_send('Alert delivery and recent operational evidence.', chat,
                     message_id, alerts_menu())
    elif action in {'menu_health', 'menu_system'}:
        edit_or_send(
            'Safety and health — read-only service and deployment evidence.',
            chat, message_id, health_menu())
    elif action == 'menu_emergency':
        edit_or_send(
            'Emergency stop pauses new entries only. It does not sell, delete '
            'or change strategy/risk settings. Confirmation is mandatory.',
            chat, message_id, emergency_menu())
    elif action == 'menu_help':
        edit_or_send(
            help_text(),
            chat, message_id,
            [[{'text': '🕌 Sharia', 'callback_data': 'do|menu_sharia'},
              {'text': '🏠 Home', 'callback_data': 'do|home'}]])
    elif action == 'entries_on_confirm':
        button = confirm_button('✅ CONFIRM resume entries', 'resume_entries')
        send('Confirm enabling new strategy signals and sidecar entries.', chat, [[button], [{'text': '❌ Cancel', 'callback_data': 'do|help'}]])
    elif action == 'entries_off':
        send(json.dumps(_pause_entries(), indent=2), chat)
    elif action in {'entries_off_confirm', 'emergency_stop_confirm'}:
        _ask_confirm(
            chat, '✅ CONFIRM stop new entries', 'pause_entries',
            text=(
                'Confirm pausing all new entries. Existing positions are not '
                'sold or deleted, and the strategy/risk configuration is not '
                'changed.'),
            message_id=message_id,
            cancel_action='home',
        )
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
    elif action == 'open_trades':
        send(_open_trades(), chat)
    elif action == 'trade_history':
        send(_trade_history(), chat)
    elif action == 'balance':
        send(json.dumps(sidecar_command('balance', wait=True), indent=2), chat)
    elif action == 'profit':
        send(json.dumps(sidecar_command('profit', wait=True), indent=2), chat)
    elif action == 'daily_report':
        send(_daily_report(), chat)
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
        edit_or_send(_sharia_service_status()[:3900], chat, message_id,
                     sharia_menu())
    elif action == 'sharia_service':
        edit_or_send(_sharia_service_status(), chat, message_id,
                     sharia_menu())
    elif action == 'scan_help':
        edit_or_send(
            'Automatic coin scanning is disabled in manual-registry mode. '
            'Review the coin outside the trading bot, then update '
            'shared/sharia/halal_coins.json if you approve it.\n' + NOT_FATWA,
            chat, message_id,
            [[{'text': '⬅️ Sharia menu', 'callback_data': 'do|menu_sharia'},
              {'text': '🏠 Home', 'callback_data': 'do|home'}]])
    elif action == 'scan_bulk_help':
        edit_or_send(
            'Bulk Sharia scanning is disabled in manual-registry mode. '
            'Only the owner-maintained halal_coins.json file can approve a '
            'coin.\n' + NOT_FATWA,
            chat, message_id,
            [[{'text': '⬅️ Sharia menu', 'callback_data': 'do|menu_sharia'},
              {'text': '🏠 Home', 'callback_data': 'do|home'}]])
    elif (action.startswith('scan_bulk_') and
          action.endswith('_confirm')):
        edit_or_send(
            'Automatic Sharia scanning is disabled. Update the manual registry '
            'instead.', chat, message_id, sharia_menu())
    elif action == 'scanall_confirm':
        edit_or_send(
            'Automatic Sharia scanning is disabled. Update the manual registry '
            'instead.', chat, message_id, sharia_menu())
    elif action == 'manual_registry_help':
        edit_or_send(
            'Edit shared/sharia/halal_coins.json. Use exact sorted uppercase '
            'Spot/USDT symbols, increase version, and set last_reviewed and '
            'next_review. Missing, malformed or expired data blocks every new '
            'entry. The bot sends a Telegram alert after a valid update.\n' +
            NOT_FATWA,
            chat, message_id,
            [[{'text': '⬅️ Sharia menu', 'callback_data': 'do|menu_sharia'},
              {'text': '🏠 Home', 'callback_data': 'do|home'}]])
    elif action == 'sharia_report_help':
        edit_or_send(
            'Send /shariareport followed by the ticker, for example '
            '/shariareport ETH. The response shows whether that coin is in the '
            'current signed manual registry and why it is allowed or blocked.',
            chat, message_id,
            [[{'text': '⬅️ Sharia menu', 'callback_data': 'do|menu_sharia'},
              {'text': '🏠 Home', 'callback_data': 'do|home'}]])
    elif action == 'deploy':
        send(json.dumps(read_json(RUNTIME / 'deployment_status.json', {}), indent=2), chat)
    elif action == 'last_signal':
        send(_latest_signal(), chat)
    elif action == 'signal_history':
        send(_signal_history(), chat)
    elif action == 'signal_rejected':
        send(_signal_history(SIGNAL_REJECTED), chat)
    elif action == 'market_context':
        send(_market_context_status(), chat)
    elif action == 'provider_status':
        send(_external_provider_status(), chat)
    elif action == 'provider_coingecko':
        send(_external_provider_status('coingecko'), chat)
    elif action == 'provider_coinmarketcap':
        send(_external_provider_status('cmc'), chat)
    elif action == 'universe_movers':
        send(_universe_movers(), chat)
    elif action == 'data_sources':
        send(
            'Sources in use:\n'
            '• Binance Spot: listings, ranking, trades and best bid/ask.\n'
            '• CoinGecko and CoinMarketCap: rate-limited advisory identity/'
            'market annotations only.\n'
            '• Sharia: owner-maintained halal_coins.json operational allowlist; '
            'automatic Sharia research is disabled.\n'
            'No Futures market context and no external AI inference API.', chat)
    elif action == 'data_readiness':
        send(_data_readiness(), chat)
    elif action == 'sharia_review_queue':
        send(_sharia_review_queue(), chat)
    elif action == 'sharia_failures':
        send(_sharia_failure_status(), chat)
    elif action == 'alert_status':
        send(_alert_status(), chat)
    elif action == 'alert_policy':
        send(_alert_policy(), chat)
    elif action == 'activity':
        send(_tail_audit(25), chat)
    elif action == 'system_health':
        send(_system_health(), chat)
    elif action == 'test_telegram':
        send(
            'Telegram delivery test received by the owner channel. No trading '
            'or configuration action was performed.', chat)
    elif action == 'restart_services_info':
        send(
            'Host service restart is intentionally unavailable from Telegram. '
            'Use the authenticated AWS/Oracle host deployment service, then '
            'verify Safety & Health. This prevents Telegram from becoming a '
            'remote shell.', chat)
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
    elif action == 'pause_entries':
        send(json.dumps(_pause_entries(), indent=2), chat)
    elif action == 'restart_stream':
        send(json.dumps(sidecar_command('restart_stream', wait=True), indent=2), chat)
    elif action == 'reload_config':
        ft = ft_call('POST', '/reload_config')
        sidecar = sidecar_command('reload_sharia', wait=True)
        send(json.dumps({'sidecar': sidecar, 'freqtrade': ft}, indent=2), chat)
    elif action == 'set_mode':
        send(json.dumps(sidecar_command('mode', args, wait=True), indent=2), chat)
    elif action == 'scan_all':
        send('Automatic Sharia scanning is disabled. Update '
             'shared/sharia/halal_coins.json instead.\n' + NOT_FATWA, chat)
    elif action == 'scan_bulk':
        send('Automatic Sharia scanning is disabled. Update '
             'shared/sharia/halal_coins.json instead.\n' + NOT_FATWA, chat)
    elif action in {'sharia_approve', 'sharia_reject'}:
        send('Evidence-card approvals are disabled in manual-registry mode. '
             'Update shared/sharia/halal_coins.json instead.\n' + NOT_FATWA, chat)
    elif action in {'convert', 'break_even', 'lock_profit', 'emergency_exit', 'set_size', 'set_max'}:
        send(json.dumps(sidecar_command(action, args, wait=True), indent=2), chat)
    else:
        send('Unsupported confirmation action.', chat)


def _ask_confirm(chat, label, action, args=None, text='Confirm action.',
                 *, message_id=None, cancel_action='home'):
    button = confirm_button(label, action, args)
    edit_or_send(
        text, chat, message_id,
        [[button], [{'text': '❌ Cancel',
                     'callback_data': 'do|' + cancel_action}]])


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
        send(f'{_release_label()} safe monitoring panel', chat, menu())
    elif cmd == '/start': route('entries_on_confirm', chat)
    elif cmd in ('/stop', '/pause'): route('entries_off', chat)
    elif cmd == '/status': route('status', chat)
    elif cmd == '/orders': route('open_trades', chat)
    elif cmd in ('/history', '/tradehistory'): route('trade_history', chat)
    elif cmd == '/balance': route('balance', chat)
    elif cmd == '/profit': route('profit', chat)
    elif cmd in ('/daily', '/dailyreport'): route('daily_report', chat)
    elif cmd == '/logs': route('logs', chat)
    elif cmd == '/reload': route('reload_confirm', chat)
    elif cmd == '/fixed_oco': route('mode_fixed', chat)
    elif cmd == '/trailing_only': route('mode_trailing', chat)
    elif cmd == '/oco_trailing': route('mode_oco_trailing', chat)
    elif cmd == '/reconcile': route('reconcile', chat)
    elif cmd == '/universe': route('universe', chat)
    elif cmd in ('/sharia', '/halal'): route('sharia', chat)
    elif cmd in ('/shariastatus', '/shariaservice'): route('sharia_service', chat)
    elif cmd == '/scanall':
        send('Automatic Sharia scanning is disabled. Update '
             'shared/sharia/halal_coins.json instead.\n' + NOT_FATWA, chat)
    elif cmd == '/scanbulk' and len(parts) == 2:
        send('Automatic Sharia scanning is disabled. Update '
             'shared/sharia/halal_coins.json instead.\n' + NOT_FATWA, chat)
    elif cmd == '/scan' and len(parts) == 2:
        base, why = normalize_pair_input(parts[1])
        if not base:
            send('Scan rejected: ' + why, chat); return
        send(_manual_registry_coin_status(base) + '\n\nAutomatic scanning is disabled; '
             'update shared/sharia/halal_coins.json after your manual review.', chat)
    elif cmd == '/shariareport' and len(parts) == 2:
        base, why = normalize_pair_input(parts[1])
        if not base:
            send('Rejected: ' + why, chat); return
        send(_manual_registry_coin_status(base), chat)
    elif cmd == '/deploy': route('deploy', chat)
    elif cmd == '/lastsignal': route('last_signal', chat)
    elif cmd == '/signals': route('signal_history', chat)
    elif cmd == '/rejected': route('signal_rejected', chat)
    elif cmd in ('/spotcontext', '/marketcontext'):
        route('market_context', chat)
    elif cmd == '/providers': route('provider_status', chat)
    elif cmd == '/readiness': route('data_readiness', chat)
    elif cmd == '/alerts': route('alert_status', chat)
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
        # Direct owner messages now report the manual-registry gate only.
        base, _ = normalize_pair_input(parts[0])
        send(_manual_registry_coin_status(base), chat)
    else:
        send(help_text(), chat)


def handle_callback(callback):
    user_id = callback.get('from', {}).get('id')
    message = callback.get('message', {})
    chat = str(message.get('chat', {}).get('id', ''))
    raw_message_id = message.get('message_id')
    message_id = (raw_message_id if isinstance(raw_message_id, int)
                  and not isinstance(raw_message_id, bool)
                  and raw_message_id > 0 else None)
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
        route(data.split('|', 1)[1], chat, message_id)
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
    if not OFFSET_PATH.exists():
        return 0
    try:
        data = json.loads(OFFSET_PATH.read_text(encoding='utf-8'))
        if not isinstance(data, dict) or isinstance(data.get('offset'), bool):
            raise ValueError('invalid offset schema')
        offset = int(data['offset'])
        if offset < 0:
            raise ValueError('negative offset')
        return offset
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            'Telegram offset state is unreadable or corrupt; refusing to replay updates'
        ) from exc


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


def _dispatch_claimed_update(update: dict) -> None:
    """Isolate one claimed Telegram update from the long-poll supervisor.

    The durable offset is intentionally committed before this function runs.
    A failed menu renderer must therefore return a useful, secret-free error to
    the owner and allow later updates in the same batch to continue.
    """
    try:
        if 'message' in update:
            handle_message(update['message'])
        if 'callback_query' in update:
            handle_callback(update['callback_query'])
    except Exception as exc:
        safe = _redact_secrets(exc)[:300]
        audit('telegram_update_handler_failed', severity='ERROR', details={
            'update_id': update.get('update_id'),
            'error_type': type(exc).__name__,
            'error': safe,
        })
        callback = update.get('callback_query')
        message = update.get('message')
        if isinstance(callback, dict):
            message = callback.get('message')
        chat = ''
        if isinstance(message, dict):
            chat = str(message.get('chat', {}).get('id', ''))
        if chat:
            try:
                send(
                    'This request failed safely and no trading action was '
                    f'performed. {type(exc).__name__}: {safe}', chat)
            except Exception as send_exc:
                audit('telegram_update_error_reply_failed', severity='ERROR', details={
                    'update_id': update.get('update_id'),
                    'error_type': type(send_exc).__name__,
                })


def main():
    logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO'), format='%(asctime)s %(levelname)s %(name)s %(message)s')
    if not TOKEN or not OWNER:
        raise SystemExit('TELEGRAM_BOT_TOKEN and TELEGRAM_OWNER_CHAT_ID required')
    try:
        envelope.load_key(envelope.BUS_COMMAND)
        envelope.load_key(envelope.BUS_SIGNAL)
        envelope.load_key(envelope.BUS_SHARIA_REQUEST)
        envelope.load_key(envelope.BUS_SHARIA_DECISION)
    except envelope.EnvelopeError as exc:
        raise SystemExit(f'BUS KEYS MISSING: {exc}') from exc
    offset = _load_offset()
    while True:
        try:
            deliver_sidecar_notifications()
            deliver_signal_notifications()
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
                _dispatch_claimed_update(update)
            deliver_sidecar_notifications()
            deliver_signal_notifications()
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
