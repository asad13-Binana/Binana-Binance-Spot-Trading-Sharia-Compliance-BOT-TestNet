from __future__ import annotations
import json, sqlite3, threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from services.common.atomic import atomic_write_json, read_json
from services.common.models import LifecycleState, ProtectionMode

SCHEMA = '''
PRAGMA journal_mode=WAL;
PRAGMA synchronous=FULL;
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
'''

class StateStore:
    def __init__(self, path: str | Path, db_path: str | Path | None = None):
        self.path = Path(path)
        self.db_path = Path(db_path or self.path.with_suffix('.sqlite'))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.data = read_json(self.path, {}) or {}
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
            con.executescript(SCHEMA)
        self.save()

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
            atomic_write_json(self.path, self.data)

    def get_mode(self):
        return self.data.get('protection_mode', ProtectionMode.OCO_TRAILING.value)

    def set_mode(self, mode):
        self.data['protection_mode'] = ProtectionMode(mode).value
        self.save()

    def entries(self):
        return bool(self.data.get('entries_enabled', False))

    def set_entries(self, value: bool, reason: str = ''):
        with self.lock:
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
            halts = self.data.setdefault('safety_halts', {})
            intents = self.data.setdefault('recovery_intents', {})
            prior_halt = halts.get(key, {})
            prior_intent = intents.get(key, {})
            if prior_halt.get('latched_at'):
                incident['latched_at'] = prior_halt['latched_at']
            halts[key] = incident
            # Re-latching the same incident must not erase a deterministic
            # client ID, accepted order ID, or recovery stage already written.
            intents[key] = dict(prior_intent, **incident,
                                status=prior_intent.get('status', 'PENDING'))
            self.data['entries_enabled'] = False
            self.data['pause_reason'] = 'safety-reconciliation-required:' + key
            self.save()
        return incident

    def update_recovery_intent(self, key: str, **fields) -> None:
        with self.lock:
            intents = self.data.setdefault('recovery_intents', {})
            if key not in intents:
                raise KeyError(f'unknown recovery intent {key}')
            intents[key].update({k: v for k, v in fields.items() if k not in {'key'}})
            intents[key]['updated_at'] = self._now()
            self.save()

    def resolve_safety(self, key: str, resolution: str) -> None:
        """Resolve one verified incident without ever re-enabling entries."""
        with self.lock:
            incident = self.data.setdefault('recovery_intents', {}).pop(key, None)
            self.data.setdefault('safety_halts', {}).pop(key, None)
            self.data['last_safety_resolution'] = {
                'key': key, 'resolution': str(resolution),
                'incident': incident or {}, 'resolved_at': self._now(),
            }
            self.data['entries_enabled'] = False
            self.data['pause_reason'] = (
                'safety-reconciliation-required:' + ','.join(sorted(self.data['safety_halts']))
                if self.data['safety_halts'] else 'owner-resume-required-after-safety-action')
            self.save()

    def safety_halts(self) -> dict:
        with self.lock:
            return json.loads(json.dumps(self.data.get('safety_halts', {}), default=str))

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
        columns = ['trade_id','pair'] + list(clean)
        values = [trade_id, pair] + [clean[c] for c in clean]
        placeholders = ','.join('?' for _ in columns)
        update_keys = [c for c in clean if c != 'lifecycle_state' or explicit_lifecycle]
        updates = ','.join(f'{c}=excluded.{c}' for c in update_keys)
        # SQL identifiers come only from the fixed allowlist above; all values are bound.
        sql = f"INSERT INTO trade_records({','.join(columns)}) VALUES({placeholders}) ON CONFLICT(trade_id) DO UPDATE SET {updates}"  # nosec B608
        with self._connect() as con:
            con.execute(sql, values)

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

        with self._connect() as con:
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

            # The dedup key stored in the UNIQUE exchange_events.event_key is the
            # composite above (M-007). The human-facing last_exchange_event_id on
            # the trade row stays the readable raw execution id when present.
            readable_event_id = str(raw_event_id) if raw_event_id is not None else event_key
            rows = con.execute(
                # Clauses are fixed literals selected above; all values are bound.
                'SELECT * FROM trade_records WHERE ' + ' OR '.join(clauses), values  # nosec B608
            ).fetchall()
            for row in rows:
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
                            lifecycle = LifecycleState.ERROR.value
                            updates['reconciliation_status'] = 'ENTRY_TERMINAL_NO_FILL'
                    elif side == 'SELL' and status in {'NEW', 'PARTIALLY_FILLED'} and order_type in {
                        'STOP_LOSS', 'STOP_LOSS_LIMIT', 'TAKE_PROFIT', 'TAKE_PROFIT_LIMIT', 'LIMIT_MAKER'
                    }:
                        lifecycle = LifecycleState.PROTECTION_ACTIVE.value
                    elif side == 'SELL' and status == 'FILLED':
                        lifecycle = LifecycleState.EXIT_FILLED.value
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
                        lifecycle = LifecycleState.PROTECTION_ACTIVE.value
                    elif list_status == 'ALL_DONE' or order_status == 'ALL_DONE':
                        # ALL_DONE explicitly means the list is no longer active. It
                        # does not say which leg filled; executionReport/reconciliation
                        # must determine whether the position exited or is unprotected.
                        updates['reconciliation_status'] = 'ORDER_LIST_TERMINAL_RECONCILE_REQUIRED'
                    elif list_status == 'RESPONSE' or order_status == 'REJECT':
                        # Binance defines these as a failed placement/cancel action.
                        # The pre-existing order list may still be live, so never mark
                        # the entire trade ERROR from this event alone.
                        updates['reconciliation_status'] = 'ORDER_LIST_ACTION_REJECTED_RECONCILE_REQUIRED'

                if lifecycle:
                    old = str(row['lifecycle_state'])
                    if old == LifecycleState.RECONCILED.value or self._state_rank(lifecycle) >= self._state_rank(old):
                        updates['lifecycle_state'] = lifecycle

                set_sql = ','.join(f'{key}=?' for key in updates)
                con.execute(
                    # Update keys are fixed internal literals; the trade ID is bound.
                    f'UPDATE trade_records SET {set_sql} WHERE trade_id=?',  # nosec B608
                    [*updates.values(), row['trade_id']],
                )
        return True

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

        with self._connect() as con:
            existing = con.execute(
                '''SELECT lifecycle_state,protection_mode,protected_quantity,reconciliation_status
                   FROM trade_records WHERE trade_id=?''',
                (trade_id,),
            ).fetchone()
        if existing and state_name == 'CLOSED' and 'RECONCILE_REQUIRED' in str(
                existing['reconciliation_status'] or ''):
            lifecycle = LifecycleState.RECONCILIATION_REQUIRED.value
        lifecycle_override = requested_lifecycle in {
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
            filled_quantity=str(filled or ''),
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
                       WHERE ''' + ' OR '.join(clauses) + ' ORDER BY id DESC LIMIT 1',  # nosec B608
                    params,
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
                           reconciliation_status='MATCHED_TO_LEGACY_STATE_AND_EVENTS',updated_at=?
                           WHERE trade_id=?''',
                        (event['commission_asset'], readable, event['event_time'], self._now(), trade_id),
                    )
