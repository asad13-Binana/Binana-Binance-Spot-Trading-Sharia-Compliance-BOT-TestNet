from __future__ import annotations
"""Persistent free-tier API budget: per-minute request window + daily and
monthly credit caps.

Hardened per the independent audit (V102-REM-003/005/006):

- The per-minute request window PERSISTS across restarts, so a crash-looping
  container cannot burst past the per-minute cap by resetting its own memory
  (audit ISSUE 5).
- Corrupted or unreadable state FAILS CLOSED: the bad file is quarantined,
  a last-known-good backup is tried, and if none is valid the month is
  treated as fully spent until provider reconciliation or a documented
  manual reset. Corruption can reduce availability, never increase allowance
  (audit ISSUE 6).
- ``record_extra`` books provider-reported credit charges above the estimate
  and ``reconcile_month`` adopts the provider's authoritative monthly usage,
  only ever raising the local count — except as the sanctioned recovery from
  a quarantined state (audit ISSUE 3).

Callers never block: the universe scan must fall back to Binance-only data
instead of stalling behind a third-party quota.
"""
import logging
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from services.common.atomic import atomic_write_json, read_json
from services.common.audit import audit

log = logging.getLogger('universe.external')

_MAX_COUNT = 10_000_000
_MAX_MARKS = 10_000


def _utc() -> datetime:
    return datetime.now(timezone.utc)


