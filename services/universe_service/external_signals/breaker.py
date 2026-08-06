from __future__ import annotations
"""Fail-safe circuit breaker for third-party market-data providers.

External signals are advisory: on provider trouble the universe scan must
keep publishing Binance-only data. The breaker converts repeated failures
into a cool-down so a broken or rate-limiting provider is never hammered
(free-tier ban avoidance), then allows a single probe after the cool-down.

V102-REM-005 (audit ISSUE 5): breaker state persists across restarts using
wall-clock UTC time, so a container restart cannot erase an active 429
Retry-After or auth cool-down. Unreadable persisted state fails CLOSED (the
breaker opens for one full cool-down rather than trusting a corrupt file).
"""
import logging
import time
from pathlib import Path

from services.common.atomic import atomic_write_json, read_json

log = logging.getLogger('universe.external')

# Persisted cool-downs are capped so a corrupt or hostile timestamp cannot
# park a provider forever. 24h comfortably covers the longest legitimate
# cool-down (auth errors, 6h).
_MAX_PERSISTED_OPEN_SECONDS = 86_400.0


class CircuitBreaker:
    def __init__(self, name: str, failure_threshold: int = 3,
                 cooldown_seconds: float = 900.0,
                 state_path: str | Path | None = None):
        if failure_threshold < 1 or cooldown_seconds < 0:
            raise ValueError(f'{name}: invalid circuit breaker settings')
        self.name = name
        self.failure_threshold = int(failure_threshold)
        self.cooldown_seconds = float(cooldown_seconds)
        self.state_path = Path(state_path) if state_path is not None else None
        self._failures = 0
        self._open_until = 0.0  # epoch seconds (persistable wall clock)
        self._load_state()

    def _load_state(self) -> None:
        if self.state_path is None or not self.state_path.exists():
            return
        state = read_json(self.state_path, None)
        now = time.time()
        try:
            if not isinstance(state, dict):
                raise ValueError('not a dict')
            failures = int(state.get('failures', 0))
            open_until = float(state.get('open_until_epoch', 0.0))
            if failures < 0 or failures > 1_000_000:
                raise ValueError('failures out of range')
            if open_until > now + _MAX_PERSISTED_OPEN_SECONDS:
                raise ValueError('open_until too far in the future')
            self._failures = failures
            self._open_until = max(0.0, open_until)
        except (TypeError, ValueError) as exc:
            # Unreadable throttle state fails CLOSED: open for one cool-down
            # instead of trusting memory that a cool-down never existed.
            self._failures = self.failure_threshold
            self._open_until = now + self.cooldown_seconds
            log.warning('%s breaker state unreadable (%s); failing closed '
                        'for %.0fs', self.name, exc, self.cooldown_seconds)
            self._persist()

    def _persist(self) -> None:
        if self.state_path is None:
            return
        try:
            atomic_write_json(self.state_path, {
                'name': self.name,
                'failures': self._failures,
                'open_until_epoch': round(self._open_until, 3),
            })
        except OSError:
            log.warning('%s breaker state could not be persisted', self.name)

    def allows(self) -> bool:
        return time.time() >= self._open_until

    def record_success(self) -> None:
        self._failures = 0
        self._open_until = 0.0
        self._persist()

    def record_failure(self, cooldown_override: float | None = None) -> None:
        """Count a failure. ``cooldown_override`` opens the breaker
        immediately for that many seconds (used for 429 Retry-After and for
        auth/plan errors, which retrying cannot fix)."""
        self._failures += 1
        if cooldown_override is not None:
            self._open_until = time.time() + max(0.0, float(cooldown_override))
        elif self._failures >= self.failure_threshold:
            self._open_until = time.time() + self.cooldown_seconds
        self._persist()

    def state(self) -> dict:
        now = time.time()
        return {
            'name': self.name,
            'failures': self._failures,
            'open': now < self._open_until,
            'retry_in_seconds': max(0.0, round(self._open_until - now, 1)),
        }
