from __future__ import annotations
"""Deterministic exchange simulator behind the CoreAdapter interface (C-004).

The previous simulation mode short-circuited before the adapter: approved
signals were archived as "simulated" without exercising the claim/submit/
protection/event/state-machine paths that carry live capital. This adapter
routes simulation through the SAME lifecycle surfaces:

  * entry submission produces deterministic executionReport events applied
    through StateStore.record_exchange_event (the real dedup + state machine)
    and FreshSignalGuard.on_exchange_event (the real risk sink);
  * protection placement produces listStatus/NEW protective-sell events;
  * fault injection (timeout / reject / partial_fill) is driven by a durable
    JSON plan so tests can prove each failure path deterministically.

Honest limitation (documented, unchanged from the audit): this simulator is
still not the real exchange. Binance Spot Testnet remains mandatory before
any live promotion.
"""
import json
import time
from decimal import Decimal
from pathlib import Path

from services.common.atomic import atomic_write_json, read_json
from services.common.audit import audit


class _SimTrader:
    def __init__(self):
        self._running = False

    def is_running(self):
        return self._running


class SimulationAdapter:
    def __init__(self, state_store=None, guard=None, notifier=None, fault_path=None):
        self.state_store = state_store
        self.guard = guard
        self.notifier = notifier or (lambda *a, **k: None)
        self.trader = _SimTrader()
        self.fault_path = Path(fault_path) if fault_path else None
        self._counter = 0
        self.sim_positions: dict[str, dict] = {}
        self.trade_size_usdt = 100.0
        self.max_positions = 2

    # ---- lifecycle ----
    def start(self):
        if self.state_store is not None:
            with self.state_store._connect() as con:
                maximum = con.execute('SELECT MAX(entry_order_id) FROM trade_records').fetchone()[0]
                active = con.execute(
                    "SELECT COUNT(*) FROM trade_records WHERE lifecycle_state NOT IN ('EXIT_FILLED','ERROR')").fetchone()[0]
            # Reusing simulation IDs after restart can correlate a new event
            # with an old position. Continue the deterministic ID sequence.
            self._counter = max(self._counter, (int(maximum or 100000) - 100000) // 10)
            if active and not self.sim_positions:
                self.state_store.latch_safety(
                    'simulation-restart', 'simulation positions require recovery after restart',
                    kind='reconciliation', details={'active_records': active})
        self.trader._running = True
        audit('simulation_adapter_started', details={'deterministic': True})
        return True

    def set_enabled(self, on: bool):
        if on and self.state_store and self.state_store.safety_halts():
            raise RuntimeError('simulation remains disabled while reconciliation is pending')
        return 'ON' if on else 'OFF'

    def _next_fault(self) -> str:
        if not self.fault_path:
            return 'none'
        data = read_json(self.fault_path, {}) or {}
        plan = list(data.get('queue') or [])
        if not plan:
            return 'none'
        nxt = str(plan.pop(0))
        atomic_write_json(self.fault_path, {'queue': plan})
        return nxt

    def _emit(self, event: dict):
        is_new = True
        if self.state_store is not None:
            is_new = self.state_store.record_exchange_event(event)
        if is_new and self.guard is not None:
            self.guard.on_exchange_event(event)

    def _link_trade(self, pair: str, **fields):
        if self.state_store is None:
            return
        trade_id = self.state_store._active_trade_id(pair)
        if trade_id:
            self.state_store.upsert_trade(trade_id, pair, **fields)

    def submit(self, symbol: str, note: str = ''):
        """Deterministic entry + protection lifecycle with injectable faults."""
        self.last_submission_outcome = 'REJECTED'
        if not self.trader.is_running():
            return False, 'simulation adapter not started'
        symbol = str(symbol).upper()
        pair = symbol[:-4] + '/USDT' if symbol.endswith('USDT') else symbol
        self._counter += 1
        n = self._counter
        fault = self._next_fault()
        entry_order_id = 100_000 + n * 10
        protection_order_id = entry_order_id + 2
        list_id = 500_000 + n
        price = Decimal('100')
        qty = Decimal('1')
        ts = int(time.time() * 1000)

        if fault == 'timeout':
            self.last_submission_outcome = 'UNKNOWN'
            audit('simulated_submit_timeout', severity='WARNING',
                  details={'symbol': symbol, 'note': note})
            return False, ('SIMULATED network timeout after possible acceptance — '
                           'outcome unknown; reconciliation required before any retry')

        self._link_trade(pair, entry_order_id=entry_order_id, order_list_id=list_id)
        common = {'e': 'executionReport', 's': symbol, 'i': entry_order_id,
                  'S': 'BUY', 'o': 'LIMIT', 'q': str(qty)}
        self._emit(dict(common, I=n * 100 + 1, X='NEW', z='0', Z='0', E=ts))
        if fault == 'reject':
            self._emit(dict(common, I=n * 100 + 2, X='REJECTED', z='0', Z='0',
                            r='INSUFFICIENT_BALANCE', E=ts + 1))
            return False, 'SIMULATED entry rejected by exchange (insufficient balance)'

        fill_qty = qty / 2 if fault == 'partial_fill' else qty
        if fault == 'partial_fill':
            self._emit(dict(common, I=n * 100 + 3, X='PARTIALLY_FILLED',
                            z=str(fill_qty), Z=str(fill_qty * price), N='USDT', E=ts + 1))
            self._emit(dict(common, I=n * 100 + 4, X='CANCELED',
                            z=str(fill_qty), Z=str(fill_qty * price), E=ts + 2))
        else:
            self._emit(dict(common, I=n * 100 + 3, X='FILLED',
                            z=str(fill_qty), Z=str(fill_qty * price), N='USDT', E=ts + 1))

        # Protection bracket on exactly the filled quantity.
        self._emit({'e': 'listStatus', 'E': ts + 3, 's': symbol, 'g': list_id,
                    'l': 'EXEC_STARTED', 'L': 'EXECUTING', 'C': f'sim-list-{n}'})
        self._emit({'e': 'executionReport', 'I': n * 100 + 5, 's': symbol,
                    'i': protection_order_id, 'g': list_id, 'S': 'SELL',
                    'o': 'STOP_LOSS_LIMIT', 'X': 'NEW', 'q': str(fill_qty),
                    'z': '0', 'E': ts + 4})
        self._link_trade(pair, stop_order_id=protection_order_id,
                         protected_quantity=str(fill_qty))
        self.sim_positions[symbol] = {
            'qty': str(fill_qty), 'entry_price': str(price), 'order_list_id': list_id,
            'entry_order_id': entry_order_id, 'stop_order_id': protection_order_id,
            'partial': fault == 'partial_fill',
        }
        detail = 'partial fill protected' if fault == 'partial_fill' else 'filled and protected'
        self.last_submission_outcome = 'ACCEPTED'
        return True, (f'SIMULATED {symbol} entry {detail} '
                      f'(order {entry_order_id}, list {list_id})')

    # ---- owner/command surface (interface parity with CoreAdapter) ----
    def status(self):
        return json.dumps({
            'simulation': True, 'deterministic': True,
            'open_positions': self.sim_positions,
            'trade_size_usdt': self.trade_size_usdt, 'max_positions': self.max_positions,
        }, sort_keys=True)

    def positions(self):
        return list(self.sim_positions.keys())

    def balance(self):
        return {'available': True, 'simulation': True, 'USDT_free': '1000',
                'open_positions': list(self.sim_positions.keys())}

    def profit(self):
        return {'available': True, 'simulation': True, 'daily_pnl_pct': 0.0,
                'daily_trades': self._counter, 'open_positions': len(self.sim_positions)}

    def restart_user_stream(self):
        return 'restarted'

    def reload_sharia(self):
        return 0

    def set_size(self, usdt: float):
        if usdt <= 0 or usdt > 10_000:
            return 'size must be 1–10000 USDT'
        self.trade_size_usdt = float(usdt)
        return f'trade size = {usdt:.0f} USDT'

    def set_max(self, count: int):
        if count < 1 or count > 10:
            return 'max positions 1–10'
        self.max_positions = int(count)
        return f'max concurrent positions = {count}'

    def emergency_exit(self, symbol) -> dict:
        symbol = str(symbol).upper()
        position = self.sim_positions.get(symbol)
        if not position:
            return {'ok': False, 'stage': 'not-found', 'halt_persisted': False,
                    'detail': 'no such open position (already closed?)'}
        ts = int(time.time() * 1000)
        self._emit({'e': 'executionReport', 'I': ts, 's': symbol,
                    'i': position['stop_order_id'], 'g': position['order_list_id'],
                    'S': 'SELL', 'o': 'STOP_LOSS_LIMIT', 'X': 'FILLED',
                    'z': position['qty'], 'Z': str(Decimal(position['qty']) * Decimal(position['entry_price'])),
                    'E': ts})
        self.sim_positions.pop(symbol, None)
        return {'ok': True, 'stage': 'verified-exit', 'halt_persisted': True,
                'submitted': True, 'executed_qty': position['qty'], 'remaining_base': '0',
                'detail': f'SIMULATED emergency exit of {symbol} verified'}

    def convert(self, symbol: str, mode, *, break_even=False, lock_profit_pct=None):
        symbol = str(symbol).upper()
        position = self.sim_positions.get(symbol)
        if not position:
            return False, 'position not found'
        ts = int(time.time() * 1000)
        old_list = position['order_list_id']
        new_list = old_list + 1000
        self._emit({'e': 'listStatus', 'E': ts, 's': symbol, 'g': old_list,
                    'l': 'ALL_DONE', 'L': 'ALL_DONE', 'C': f'sim-convert-{old_list}'})
        self._emit({'e': 'listStatus', 'E': ts + 1, 's': symbol, 'g': new_list,
                    'l': 'EXEC_STARTED', 'L': 'EXECUTING', 'C': f'sim-convert-{new_list}'})
        position['order_list_id'] = new_list
        mode_value = getattr(mode, 'value', str(mode))
        return True, f'SIMULATED {symbol} protection changed to {mode_value}'

    def mirror_positions(self, status='MATCHED'):
        if self.state_store is not None:
            self.state_store.data['last_reconciliation_status'] = status
            self.state_store.data['last_reconciliation_at'] = time.time()
            self.state_store.save()
        return len(self.sim_positions)

    def verified_reconcile(self) -> dict:
        if self.state_store is not None:
            # The simulator has no external exchange from which to recover an
            # uncertain order. Never clear a durable incident using empty RAM.
            with self.state_store._connect() as con:
                active = {row[0].replace('/', '') for row in con.execute(
                    "SELECT pair FROM trade_records WHERE lifecycle_state NOT IN ('EXIT_FILLED','ERROR')")}
            if self.state_store.safety_halts() or active != set(self.sim_positions):
                return {'ok': False, 'mirrored': 0,
                        'detail': 'simulation state uncertain; durable reconciliation required'}
        mirrored = self.mirror_positions('RECONCILED_SIMULATION')
        return {'ok': True, 'endpoints': {'simulation': {'ok': True}},
                'mirrored': mirrored,
                'detail': f'simulation reconcile complete; {mirrored} position(s)'}

    def reconcile(self):
        return self.verified_reconcile()['detail']
