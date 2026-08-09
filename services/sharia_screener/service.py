from __future__ import annotations

"""V19.1 Sharia screening service — the fifth, independent Oracle container.

Responsibilities (master protocol sections 8.3–8.8):
  * verify the immutable controller's exact SHA-256 at startup and refuse to
    run on any mismatch;
  * ingest HMAC-signed screening requests (signal > manual > bulk > idle
    priority) from the sidecar, the Telegram broker and the installer;
  * validate that a requested pair is currently a TRADING, spot-enabled,
    USDT-quoted Binance symbol before spending any API quota on it;
  * execute the controller locally against owner-identified, content-addressed
    sources, with no model or separately billed API dependency;
  * write the report, the signed result envelope, the canonical status
    projection and the legacy compatibility whitelist (single-writer);
  * when idle, continuously screen the current top-50 universe and refresh
    stale records, within explicit daily/persecond quotas;
  * expose durable health/progress state and survive restarts (durable
    SQLite queue; RUNNING requests re-queue on startup).

This service never receives Binance trading credentials and can never place
an order. Its output is research screening, not a fatwa.
"""
import logging
import os
import signal as os_signal
import threading
import time
from datetime import datetime, timezone

from services.common import envelope
from services.common.atomic import atomic_write_json
from services.common.audit import audit
from services.common.binance_public import BinancePublicClient
from services.common.config_bounds import ConfigError, env_int
from services.common.paths import (
    SHARIA_CONTROLLER_FILE,
    SHARIA_DECISION_INBOX,
    SHARIA_DECISION_PROCESSED,
    SHARIA_EVIDENCE_DIR,
    SHARIA_QUEUE_INBOX,
    SHARIA_QUEUE_PROCESSED,
    SHARIA_RESULTS_DIR,
    SHARIA_RUNTIME_DIR,
    SHARIA_SOURCE_REGISTRY,
    UNIVERSE_CURRENT,
)
from services.common.retention import prune_files
from services.common.sharia_attestation import load_private_key, load_public_key
from services.common.sharia_v19 import (
    ControllerIntegrityError,
    ResultValidationError,
    fail_closed_report,
    load_controller,
    validate_result,
)
from services.sharia_screener.approval import (
    OwnerDecisionError,
    apply_owner_decision,
)
from services.sharia_screener.bridge import (
    ensure_status_file_exists,
    write_screening_outcome,
)
from services.sharia_screener.local_runner import LocalScreeningRunner
from services.sharia_screener.queue_store import PRIORITIES, QueueStore
from services.sharia_screener.runner import ScreeningUnavailable
from services.universe_service.snapshot_store import load_current

log = logging.getLogger('sharia-screener')
STOP = threading.Event()
REQUEST_PRODUCERS = {'execution-sidecar', 'telegram-broker', 'deploy-installer'}
HARD_MAX_SCANS_PER_DAY = 1000
HARD_MAX_URGENT_RESERVE = 200
HARD_MAX_SCANS_PER_BASE_PER_DAY = 24
HARD_MAX_SCANS_PER_ACTOR_PER_DAY = 1000


def _quota_settings() -> dict[str, int]:
    """Load bounded, relationally valid screening-cost controls."""
    daily = env_int('SHARIA_MAX_SCANS_PER_DAY', 1000, 1, HARD_MAX_SCANS_PER_DAY)
    settings = {
        'daily': daily,
        'min_between': env_int(
            'SHARIA_MIN_SECONDS_BETWEEN_SCANS', 10, 1, 86_400),
        'urgent_reserve': env_int(
            'SHARIA_URGENT_RESERVE_PER_DAY', min(20, daily),
            1, HARD_MAX_URGENT_RESERVE),
        'per_base': env_int(
            'SHARIA_MAX_SCANS_PER_BASE_PER_DAY', min(4, daily),
            1, HARD_MAX_SCANS_PER_BASE_PER_DAY),
        'per_actor': env_int(
            'SHARIA_MAX_SCANS_PER_ACTOR_PER_DAY', daily,
            1, HARD_MAX_SCANS_PER_ACTOR_PER_DAY),
    }
    for name in ('urgent_reserve', 'per_base', 'per_actor'):
        if settings[name] > daily:
            raise ConfigError(
                f'{name}={settings[name]} cannot exceed SHARIA_MAX_SCANS_PER_DAY={daily}')
    return settings