class ApiBudget:
    """Per-minute request window plus persisted daily/monthly credit caps.

    The per-minute window counts REQUESTS; daily and monthly counters count
    CREDITS (CoinMarketCap charges 1 credit per started batch of 200 rows,
    so one request can cost several credits). All three persist through
    ``state_path`` on the shared volume.
    """

    def __init__(self, name: str, state_path: str | Path, per_minute: int,
                 per_month: int, per_day: int | None = None, *,
                 disabled: bool = False):
        if per_minute < 1 or per_month < 1 or (per_day is not None and per_day < 1):
            raise ValueError(f'{name}: all budget caps must be >= 1')
        self.name = name
        self.state_path = Path(state_path)
        self.per_minute = int(per_minute)
        self.per_month = int(per_month)
        # Spending a whole month's credits in one burst is still abuse;
        # spread the monthly cap over a worst-case 31-day month by default.
        self.per_day = int(per_day) if per_day is not None else max(1, self.per_month // 31)
        self._lock = threading.Lock()
        self._minute_marks: list[float] = []
        self._day = _utc().strftime('%Y-%m-%d')
        self._month = _utc().strftime('%Y-%m')
        self._day_count = 0
        self._month_count = 0
        self.quarantined = False
        self.disabled = bool(disabled)
        if self.disabled:
            self.quarantined = True
            self._day_count = self.per_day
            self._month_count = self.per_month
            return
        self._load_state()

    # ── state loading (fail closed on corruption) ────────────────────────

    def _validated_state(self, state: object) -> dict | None:
        """Return the state dict if structurally sane, else None.

        A future-dated day/month or out-of-range counter is treated as
        corruption, not as data: a corrupt ledger must never be trusted.
        """
        if not isinstance(state, dict):
            return None
        try:
            day = str(state.get('day', ''))
            month = str(state.get('month', ''))
            datetime.strptime(day, '%Y-%m-%d')
            datetime.strptime(month, '%Y-%m')
            if day > self._day or month > self._month:
                return None
            day_count = int(state.get('day_count', 0))
            month_count = int(state.get('month_count', 0))
            if not (0 <= day_count <= _MAX_COUNT and 0 <= month_count <= _MAX_COUNT):
                return None
            marks = state.get('minute_marks', [])
            if not isinstance(marks, list) or len(marks) > _MAX_MARKS:
                return None
            now = time.time()
            clean_marks = []
            for mark in marks:
                if not isinstance(mark, (int, float)) or isinstance(mark, bool):
                    return None
                # Small forward skew tolerated; a far-future mark is corruption.
                if mark > now + 120:
                    return None
                if now - float(mark) < 60.0:
                    clean_marks.append(float(mark))
            quarantined = state.get('quarantined', False)
            if not isinstance(quarantined, bool):
                return None
            return {'day': day, 'month': month, 'day_count': day_count,
                    'month_count': month_count, 'minute_marks': clean_marks,
                    'quarantined': quarantined}
        except (TypeError, ValueError):
            return None

    def _adopt(self, state: dict) -> None:
        self.quarantined = state['quarantined']
        self._minute_marks = state['minute_marks']
        if state['month'] == self._month:
            self._month_count = state['month_count']
        if state['day'] == self._day:
            self._day_count = state['day_count']

    def _bak_path(self) -> Path:
        return self.state_path.with_name(self.state_path.name + '.bak')

    def _install_marker_path(self) -> Path:
        return self.state_path.with_name(self.state_path.name + '.install')

    def _fail_closed(self, reason: str, quarantined_file: str | None = None) -> None:
        """Block the provider until CMC /v1/key/info reconciliation or a
        documented manual reset. Fail-closed can only REDUCE availability."""
        self.quarantined = True
        self._day_count = self.per_day
        self._month_count = self.per_month
        self._persist()
        log.error('%s budget FAILING CLOSED (provider blocked): %s',
                  self.name, reason)
        audit('external_budget_state_quarantined', severity='WARNING',
              details={'provider': self.name, 'reason': reason,
                       'quarantined_file': quarantined_file,
                       'action': 'provider blocked until reconciliation or manual reset'})

    def _load_state(self) -> None:
        marker = self._install_marker_path()
        if not self.state_path.exists():
            # V102-REM-013 (deep-audit HIGH): distinguish a genuine first
            # install from unexplained state loss. A missing ledger is only
            # allowed to start at zero on first install (no marker yet). If the
            # install marker exists, the ledger vanished under a running
            # deployment — treat that as corruption and FAIL CLOSED, never as a
            # free reset to zero. A documented manual reset deletes BOTH files.
            if marker.exists():
                self._fail_closed('budget ledger missing but install marker present '
                                  '(unexplained state loss)')
                return
            self._persist()
            self._mark_installed()
            return  # true fresh install: durable zero usage is correct
        state = self._validated_state(read_json(self.state_path, None))
        if state is not None:
            self._adopt(state)
            self._mark_installed()
            try:  # refresh the last-known-good backup (forensic + CMC floor)
                shutil.copyfile(self.state_path, self._bak_path())
            except OSError:
                pass
            return
        # V102-REM-012 (deep-audit HIGH): corrupted main state ALWAYS fails
        # closed. The previous design restored a `.bak` that could hold a
        # LOWER, stale counter than the corrupted-but-real main ledger, which
        # would hand back already-spent quota — a fail-OPEN. A local backup can
        # never authoritatively prove current usage after corruption, so the
        # only safe action is to block until an authoritative source
        # (CMC /v1/key/info) or a deliberate manual reset restores trust. The
        # `.bak` is retained for forensics and as a CMC reconciliation floor,
        # never to increase availability.
        stamp = _utc().strftime('%Y%m%dT%H%M%SZ')
        quarantine_name = self.state_path.with_name(
            f'{self.state_path.name}.corrupt-{stamp}')
        try:
            self.state_path.replace(quarantine_name)
        except OSError:
            quarantine_name = self.state_path  # rename failed; leave in place
        self._fail_closed('budget ledger corrupt (backup cannot authoritatively '
                          'prove current usage)', quarantined_file=quarantine_name.name)

    def _mark_installed(self) -> None:
        marker = self._install_marker_path()
        if marker.exists():
            return
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(f'{self.name} budget install marker; deleting this '
                              f'file plus the budget json is the documented '
                              f'manual reset.\n', encoding='utf-8')
        except OSError:
            pass

    # ── rolling / persistence ────────────────────────────────────────────

    def _roll(self) -> None:
        now = time.time()
        self._minute_marks = [t for t in self._minute_marks if now - t < 60.0]
        day, month = _utc().strftime('%Y-%m-%d'), _utc().strftime('%Y-%m')
        if day != self._day:
            self._day = day
            self._day_count = self.per_day if self.quarantined else 0
        if month != self._month:
            # A quarantined ledger stays blocked across rollovers until it is
            # reconciled or manually reset; its real usage history is unknown.
            self._month = month
            self._month_count = self.per_month if self.quarantined else 0

    def _persist(self) -> None:
        atomic_write_json(self.state_path, {
            'name': self.name,
            'day': self._day, 'day_count': self._day_count,
            'month': self._month, 'month_count': self._month_count,
            'per_minute': self.per_minute, 'per_day': self.per_day,
            'per_month': self.per_month,
            'minute_marks': [round(m, 3) for m in self._minute_marks],
            'quarantined': self.quarantined,
        })

    # ── public API ───────────────────────────────────────────────────────

    def try_acquire(self, cost: int = 1) -> bool:
        """Reserve one request worth ``cost`` credits, or refuse immediately.

        The reservation happens BEFORE the HTTP call is sent because both
        providers count failed requests toward quota (preserved V4.9.1
        Codex L4-01 finding).
        """
        cost = max(1, int(cost))
        if self.disabled:
            return False
        with self._lock:
            self._roll()
            if self.quarantined:
                return False
            if len(self._minute_marks) + 1 > self.per_minute:
                return False
            if self._day_count + cost > self.per_day:
                return False
            if self._month_count + cost > self.per_month:
                return False
            self._minute_marks.append(time.time())
            self._day_count += cost
            self._month_count += cost
            self._persist()
            return True

    def record_extra(self, cost: int) -> None:
        """Book credits the provider actually charged beyond the estimate.

        Never subtracts; counters may exceed the cap — that only makes the
        budget refuse sooner, which is the safe direction.
        """
        cost = int(cost)
        if cost <= 0:
            return
        if self.disabled:
            return
        with self._lock:
            self._roll()
            self._day_count += cost
            self._month_count += cost
            self._persist()

    def reconcile_month(self, provider_used: object) -> int | None:
        """Adopt the provider's authoritative month usage.

        Normal state: only ever RAISES the local count (malformed or lower
        provider data can never free up local budget). Quarantined state:
        a valid provider figure is the sanctioned recovery and is adopted
        exactly; the day counter stays conservative until UTC midnight.
        """
        if self.disabled:
            return None
        if not isinstance(provider_used, int) or isinstance(provider_used, bool):
            return None
        if provider_used < 0 or provider_used > _MAX_COUNT:
            return None
        with self._lock:
            self._roll()
            if self.quarantined:
                self._month_count = provider_used
                self.quarantined = False
                log.warning('%s budget recovered from quarantine via provider '
                            'reconciliation: month usage set to %d',
                            self.name, provider_used)
                audit('external_budget_state_reconciled', severity='INFO',
                      details={'provider': self.name,
                               'provider_month_used': provider_used})
            else:
                self._month_count = max(self._month_count, provider_used)
            self._persist()
            return self._month_count

    def stats(self) -> dict:
        with self._lock:
            self._roll()
            return {
                'name': self.name,
                'minute_used': len(self._minute_marks), 'minute_cap': self.per_minute,
                'day_used': self._day_count, 'day_cap': self.per_day,
                'month_used': self._month_count, 'month_cap': self.per_month,
                'quarantined': self.quarantined,
            }
