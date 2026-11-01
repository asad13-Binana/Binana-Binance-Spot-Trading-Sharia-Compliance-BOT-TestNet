from __future__ import annotations
import hashlib, json, sqlite3, threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from services.common.atomic import atomic_write_json
from services.common.models import LifecycleState, ProtectionMode

SCHEMA_VERSION = 1
SCHEMA = '''
CREATE TABLE IF NOT EXISTS trade_records (
  trade_id TEXT PRIMARY KEY,
  pair TEXT NOT NULL,
  lifecycle_state TEXT NOT NULL,
  entry_client_order_id TEXT,
  entry_order_id INTEGER,
  order_list_id INTEGER,
  take_profit_order_id INTEGER,
  stop_order_id INTEGER,
  filled_quantity TEXT,
  protected_quantity TEXT,
  average_entry_price TEXT,
  protection_mode TEXT,
  trailing_delta INTEGER,
  commission_asset TEXT,
  last_exchange_event_id TEXT,
  last_event_time TEXT,
  reconciliation_status TEXT,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS processed_signals (
  signal_id TEXT PRIMARY KEY,
  pair TEXT NOT NULL,
  candle_time TEXT,
  result TEXT NOT NULL,
  processed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS processed_commands (
  command_id TEXT PRIMARY KEY,
  command TEXT NOT NULL,
  result TEXT NOT NULL,
  processed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS exchange_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_key TEXT UNIQUE,
  event_type TEXT NOT NULL,
  symbol TEXT,
  order_id INTEGER,
  order_list_id INTEGER,
  commission_asset TEXT,
  commission_amount TEXT,
  event_time TEXT,
  payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS safety_halts (
  safety_key TEXT PRIMARY KEY,
  payload_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recovery_intents (
  safety_key TEXT PRIMARY KEY,
  payload_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS risk_guard_state (
  singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
  state_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS state_migrations (
  migration_name TEXT PRIMARY KEY,
  source_path TEXT,
  source_sha256 TEXT,
  migrated_at TEXT NOT NULL
);
'''