class ShariaScreenerService:
    def __init__(self):
        self.controller_raw, self.controller = load_controller(SHARIA_CONTROLLER_FILE)
        self.queue = QueueStore(SHARIA_RUNTIME_DIR / 'screening_queue.sqlite')
        self.runner = LocalScreeningRunner(
            self.controller, registry_path=SHARIA_SOURCE_REGISTRY,
            evidence_root=SHARIA_EVIDENCE_DIR)
        self.public = BinancePublicClient()
        self.poll_seconds = env_int('SHARIA_POLL_SECONDS', 2, 1, 60)
        quota = _quota_settings()
        self.min_between_scans = quota['min_between']
        self.max_scans_per_day = quota['daily']
        self.urgent_reserve_per_day = quota['urgent_reserve']
        self.max_scans_per_base_per_day = quota['per_base']
        self.max_scans_per_actor_per_day = quota['per_actor']
        self.idle_cycle_seconds = env_int('SHARIA_IDLE_CYCLE_SECONDS', 300, 30, 86_400)
        self.idle_retry_base_seconds = env_int(
            'SHARIA_FAILED_RETRY_SECONDS', 21_600, 300, 7 * 86_400)
        self.idle_retry_max_seconds = env_int(
            'SHARIA_FAILED_RETRY_MAX_SECONDS', 86_400, 300, 7 * 86_400)
        self.idle_retry_max_attempts = env_int(
            'SHARIA_IDLE_MAX_ATTEMPTS', 3, 1, 10)
        self.idle_enabled = os.getenv('SHARIA_IDLE_SCAN_ENABLED', 'true').lower() == 'true'
        self._last_scan_at = self.queue.last_activity_at()
        self._next_idle_at = 0.0
        self._symbol_cache: tuple[float, dict] = (0.0, {})

    # ---- request ingestion ----
    def ingest_owner_decisions(self):
        """Apply Telegram-owner decisions through a dedicated signed bus."""
        for path in sorted(SHARIA_DECISION_INBOX.glob('*.json')):
            archive = True
            try:
                payload = envelope.read_verified_file(
                    path, purpose=envelope.BUS_SHARIA_DECISION,
                    expected_producers={'telegram-broker'})
                report, request_id = apply_owner_decision(
                    payload, reports_root=SHARIA_REPORTS_DIR,
                    evidence_root=SHARIA_EVIDENCE_DIR)
                result_path = SHARIA_RESULTS_DIR / f'result_{request_id}.json'
                if result_path.exists():
                    audit('sharia_owner_decision_duplicate', details={
                        'decision_id': payload.get('decision_id'),
                        'base': payload.get('base')})
                else:
                    base = str(payload['base']).upper()
                    write_screening_outcome(
                        request_id, base, f'{base}/USDT', report,
                        validated=True,
                        meta={
                            'backend': 'local-oracle-v1',
                            'owner_decision': str(payload['action']).upper(),
                            'proposal_report_sha256': payload['report_sha256'],
                        })
                    audit('sharia_owner_decision_applied', actor='telegram-owner',
                          details={
                              'decision_id': payload['decision_id'],
                              'base': base,
                              'action': str(payload['action']).upper(),
                          })
            except (envelope.EnvelopeError, OwnerDecisionError) as exc:
                audit('sharia_owner_decision_rejected', severity='CRITICAL',
                      details={'file': path.name, 'error': str(exc)})
            except Exception as exc:
                # A valid decision must survive transient storage/signing
                # failures. Leave it in the inbox for retry; duplicate output
                # is suppressed by the decision-bound result filename.
                archive = False
                audit('sharia_owner_decision_error', severity='ERROR',
                      details={'file': path.name,
                               'error': f'{type(exc).__name__}: {exc}'})
            finally:
                if archive:
                    try:
                        SHARIA_DECISION_PROCESSED.mkdir(parents=True, exist_ok=True)
                        path.rename(SHARIA_DECISION_PROCESSED / path.name)
                    except OSError:
                        path.unlink(missing_ok=True)
        prune_files(SHARIA_DECISION_PROCESSED, '*.json', max_files=2000)

    def ingest_requests(self):
        for path in sorted(SHARIA_QUEUE_INBOX.glob('*.json')):
            try:
                payload = envelope.read_verified_file(
                    path, purpose=envelope.BUS_SHARIA_REQUEST,
                    expected_producers=REQUEST_PRODUCERS)
            except envelope.EnvelopeError as exc:
                audit('sharia_request_rejected_unauthenticated', severity='CRITICAL',
                      details={'file': path.name, 'error': str(exc)})
                path.unlink(missing_ok=True)
                continue
            except Exception as exc:
                audit('sharia_request_malformed', severity='ERROR',
                      details={'file': path.name, 'error': str(exc)})
                path.unlink(missing_ok=True)
                continue
            request_id = str(payload.get('request_id', '')).strip()
            base = str(payload.get('base', '')).upper().strip()
            pair = str(payload.get('pair', '')).upper().strip()
            priority = str(payload.get('priority', 'idle')).lower()
            requested_by = str(payload.get('requested_by', 'unknown'))
            if priority == 'bulk' and base == '*':
                self._enqueue_bulk_all(request_id, requested_by)
            elif request_id and base.isalnum() and pair == f'{base}/USDT':
                inserted = self.queue.enqueue(request_id, base, pair, priority, requested_by)
                audit('sharia_request_ingested', details={
                    'request_id': request_id, 'base': base, 'priority': priority,
                    'inserted': inserted, 'requested_by': requested_by})
            else:
                audit('sharia_request_invalid', severity='WARNING', details={
                    'request_id': request_id, 'base': base, 'pair': pair})
            try:
                SHARIA_QUEUE_PROCESSED.mkdir(parents=True, exist_ok=True)
                path.rename(SHARIA_QUEUE_PROCESSED / path.name)
            except OSError:
                path.unlink(missing_ok=True)
        prune_files(SHARIA_QUEUE_PROCESSED, '*.json', max_files=2000)

    def _spot_usdt_bases(self) -> dict:
        cached_at, cached = self._symbol_cache
        if time.time() - cached_at < 900 and cached:
            return cached
        bases = self.public.spot_usdt_trading_symbols()
        self._symbol_cache = (time.time(), bases)
        return bases

    def _enqueue_bulk_all(self, request_id: str, requested_by: str):
        """Expand a confirmed scan-all into one durable bulk request per base."""
        try:
            bases = self._spot_usdt_bases()
        except Exception as exc:
            audit('sharia_bulk_expansion_failed', severity='ERROR', details={'error': str(exc)})
            return
        added = 0
        stamp = datetime.now(timezone.utc).strftime('%Y%m%d')
        for base in sorted(bases):
            if self.queue.enqueue(f'bulk-{base}-{stamp}', base, f'{base}/USDT',
                                  'bulk', requested_by):
                added += 1
        audit('sharia_bulk_expanded', details={
            'request_id': request_id, 'universe': len(bases), 'enqueued': added})

    # ---- eligibility ----
    def _pair_eligible(self, base: str) -> tuple[bool, str]:
        try:
            info = self.public.exchange_info(base + 'USDT')
            entry = next((s for s in info.get('symbols', [])
                          if s.get('symbol') == base + 'USDT'), None)
        except Exception as exc:
            return False, f'exchangeInfo lookup failed: {exc}'
        if not entry:
            return False, 'symbol does not exist on Binance Spot'
        if entry.get('status') != 'TRADING':
            return False, f'symbol status is {entry.get("status")!r}, not TRADING'
        if not entry.get('isSpotTradingAllowed', False):
            return False, 'spot trading is not allowed for this symbol'
        if str(entry.get('quoteAsset', '')).upper() != 'USDT':
            return False, 'symbol is not USDT-quoted'
        return True, 'TRADING spot USDT'

    # ---- screening execution ----
    def _mark_failed(self, row: dict, error: str) -> bool:
        """Retry only autonomous idle work; user/signal results stay terminal."""
        retry = int(row.get('priority', PRIORITIES['idle'])) == PRIORITIES['idle']
        scheduled = self.queue.mark_failed(
            row['request_id'], error,
            retry_base_seconds=self.idle_retry_base_seconds if retry else None,
            retry_max_seconds=self.idle_retry_max_seconds if retry else None,
            max_attempts=self.idle_retry_max_attempts if retry else 1)
        if scheduled:
            audit('sharia_idle_retry_scheduled', details={
                'request_id': row['request_id'], 'base': row['base'],
                'error': str(error)[:200]})
        return scheduled

    def process_request(self, row: dict):
        request_id, base, pair = row['request_id'], row['base'], row['pair']
        claimed = self.queue.mark_running(
            request_id,
            daily_ceiling=self.max_scans_per_day,
            max_per_base=self.max_scans_per_base_per_day,
            max_per_requested_by=self.max_scans_per_actor_per_day,
            min_spacing_seconds=self.min_between_scans,
        )
        if not claimed:
            audit('sharia_request_quota_claim_deferred', details={
                'request_id': request_id, 'base': base,
                'requested_by': row.get('requested_by'),
            })
            return
        eligible, why = self._pair_eligible(base)
        if not eligible:
            report = fail_closed_report(base, reason=f'pair ineligible: {why}')
            write_screening_outcome(request_id, base, pair, report,
                                    validated=False, error=why)
            self._mark_failed(row, why)
            return
        try:
            report, meta = self.runner.run(base, pair)
        except ScreeningUnavailable as exc:
            report = fail_closed_report(base, reason=str(exc))
            write_screening_outcome(request_id, base, pair, report,
                                    validated=False, error=str(exc))
            self._mark_failed(row, str(exc))
            self._last_scan_at = time.time()
            return
        except Exception as exc:
            # M-001: only ScreeningUnavailable was caught here. A malformed
            # provider response (JSON decode error, non-dict payload, unexpected
            # extraction failure) escaped as a generic exception, the outer loop
            # merely logged it, and the request stayed RUNNING forever —
            # blocking every future screening for that asset until a restart.
            # Any provider failure must produce a fail-closed outcome AND move
            # the request out of RUNNING.
            why = f'{type(exc).__name__}: {exc}'
            audit('sharia_provider_error', severity='ERROR', details={
                'request_id': request_id, 'base': base, 'error': why})
            report = fail_closed_report(base, reason=f'provider error: {why}')
            write_screening_outcome(request_id, base, pair, report,
                                    validated=False, error=why)
            self._mark_failed(row, why)
            self._last_scan_at = time.time()
            return
        try:
            validate_result(report, expected_base=base)
        except ResultValidationError as exc:
            audit('sharia_result_validation_failed', severity='ERROR', details={
                'request_id': request_id, 'base': base, 'error': str(exc)})
            outcome_report = fail_closed_report(
                base, reason=f'result failed V19.1 validation: {exc}')
            outcome_report['rejected_model_output_final_code'] = str(report.get('final_code', ''))
            write_screening_outcome(request_id, base, pair, outcome_report,
                                    validated=False, error=str(exc), meta=meta)
            self._mark_failed(row, f'validation: {exc}')
            self._last_scan_at = time.time()
            return
        except Exception as exc:  # M-001: never leave the request RUNNING
            why = f'{type(exc).__name__}: {exc}'
            audit('sharia_validation_error', severity='ERROR', details={
                'request_id': request_id, 'base': base, 'error': why})
            write_screening_outcome(request_id, base, pair,
                                    fail_closed_report(base, reason=why),
                                    validated=False, error=why)
            self._mark_failed(row, why)
            self._last_scan_at = time.time()
            return
        try:
            outcome = write_screening_outcome(request_id, base, pair, report,
                                              validated=True, meta=meta)
            self.queue.mark_done(request_id, outcome['payload']['final_code'],
                                 outcome['payload']['report_file'])
        except Exception as exc:  # M-001: persist failure must not strand RUNNING
            why = f'{type(exc).__name__}: {exc}'
            audit('sharia_outcome_write_failed', severity='ERROR', details={
                'request_id': request_id, 'base': base, 'error': why})
            self._mark_failed(row, why)
        self._last_scan_at = time.time()

    def _throttled_next(self):
        """Choose quota-eligible work without letting urgent work bypass safety."""
        durable_last = self.queue.last_activity_at()
        self._last_scan_at = max(self._last_scan_at, durable_last)
        if time.time() - self._last_scan_at < self.min_between_scans:
            return None
        spent = self.queue.cost_today()
        if spent >= self.max_scans_per_day:
            return None
        quota_filters = {
            'max_per_base': self.max_scans_per_base_per_day,
            'max_per_requested_by': self.max_scans_per_actor_per_day,
        }
        urgent = self.queue.next_request(
            max_priority=PRIORITIES['manual'], **quota_filters)
        if urgent:
            return urgent
        nonurgent_ceiling = self.max_scans_per_day - self.urgent_reserve_per_day
        if spent >= nonurgent_ceiling:
            return None
        return self.queue.next_request(
            min_priority=PRIORITIES['bulk'], **quota_filters)

    # ---- idle scanning (master protocol 8.4) ----
    def idle_enqueue(self):
        if not self.idle_enabled or time.time() < self._next_idle_at:
            return
        self._next_idle_at = time.time() + self.idle_cycle_seconds
        from services.common.paths import SHARIA_FILE
        from services.universe_service.sharia_filter import ShariaFilter
        try:
            gate = ShariaFilter(SHARIA_FILE)
        except Exception as exc:
            audit('sharia_idle_status_unreadable', severity='ERROR', details={'error': str(exc)})
            return
        try:
            tradable = self._spot_usdt_bases()
        except Exception as exc:
            audit('sharia_idle_universe_unreadable', severity='WARNING',
                  details={'error': str(exc)})
            return
        # Discovery must not depend on the already Sharia-filtered executable
        # universe.  Prioritize known/current bases, then append every current
        # Binance Spot USDT candidate as the independent pre-Sharia source.
        candidates: list[str] = []
        def add_candidate(base: str) -> None:
            if base and base in tradable and base not in candidates:
                candidates.append(base)
        try:
            universe = load_current(
                UNIVERSE_CURRENT,
                max_age_seconds=env_int(
                    'MAX_UNIVERSE_AGE_SECONDS', 1800, 1, 86_400),
            )
        except Exception as exc:
            # The Binance discovery below remains the independent pre-Sharia
            # source.  A forged/stale pointer is ignored, never trusted merely
            # for prioritisation.
            universe = {}
            audit('sharia_idle_universe_pointer_rejected', severity='WARNING',
                  details={'error': str(exc)})
        for entry in universe.get('pairs', []):
            base = str(entry).split('/')[0].upper()
            add_candidate(base)
        # Refresh stale existing records too, newest-expiring last.
        now = datetime.now(timezone.utc)
        for base, record in gate.records.items():
            add_candidate(base)
        for base in sorted(tradable):
            add_candidate(base)
        stamp = now.strftime('%Y%m%d')
        margin = env_int('SHARIA_RESCREEN_MARGIN_SECONDS', 86_400, 0, 30 * 86_400)
        interval_days = env_int('SHARIA_RESCAN_INTERVAL_DAYS', 7, 1, 30)
        enqueued = 0
        for base in candidates:
            if base == 'BNB':
                continue  # excluded from trading; do not spend quota on it
            record = gate.records.get(base)
            needs_scan = record is None or not gate.is_record_verified(base)
            if record is not None and gate.is_record_verified(base):
                try:
                    expires = datetime.fromisoformat(str(record['expires_at']).replace('Z', '+00:00'))
                    completed = datetime.fromisoformat(
                        str(record['completed_at']).replace('Z', '+00:00'))
                    needs_scan = (
                        (expires - now).total_seconds() < margin or
                        (now - completed).total_seconds() >= interval_days * 86_400
                    )
                except Exception:
                    needs_scan = True
            if needs_scan and not self.queue.has_active_for_base(base):
                if self.queue.enqueue(f'idle-{base}-{stamp}', base, f'{base}/USDT',
                                      'idle', 'idle-scanner'):
                    enqueued += 1
        if enqueued:
            audit('sharia_idle_enqueued', details={'count': enqueued,
                                                   'universe': len(candidates)})

    # ---- health ----
    def write_health(self):
        available, reason = self.runner.available()
        # SHARIA-HEALTH-001 / F5-004: 'ok' was hardcoded True, so Docker (and
        # therefore the whole five-service stack) reported healthy even when the
        # local backend was unusable and no new screening could be produced.
        # The trade gate still fails closed, so this was never a Sharia bypass —
        # but it produced a false-green deployment in which every screening
        # signal is silently rejected. Process liveness and screening readiness
        # are now reported separately, and 'ok' requires both.
        atomic_write_json(SHARIA_RUNTIME_DIR / 'health.json', {
            'ok': bool(available), 'process_alive': True,
            'ready_for_screening': bool(available),
            'degraded_reason': '' if available else (reason or 'screening API unavailable'),
            'ts': time.time(),
            'controller_sha256': __import__('services.common.sharia_v19', fromlist=['V19_CONTROLLER_SHA256']).V19_CONTROLLER_SHA256,
            'queue': self.queue.counts(),
            'completed_today': self.queue.completed_today(),
            'cost_events_today': self.queue.cost_today(),
            'daily_quota': self.max_scans_per_day,
            'quota_policy': {
                'daily_ceiling': self.max_scans_per_day,
                'urgent_reserve': self.urgent_reserve_per_day,
                'nonurgent_ceiling': (
                    self.max_scans_per_day - self.urgent_reserve_per_day),
                'per_base_ceiling': self.max_scans_per_base_per_day,
                'per_actor_ceiling': self.max_scans_per_actor_per_day,
                'minimum_spacing_seconds': self.min_between_scans,
            },
            'backend': {'name': self.runner.provider,
                        'available': available, 'reason': reason,
                        'external_ai': False},
            'last_done': self.queue.last('DONE'),
            'last_failed': self.queue.last('FAILED'),
            'idle_scanning': self.idle_enabled,
        })

    def run_forever(self):
        requeued = self.queue.requeue_running()
        if requeued:
            audit('sharia_requests_requeued_after_restart', details={'count': requeued})
        ensure_status_file_exists()
        audit('sharia_screener_started', details={
            'controller_version': self.controller.get('VERSION'),
            'idle_scanning': self.idle_enabled,
            'daily_quota': self.max_scans_per_day,
            'urgent_reserve': self.urgent_reserve_per_day,
            'per_base_quota': self.max_scans_per_base_per_day,
            'per_actor_quota': self.max_scans_per_actor_per_day,
        })
        while not STOP.is_set():
            try:
                self.ingest_requests()
                self.ingest_owner_decisions()
                row = self._throttled_next()
                if row:
                    self.process_request(row)
                else:
                    self.idle_enqueue()
                self.write_health()
            except Exception as exc:
                log.exception('screener loop error')
                audit('sharia_screener_loop_error', severity='ERROR',
                      details={'error': str(exc)})
            STOP.wait(self.poll_seconds)
        audit('sharia_screener_stopped')


