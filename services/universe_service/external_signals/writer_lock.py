from __future__ import annotations
"""Cross-process single-writer lease for the external-signals quota state.

A-004 (deep audit): the free-tier budgets are a read-modify-write ledger
guarded only by a ``threading.Lock``. That is correct for the shipped topology
(exactly one ``universe`` service writes them) but gives no protection if a
second process — a duplicate container, a manual scan, an accidental second
replica — ever shares the same volume. Two writers could each believe they
have quota left and together exceed the provider limit.

Behaviour by platform:

  * POSIX (Linux — the Docker/Oracle deployment target and CI): a real
    ``fcntl.flock(LOCK_EX | LOCK_NB)`` advisory lock is held for the lifetime
    of the process. A second process cannot acquire it, and the enrichment
    layer disables itself there (fail closed to Binance-only scanning) rather
    than double-spending quota.
  * Non-POSIX (Windows development hosts): no OS lock is taken. The lease file
    is still written for observability, and the single-writer invariant remains
    a documented operational constraint (docs/EXTERNAL_SIGNALS.md). Production
    never runs on Windows, so mutual exclusion is enforced where it matters.

Re-entrancy: a lease already held by THIS process is reused, so building the
enrichment object more than once in one process (tests, a re-scan) is safe and
never self-deadlocks.
"""
import logging
import os
from pathlib import Path

log = logging.getLogger('universe.external')

# Leases held by THIS process, keyed by lease path. Only populated on POSIX,
# where an open file descriptor is what actually holds the advisory lock.
_HELD: dict[str, object] = {}

try:  # POSIX: real advisory locking
    import fcntl
    _LOCKING = 'fcntl'
except ImportError:  # Windows / other
    fcntl = None  # type: ignore[assignment]
    _LOCKING = 'unavailable'


def locking_backend() -> str:
    return _LOCKING


def _key(path: Path) -> str:
    try:
        return str(path.resolve())
    except OSError:
        return str(path)


def acquire_writer_lease(lease_path: str | Path) -> bool:
    """Acquire (or reuse) the single-writer lease. Never raises.

    Returns True ONLY when exclusive writer safety has been established for this
    process. Returns False whenever that cannot be established FOR ANY REASON —
    a different process holds the POSIX lock, the lease directory cannot be
    created, the lease file cannot be opened, or the lease marker cannot be
    written. The caller must then disable quota-spending enrichment (fail
    closed); a broken lock path must never be read as "safe to write"
    (R-001/LOCK-002).
    """
    path = Path(lease_path)
    key = _key(path)
    if key in _HELD:
        return True  # already ours
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # R-001 fix: previously returned True here, leaving enrichment enabled
        # with NO lock in place. A state path we cannot even create is exactly
        # when concurrent writers are most likely; fail closed.
        log.error('external-signals lease directory could not be created (%s); '
                  'failing closed — enrichment will be disabled', exc)
        return False

    if _LOCKING != 'fcntl':
        # No OS advisory locking here (Windows dev host, no production role).
        # Record the holder; if even that write fails, the state path is broken
        # and we fail closed rather than assume safety.
        try:
            path.write_text(f'pid={os.getpid()} backend={_LOCKING}\n', encoding='utf-8')
        except OSError as exc:
            log.error('external-signals lease marker could not be written (%s); '
                      'failing closed', exc)
            return False
        return True

    try:
        handle = path.open('a+', encoding='utf-8')
    except OSError as exc:
        # R-001 fix: previously returned True (fail open) — see above.
        log.error('external-signals lease file could not be opened (%s); '
                  'failing closed — enrichment will be disabled', exc)
        return False
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        try:
            handle.close()
        except OSError:
            pass
        return False  # another process holds it
    try:
        handle.seek(0)
        handle.truncate()
        handle.write(f'pid={os.getpid()} backend={_LOCKING}\n')
        handle.flush()
    except OSError:
        pass
    _HELD[key] = handle  # keep open: the descriptor IS the lock
    return True


def release_writer_lease(lease_path: str | Path) -> None:
    """Release a lease held by this process (used by tests and shutdown)."""
    handle = _HELD.pop(_key(Path(lease_path)), None)
    if handle is None:
        return
    try:
        if _LOCKING == 'fcntl':
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        handle.close()
    except OSError:
        pass
