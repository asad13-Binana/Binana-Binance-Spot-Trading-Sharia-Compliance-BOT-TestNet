from __future__ import annotations
"""Durable, restart-safe screening request queue for the V19.1 service."""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = '''
PRAGMA journal_mode=WAL;
PRAGMA synchronous=FULL;
CREATE TABLE IF NOT EXISTS screening_requests (
  request_id TEXT PRIMARY KEY,
  base TEXT NOT NULL,
  pair TEXT NOT NULL,
  priority INTEGER NOT NULL,
  status TEXT NOT NULL,
  requested_by TEXT,
  enqueued_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  final_code TEXT,
  error TEXT,
  report_file TEXT,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_requests_status ON screening_requests(status, priority, enqueued_at);
CREATE TABLE IF NOT EXISTS screening_attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id TEXT NOT NULL,
  base TEXT NOT NULL,
  priority INTEGER NOT NULL,
  requested_by TEXT NOT NULL,
  started_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attempts_started ON screening_attempts(started_at);
CREATE INDEX IF NOT EXISTS idx_attempts_base_actor
  ON screening_attempts(base, requested_by, started_at);
'''

PRIORITIES = {'signal': 0, 'manual': 1, 'bulk': 2, 'idle': 3}
ACTIVE = ('QUEUED', 'RUNNING')


class QueueStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.executescript(SCHEMA)
            columns = {str(row['name']) for row in
                       con.execute('PRAGMA table_info(screening_requests)').fetchall()}
            if 'attempt_count' not in columns:
                con.execute('ALTER TABLE screening_requests ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0')
            if 'next_attempt_at' not in columns:
                con.execute('ALTER TABLE screening_requests ADD COLUMN next_attempt_at TEXT')
            # Upgrade without handing back already-spent quota. Older queues
            # have attempt_count/start/finish evidence but no attempt ledger.
            prior = con.execute(
                'SELECT request_id, base, priority, requested_by, enqueued_at, '
                'started_at, finished_at, status, attempt_count '
                'FROM screening_requests').fetchall()
            for row in prior:
                expected = max(
                    int(row['attempt_count'] or 0),
                    1 if row['status'] in ('DONE', 'FAILED') else 0,
                )
                existing = con.execute(
                    'SELECT COUNT(*) AS n FROM screening_attempts WHERE request_id=?',
                    (row['request_id'],)).fetchone()
                missing = max(0, expected - int(existing['n'] if existing else 0))
                stamp = row['started_at'] or row['finished_at'] or row['enqueued_at']
                for _ in range(missing):
                    con.execute(
                        'INSERT INTO screening_attempts '
                        '(request_id, base, priority, requested_by, started_at) '
                        'VALUES (?,?,?,?,?)',
                        (row['request_id'], str(row['base']).upper(),
                         int(row['priority']), self._actor(row['requested_by']), stamp))

    @contextmanager
    def _connect(self):
        con = sqlite3.connect(self.db_path, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        finally:
            con.close()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def requeue_running(self) -> int:
        """Restart recovery: anything RUNNING when the process died re-queues."""
        with self._connect() as con:
            cur = con.execute(
                "UPDATE screening_requests SET status='QUEUED' WHERE status='RUNNING'")
            return cur.rowcount

    @staticmethod
    def _actor(value: object) -> str:
        return (str(value or '').strip().casefold() or 'unknown')[:128]

    def enqueue(self, request_id: str, base: str, pair: str, priority: str,
                requested_by: str) -> bool:
        """Idempotent enqueue with duplicate-concurrent-scan suppression.

        A new request is suppressed when the same request_id already exists,
        or when an ACTIVE request for the same base already exists at an equal
        or more urgent priority class.
        """
        prio = PRIORITIES.get(str(priority).lower(), PRIORITIES['idle'])
        with self._connect() as con:
            row = con.execute(
                'SELECT 1 FROM screening_requests WHERE base=? AND status IN (?,?) AND priority<=?',
                (base.upper(), *ACTIVE, prio)).fetchone()
            if row:
                return False
            cur = con.execute(
                '''INSERT OR IGNORE INTO screening_requests
                   (request_id, base, pair, priority, status, requested_by, enqueued_at)
                   VALUES (?,?,?,?,?,?,?)''',
                (request_id, base.upper(), pair.upper(), prio, 'QUEUED',
                 self._actor(requested_by), self._now()))
            return cur.rowcount == 1

    def next_request(self, *, min_priority: int | None = None,
                     max_priority: int | None = None,
                     max_per_base: int | None = None,
                     max_per_requested_by: int | None = None):
        """Return the first due request that remains within durable daily caps."""
        query = ("SELECT pending.* FROM screening_requests AS pending "
                 "WHERE pending.status='QUEUED' "
                 "AND (pending.next_attempt_at IS NULL OR pending.next_attempt_at<=?)")
        params: list = [self._now()]
        if min_priority is not None:
            query += ' AND pending.priority>=?'
            params.append(int(min_priority))
        if max_priority is not None:
            query += ' AND pending.priority<=?'
            params.append(int(max_priority))
        today = datetime.now(timezone.utc).date()
        day_start = today.isoformat()
        day_end = (today + timedelta(days=1)).isoformat()
        if max_per_base is not None:
            if isinstance(max_per_base, bool) or int(max_per_base) < 1:
                raise ValueError('max_per_base must be a positive integer')
            query += ''' AND (
                SELECT COUNT(*) FROM screening_attempts AS spent
                WHERE spent.started_at>=? AND spent.started_at<?
                  AND UPPER(spent.base)=UPPER(pending.base)
            ) < ?'''
            params.extend((day_start, day_end, int(max_per_base)))
        if max_per_requested_by is not None:
            if (isinstance(max_per_requested_by, bool)
                    or int(max_per_requested_by) < 1):
                raise ValueError('max_per_requested_by must be a positive integer')
            actor_sql = "COALESCE(NULLIF(LOWER(TRIM({}.requested_by)),''),'unknown')"
            query += f''' AND (
                SELECT COUNT(*) FROM screening_attempts AS spent
                WHERE spent.started_at>=? AND spent.started_at<?
                  AND {actor_sql.format('spent')}={actor_sql.format('pending')}
            ) < ?'''
            params.extend((day_start, day_end, int(max_per_requested_by)))
        query += ' ORDER BY pending.priority, pending.enqueued_at, pending.request_id LIMIT 1'
        with self._connect() as con:
            row = con.execute(query, params).fetchone()
        return dict(row) if row else None

    def mark_running(self, request_id: str, *, daily_ceiling: int | None = None,
                     max_per_base: int | None = None,
                     max_per_requested_by: int | None = None,
                     min_spacing_seconds: int | None = None) -> bool:
        """Atomically claim queued work and reserve one durable cost event."""
        now = datetime.now(timezone.utc)
        day_start = now.date().isoformat()
        day_end = (now.date() + timedelta(days=1)).isoformat()
        with self._connect() as con:
            con.execute('BEGIN IMMEDIATE')
            row = con.execute(
                "SELECT * FROM screening_requests WHERE request_id=? AND status='QUEUED'",
                (request_id,)).fetchone()
            if not row:
                return False
            limits = (
                ('daily_ceiling', daily_ceiling),
                ('max_per_base', max_per_base),
                ('max_per_requested_by', max_per_requested_by),
                ('min_spacing_seconds', min_spacing_seconds),
            )
            for name, value in limits:
                if value is not None and (
                        isinstance(value, bool) or not isinstance(value, int) or value < 1):
                    raise ValueError(f'{name} must be a positive integer')
            if daily_ceiling is not None:
                used = con.execute(
                    'SELECT COUNT(*) AS n FROM screening_attempts '
                    'WHERE started_at>=? AND started_at<?',
                    (day_start, day_end)).fetchone()
                if int(used['n'] if used else 0) >= daily_ceiling:
                    return False
            if max_per_base is not None:
                used = con.execute(
                    'SELECT COUNT(*) AS n FROM screening_attempts '
                    'WHERE started_at>=? AND started_at<? AND UPPER(base)=?',
                    (day_start, day_end, str(row['base']).upper())).fetchone()
                if int(used['n'] if used else 0) >= max_per_base:
                    return False
            if max_per_requested_by is not None:
                actor_sql = (
                    "COALESCE(NULLIF(LOWER(TRIM(requested_by)),''),'unknown')")
                used = con.execute(
                    'SELECT COUNT(*) AS n FROM screening_attempts '
                    f'WHERE started_at>=? AND started_at<? AND {actor_sql}=?',
                    (day_start, day_end, self._actor(row['requested_by']))).fetchone()
                if int(used['n'] if used else 0) >= max_per_requested_by:
                    return False
            if min_spacing_seconds is not None:
                latest = con.execute(
                    'SELECT MAX(started_at) AS value FROM screening_attempts').fetchone()
                if latest and latest['value']:
                    try:
                        previous = datetime.fromisoformat(
                            str(latest['value']).replace('Z', '+00:00'))
                        if previous.tzinfo is None:
                            previous = previous.replace(tzinfo=timezone.utc)
                    except (TypeError, ValueError, OverflowError):
                        return False
                    if (now - previous).total_seconds() < min_spacing_seconds:
                        return False
            stamp = now.isoformat()
            cur = con.execute(
                "UPDATE screening_requests SET status='RUNNING', started_at=?, "
                "attempt_count=attempt_count+1, next_attempt_at=NULL "
                "WHERE request_id=? AND status='QUEUED'",
                (stamp, request_id))
            if cur.rowcount != 1:
                return False
            con.execute(
                'INSERT INTO screening_attempts '
                '(request_id, base, priority, requested_by, started_at) '
                'VALUES (?,?,?,?,?)',
                (request_id, str(row['base']).upper(), int(row['priority']),
                 self._actor(row['requested_by']), stamp))
            return True

    def mark_done(self, request_id: str, final_code: str, report_file: str = ''):
        with self._connect() as con:
            con.execute(
                "UPDATE screening_requests SET status='DONE', finished_at=?, final_code=?, "
                "report_file=?, next_attempt_at=NULL WHERE request_id=?",
                (self._now(), final_code, report_file, request_id))

    def mark_failed(self, request_id: str, error: str, *,
                    retry_base_seconds: int | None = None,
                    retry_max_seconds: int | None = None,
                    max_attempts: int = 1) -> bool:
        """Fail a request or durably schedule an idle retry.

        Returns True when the same request was re-queued behind a bounded
        exponential backoff; False when it reached terminal FAILED.
        """
        with self._connect() as con:
            row = con.execute(
                'SELECT priority, attempt_count FROM screening_requests WHERE request_id=?',
                (request_id,)).fetchone()
            attempts = int(row['attempt_count']) if row else max_attempts
            if (row and int(row['priority']) == PRIORITIES['idle'] and
                    retry_base_seconds is not None and attempts < max(1, int(max_attempts))):
                base = max(1, int(retry_base_seconds))
                maximum = max(base, int(retry_max_seconds or base))
                delay = min(maximum, base * (2 ** max(0, attempts - 1)))
                next_at = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
                con.execute(
                    "UPDATE screening_requests SET status='QUEUED', finished_at=?, "
                    "started_at=NULL, next_attempt_at=?, error=? WHERE request_id=?",
                    (self._now(), next_at, str(error)[:500], request_id))
                return True
            con.execute(
                "UPDATE screening_requests SET status='FAILED', finished_at=?, error=?, "
                "next_attempt_at=NULL WHERE request_id=?",
                (self._now(), str(error)[:500], request_id))
            return False

    def has_active_for_base(self, base: str) -> bool:
        with self._connect() as con:
            row = con.execute(
                'SELECT 1 FROM screening_requests WHERE base=? AND status IN (?,?)',
                (base.upper(), *ACTIVE)).fetchone()
        return bool(row)

    def counts(self) -> dict:
        with self._connect() as con:
            rows = con.execute(
                'SELECT status, COUNT(*) AS n FROM screening_requests GROUP BY status').fetchall()
        return {str(row['status']): int(row['n']) for row in rows}

    def completed_today(self, *, base: str | None = None,
                        requested_by: str | None = None) -> int:
        today = datetime.now(timezone.utc).date()
        query = ("SELECT COUNT(*) AS n FROM screening_requests "
                 "WHERE finished_at>=? AND finished_at<? "
                 "AND status IN ('DONE','FAILED')")
        params: list = [today.isoformat(), (today + timedelta(days=1)).isoformat()]
        if base is not None:
            query += ' AND UPPER(base)=?'
            params.append(str(base).strip().upper())
        if requested_by is not None:
            query += (" AND COALESCE(NULLIF(LOWER(TRIM(requested_by)),''),'unknown')=?")
            params.append(self._actor(requested_by))
        with self._connect() as con:
            row = con.execute(query, params).fetchone()
        return int(row['n'] if row else 0)

    def cost_today(self, *, base: str | None = None,
                   requested_by: str | None = None) -> int:
        """Count every durable screening attempt, including retries/crashes."""
        today = datetime.now(timezone.utc).date()
        query = ('SELECT COUNT(*) AS n FROM screening_attempts '
                 'WHERE started_at>=? AND started_at<?')
        params: list = [today.isoformat(), (today + timedelta(days=1)).isoformat()]
        if base is not None:
            query += ' AND UPPER(base)=?'
            params.append(str(base).strip().upper())
        if requested_by is not None:
            query += (" AND COALESCE(NULLIF(LOWER(TRIM(requested_by)),''),'unknown')=?")
            params.append(self._actor(requested_by))
        with self._connect() as con:
            row = con.execute(query, params).fetchone()
        return int(row['n'] if row else 0)

    def last_activity_at(self) -> float:
        """Return the latest durable start/finish timestamp as an epoch value."""
        with self._connect() as con:
            row = con.execute(
                'SELECT MAX(started_at) AS started, MAX(finished_at) AS finished '
                'FROM screening_requests').fetchone()
        values = []
        for raw in (row['started'], row['finished']):
            if raw:
                try:
                    parsed = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=timezone.utc)
                    values.append(parsed.timestamp())
                except (TypeError, ValueError, OverflowError):
                    continue
        return max(values, default=0.0)

    def last(self, status: str):
        with self._connect() as con:
            row = con.execute(
                'SELECT request_id, base, finished_at, final_code, error FROM screening_requests '
                'WHERE status=? ORDER BY finished_at DESC LIMIT 1', (status,)).fetchone()
        return dict(row) if row else None

    def recent(self, limit: int = 10) -> list[dict]:
        with self._connect() as con:
            rows = con.execute(
                'SELECT request_id, base, status, priority, enqueued_at, finished_at, final_code, '
                'attempt_count, next_attempt_at '
                'FROM screening_requests ORDER BY enqueued_at DESC LIMIT ?', (int(limit),)).fetchall()
        return [dict(row) for row in rows]