def main():
    logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO'),
                        format='%(asctime)s %(levelname)s %(name)s %(message)s')
    try:
        envelope.load_key(envelope.BUS_SHARIA_REQUEST)
        envelope.load_key(envelope.BUS_SHARIA_DECISION)
        envelope.load_key(envelope.BUS_SHARIA_RESULT)
        load_private_key()
        load_public_key()
    except ScreeningUnavailable as exc:
        raise SystemExit(f'LIVE SCREENING POLICY BLOCKED: {exc}') from exc
    except envelope.EnvelopeError as exc:
        raise SystemExit(f'BUS KEY MISSING: {exc}') from exc
    except Exception as exc:
        raise SystemExit(f'SHARIA RESULT ATTESTATION KEY INVALID: {exc}') from exc
    if not envelope.installed_release_hash():
        raise SystemExit('RELEASE BINDING MISSING: set ENVELOPE_RELEASE_HASH or install RELEASE_SHA256.txt')
    try:
        service = ShariaScreenerService()
    except ControllerIntegrityError as exc:
        raise SystemExit(f'V19.1 CONTROLLER INTEGRITY FAILURE: {exc}') from exc
    except ScreeningUnavailable as exc:
        raise SystemExit(f'SHARIA SCREENER STARTUP BLOCKED: {exc}') from exc
    os_signal.signal(os_signal.SIGTERM, lambda *_: STOP.set())
    os_signal.signal(os_signal.SIGINT, lambda *_: STOP.set())
    service.run_forever()


if __name__ == '__main__':
    main()