class StateStore:
    def __init__(self, path: str | Path, db_path: str | Path | None = None):
        self.path = Path(path)
        self.db_path = Path(db_path or self.path.with_suffix('.sqlite'))
        self._database_was_existing = self.db_path.exists()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self._durability_fault = ''
        legacy_data, legacy_status, legacy_sha256 = self._read_legacy_json(self.path)
        self.data = legacy_data if isinstance(legacy_data, dict) else {}
        self.data.setdefault('protection_mode', ProtectionMode.OCO_TRAILING.value)
        self.data.setdefault('entries_enabled', False)
        self.data.setdefault('simulation', True)
        self.data.setdefault('pause_reason', 'startup-safe-default')
        self.data.setdefault('last_reconciliation_status', 'NOT_RUN')
        self.data.setdefault('safety_halts', {})
        self.data.setdefault('recovery_intents', {})
        with self._connect() as con:
            # H-006: refuse to run on a corrupted database. Idempotency and
            # restart-recovery guarantees are meaningless if the authoritative
            # store is silently damaged; the operator must restore a verified
            # backup instead.
            self._verify_integrity(con)
            con.execute('PRAGMA journal_mode=WAL')
            con.execute('PRAGMA synchronous=FULL')
            self._migrate_schema(con)
        self._load_or_migrate_safety_state(
            legacy_status=legacy_status,
            legacy_sha256=legacy_sha256,
        )
        # Do not destroy a corrupt legacy file. SQLite is authoritative for
        # safety state, while entries retain their startup-safe default.
        if legacy_status != 'corrupt':
            self.save()

    @staticmethod
    def _read_legacy_json(path: Path) -> tuple[dict | None, str, str | None]:
        if not path.exists():
            return None, 'missing', None
        try:
            raw = path.read_bytes()
            value = json.loads(raw.decode('utf-8'))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None, 'corrupt', None
        if not isinstance(value, dict):
            return None, 'corrupt', hashlib.sha256(raw).hexdigest()
        return value, 'valid', hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _canonical_json(value: Any) -> str:
        return json.dumps(value, sort_keys=True, separators=(',', ':'), default=str)

    @staticmethod
    def _validate_mapping(value: Any, label: str) -> dict:
        if not isinstance(value, dict):
            raise RuntimeError(f'{label} is not a JSON object; fail closed')
        return json.loads(json.dumps(value, default=str))

    def _migrate_schema(self, con) -> None:
        version = int(con.execute('PRAGMA user_version').fetchone()[0])
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f'unsupported future SQLite schema version {version}; '
                f'maximum supported is {SCHEMA_VERSION}; fail closed')
        while version < SCHEMA_VERSION:
            if version != 0:
                raise RuntimeError(f'no deterministic migration from SQLite schema {version}')
            try:
                con.execute('BEGIN IMMEDIATE')
                for statement in (item.strip() for item in SCHEMA.split(';')):
                    if statement:
                        con.execute(statement)
                con.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
                con.commit()
            except Exception:
                con.rollback()
                raise
            version = SCHEMA_VERSION

    def _migration(self, con, name: str):
        return con.execute(
            'SELECT * FROM state_migrations WHERE migration_name=?', (name,)
        ).fetchone()

    def _replace_mapping_table(self, con, table: str, values: dict) -> None:
        if table not in {'safety_halts', 'recovery_intents'}:
            raise ValueError('invalid safety table')
        con.execute(f'DELETE FROM {table}')  # nosec B608 - fixed allowlist above
        now = self._now()
        con.executemany(
            f'INSERT INTO {table}(safety_key,payload_json,updated_at) VALUES(?,?,?)',  # nosec B608
            [(str(key), self._canonical_json(payload), now)
             for key, payload in sorted(values.items())],
        )

    def _read_mapping_table(self, con, table: str) -> dict:
        if table not in {'safety_halts', 'recovery_intents'}:
            raise ValueError('invalid safety table')
        result = {}
        for row in con.execute(
                f'SELECT safety_key,payload_json FROM {table} ORDER BY safety_key'):  # nosec B608
            try:
                payload = json.loads(row['payload_json'])
            except (TypeError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    f'authoritative SQLite {table} payload is corrupt; fail closed') from exc
            result[str(row['safety_key'])] = self._validate_mapping(
                payload, f'authoritative SQLite {table} payload')
        return result

    def _load_or_migrate_safety_state(
            self, *, legacy_status: str, legacy_sha256: str | None) -> None:
        name = 'legacy-sidecar-safety-v1'
        with self._connect() as con:
            migrated = self._migration(con, name)
            if migrated:
                halts = self._read_mapping_table(con, 'safety_halts')
                intents = self._read_mapping_table(con, 'recovery_intents')
            else:
                if legacy_status == 'corrupt':
                    raise RuntimeError(
                        'legacy sidecar safety state is unreadable/corrupt and '
                        'authoritative SQLite migration cannot be proven; fail closed')
                if legacy_status == 'missing' and self._database_was_existing:
                    raise RuntimeError(
                        'legacy sidecar safety state is missing for an existing '
                        'SQLite database and migration cannot be proven; fail closed')
                halts = self._validate_mapping(
                    self.data.get('safety_halts', {}), 'legacy safety_halts')
                intents = self._validate_mapping(
                    self.data.get('recovery_intents', {}), 'legacy recovery_intents')
                self._replace_mapping_table(con, 'safety_halts', halts)
                self._replace_mapping_table(con, 'recovery_intents', intents)
                con.execute(
                    '''INSERT INTO state_migrations(
                       migration_name,source_path,source_sha256,migrated_at)
                       VALUES(?,?,?,?)''',
                    (name, str(self.path), legacy_sha256, self._now()),
                )
        self.data['safety_halts'] = halts
        self.data['recovery_intents'] = intents
        if legacy_status == 'corrupt':
            self.data['entries_enabled'] = False
            self.data['pause_reason'] = 'legacy-sidecar-corrupt-authoritative-safety-restored'

    def _mark_durability_fault(self, exc: BaseException) -> None:
        self._durability_fault = type(exc).__name__ + ': ' + str(exc)
        self.data['entries_enabled'] = False
        self.data['pause_reason'] = 'safety-state-durability-failure'

    def _write_safety_state(self, halts: dict, intents: dict) -> None:
        try:
            with self._connect() as con:
                con.execute('BEGIN IMMEDIATE')
                # Exchange proofs have dedicated compare-and-set writers.
                # Generic JSON saves must neither restore nor erase their state.
                coverage = {row['safety_key']:json.loads(row['payload_json']) for row in con.execute(
                    "SELECT * FROM recovery_intents WHERE safety_key LIKE 'coverage:%' OR safety_key LIKE 'entry-terminal:%'")}
                for key in list(intents):
                    if key.startswith(('coverage:', 'entry-terminal:')):
                        del intents[key]
                intents.update(coverage)
                self._replace_mapping_table(con, 'safety_halts', halts)
                self._replace_mapping_table(con, 'recovery_intents', intents)
        except Exception as exc:
            self._mark_durability_fault(exc)
            raise RuntimeError(
                'authoritative SQLite safety-state write failed; entries remain fail closed'
            ) from exc

    @staticmethod
    def _verify_integrity(con) -> None:
        try:
            rows = con.execute('PRAGMA quick_check').fetchall()
        except sqlite3.DatabaseError as exc:
            # A truncated/garbage file raises "file is not a database" here.
            raise RuntimeError(
                'SQLite state database is unreadable/corrupt — fail closed; '
                'restore a verified backup before restarting. Details: ' + str(exc)) from exc
        if not rows or str(rows[0][0]).strip().lower() != 'ok':
            details = '; '.join(str(row[0]) for row in rows[:5]) if rows else 'no result'
            raise RuntimeError(
                'SQLite state database failed integrity quick_check — fail closed; '
                'restore a verified backup before restarting. Details: ' + details)

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

    def save(self):
        with self.lock:
            self._write_safety_state(
                self._validate_mapping(self.data.get('safety_halts', {}), 'safety_halts'),
                self._validate_mapping(self.data.get('recovery_intents', {}), 'recovery_intents'),
            )
            atomic_write_json(self.path, self.data)

    def get_mode(self):
        return self.data.get('protection_mode', ProtectionMode.OCO_TRAILING.value)

    def set_mode(self, mode):
        self.data['protection_mode'] = ProtectionMode(mode).value
        self.save()

    def entries(self):
        return not self._durability_fault and bool(self.data.get('entries_enabled', False))

    def set_entries(self, value: bool, reason: str = ''):
        with self.lock:
            if value and self._durability_fault:
                raise RuntimeError(
                    'entries remain fail-closed after a safety-state durability failure')
            if value and self.data.get('safety_halts'):
                reasons = ', '.join(sorted(self.data['safety_halts']))
                raise RuntimeError(
                    'entries remain fail-closed while safety reconciliation is pending: ' + reasons)
            self.data['entries_enabled'] = bool(value)
            self.data['pause_reason'] = '' if value else (reason or 'operator-paused')
            self.save()

    def latch_safety(self, key: str, reason: str, *, symbol: str = '',
                     kind: str = 'reconciliation', details=None) -> dict:
        """Atomically persist an entry-blocking incident and recovery intent."""
        key = str(key or '').strip()
        if not key or len(key) > 200:
            raise ValueError('invalid safety-halt key')
        now = self._now()
        incident = {
            'reason': str(reason), 'symbol': str(symbol or '').upper(),
            'kind': str(kind), 'details': details or {}, 'latched_at': now,
        }
        with self.lock:
            halts = self._validate_mapping(
                self.data.setdefault('safety_halts', {}), 'safety_halts')
            intents = self._validate_mapping(
                self.data.setdefault('recovery_intents', {}), 'recovery_intents')
            prior_halt = halts.get(key, {})
            prior_intent = intents.get(key, {})
            if prior_halt.get('latched_at'):
                incident['latched_at'] = prior_halt['latched_at']
            halts[key] = incident
            # Re-latching the same incident must not erase a deterministic
            # client ID, accepted order ID, or recovery stage already written.
            intents[key] = dict(prior_intent, **incident,
                                status=prior_intent.get('status', 'PENDING'))
            self._write_safety_state(halts, intents)
            self.data['safety_halts'] = halts
            self.data['recovery_intents'] = intents
            self.data['entries_enabled'] = False
            self.data['pause_reason'] = 'safety-reconciliation-required:' + key
            atomic_write_json(self.path, self.data)
        return incident

    def update_recovery_intent(self, key: str, **fields) -> None:
        with self.lock:
            halts = self._validate_mapping(
                self.data.setdefault('safety_halts', {}), 'safety_halts')
            intents = self._validate_mapping(
                self.data.setdefault('recovery_intents', {}), 'recovery_intents')
            if key not in intents:
                raise KeyError(f'unknown recovery intent {key}')
            intents[key].update({k: v for k, v in fields.items() if k not in {'key'}})
            intents[key]['updated_at'] = self._now()
            self._write_safety_state(halts, intents)
            self.data['safety_halts'] = halts
            self.data['recovery_intents'] = intents
            atomic_write_json(self.path, self.data)

    def save_protection_proof(self, key: str, proof: dict, *, symbol: str, event_cursor: int) -> None:
        """Persist coverage before any caller may label protection verified."""
        with self.lock:
            proof = self._validate_mapping(proof,'protection coverage proof')
            with self._connect() as connection:
                connection.execute('BEGIN IMMEDIATE')
                latest = connection.execute('SELECT COALESCE(MAX(id),0) FROM exchange_events WHERE symbol=?',
                                            (symbol,)).fetchone()[0]
                if latest != event_cursor:
                    raise RuntimeError('exchange events changed during protection verification')
                connection.execute('''INSERT INTO recovery_intents VALUES(?,?,?)
                    ON CONFLICT(safety_key) DO UPDATE SET payload_json=excluded.payload_json,updated_at=excluded.updated_at''',
                    (key,self._canonical_json(proof),self._now()))
            self.data.setdefault('recovery_intents',{})[key] = proof
            atomic_write_json(self.path,self.data)

    def resolve_safety(self, key: str, resolution: str) -> None:
        """Resolve one verified incident without ever re-enabling entries."""
        with self.lock:
            halts = self._validate_mapping(
                self.data.setdefault('safety_halts', {}), 'safety_halts')
            intents = self._validate_mapping(
                self.data.setdefault('recovery_intents', {}), 'recovery_intents')
            incident = intents.pop(key, None)
            halts.pop(key, None)
            self._write_safety_state(halts, intents)
            self.data['safety_halts'] = halts
            self.data['recovery_intents'] = intents
            self.data['last_safety_resolution'] = {
                'key': key, 'resolution': str(resolution),
                'incident': incident or {}, 'resolved_at': self._now(),
            }
            self.data['entries_enabled'] = False
            self.data['pause_reason'] = (
                'safety-reconciliation-required:' + ','.join(sorted(halts))
                if halts else 'owner-resume-required-after-safety-action')
            atomic_write_json(self.path, self.data)

    def safety_halts(self) -> dict:
        with self.lock:
            return json.loads(json.dumps(self.data.get('safety_halts', {}), default=str))

    def load_or_migrate_risk_guard_state(self, legacy_path: str | Path) -> dict:
        path = Path(legacy_path)
        legacy, status, source_sha256 = self._read_legacy_json(path)
        name = 'legacy-fresh-signal-guard-v1'
        with self.lock:
            with self._connect() as con:
                migrated = self._migration(con, name)
                row = con.execute(
                    'SELECT state_json FROM risk_guard_state WHERE singleton=1'
                ).fetchone()
                if migrated:
                    if not row:
                        raise RuntimeError(
                            'authoritative SQLite risk state is missing after migration; fail closed')
                    try:
                        return self._validate_risk_guard_state(json.loads(row['state_json']))
                    except (TypeError, json.JSONDecodeError) as exc:
                        raise RuntimeError(
                            'authoritative SQLite risk state is corrupt; fail closed') from exc
                if row:
                    raise RuntimeError(
                        'SQLite risk state exists without its migration record; fail closed')
                if status == 'corrupt':
                    raise RuntimeError(
                        'legacy risk state is unreadable/corrupt and authoritative '
                        'SQLite recovery cannot be proven; fail closed')
                if status == 'missing' and self._database_was_existing:
                    raise RuntimeError(
                        'legacy risk state is missing for an existing SQLite '
                        'database and migration cannot be proven; fail closed')
                state = self._validate_risk_guard_state(legacy or {})
                con.execute(
                    '''INSERT INTO risk_guard_state(singleton,state_json,updated_at)
                       VALUES(1,?,?)''',
                    (self._canonical_json(state), self._now()),
                )
                con.execute(
                    '''INSERT INTO state_migrations(
                       migration_name,source_path,source_sha256,migrated_at)
                       VALUES(?,?,?,?)''',
                    (name, str(path), source_sha256, self._now()),
                )
                return state

    @classmethod
    def _validate_risk_guard_state(cls, value: Any) -> dict:
        state = cls._validate_mapping(value, 'risk guard state')
        state.setdefault('pairs', {})
        state.setdefault('daily', {})
        state.setdefault('global_pause', '')
        if not isinstance(state['pairs'], dict) or not isinstance(state['daily'], dict):
            raise RuntimeError('risk guard pairs/daily state is invalid; fail closed')
        if not isinstance(state['global_pause'], str):
            raise RuntimeError('risk guard global_pause state is invalid; fail closed')
        return state

    def save_risk_guard_state(self, value: dict) -> None:
        state = self._validate_risk_guard_state(value)
        try:
            with self.lock:
                with self._connect() as con:
                    cur = con.execute(
                        '''UPDATE risk_guard_state
                           SET state_json=?,updated_at=? WHERE singleton=1''',
                        (self._canonical_json(state), self._now()),
                    )
                    if cur.rowcount != 1:
                        raise RuntimeError(
                            'authoritative SQLite risk state is not initialized')
        except Exception as exc:
            self._mark_durability_fault(exc)
            raise RuntimeError(
                'authoritative SQLite risk-state write failed; entries remain fail closed'
            ) from exc

    def signal_seen(self, signal_id: str) -> bool:
        return self.signal_result(signal_id) is not None

    def signal_result(self, signal_id: str) -> str | None:
        with self._connect() as con:
            row = con.execute('SELECT result FROM processed_signals WHERE signal_id=?', (signal_id,)).fetchone()
        return str(row['result']) if row else None

    def claim_signal(self, signal: dict[str, Any]) -> bool:
        """Atomically reserve a signal before any exchange submission."""
        with self._connect() as con:
            cur = con.execute(
                '''INSERT OR IGNORE INTO processed_signals(signal_id,pair,candle_time,result,processed_at)
                   VALUES(?,?,?,?,?)''',
                (signal['signal_id'], signal['pair'], signal.get('candle_time'), 'IN_PROGRESS', self._now()),
            )
            return cur.rowcount == 1

    def record_signal(self, signal: dict[str, Any], result: str):
        with self._connect() as con:
            con.execute(
                '''INSERT INTO processed_signals(signal_id,pair,candle_time,result,processed_at)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(signal_id) DO UPDATE SET result=excluded.result, processed_at=excluded.processed_at''',
                (signal['signal_id'], signal['pair'], signal.get('candle_time'), result, self._now()),
            )

    def command_seen(self, command_id: str) -> bool:
        return self.command_result(command_id) is not None

    def command_result(self, command_id: str) -> str | None:
        with self._connect() as con:
            row = con.execute('SELECT result FROM processed_commands WHERE command_id=?', (command_id,)).fetchone()
        return str(row['result']) if row else None

    def claim_command(self, command_id: str, command: str) -> bool:
        """Atomically reserve a command before any exchange-affecting action."""
        with self._connect() as con:
            cur = con.execute(
                'INSERT OR IGNORE INTO processed_commands(command_id,command,result,processed_at) VALUES(?,?,?,?)',
                (command_id, command, 'IN_PROGRESS', self._now()),
            )
            return cur.rowcount == 1

    def record_command(self, command_id: str, command: str, result: str):
        with self._connect() as con:
            con.execute(
                '''INSERT INTO processed_commands(command_id,command,result,processed_at) VALUES(?,?,?,?)
                   ON CONFLICT(command_id) DO UPDATE SET result=excluded.result, processed_at=excluded.processed_at''',
                (command_id, command, result, self._now()),
            )

    def average_entry_price_for_symbol(self, symbol: str):
        """Entry average of the active trade for one symbol (M-005 support)."""
        from decimal import Decimal
        pair = self._symbol_to_pair(symbol)
        trade_id = self._active_trade_id(pair)
        if not trade_id:
            return None
        with self._connect() as con:
            row = con.execute(
                'SELECT average_entry_price FROM trade_records WHERE trade_id=?', (trade_id,)
            ).fetchone()
        value = row['average_entry_price'] if row else None
        if value in (None, ''):
            return None
        try:
            return Decimal(str(value))
        except Exception:
            return None

    def backup(self, directory, retain: int = 14):
        """SQLite online backup with post-copy integrity verification (H-006)."""
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        dest = directory / f'execution_state.{stamp}.sqlite'
        with self._connect() as source:
            target = sqlite3.connect(dest)
            try:
                source.backup(target)
                rows = target.execute('PRAGMA quick_check').fetchall()
                if not rows or str(rows[0][0]).strip().lower() != 'ok':
                    raise RuntimeError('backup failed post-copy integrity quick_check')
                target.commit()
            finally:
                target.close()
        from services.common.retention import prune_files
        prune_files(directory, 'execution_state.*.sqlite', max_files=max(1, int(retain)))
        return dest

    def upsert_trade(self, trade_id: str, pair: str, **fields):
        allowed = {
            'lifecycle_state','entry_client_order_id','entry_order_id','order_list_id',
            'take_profit_order_id','stop_order_id','filled_quantity','protected_quantity',
            'average_entry_price','protection_mode','trailing_delta','commission_asset',
            'last_exchange_event_id','last_event_time','reconciliation_status'
        }
        explicit_lifecycle = 'lifecycle_state' in fields
        clean = {k: v for k, v in fields.items() if k in allowed}
        # A new row gets the default lifecycle, but updating OTHER fields on an
        # existing row must never silently regress its lifecycle back to
        # SIGNAL_APPROVED. lifecycle_state is therefore only written on UPDATE
        # when the caller passed it explicitly.
        clean.setdefault('lifecycle_state', LifecycleState.SIGNAL_APPROVED.value)
        clean['updated_at'] = self._now()
        with self._connect() as con:
            con.execute('BEGIN IMMEDIATE')
            active = {LifecycleState.PROTECTION_ACTIVE.value,LifecycleState.TRAILING_ACTIVE.value,
                      LifecycleState.BREAK_EVEN_ARMED.value,LifecycleState.PROFIT_LOCKED.value}
            if explicit_lifecycle and clean['lifecycle_state'] in active:
                from types import SimpleNamespace
                current = con.execute('SELECT * FROM trade_records WHERE trade_id=?',(trade_id,)).fetchone()
                current = dict(current) if current else {}
                position = SimpleNamespace(entry_order_id=clean.get('entry_order_id',current.get('entry_order_id')),
                    tp_order_id=clean.get('take_profit_order_id',current.get('take_profit_order_id')),
                    sl_order_id=clean.get('stop_order_id',current.get('stop_order_id')),exit_order_id=None,
                    filled_qty=clean.get('filled_quantity',current.get('filled_quantity')))
                if not self._coverage_is_current(pair.replace('/',''),position,connection=con):
                    clean['lifecycle_state'] = LifecycleState.RECONCILIATION_REQUIRED.value
                    clean['reconciliation_status'] = 'PROTECTION_COVERAGE_PENDING_RECONCILE_REQUIRED'
                elif 'COVERAGE_PENDING' in str(clean.get('reconciliation_status') or ''):
                    clean['reconciliation_status'] = 'PROTECTION_COVERAGE_VERIFIED'
            columns = ['trade_id','pair'] + list(clean)
            values = [trade_id,pair] + [clean[c] for c in clean]
            placeholders = ','.join('?' for _ in columns)
            update_keys = [c for c in clean if c != 'lifecycle_state' or explicit_lifecycle]
            updates = ','.join(f'{c}=excluded.{c}' for c in update_keys)
            # Fixed allowlist above supplies every identifier; values are bound.
            sql = f"INSERT INTO trade_records({','.join(columns)}) VALUES({placeholders}) ON CONFLICT(trade_id) DO UPDATE SET {updates}"  # nosec B608
            con.execute(sql, values)

    def finalize_submission(self, trade_id: str, outcome: str):
        """Record the transport result without overwriting an earlier stream fill.

        The conditional UPDATE is atomic with concurrent SQLite writers. UNKNOWN
        never means rejected/flat, even when an exchange event arrived first.
        """
        states = {'ACCEPTED': ('ENTRY_SUBMITTED', 'SUBMITTED'),
                  'REJECTED': ('ERROR', 'ENTRY_REJECTED'),
                  'UNKNOWN': ('RECONCILIATION_REQUIRED', 'SUBMISSION_UNKNOWN')}
        lifecycle, status = states[outcome]
        try:
            with self._connect() as con:
                con.execute(
                    '''UPDATE trade_records SET lifecycle_state=?,
                       reconciliation_status=?,updated_at=?
                       WHERE trade_id=? AND lifecycle_state='SIGNAL_APPROVED' ''',
                    (lifecycle, status, self._now(), trade_id))
                if outcome == 'UNKNOWN':
                    con.execute(
                        '''UPDATE trade_records SET reconciliation_status=?,updated_at=?
                           WHERE trade_id=?''', (status, self._now(), trade_id))
        except Exception as exc:
            self._mark_durability_fault(exc)
            raise

    @staticmethod
    def _symbol_to_pair(symbol: str) -> str:
        symbol = str(symbol or '').upper()
        return symbol[:-4] + '/USDT' if symbol.endswith('USDT') else symbol

    @staticmethod
    def _state_rank(value: str) -> int:
        order = {
            LifecycleState.SIGNAL_APPROVED.value: 0,
            LifecycleState.ENTRY_SUBMITTED.value: 1,
            LifecycleState.ENTRY_PARTIALLY_FILLED.value: 2,
            LifecycleState.ENTRY_FILLED.value: 3,
            LifecycleState.REPROTECT_REQUIRED.value: 4,
            LifecycleState.RECONCILIATION_REQUIRED.value: 4,
            LifecycleState.RECONCILED.value: 5,
            LifecycleState.PROTECTION_ACTIVE.value: 6,
            LifecycleState.BREAK_EVEN_ARMED.value: 7,
            LifecycleState.PROFIT_LOCKED.value: 8,
            LifecycleState.TRAILING_ACTIVE.value: 9,
            LifecycleState.EXIT_FILLED.value: 10,
            LifecycleState.ERROR.value: 11,
        }
        return order.get(str(value), -1)

    def finalize_zero_fill_entry(self, saved: dict, symbol: str, event_cursor: int, evidence: dict) -> None:
        """Atomically terminalize the exact identity whose REST facts were read."""
        from services.execution_sidecar.protection_proof import number, zero_fill_event
        trade_id, entry_id = saved['trade_id'], saved['entry_order_id']
        key = f'entry-terminal:{symbol}:{entry_id}'
        binding_fields = ('pair', 'entry_order_id', 'entry_client_order_id', 'order_list_id',
                          'take_profit_order_id', 'stop_order_id', 'lifecycle_state')
        with self.lock, self._connect() as con:
            con.execute('BEGIN IMMEDIATE')
            cursor = con.execute('SELECT COALESCE(MAX(id),0) FROM exchange_events WHERE symbol=?',
                                 (symbol,)).fetchone()[0]
            row = con.execute('SELECT * FROM trade_records WHERE trade_id=?', (trade_id,)).fetchone()
            if (cursor != event_cursor or not row or row['pair'] != self._symbol_to_pair(symbol)
                    or any(row[field] != saved[field] for field in binding_fields)
                    or number(row['filled_quantity'] or '0') != 0):
                raise RuntimeError('entry identity or events changed during zero-fill verification')
            known_ids = tuple(int(oid) for oid in evidence['orders'])
            placeholders = ','.join('?' for _ in known_ids)
            events = con.execute(
                f"SELECT payload_json FROM exchange_events WHERE symbol=? AND event_type='executionReport' AND order_id IN ({placeholders})",  # nosec B608
                (symbol, *known_ids)).fetchall()
            if any(not zero_fill_event(json.loads(event[0])) for event in events):
                raise RuntimeError('durable execution evidence contradicts verified zero fill')
            proof = {**evidence, 'event_cursor': event_cursor,
                     'durable_identity': {field: saved[field] for field in binding_fields}}
            con.execute("UPDATE trade_records SET lifecycle_state='ERROR',reconciliation_status='ENTRY_TERMINAL_NO_FILL_VERIFIED',updated_at=? WHERE trade_id=?",
                        (self._now(), trade_id))
            con.execute("INSERT INTO recovery_intents VALUES(?,?,?) ON CONFLICT(safety_key) DO UPDATE SET payload_json=excluded.payload_json,updated_at=excluded.updated_at",
                        (key, self._canonical_json(proof), self._now()))


    def _active_trade_id(self, pair: str) -> str | None:
        terminal = (LifecycleState.EXIT_FILLED.value, LifecycleState.ERROR.value)
        with self._connect() as con:
            row = con.execute(
                '''SELECT trade_id FROM trade_records
                   WHERE pair=? AND lifecycle_state NOT IN (?,?)
                   ORDER BY updated_at DESC LIMIT 1''',
                (pair, *terminal),
            ).fetchone()
        return str(row['trade_id']) if row else None

    def record_exchange_event(self, event: dict[str, Any]) -> bool:
        """Persist and apply one Binance user-stream event exactly once.

        Raw events remain the audit source of truth. When the event can be matched to
        a known entry/order-list/protection leg, the owning trade row is also advanced
        without allowing a delayed event to regress an already later lifecycle state.
        """
        event_type = str(event.get('e') or event.get('eventType') or 'unknown')
        order_id = event.get('i') if event.get('i') is not None else event.get('orderId')
        order_list_id = event.get('g') if event.get('g') is not None else event.get('orderListId')
        event_time = event.get('E') if event.get('E') is not None else (event.get('eventTime') or self._now())
        raw_event_id = event.get('I') if event.get('I') is not None else event.get('u')
        symbol = event.get('s') or event.get('symbol')
        # M-007 fix: the raw execution id is never trusted as globally unique
        # on its own. The dedup key is ALWAYS the composite of event type,
        # symbol, order identifiers, timestamps and status fields, with the
        # raw id included as one component.
        event_key = ':'.join(map(str, (
            event_type, symbol, order_id, order_list_id, raw_event_id, event_time,
            event.get('l') or event.get('listStatusType'),
            event.get('L') or event.get('listOrderStatus'),
            event.get('C') or event.get('listClientOrderId'),
            event.get('r') or event.get('rejectReason'),
            event.get('x') or event.get('executionType'),
            event.get('X') or event.get('orderStatus'),
            event.get('z') or event.get('executedQty'),
            event.get('expiryReason'),
        )))
        commission_asset = event.get('N') or event.get('commissionAsset')
        commission_amount = event.get('n') or event.get('commission')

        with self.lock, self._connect() as con:
            try:
                con.execute(
                    '''INSERT INTO exchange_events(event_key,event_type,symbol,order_id,order_list_id,
                       commission_asset,commission_amount,event_time,payload_json)
                       VALUES(?,?,?,?,?,?,?,?,?)''',
                    (event_key, event_type, symbol, order_id, order_list_id,
                     commission_asset, commission_amount, str(event_time),
                     json.dumps(event, sort_keys=True, default=str)),
                )
            except sqlite3.IntegrityError:
                return False

            clauses, values = [], []
            if order_id not in (None, -1, '-1'):
                clauses.append('(entry_order_id=? OR take_profit_order_id=? OR stop_order_id=?)')
                values.extend([order_id, order_id, order_id])
            if order_list_id not in (None, -1, '-1'):
                clauses.append('order_list_id=?')
                values.append(order_list_id)
            if not clauses:
                return True
            if not isinstance(symbol,str) or not symbol:
                return True

            # The dedup key stored in the UNIQUE exchange_events.event_key is the
            # composite above (M-007). The human-facing last_exchange_event_id on
            # the trade row stays the readable raw execution id when present.
            readable_event_id = str(raw_event_id) if raw_event_id is not None else event_key
            rows = con.execute(
                # Clauses are fixed literals selected above; all values are bound.
                'SELECT * FROM trade_records WHERE pair=? AND (' + ' OR '.join(clauses) + ')',  # nosec B608
                [self._symbol_to_pair(symbol),*values]
            ).fetchall()
            for row in rows:
                proof_key = f"coverage:{symbol}:{row['entry_order_id']}"
                previous = con.execute('SELECT payload_json FROM recovery_intents WHERE safety_key=?',
                                       (proof_key,)).fetchone()
                if previous and event_type in {'executionReport','listStatus'}:
                    invalid = json.loads(previous[0])
                    invalid.update(invalidated=True,invalidated_by_event=event_key)
                    con.execute('UPDATE recovery_intents SET payload_json=?,updated_at=? WHERE safety_key=?',
                                (self._canonical_json(invalid),self._now(),proof_key))
                    self.data.setdefault('recovery_intents',{})[proof_key] = invalid
                updates: dict[str, Any] = {
                    'last_exchange_event_id': readable_event_id,
                    'last_event_time': str(event_time),
                    'reconciliation_status': 'USER_STREAM_EVENT_APPLIED',
                    'updated_at': self._now(),
                }
                if commission_asset:
                    updates['commission_asset'] = str(commission_asset)

                lifecycle = None
                if event_type == 'executionReport':
                    side = str(event.get('S') or event.get('side') or '').upper()
                    status = str(event.get('X') or event.get('orderStatus') or event.get('status') or '').upper()
                    order_type = str(event.get('o') or event.get('orderType') or event.get('type') or '').upper()
                    cumulative = event.get('z') if event.get('z') is not None else event.get('executedQty')
                    quote = event.get('Z') if event.get('Z') is not None else event.get('cummulativeQuoteQty')
                    original_qty = event.get('q') if event.get('q') is not None else event.get('origQty')
                    expiry_reason = event.get('expiryReason')
                    unsafe_expiry = bool(expiry_reason) or status == 'EXPIRED_IN_MATCH'
                    if side == 'BUY' and cumulative not in (None, ''):
                        updates['filled_quantity'] = str(cumulative)
                    if side == 'BUY' and cumulative not in (None, '', '0', 0) and quote not in (None, ''):
                        try:
                            from decimal import Decimal
                            updates['average_entry_price'] = str(Decimal(str(quote)) / Decimal(str(cumulative)))
                        except Exception:
                            updates['reconciliation_status'] = 'MALFORMED_FILL_NUMERIC_RECONCILE_REQUIRED'
                    if side == 'SELL' and original_qty not in (None, '') and status in {'NEW', 'PARTIALLY_FILLED'}:
                        updates['protected_quantity'] = str(original_qty)
                    try:
                        from decimal import Decimal
                        has_fill = Decimal(str(cumulative or '0')) > 0
                    except Exception:
                        has_fill = False
                    if unsafe_expiry:
                        # Current official field names are journaled verbatim, but
                        # their business semantics are not guessed. Either signal
                        # is an unsafe terminal requiring REST/account reconciliation.
                        lifecycle = (LifecycleState.ENTRY_PARTIALLY_FILLED.value
                                     if side == 'BUY' and has_fill else None)
                        updates['reconciliation_status'] = (
                            'UNSAFE_EXPIRY_RECONCILE_REQUIRED:' +
                            (str(expiry_reason) if expiry_reason else status))
                    elif side == 'BUY' and status == 'PARTIALLY_FILLED':
                        lifecycle = LifecycleState.ENTRY_PARTIALLY_FILLED.value
                    elif side == 'BUY' and status == 'FILLED':
                        lifecycle = LifecycleState.ENTRY_FILLED.value
                    elif side == 'BUY' and status in {'CANCELED', 'EXPIRED', 'REJECTED'}:
                        if has_fill:
                            lifecycle = LifecycleState.ENTRY_PARTIALLY_FILLED.value
                            updates['reconciliation_status'] = 'ENTRY_TERMINAL_PARTIAL_FILL_RECONCILE_REQUIRED'
                        else:
                            lifecycle = LifecycleState.RECONCILIATION_REQUIRED.value
                            updates['reconciliation_status'] = 'ENTRY_TERMINAL_NO_FILL_RECONCILE_REQUIRED'
                    elif side == 'SELL' and status in {'NEW', 'PARTIALLY_FILLED'} and order_type in {
                        'STOP_LOSS', 'STOP_LOSS_LIMIT', 'TAKE_PROFIT', 'TAKE_PROFIT_LIMIT', 'LIMIT_MAKER'
                    }:
                        lifecycle = LifecycleState.RECONCILIATION_REQUIRED.value
                        updates['reconciliation_status'] = 'PROTECTION_COVERAGE_PENDING_RECONCILE_REQUIRED'
                    elif side == 'SELL' and status == 'FILLED':
                        lifecycle = LifecycleState.RECONCILIATION_REQUIRED.value
                        updates['reconciliation_status'] = 'EXIT_EXECUTION_RECONCILE_REQUIRED'
                    elif side == 'SELL' and status in {'CANCELED', 'EXPIRED', 'REJECTED'}:
                        # A failed/terminal protective sell does not mean the base
                        # position disappeared. Preserve the lifecycle and force
                        # exchange reconciliation/re-protection instead of making
                        # the trade look terminal to _active_trade_id().
                        updates['reconciliation_status'] = 'SELL_PROTECTION_TERMINAL_RECONCILE_REQUIRED'
                    elif status == 'REJECTED':
                        updates['reconciliation_status'] = 'UNMATCHED_REJECTION_RECONCILE_REQUIRED'
                elif event_type == 'listStatus':
                    list_status = str(event.get('l') or event.get('listStatusType') or '').upper()
                    order_status = str(event.get('L') or event.get('listOrderStatus') or '').upper()
                    expiry_reason = event.get('expiryReason')
                    if expiry_reason or order_status == 'EXPIRED_IN_MATCH':
                        updates['reconciliation_status'] = (
                            'UNSAFE_LIST_EXPIRY_RECONCILE_REQUIRED:' +
                            (str(expiry_reason) if expiry_reason else order_status))
                    elif list_status in {'EXEC_STARTED', 'UPDATED'} and order_status == 'EXECUTING':
                        lifecycle = LifecycleState.RECONCILIATION_REQUIRED.value
                        updates['reconciliation_status'] = 'PROTECTION_COVERAGE_PENDING_RECONCILE_REQUIRED'
                    elif list_status == 'ALL_DONE' or order_status == 'ALL_DONE':
                        # ALL_DONE explicitly means the list is no longer active. It
                        # does not say which leg filled; executionReport/reconciliation
                        # must determine whether the position exited or is unprotected.
                        updates['reconciliation_status'] = 'ORDER_LIST_TERMINAL_RECONCILE_REQUIRED'
                        lifecycle = LifecycleState.RECONCILIATION_REQUIRED.value
                    elif list_status == 'RESPONSE' or order_status == 'REJECT':
                        # Binance defines these as a failed placement/cancel action.
                        # The pre-existing order list may still be live, so never mark
                        # the entire trade ERROR from this event alone.
                        updates['reconciliation_status'] = 'ORDER_LIST_ACTION_REJECTED_RECONCILE_REQUIRED'

                old = str(row['lifecycle_state'])
                terminal_key = f"entry-terminal:{symbol}:{row['entry_order_id']}"
                terminal = con.execute('SELECT payload_json FROM recovery_intents WHERE safety_key=?',
                                       (terminal_key,)).fetchone()
                terminal_contradicted = False
                if terminal and event_type == 'executionReport':
                    from services.execution_sidecar.protection_proof import zero_fill_event
                    terminal_contradicted = not zero_fill_event(event)
                    if terminal_contradicted:
                        invalid = json.loads(terminal[0])
                        invalid.update(invalidated=True, invalidated_by_event=event_key)
                        con.execute('UPDATE recovery_intents SET payload_json=?,updated_at=? WHERE safety_key=?',
                                    (self._canonical_json(invalid), self._now(), terminal_key))
                        updates['lifecycle_state'] = LifecycleState.RECONCILIATION_REQUIRED.value
                        updates['reconciliation_status'] = 'TERMINAL_ENTRY_CONTRADICTION_RECONCILE_REQUIRED'
                if terminal_contradicted:
                    # A late fill must re-reserve exposure even after zero-fill
                    # terminal proof released the slot; terminal is not immutable.
                    pass
                elif (old not in {LifecycleState.EXIT_FILLED.value,LifecycleState.ERROR.value}
                        and event_type in {'executionReport','listStatus'}
                        and (lifecycle == LifecycleState.RECONCILIATION_REQUIRED.value
                             or self._state_rank(old) >= self._state_rank(LifecycleState.PROTECTION_ACTIVE.value))):
                    lifecycle = LifecycleState.RECONCILIATION_REQUIRED.value
                    updates['lifecycle_state'] = lifecycle
                elif lifecycle:
                    if old == LifecycleState.RECONCILED.value or self._state_rank(lifecycle) >= self._state_rank(old):
                        updates['lifecycle_state'] = lifecycle

                set_sql = ','.join(f'{key}=?' for key in updates)
                con.execute(
                    # Update keys are fixed internal literals; the trade ID is bound.
                    f'UPDATE trade_records SET {set_sql} WHERE trade_id=?',  # nosec B608
                    [*updates.values(), row['trade_id']],
                )
        return True

    def _coverage_is_current(self, symbol: str, position: Any, *, connection=None) -> bool:
        try:
            from decimal import Decimal
            key = f'coverage:{symbol}:{int(position.entry_order_id)}'
            if connection is None:
                with self._connect() as proof_connection:
                    row = proof_connection.execute('SELECT payload_json FROM recovery_intents WHERE safety_key=?',(key,)).fetchone()
            else:
                row = connection.execute('SELECT payload_json FROM recovery_intents WHERE safety_key=?',(key,)).fetchone()
            proof = json.loads(row[0]) if row else {}
            age = datetime.now(timezone.utc).timestamp()-float(proof['verified_at'])
            ids = {str(int(getattr(position,name))) for name in
                   ('tp_order_id','sl_order_id','exit_order_id') if getattr(position,name,None)}
            quantity = Decimal(proof['protected_quantity'])
            return (proof.get('invalidated') is not True and 0 <= age <= 35
                    and ids and ids.issubset(set(map(str,proof['orders'])))
                    and Decimal(str(position.filled_qty)) == Decimal(proof['entry_filled_quantity'])
                    and quantity.is_finite() and quantity > 0)
        except (ValueError,TypeError,KeyError,AttributeError,OverflowError,ArithmeticError):
            return False

    def mirror_legacy_position(self, symbol: str, position: Any):
        pair = self._symbol_to_pair(symbol)
        trade_id = self._active_trade_id(pair)
        if not trade_id:
            trade_id = str(getattr(position, 'entry_order_id', '') or getattr(position, 'order_list_id', '') or symbol)

        state_name = str(getattr(getattr(position, 'state', None), 'name', getattr(position, 'state', 'RECONCILED'))).upper()
        filled = getattr(position, 'filled_qty', None)
        lifecycle = LifecycleState.RECONCILED.value
        if state_name == 'PENDING_ENTRY':
            lifecycle = (LifecycleState.ENTRY_PARTIALLY_FILLED.value
                         if filled not in (None, '', 0, '0') else LifecycleState.ENTRY_SUBMITTED.value)
        elif state_name == 'CLOSED':
            lifecycle = LifecycleState.EXIT_FILLED.value
        elif 'TRAIL' in state_name:
            lifecycle = LifecycleState.TRAILING_ACTIVE.value
        elif getattr(position, 'sl_order_id', None) or getattr(position, 'exit_order_id', None):
            lifecycle = LifecycleState.PROTECTION_ACTIVE.value
        elif filled not in (None, '', 0, '0'):
            lifecycle = LifecycleState.ENTRY_FILLED.value

        requested_lifecycle = str(getattr(position, '_sidecar_lifecycle', '') or '')
        if requested_lifecycle in {item.value for item in LifecycleState}:
            lifecycle = requested_lifecycle
        active_states = {LifecycleState.PROTECTION_ACTIVE.value,LifecycleState.TRAILING_ACTIVE.value,
                         LifecycleState.BREAK_EVEN_ARMED.value,LifecycleState.PROFIT_LOCKED.value}
        coverage_pending = lifecycle in active_states and not self._coverage_is_current(symbol,position)
        if coverage_pending:
            lifecycle = LifecycleState.RECONCILIATION_REQUIRED.value

        with self._connect() as con:
            existing = con.execute(
                '''SELECT lifecycle_state,protection_mode,protected_quantity,reconciliation_status
                   FROM trade_records WHERE trade_id=?''',
                (trade_id,),
            ).fetchone()
        if existing and state_name == 'CLOSED' and 'RECONCILE_REQUIRED' in str(
                existing['reconciliation_status'] or ''):
            lifecycle = LifecycleState.RECONCILIATION_REQUIRED.value
        if (existing and not requested_lifecycle and not coverage_pending and lifecycle in active_states
                and str(existing['lifecycle_state']) in {
                    LifecycleState.BREAK_EVEN_ARMED.value, LifecycleState.PROFIT_LOCKED.value}):
            # Generic legacy ARMED/TRAIL state cannot overwrite a durable,
            # explicitly selected protection phase after restart/projection.
            lifecycle = str(existing['lifecycle_state'])
        lifecycle_override = coverage_pending or (requested_lifecycle in active_states and not coverage_pending) or requested_lifecycle in {
            LifecycleState.REPROTECT_REQUIRED.value,
            LifecycleState.RECONCILIATION_REQUIRED.value,
        } or (
            requested_lifecycle == LifecycleState.RECONCILED.value and
            state_name == 'CLOSED' and existing and
            str(existing['lifecycle_state']) not in {
                LifecycleState.EXIT_FILLED.value, LifecycleState.ERROR.value})
        if existing and not lifecycle_override and \
                self._state_rank(str(existing['lifecycle_state'])) > self._state_rank(lifecycle):
            lifecycle = str(existing['lifecycle_state'])

        protection_mode = str(getattr(position, '_sidecar_protection_mode', '') or '')
        if protection_mode not in {item.value for item in ProtectionMode}:
            if existing and str(existing['protection_mode'] or '') in {item.value for item in ProtectionMode}:
                protection_mode = str(existing['protection_mode'])
            elif bool(getattr(position, 'uncapped', False)):
                protection_mode = ProtectionMode.TRAILING_ONLY.value
            else:
                protection_mode = self.get_mode()

        protected_quantity = str(getattr(position, '_sidecar_protected_quantity', '') or '')
        if not protected_quantity and existing and existing['protected_quantity'] not in (None, ''):
            protected_quantity = str(existing['protected_quantity'])
        if not protected_quantity:
            protected_quantity = str(filled or '')

        reconciliation_status = 'MATCHED_TO_LEGACY_STATE'
        if coverage_pending:
            reconciliation_status = 'PROTECTION_COVERAGE_PENDING_RECONCILE_REQUIRED'
        if existing and 'RECONCILE_REQUIRED' in str(existing['reconciliation_status'] or ''):
            reconciliation_status = str(existing['reconciliation_status'])
        if requested_lifecycle in {
                LifecycleState.REPROTECT_REQUIRED.value,
                LifecycleState.RECONCILIATION_REQUIRED.value}:
            reconciliation_status = requested_lifecycle

        self.upsert_trade(
            trade_id, pair, lifecycle_state=lifecycle,
            entry_client_order_id=getattr(position, 'entry_client_id', None),
            entry_order_id=getattr(position, 'entry_order_id', None),
            order_list_id=getattr(position, 'order_list_id', None),
            take_profit_order_id=getattr(position, 'tp_order_id', None),
            stop_order_id=getattr(position, 'sl_order_id', None) or getattr(position, 'exit_order_id', None),
            filled_quantity=str(filled if filled is not None else ''),
            protected_quantity=protected_quantity,
            average_entry_price=str(getattr(position, 'entry_price', '') or ''),
            protection_mode=protection_mode,
            trailing_delta=getattr(position, 'trail_delta', None),
            last_event_time=self._now(), reconciliation_status=reconciliation_status
        )

        # A user-stream event can beat the REST response that creates the local
        # Position. Backfill its audit identifiers and actual commission asset
        # after the position IDs become known, so early accepted events are not
        # stranded only in the raw-event table.
        ids = [
            getattr(position, 'entry_order_id', None),
            getattr(position, 'tp_order_id', None),
            getattr(position, 'sl_order_id', None),
            getattr(position, 'exit_order_id', None),
        ]
        ids = [value for value in ids if value not in (None, 0, '', '0')]
        list_id = getattr(position, 'order_list_id', None)
        clauses, params = [], []
        if ids:
            clauses.append('order_id IN (' + ','.join('?' for _ in ids) + ')')
            params.extend(ids)
        if list_id not in (None, 0, '', '0'):
            clauses.append('order_list_id=?')
            params.append(list_id)
        if clauses:
            with self._connect() as con:
                # Fixed clauses and bound parameters only.
                event = con.execute(
                    '''SELECT event_key,commission_asset,event_time,payload_json FROM exchange_events
                       WHERE symbol=? AND (''' + ' OR '.join(clauses) + ') ORDER BY id DESC LIMIT 1',  # nosec B608
                    [symbol,*params],
                ).fetchone()
                if event:
                    # Store the readable raw execution id (falling back to the
                    # composite dedup key) rather than the composite itself.
                    readable = event['event_key']
                    try:
                        payload = json.loads(event['payload_json'])
                        raw = payload.get('I') if payload.get('I') is not None else payload.get('u')
                        if raw is not None:
                            readable = str(raw)
                    except Exception:
                        pass
                    con.execute(
                        '''UPDATE trade_records SET commission_asset=COALESCE(?,commission_asset),
                           last_exchange_event_id=?,last_event_time=?,
                           reconciliation_status=CASE WHEN reconciliation_status LIKE '%RECONCILE_REQUIRED%'
                           THEN reconciliation_status ELSE 'MATCHED_TO_LEGACY_STATE_AND_EVENTS' END,updated_at=?
                           WHERE trade_id=?''',
                        (event['commission_asset'], readable, event['event_time'], self._now(), trade_id),
                    )
