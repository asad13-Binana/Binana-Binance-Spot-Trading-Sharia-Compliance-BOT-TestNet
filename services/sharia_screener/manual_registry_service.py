"""Isolated projector for the owner-maintained ``halal_coins.json`` file.

This service performs no web research and has no network requirement.  It
converts the strictly validated manual list into the signed, report-bound
status records already enforced by the universe, strategy and execution
sidecar.  Missing, changed, malformed or expired input fails closed.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import signal
import threading
import time
import uuid
from datetime import datetime, time as wall_time, timezone
from pathlib import Path

from services.common import envelope
from services.common.atomic import atomic_write_json
from services.common.audit import audit
from services.common.config_bounds import env_int
from services.common.paths import (
    LEGACY_HALAL_FILE,
    SHARIA_FILE,
    SHARIA_REPORTS_DIR,
    SHARIA_RUNTIME_DIR,
    TELEGRAM_ALERT_OUTBOX,
)
from services.common.sharia_attestation import (
    STATUS_PURPOSE,
    attach,
    load_private_key,
    load_public_key,
)
from services.sharia_screener.manual_registry import (
    MANUAL_PROJECTION_MODE,
    MANUAL_STATUS_SOURCE,
    ManualRegistry,
    ManualRegistryError,
    build_manual_decision_report,
    load_manual_registry,
)
from services.universe_service.sharia_filter import SCHEMA_VERSION
from services.universe_service.sharia_gate import ManualRegistryFilter


log = logging.getLogger('sharia-manual-registry')
STOP = threading.Event()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _file_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return '0' * 64


class ManualRegistryProjector:
    def __init__(self, *, registry_path: str | Path = LEGACY_HALAL_FILE,
                 status_path: str | Path = SHARIA_FILE,
                 reports_dir: str | Path = SHARIA_REPORTS_DIR,
                 runtime_dir: str | Path = SHARIA_RUNTIME_DIR,
                 alert_outbox: str | Path = TELEGRAM_ALERT_OUTBOX):
        self.registry_path = Path(registry_path)
        self.status_path = Path(status_path)
        self.reports_dir = Path(reports_dir)
        self.runtime_dir = Path(runtime_dir)
        self.alert_outbox = Path(alert_outbox)
        self.state_path = self.runtime_dir / 'manual_registry_state.json'
        self.health_path = self.runtime_dir / 'health.json'
        self.last_error_key = ''

    def _status_document(self, registry: ManualRegistry, records: list[dict],
                         generated_at: datetime) -> dict:
        return {
            '_comment': (
                'Execution-gate projection generated from the owner-maintained '
                'halal_coins.json list. No automatic Sharia research is performed. '
                'Operational allowlist only - not a fatwa.'),
            'schema_version': SCHEMA_VERSION,
            'projection_mode': MANUAL_PROJECTION_MODE,
            'registry_valid': True,
            'registry_sha256': registry.sha256,
            'registry_version': registry.version,
            'projection_complete': True,
            'generated_at': generated_at.isoformat(),
            'records': records,
        }

    def _blocked_status(self, error: str) -> dict:
        return {
            '_comment': (
                'Deny-all manual-registry projection. The owner-maintained '
                'halal_coins.json file is missing, stale or invalid.'),
            'schema_version': SCHEMA_VERSION,
            'projection_mode': MANUAL_PROJECTION_MODE,
            'registry_valid': False,
            'registry_sha256': _file_sha256(self.registry_path),
            'registry_version': '',
            'projection_complete': False,
            'generated_at': _now().isoformat(),
            'registry_error': str(error)[:500],
            'records': [],
        }

    def _record(self, registry: ManualRegistry, base: str,
                generated_at: datetime) -> dict:
        report = build_manual_decision_report(registry, base)
        request_id = f'manual-{registry.sha256[:24]}'
        report_name = f'{base}_{request_id}.json'
        report_path = self.reports_dir / report_name
        atomic_write_json(report_path, report)
        report_sha = hashlib.sha256(report_path.read_bytes()).hexdigest()
        assert registry.last_reviewed is not None
        assert registry.next_review is not None
        expiry = datetime.combine(
            registry.next_review, wall_time(23, 59, 59), tzinfo=timezone.utc)
        return attach({
            'symbol': base,
            'pair': f'{base}/USDT',
            'status': 'GREEN',
            'final_code': 'GREEN',
            'validated': True,
            'reviewed_at': registry.last_reviewed.isoformat(),
            'completed_at': generated_at.isoformat(),
            'expires_at': expiry.isoformat(),
            'source': MANUAL_STATUS_SOURCE,
            'confidence': 'OWNER_REVIEWED',
            'human_escalation_required': False,
            'request_id': request_id,
            'report_file': report_name,
            'report_sha256': report_sha,
            'registry_sha256': registry.sha256,
            'registry_version': registry.version,
        }, purpose=STATUS_PURPOSE)

    def _load_state_symbols(self) -> set[str]:
        try:
            payload = json.loads(self.state_path.read_text(encoding='utf-8'))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return set()
        symbols = payload.get('symbols') if isinstance(payload, dict) else None
        if not isinstance(symbols, list) or not all(isinstance(x, str) for x in symbols):
            return set()
        return set(symbols)

    def _notify(self, text: str) -> None:
        notification_id = uuid.uuid4().hex
        try:
            atomic_write_json(self.alert_outbox / f'{notification_id}.json', {
                'schema': 1,
                'notification_id': notification_id,
                'created_at': time.time(),
                'text': text,
                'buttons': None,
                'chat_id': None,
            })
        except Exception as exc:
            audit('manual_sharia_registry_notification_failed', severity='ERROR',
                  details={'error': f'{type(exc).__name__}: {exc}'})

    def _record_change(self, registry: ManualRegistry) -> None:
        previous = self._load_state_symbols()
        current = set(registry.symbols)
        added = sorted(current - previous)
        removed = sorted(previous - current)
        atomic_write_json(self.state_path, {
            'registry_sha256': registry.sha256,
            'registry_version': registry.version,
            'symbols': sorted(current),
            'updated_at': _now().isoformat(),
        })
        if not previous and current:
            self._notify(
                f'🕌 Manual Sharia registry loaded: {len(current)} approved Spot/USDT '
                'symbols. Operational allowlist only — not a fatwa.')
        elif added or removed:
            parts = ['🕌 Manual Sharia registry updated.']
            if added:
                parts.append('Approved: ' + ', '.join(added))
            if removed:
                parts.append('Blocked/removed: ' + ', '.join(removed))
            parts.append('Operational allowlist only — not a fatwa.')
            self._notify('\n'.join(parts))

    def _write_health(self, *, ok: bool, registry: ManualRegistry | None = None,
                      error: str = '') -> None:
        count = len(registry.symbols) if registry is not None else 0
        atomic_write_json(self.health_path, {
            'ok': bool(ok),
            'process_alive': True,
            'ready_for_screening': False,
            'registry_mode': MANUAL_PROJECTION_MODE,
            'manual_registry_valid': bool(ok),
            'eligible_assets': count if ok else 0,
            'sharia_trade_ready': bool(ok and count),
            'eligibility_blocker': (
                '' if ok and count else
                'OWNER_MAINTAINED_HALAL_LIST_EMPTY' if ok else
                'MANUAL_HALAL_REGISTRY_INVALID'),
            'registry_version': registry.version if registry else '',
            'registry_sha256': registry.sha256 if registry else _file_sha256(self.registry_path),
            'degraded_reason': str(error)[:500] if error else '',
            'automatic_research_enabled': False,
            'ts': time.time(),
        })

    def sync(self, *, force: bool = False) -> bool:
        try:
            registry = load_manual_registry(self.registry_path)
            unchanged = False
            if not force and self.status_path.is_file():
                try:
                    gate = ManualRegistryFilter(self.status_path)
                    unchanged = (
                        gate.projection_mode == MANUAL_PROJECTION_MODE and
                        gate.registry_sha256 == registry.sha256)
                except Exception:
                    unchanged = False
            if not unchanged:
                generated_at = _now()
                self.reports_dir.mkdir(parents=True, exist_ok=True)
                records = [self._record(registry, base, generated_at)
                           for base in registry.bases]
                atomic_write_json(
                    self.status_path,
                    self._status_document(registry, records, generated_at))
                # Re-open through the exact consumer gate before reporting success.
                verified = ManualRegistryFilter(self.status_path)
                if verified.registry_sha256 != registry.sha256:
                    raise ManualRegistryError('manual projection verification mismatch')
                self._record_change(registry)
                audit('manual_sharia_registry_projected', details={
                    'registry_version': registry.version,
                    'registry_sha256': registry.sha256,
                    'approved_symbols': len(registry.symbols),
                })
            self.last_error_key = ''
            self._write_health(ok=True, registry=registry)
            return True
        except Exception as exc:
            error = f'{type(exc).__name__}: {exc}'
            atomic_write_json(self.status_path, self._blocked_status(error))
            self._write_health(ok=False, error=error)
            error_key = self.status_path.read_text(encoding='utf-8')[:1000]
            if error_key != self.last_error_key:
                self.last_error_key = error_key
                self._notify(
                    '🚫 Manual Sharia registry invalid or expired. All new entries '
                    f'are blocked. {error[:300]}')
            audit('manual_sharia_registry_blocked', severity='ERROR',
                  details={'error': error})
            return False


def main() -> None:
    logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO'),
                        format='%(asctime)s %(levelname)s %(name)s %(message)s')
    if not envelope.installed_release_hash():
        raise SystemExit(
            'RELEASE BINDING MISSING: set ENVELOPE_RELEASE_HASH or install '
            'RELEASE_SHA256.txt')
    try:
        load_private_key()
        load_public_key()
    except Exception as exc:
        raise SystemExit(f'SHARIA RESULT ATTESTATION KEY INVALID: {exc}') from exc
    poll_seconds = env_int('SHARIA_MANUAL_REGISTRY_POLL_SECONDS', 5, 1, 300)
    projector = ManualRegistryProjector()
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    projector.sync(force=True)
    audit('manual_sharia_registry_service_started', details={
        'automatic_research_enabled': False,
        'poll_seconds': poll_seconds,
    })
    while not STOP.wait(poll_seconds):
        projector.sync()
    audit('manual_sharia_registry_service_stopped')


if __name__ == '__main__':
    main()
