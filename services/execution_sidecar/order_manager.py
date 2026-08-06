from __future__ import annotations
import json
import hashlib
import os
import re
import shutil
import time

from services.common import envelope
from services.common.audit import audit
from services.common.config_bounds import env_int
from services.common.paths import SHARIA_QUEUE_INBOX, SHARIA_REPORTS_DIR, SHARIA_RESULTS_DIR
from services.common.retention import prune_files
from services.common.sharia_attestation import RESULT_PURPOSE, verify_attached
from services.common.sharia_v19 import TRADE_ELIGIBLE_CODES, validate_result
from services.execution_sidecar.package_mode import enforce_sharia_gate_mode, load_package_mode
from services.universe_service.sharia_filter import ShariaFilter

SIGNAL_PRODUCER = 'freqtrade-strategy'
SCREENER_PRODUCER = 'sharia-screener'


class OrderManager:
    """Owns the signal-to-entry pipeline of the execution sidecar.

    Order of gates for every signal file (all fail closed):
      1. HMAC envelope authentication (V101-NEW-001) — only the Freqtrade
         strategy producer is trusted;
      2. structural pair/symbol/base validation, BNB/BTC exclusion;
      3. duplicate/uncertain prior-submission checks (durable claims);
      4. cached V19.1 status gate (current GREEN/GREEN_AVOID_OPTIONAL record);
      5. freshness/universe/risk guard;
      6. entries armed check;
      7. FRESH V19.1 screening of the exact pair (master protocol 8.5):
         a signed screening request is enqueued and the signal waits — without
         blocking the command loop — for a signed, validated, trade-eligible
         result. Timeout, invalid, mismatched or non-eligible results reject
         the signal;
      8. durable claim, then submission through the adapter lifecycle (the
         deterministic simulator in simulation mode — C-004 — or the preserved
         core in testnet/live).
    """

    def __init__(self, adapter, state, guard, state_store, sharia_path, paths):
        self.adapter = adapter
        self.state = state
        self.guard = guard
        self.store = state_store
        self.sharia_path = sharia_path
        self.paths = paths
        self.gate_mode = enforce_sharia_gate_mode(
            load_package_mode(),
            os.getenv('SHARIA_SIGNAL_GATE_MODE', 'fresh'),
        )
        self.screening_timeout = env_int('SIGNAL_SCREENING_TIMEOUT_SECONDS', 150, 5, 900)
        self._pending_screenings: dict[str, float] = {}

    AWAITING = 'awaiting_sharia_screening'

    def _pause_entries(self, reason: str) -> None:
        errors = []
        try:
            self.state.set_entries(False, reason)
        except Exception as exc:
            errors.append('state persistence failed: ' + str(exc))
        try:
            trader = getattr(self.adapter, 'trader', None)
            if not self.state.data.get('simulation', True) and trader and trader.is_running():
                result = self.adapter.set_enabled(False)
                if result != 'OFF':
                    errors.append('execution core did not confirm OFF: ' + str(result))
        except Exception as exc:
            errors.append('execution core disarm failed: ' + str(exc))
        if errors:
            audit('signal_fail_closed_disarm_failed', severity='CRITICAL', details={'errors': errors})

    # ---- V19.1 fresh screening seam (master protocol 8.5) ----
    def _request_fresh_screening(self, sig: dict, base: str) -> str:
        request_id = 'signal-' + sig['signal_id']
        request_path = SHARIA_QUEUE_INBOX / f'request_{request_id}.json'
        if not request_path.exists():
            payload = {
                'request_id': request_id, 'pair': sig['pair'], 'base': base,
                'priority': 'signal', 'requested_by': 'execution-sidecar',
                'signal_id': sig['signal_id'],
            }
            signed = envelope.sign_envelope(
                producer='execution-sidecar', purpose=envelope.BUS_SHARIA_REQUEST,
                payload=payload, ttl_seconds=self.screening_timeout + 120)
            SHARIA_QUEUE_INBOX.mkdir(parents=True, exist_ok=True)
            tmp = request_path.with_suffix('.tmp')
            tmp.write_text(json.dumps(signed, sort_keys=True), encoding='utf-8')
            os.replace(tmp, request_path)
            audit('signal_sharia_screening_requested', details={
                'request_id': request_id, 'pair': sig['pair']})
        return request_id

    def _fresh_screening_result(self, request_id: str, base: str):
        """(state, detail): state in {'eligible','pending','rejected'}."""
        result_path = SHARIA_RESULTS_DIR / f'result_{request_id}.json'
        if not result_path.exists():
            return 'pending', 'result not yet available'
        try:
            payload = envelope.read_verified_file(
                result_path, purpose=envelope.BUS_SHARIA_RESULT,
                expected_producers={SCREENER_PRODUCER})
        except envelope.EnvelopeError as exc:
            return 'rejected', f'unauthenticated screening result: {exc}'
        try:
            payload = verify_attached(payload, purpose=RESULT_PURPOSE)
        except Exception as exc:
            return 'rejected', f'unattested screening result: {exc}'
        if str(payload.get('request_id')) != request_id:
            return 'rejected', 'screening result belongs to a different request'
        if str(payload.get('base', '')).upper() != base:
            return 'rejected', 'screening result belongs to a different asset'
        if str(payload.get('pair', '')).upper() != f'{base}/USDT':
            return 'rejected', 'screening result belongs to a different pair'
        expected_report = f'{base}_{request_id}.json'
        if str(payload.get('report_file', '')) != expected_report:
            return 'rejected', 'screening report filename binding failed'
        try:
            report_path = SHARIA_REPORTS_DIR / expected_report
            raw_report = report_path.read_bytes()
            if hashlib.sha256(raw_report).hexdigest() != str(payload.get('report_sha256', '')):
                raise ValueError('report hash mismatch')
            report = json.loads(raw_report.decode('utf-8'))
            validate_result(report, expected_base=base)
            if str(report.get('final_code', '')) != str(payload.get('final_code', '')):
                raise ValueError('report/result final_code mismatch')
        except Exception as exc:
            return 'rejected', f'screening report binding failed: {exc}'
        if payload.get('validated') is not True:
            return 'rejected', 'screening result failed V19.1 validation: ' + str(
                payload.get('final_code'))
        code = str(payload.get('final_code', ''))
        if code not in TRADE_ELIGIBLE_CODES:
            return 'rejected', f'V19.1 verdict {code or "MISSING"} is not trade-eligible'
        return 'eligible', code

    def process_signal(self, path, current_universe, current_universe_hash):
        try:
            raw = json.loads(path.read_text(encoding='utf-8'))
        except Exception as exc:
            return self.reject(path, {}, 'malformed', str(exc))
        # Gate 1 — producer authentication. Unsigned or forged files are
        # rejected before their content is trusted for anything.
        try:
            sig = envelope.verify_envelope(
                raw, purpose=envelope.BUS_SIGNAL, expected_producers={SIGNAL_PRODUCER})
        except envelope.EnvelopeError as exc:
            return self.reject(path, {}, 'unauthenticated_signal', str(exc), record=False)
        pair = str(sig.get('pair', '')).upper()
        symbol = str(sig.get('symbol', '')).upper()
        parts = pair.split('/')
        if len(parts) != 2 or parts[1] != 'USDT' or not parts[0].isalnum():
            return self.reject(path, sig, 'invalid_pair', pair)
        base = parts[0]
        if symbol != base + 'USDT':
            return self.reject(path, sig, 'pair_symbol_mismatch', f'{pair} != {symbol}')
        if base in {'BNB', 'BTC'}:
            return self.reject(path, sig, 'excluded_base', base)
        signal_id = str(sig.get('signal_id', ''))
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', signal_id):
            return self.reject(path, sig, 'invalid_signal_id', signal_id, record=False)
        prior_result = self.store.signal_result(signal_id)
        if prior_result == 'IN_PROGRESS':
            self._pause_entries('uncertain-prior-signal-submission-reconcile-required')
            self.guard.set_global_pause('uncertain-prior-signal-submission-reconcile-required')
            return self.reject(path, sig, 'uncertain_prior_submission_reconcile_required', '', record=False)
        if prior_result is not None:
            self._pending_screenings.pop(signal_id, None)
            return self.reject(path, sig, 'duplicate signal token', '', record=False)
        if str(sig.get('strategy', '')) != 'IctSmcStrategy':
            return self.reject(path, sig, 'unexpected_strategy', str(sig.get('strategy', '')))
        # Gate 4 — cached V19.1 record must currently be trade-eligible.
        try:
            decision = ShariaFilter(self.sharia_path).decision(base)
        except Exception as exc:
            return self.reject(path, sig, 'sharia_dataset_unreadable', str(exc))
        if not decision.allowed:
            return self.reject(path, sig, 'sharia_' + decision.status.lower(), decision.reason)
        if str(sig.get('sharia_status', '')).upper() not in TRADE_ELIGIBLE_CODES:
            return self.reject(path, sig, 'signal_sharia_status_invalid',
                               str(sig.get('sharia_status', '')))
        ok, why = self.guard.allow(sig, current_universe, current_universe_hash, self.store)
        if not ok:
            self._pending_screenings.pop(signal_id, None)
            return self.reject(path, sig, why, '')
        if not self.state.entries():
            return self.reject(path, sig, 'entries_paused', self.state.data.get('pause_reason', ''))

        # Gate 7 — FRESH V19.1 screening of the exact pair before any order.
        if self.gate_mode != 'cached':
            request_id = self._request_fresh_screening(sig, base)
            requested_at = self._pending_screenings.setdefault(signal_id, time.time())
            state, detail = self._fresh_screening_result(request_id, base)
            if state == 'pending':
                if time.time() - requested_at > self.screening_timeout:
                    self._pending_screenings.pop(signal_id, None)
                    return self.reject(path, sig, 'sharia_screening_timeout_fail_closed',
                                       f'no validated result within {self.screening_timeout}s')
                # Leave the file in the inbox; the loop stays responsive and
                # this signal is re-evaluated on the next pass.
                return None, self.AWAITING
            self._pending_screenings.pop(signal_id, None)
            if state == 'rejected':
                return self.reject(path, sig, 'sharia_v19_fail_closed', detail)
            audit('signal_sharia_screening_eligible', details={
                'signal_id': signal_id, 'pair': pair, 'final_code': detail})
            # The wait consumed time — re-verify freshness/risk before claiming.
            ok, why = self.guard.allow(sig, current_universe, current_universe_hash, self.store)
            if not ok:
                return self.reject(path, sig, 'stale_after_screening:' + why, '')

        if not self.store.claim_signal(sig):
            return self.reject(path, sig, 'duplicate signal token', '', record=False)

        trade_id = sig['signal_id']
        self.store.upsert_trade(trade_id, sig['pair'], lifecycle_state='SIGNAL_APPROVED',
                                protection_mode=self.state.get_mode(), reconciliation_status='SIGNAL_ACCEPTED')
        simulation = bool(self.state.data.get('simulation', True))
        # C-004 fix: simulation submits through the same adapter lifecycle as
        # testnet/live (the adapter is the deterministic simulator there).
        accepted, msg = self.adapter.submit(
            sig['symbol'], f"Freqtrade signal {sig['signal_id']} candle {sig['candle_time']}")
        label = 'simulated:' if simulation else ''
        result = (label + 'accepted') if accepted else (label + 'rejected:' + str(msg))
        self.guard.record(sig, result)
        self.store.record_signal(sig, result)
        self.store.upsert_trade(trade_id, sig['pair'],
                                lifecycle_state='ENTRY_SUBMITTED' if accepted else 'ERROR',
                                reconciliation_status='SUBMITTED' if accepted else 'ENTRY_REJECTED')
        if accepted:
            # Close the gap between REST acceptance, an early user-stream event,
            # and the next periodic reconciliation pass.
            self.adapter.mirror_positions('ENTRY_SUBMISSION_ACCEPTED')
        self.move(path, self.paths['processed'] if accepted else self.paths['rejected'])
        audit('signal_execution_result', severity='INFO' if accepted else 'WARNING',
              details={'signal_id': sig['signal_id'], 'accepted': accepted,
                       'message': msg, 'simulation': simulation})
        return accepted, msg

    @staticmethod
    def move(path, destination):
        destination.mkdir(parents=True, exist_ok=True)
        target = destination / path.name
        if target.exists():
            target = destination / f'{path.stem}.{time.time_ns()}{path.suffix}'
        shutil.move(str(path), str(target))
        prune_files(destination, '*.json',
                    max_files=max(0, int(os.getenv('SIGNAL_ARCHIVE_MAX_FILES', '10000'))))

    def reject(self, path, signal, reason, detail, *, record=True):
        if record and signal.get('signal_id') and signal.get('pair'):
            self.store.record_signal(signal, 'rejected:' + reason)
        audit('signal_rejected', severity='WARNING',
              details={'file': path.name, 'reason': reason, 'detail': detail})
        self.move(path, self.paths['rejected'])
        return False, reason
