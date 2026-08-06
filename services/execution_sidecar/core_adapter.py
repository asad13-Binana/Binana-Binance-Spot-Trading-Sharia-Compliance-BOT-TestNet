from __future__ import annotations
import importlib.util, os, sys, tempfile, types, time
from decimal import Decimal
from pathlib import Path
from typing import Callable
from services.common.models import LifecycleState, ProtectionMode
from services.common.audit import audit
from services.execution_sidecar.protection_modes import OrderRequestFactory
from services.execution_sidecar.filters import (
    FilterDataUnavailable, FilterViolation, SpotFilterValidator,
)
from services.execution_sidecar.user_data_stream import ModernUserDataStream

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The preserved engine intentionally uses relative paths for fortress_state.json,
# logs/, and data/. Run it from a dedicated persistent volume location rather
# than the read-only image filesystem. This changes no source in the engine.
#
# CI-SAFE-001: the '/app/shared' default is only reachable INSIDE the
# deployment image, where this package lives at /app and docker-compose
# sets SHARED_ROOT explicitly for every service. Outside the image -- a
# source checkout, GitHub Actions, a developer machine -- /app does not
# exist and an unprivileged user cannot create a directory at the
# filesystem root, so merely IMPORTING this module aborted with
# PermissionError: '/app' before a single test could run. Fall back to a
# writable scratch directory in that case only. An explicit SHARED_ROOT or
# LEGACY_RUNTIME_DIR still wins, so deployment behaviour is unchanged. The
# mkdir and chdir stay at import time on purpose: the preserved engine
# calls os.makedirs('logs'/'data') at ITS import, just below.
_IMAGE_ROOT = Path('/app')
_default_shared_root = (_IMAGE_ROOT / 'shared' if ROOT == _IMAGE_ROOT
                        else Path(tempfile.gettempdir()) / 'binance-bot-shared')
_shared_root = Path(os.getenv('SHARED_ROOT', _default_shared_root))
LEGACY_RUNTIME = Path(os.getenv('LEGACY_RUNTIME_DIR', _shared_root / 'legacy_runtime'))
LEGACY_RUNTIME.mkdir(parents=True, exist_ok=True)
os.chdir(LEGACY_RUNTIME)

_legacy_path = ROOT / 'legacy_core' / 'binance_bot_V4.9.16_ALL_IN_ONE.py'
_spec = importlib.util.spec_from_file_location('binance_bot_v4916_legacy', _legacy_path)
if _spec is None or _spec.loader is None:
    raise ImportError(f'cannot load preserved core from {_legacy_path}')
legacy = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = legacy
_spec.loader.exec_module(legacy)


class BalanceSnapshotUnavailable(RuntimeError):
    """The authenticated account endpoint could not prove a current balance."""


# Immutable references let repeated test/app adapter construction replace the
# global legacy class hooks without wrapping an earlier adapter's closures.
_ORIGINAL_BROKER_FREE = legacy.Broker.free
_ORIGINAL_BROKER_INVALIDATE = legacy.Broker.invalidate_balance_cache
_ORIGINAL_RECONCILE_POSITION = legacy.Portfolio._reconcile_position
_ORIGINAL_AWAIT_FILL = legacy.EntryEngine._await_fill
_ORIGINAL_REPRICE = legacy.EntryEngine._reprice
_ORIGINAL_BRACKET_FILL = legacy.AutoTrader._bracket_fill
_ORIGINAL_EMERGENCY = legacy.ExitEngine._emergency
_ORIGINAL_REPROTECT = legacy.ExitEngine.reprotect_if_naked

class CoreAdapter:
    """Adapter around the preserved V4.9.16 engine.

    The original signal and fortress implementation stays unchanged. This adapter:
    1) chooses one of three Binance protection request structures;
    2) journals executionReport/ListStatus events, including the actual commission asset;
    3) mirrors reconciled position state into the sidecar database.
    """
    def __init__(self, mode_getter, notifier, legacy_halal_path, universe_provider,
                 event_sink: Callable[[dict], None] | None = None, state_store=None):
        self.mode_getter = mode_getter
        self.event_sink = event_sink
        self.state_store = state_store
        self.trader = legacy.AutoTrader(
            notifier=notifier,
            halal_path=str(legacy_halal_path),
            top_gainer_provider=universe_provider,
        )
        self.factory = OrderRequestFactory(legacy)
        self.filter_validator = None  # created lazily; tests may inject a stub
        self._balance_cache: dict[int, tuple[float, dict[str, dict]]] = {}
        self._runtime_safety_faults: dict[str, dict] = {}
        self._patched = False
        self._install_event_wrappers()

    def runtime_safety_faults(self) -> dict[str, dict]:
        return dict(getattr(self, '_runtime_safety_faults', {}))

    def _handle_stream_safety_fault(self, event: dict, *, prefix: str,
                                    reason: str) -> None:
        """Disarm immediately and best-effort persist a stream incident.

        This method deliberately never raises into the WebSocket callback.  A
        journal, handler, or unsafe-expiry failure must keep supervision alive
        while making every new entry impossible in memory, even if the disk is
        simultaneously failing.
        """
        symbol = str(event.get('s') or event.get('symbol') or '').upper()
        order_id = event.get('i') if event.get('i') is not None else event.get('orderId')
        list_id = event.get('g') if event.get('g') is not None else event.get('orderListId')
        key = f'{prefix}:{symbol or "UNKNOWN"}:{order_id or 0}:{list_id or 0}'[:200]
        details = {
            'event_type': event.get('e') or event.get('eventType'),
            'order_id': order_id, 'order_list_id': list_id,
            'status': event.get('X') or event.get('status'),
            'list_status': event.get('L') or event.get('listOrderStatus'),
            'expiry_reason': event.get('expiryReason'),
        }
        faults = getattr(self, '_runtime_safety_faults', None)
        if faults is None:
            faults = self._runtime_safety_faults = {}
        faults[key] = {'reason': reason, 'symbol': symbol, 'details': details}

        trader = getattr(self, 'trader', None)
        pf = getattr(trader, 'pf', None)
        try:
            legacy._set_entries_armed(False)
        except Exception:
            pass
        if pf:
            try:
                pf.autotrade_on = False
                pf.protection_halt = reason
                pf.halt_reason = reason
                position = getattr(pf, 'positions', {}).get(symbol)
                if position is not None and position.state != legacy.PosState.CLOSED:
                    position.replacing_protection = True
                    position._sidecar_lifecycle = LifecycleState.RECONCILIATION_REQUIRED.value
                pf.save()
            except Exception as exc:
                audit('stream_fail_closed_memory_mirror_failed', severity='CRITICAL',
                      details={'key': key, 'error': str(exc)})

        try:
            self._latch_safety(key, reason, symbol, 'user-stream', details)
        except Exception as exc:
            # Keep a second in-memory flag in StateStore and attempt one simple
            # persistence path.  Re-arming remains denied by the runtime fault
            # even if both durable writes fail.
            store = getattr(self, 'state_store', None)
            if store is not None:
                try:
                    store.data['entries_enabled'] = False
                    store.data['pause_reason'] = 'runtime-user-stream-safety-fault'
                    store.save()
                except Exception:
                    pass
            audit('stream_safety_latch_failed', severity='CRITICAL', details={
                'key': key, 'reason': reason, 'error': str(exc),
            })

    def _install_event_wrappers(self):
        original_order = self.trader._uds_order
        original_list = self.trader._uds_list

        def wrapped_order(this, report: dict):
            is_new = True
            try:
                if self.state_store:
                    is_new = self.state_store.record_exchange_event(report)
                if self.event_sink and is_new:
                    self.event_sink(report)
                audit('binance_execution_report', details={
                    'symbol': report.get('s'), 'status': report.get('X'),
                    'order_id': report.get('i'), 'order_list_id': report.get('g'),
                    'commission_asset': report.get('N'), 'commission': report.get('n'),
                    'expiry_reason': report.get('expiryReason'),
                    'deduplicated': not is_new,
                })
            except Exception as exc:
                # UNKNOWN local state is itself safety-relevant.  Keep the
                # callback alive, but never leave new entries armed.
                audit('binance_event_journal_failed', severity='CRITICAL', details={'error': str(exc)})
                self._handle_stream_safety_fault(
                    report, prefix='stream-journal',
                    reason=f'executionReport journal/sink failed: {exc}')
            if self._unsafe_exchange_fact(report):
                self._handle_stream_safety_fault(
                    report, prefix='stream-unsafe-expiry',
                    reason='unsafe Binance execution expiry requires REST/account reconciliation')
            try:
                original_order(report)
            except Exception as exc:
                audit('binance_event_handler_failed', severity='CRITICAL', details={'error': str(exc)})
                self._handle_stream_safety_fault(
                    report, prefix='stream-handler',
                    reason=f'executionReport handler failed: {exc}')

        def wrapped_list(this, status: dict):
            is_new = True
            try:
                if self.state_store:
                    is_new = self.state_store.record_exchange_event(status)
                if self.event_sink and is_new:
                    self.event_sink(status)
                audit('binance_list_status', details={
                    'symbol': status.get('s'), 'order_list_id': status.get('g'),
                    'status': status.get('l'), 'order_status': status.get('L'),
                    'expiry_reason': status.get('expiryReason'),
                    'deduplicated': not is_new,
                })
            except Exception as exc:
                audit('binance_event_journal_failed', severity='CRITICAL', details={'error': str(exc)})
                self._handle_stream_safety_fault(
                    status, prefix='stream-journal',
                    reason=f'listStatus journal/sink failed: {exc}')
            if self._unsafe_exchange_fact(status):
                self._handle_stream_safety_fault(
                    status, prefix='stream-unsafe-expiry',
                    reason='unsafe Binance order-list expiry requires REST/account reconciliation')
            try:
                original_list(status)
            except Exception as exc:
                audit('binance_event_handler_failed', severity='CRITICAL', details={'error': str(exc)})
                self._handle_stream_safety_fault(
                    status, prefix='stream-handler',
                    reason=f'listStatus handler failed: {exc}')

        self.trader._uds_order = types.MethodType(wrapped_order, self.trader)
        self.trader._uds_list = types.MethodType(wrapped_list, self.trader)

    def _configure_environment(self):
        mode = os.getenv('EXECUTION_MODE', 'simulation').lower()
        testnet = mode != 'live'
        os.environ['BINANCE_TESTNET'] = 'true' if testnet else 'false'
        # CFG and FORTRESS_CFG are the same preserved object; set it before Broker construction.
        legacy.CFG.TESTNET = testnet
        legacy.FORTRESS_CFG.TESTNET = testnet

    def start(self):
        self._configure_environment()
        self._install_safety_interpositions()
        # Step 2 adoption: Binance retired the listen-key REST user-data-stream
        # endpoints on 2026-02-20 (verified against the official Spot changelog).
        # Swap only the runtime class reference so the preserved legacy trader
        # subscribes via the WebSocket API instead; the legacy source stays
        # byte-for-byte intact and is still enforced by the release tests.
        legacy.UserDataStream = ModernUserDataStream
        ok = self.trader.start()
        if not ok:
            return False
        if not self._patched:
            broker = self.trader.broker
            original = broker.place_otoco
            original_limit_buy = broker.limit_buy
            factory = self.factory
            mode_getter = self.mode_getter

            def place_mode_aware(this, sym, qty, entry, tp, trail_bips):
                mode = ProtectionMode(mode_getter())
                # V101-NEW-006: reject any entry whose fee-shaved protective
                # quantity rounds to zero, on every protection mode including
                # the preserved OCO_TRAILING path (whose internal fallback
                # would otherwise restore the unshaved full quantity).
                shaved = factory.pending_qty(sym, legacy.round_down(qty, sym.step))
                if shaved <= 0:
                    raise ValueError(
                        f'{sym.symbol}: fee-shaved protective quantity rounds to zero; '
                        'entry too small to protect safely — rejected before submission')
                endpoint, params = factory.entry(mode, sym, qty, entry, tp, trail_bips)
                # Preflight the complete live exchange filter/capacity set for
                # every mode, including the preserved default OCO_TRAILING path.
                pending_price = Decimal(params.get('pendingBelowPrice') or params.get('pendingPrice'))
                this._validate_otoco_filters(
                    sym, legacy.round_down(entry, sym.tick), legacy.round_down(tp, sym.tick),
                    pending_price, legacy.round_down(qty, sym.step), Decimal(params['pendingQuantity']),
                    max(sym.trail_min, min(trail_bips, sym.trail_max))
                )
                self._validate_replacement_filters(this, sym.symbol, endpoint, params)
                if mode is ProtectionMode.OCO_TRAILING:
                    # Keep the protected legacy request construction/submission,
                    # but only after the equivalent payload passed strict checks.
                    return original(sym, qty, entry, tp, trail_bips)
                coid = params['listClientOrderId']
                def send():
                    out = this.c._post(endpoint, True, data=params)
                    this._sync_weight()
                    return out
                return this._place_idempotent(send, lambda: this._find_list(coid), f'{mode.value} {sym.symbol}')

            broker.place_otoco = types.MethodType(place_mode_aware, broker)

            def limit_buy_strict(this, sym, qty, price):
                rounded_qty = legacy.round_down(qty, sym.step)
                rounded_price = legacy.round_down(price, sym.tick)
                params = {
                    'symbol': sym.symbol, 'side': 'BUY', 'type': 'LIMIT',
                    'quantity': legacy.dstr(rounded_qty),
                    'price': legacy.dstr(rounded_price), 'timeInForce': 'GTC',
                }
                self._validate_replacement_filters(this, sym.symbol, 'order', params)
                return original_limit_buy(sym, qty, price)

            broker.limit_buy = types.MethodType(limit_buy_strict, broker)

            if callable(getattr(broker, 'activation_trailing_sell', None)):
                def activation_trailing_sell_strict(this, sym, qty, activation, delta_bips):
                    return self._strict_trailing_sell(
                        this, sym, qty, delta_bips, activation=activation)

                broker.activation_trailing_sell = types.MethodType(
                    activation_trailing_sell_strict, broker)

            if callable(getattr(broker, 'immediate_trailing_sell', None)):
                def immediate_trailing_sell_strict(this, sym, qty, delta_bips):
                    return self._strict_trailing_sell(this, sym, qty, delta_bips)

                broker.immediate_trailing_sell = types.MethodType(
                    immediate_trailing_sell_strict, broker)
            self._patched = True
        self.mirror_positions('STARTUP_RECONCILED')
        return True

    def set_enabled(self, on: bool):
        if on and self.runtime_safety_faults():
            raise RuntimeError('execution remains disabled after an in-memory safety fault')
        if on and self.state_store and self.state_store.safety_halts():
            raise RuntimeError('execution remains disabled while safety reconciliation is pending')
        return self.trader.set_autotrade(on)

    def submit(self, symbol, note=''):
        return self.trader.submit_signal(symbol, note)

    def status(self):
        return self.trader.status_text()

    def positions(self):
        return self.trader.list_open_positions()

    def open_orders_snapshot(self) -> dict:
        """Return authoritative open orders and order lists, or fail closed.

        The preserved convenience methods convert endpoint errors into empty
        lists. Operator visibility must not confuse UNKNOWN with empty, so this
        method calls both authenticated endpoints directly and exposes data
        only when both replies are structurally valid.
        """
        if not self.trader.is_running() or not self.trader.broker:
            return {'ok': False, 'detail': 'execution core is not started'}
        broker = self.trader.broker
        try:
            orders = broker.c.get_open_orders()
            broker._sync_weight()
            if not isinstance(orders, list):
                raise RuntimeError(
                    f'unexpected openOrders response type {type(orders).__name__}')
            order_lists = broker.c._get('openOrderList', True, data={})
            broker._sync_weight()
            if not isinstance(order_lists, list):
                raise RuntimeError(
                    f'unexpected openOrderList response type {type(order_lists).__name__}')
        except Exception as exc:
            audit('open_orders_visibility_failed', severity='ERROR', details={
                'error': str(exc),
            })
            return {
                'ok': False,
                'detail': f'exchange order visibility unavailable: {exc}',
            }

        order_fields = (
            'symbol', 'orderId', 'orderListId', 'clientOrderId', 'side', 'type',
            'status', 'timeInForce', 'origQty', 'executedQty',
            'cummulativeQuoteQty', 'price', 'stopPrice', 'trailingDelta',
            'time', 'updateTime', 'workingTime',
        )
        list_fields = (
            'symbol', 'orderListId', 'contingencyType', 'listStatusType',
            'listOrderStatus', 'listClientOrderId', 'transactionTime',
        )

        def public_order(value):
            return {key: value[key] for key in order_fields if key in value}

        public_lists = []
        for value in order_lists:
            if not isinstance(value, dict):
                continue
            item = {key: value[key] for key in list_fields if key in value}
            item['orders'] = [public_order(order) for order in value.get('orders', [])
                              if isinstance(order, dict)]
            public_lists.append(item)
        result = {
            'ok': True,
            'open_order_count': len(orders),
            'open_order_list_count': len(order_lists),
            'open_orders': [public_order(order) for order in orders
                            if isinstance(order, dict)],
            'open_order_lists': public_lists,
        }
        audit('open_orders_visibility_succeeded', details={
            'open_order_count': len(orders),
            'open_order_list_count': len(order_lists),
        })
        return result

    def balance(self):
        if not self.trader.is_running() or not self.trader.broker:
            return {'available': False, 'reason': 'execution core is not started'}
        snapshot = self.strict_balance_snapshot('USDT', force=True)
        if snapshot['status'] != 'KNOWN':
            return {'available': False, 'reason': snapshot['error'],
                    'USDT': snapshot, 'open_positions': self.trader.list_open_positions()}
        return {
            'available': True,
            'USDT_free': snapshot['free'], 'USDT_locked': snapshot['locked'],
            'USDT_total': snapshot['total'],
            'open_positions': self.trader.list_open_positions(),
        }

    def profit(self):
        pf = self.trader.pf
        if not pf:
            return {'available': False, 'reason': 'execution core is not started'}
        return {
            'available': True,
            'daily_pnl_pct': float(pf.daily_pnl_pct) * 100,
            'daily_trades': int(pf.daily_trades),
            'open_positions': len(pf.positions),
        }

    def restart_user_stream(self):
        return self.trader.restart_user_stream()

    def reload_sharia(self):
        return self.trader.sharia.reload_now()

    def set_size(self, usdt: float):
        return self.trader.set_size(usdt)

    def set_max(self, count: int):
        return self.trader.set_max(count)

    def _install_safety_interpositions(self):
        """Install service-owned fail-closed guards before legacy startup/load."""
        adapter = self

        def strict_free(this, asset):
            snapshot = adapter.strict_balance_snapshot(asset, broker=this, force=False)
            if snapshot['status'] != 'KNOWN':
                raise BalanceSnapshotUnavailable(snapshot['error'])
            return Decimal(snapshot['free'])

        def invalidate(this):
            adapter._balance_cache.pop(id(this), None)
            return _ORIGINAL_BROKER_INVALIDATE(this)

        def reconcile(this, position):
            return adapter._reconcile_position_interposed(this, position)

        def await_fill(this, symbol):
            return adapter._await_fill_interposed(this, symbol)

        def reprice(this, position):
            return adapter._reprice_interposed(this, position)

        def bracket_fill(this, symbol, position):
            return adapter._bracket_fill_interposed(this, symbol, position)

        def emergency(this, position, why):
            position.state = legacy.PosState.EMERGENCY
            return adapter.emergency_exit(position.symbol, reason=why)

        def reprotect(this, position, current):
            return adapter._reprotect_interposed(this, position, current)

        legacy.Broker.free = strict_free
        legacy.Broker.invalidate_balance_cache = invalidate
        legacy.Portfolio._reconcile_position = reconcile
        legacy.EntryEngine._await_fill = await_fill
        legacy.EntryEngine._reprice = reprice
        legacy.AutoTrader._bracket_fill = bracket_fill
        legacy.ExitEngine._emergency = emergency
        legacy.ExitEngine.reprotect_if_naked = reprotect

    @staticmethod
    def _decimal(value, label: str) -> Decimal:
        try:
            parsed = Decimal(str(value))
        except Exception as exc:
            raise ValueError(f'invalid {label}') from exc
        if not parsed.is_finite() or parsed < 0:
            raise ValueError(f'invalid {label}')
        return parsed

    def strict_balance_snapshot(self, asset: str, *, broker=None, force: bool = True) -> dict:
        """Return a KNOWN free/locked/total triple, never a stale zero fallback."""
        asset = str(asset or '').upper()
        broker = broker or getattr(self.trader, 'broker', None)
        if not asset or broker is None:
            return {'status': 'UNKNOWN', 'asset': asset, 'free': None, 'locked': None,
                    'total': None, 'error': 'authenticated balance source is unavailable'}
        cache = getattr(self, '_balance_cache', {})
        self._balance_cache = cache
        cached = cache.get(id(broker))
        if not force and cached and time.monotonic() - cached[0] <= 2.0 and asset in cached[1]:
            return dict(cached[1][asset])
        try:
            account = broker.c.get_account()
            broker._sync_weight()
            balances = account.get('balances') if isinstance(account, dict) else None
            if not isinstance(balances, list):
                raise ValueError('malformed account response')
            parsed_balances = {}
            observed = time.time()
            for item in balances:
                if not isinstance(item, dict) or not item.get('asset'):
                    raise ValueError('malformed balance row')
                name = str(item['asset']).upper()
                free = self._decimal(item.get('free'), f'{name}.free')
                locked = self._decimal(item.get('locked'), f'{name}.locked')
                parsed_balances[name] = {
                    'status': 'KNOWN', 'asset': name, 'free': str(free),
                    'locked': str(locked), 'total': str(free + locked),
                    'observed_at': observed,
                }
            if asset not in parsed_balances:
                raise ValueError(f'{asset} balance absent from account response')
            cache[id(broker)] = (time.monotonic(), parsed_balances)
            return dict(parsed_balances[asset])
        except Exception as exc:
            cache.pop(id(broker), None)
            result = {'status': 'UNKNOWN', 'asset': asset, 'free': None, 'locked': None,
                      'total': None, 'error': str(exc)}
            audit('strict_balance_unknown', severity='CRITICAL', details={
                'asset': asset, 'error': str(exc),
            })
            return result

    def _latch_safety(self, key: str, reason: str, symbol: str, kind: str,
                      details=None) -> str:
        """Persist the halt first; exchange actions are forbidden if this fails."""
        if not self.state_store:
            raise RuntimeError('durable safety state store is unavailable')
        self.state_store.latch_safety(
            key, reason, symbol=symbol, kind=kind, details=details or {})
        trader = getattr(self, 'trader', None)
        pf = getattr(trader, 'pf', None)
        try:
            legacy._set_entries_armed(False)
            if pf:
                pf.autotrade_on = False
                pf.protection_halt = reason
                pf.halt_reason = reason
                pf.save()
        except Exception as exc:
            audit('legacy_halt_mirror_failed', severity='ERROR', details={
                'key': key, 'error': str(exc),
            })
        audit('safety_halt_latched', severity='CRITICAL', details={
            'key': key, 'symbol': symbol, 'kind': kind, 'reason': reason,
        })
        return key

    def _update_intent(self, key: str, **fields) -> None:
        if not self.state_store:
            raise RuntimeError('durable safety state store is unavailable')
        self.state_store.update_recovery_intent(key, **fields)

    def _resolve_safety(self, key: str, resolution: str) -> None:
        if self.state_store:
            self.state_store.resolve_safety(key, resolution)
        getattr(self, '_runtime_safety_faults', {}).pop(key, None)

    @staticmethod
    def _unsafe_exchange_fact(value: dict) -> bool:
        if not isinstance(value, dict):
            return True
        if value.get('expiryReason'):
            return True
        statuses = (
            value.get('status'), value.get('X'), value.get('orderStatus'),
            value.get('listOrderStatus'), value.get('L'),
        )
        return any(str(status or '').upper() == 'EXPIRED_IN_MATCH' for status in statuses)

    @staticmethod
    def _order_status(value: dict) -> str:
        return str(value.get('status') or value.get('X') or value.get('orderStatus') or '').upper()

    def _query_order(self, broker, symbol: str, order_id: int) -> dict:
        value = broker.c.get_order(symbol=symbol, orderId=int(order_id))
        broker._sync_weight()
        if not isinstance(value, dict) or not value.get('orderId'):
            raise RuntimeError(f'malformed order query for {order_id}')
        return value

    def _query_list(self, broker, order_list_id: int) -> dict:
        value = broker.c._get('orderList', True, data={'orderListId': int(order_list_id)})
        broker._sync_weight()
        if not isinstance(value, dict) or not value.get('orderListId'):
            raise RuntimeError(f'malformed order-list query for {order_list_id}')
        return value

    @staticmethod
    def _known_order_ids(position) -> list[int]:
        values = []
        for name in ('entry_order_id', 'tp_order_id', 'sl_order_id', 'exit_order_id'):
            try:
                value = int(getattr(position, name, None) or 0)
            except Exception:
                value = 0
            if value and value not in values:
                values.append(value)
        return values

    def _cancel_and_verify_known(self, broker, position) -> tuple[bool, str, dict]:
        """Cancel every known handle and then positively prove terminal state."""
        facts = {'cancel_errors': {}, 'orders': {}, 'order_list': None}
        list_id = int(getattr(position, 'order_list_id', None) or 0)
        if list_id:
            try:
                broker.cancel_order_list(position.symbol, list_id)
            except Exception as exc:
                facts['cancel_errors'][f'list:{list_id}'] = str(exc)
        for order_id in self._known_order_ids(position):
            try:
                broker.cancel(position.symbol, order_id)
            except Exception as exc:
                facts['cancel_errors'][f'order:{order_id}'] = str(exc)

        failures = []
        if list_id:
            try:
                value = self._query_list(broker, list_id)
                facts['order_list'] = value
                if self._unsafe_exchange_fact(value):
                    failures.append('order list has unsafe expiry semantics')
                elif str(value.get('listOrderStatus') or '').upper() != 'ALL_DONE':
                    failures.append('order list is not terminal')
            except Exception as exc:
                failures.append(f'order-list verification failed: {exc}')
        terminal = {'CANCELED', 'EXPIRED', 'REJECTED', 'FILLED'}
        for order_id in self._known_order_ids(position):
            try:
                value = self._query_order(broker, position.symbol, order_id)
                facts['orders'][str(order_id)] = value
                if self._unsafe_exchange_fact(value):
                    failures.append(f'order {order_id} has unsafe expiry semantics')
                elif self._order_status(value) not in terminal:
                    failures.append(f'order {order_id} is not terminal')
            except Exception as exc:
                failures.append(f'order {order_id} verification failed: {exc}')
        if failures:
            return False, '; '.join(failures), facts
        return True, 'all known order/list handles are positively terminal', facts

    def _verify_live_protection(self, broker, position) -> tuple[bool, str, dict]:
        facts = {'orders': {}, 'order_list': None}
        try:
            if getattr(position, 'bracket', False) and getattr(position, 'order_list_id', None):
                value = self._query_list(broker, position.order_list_id)
                facts['order_list'] = value
                if self._unsafe_exchange_fact(value):
                    return False, 'order list has unsafe expiry semantics', facts
                if str(value.get('listOrderStatus') or '').upper() != 'EXECUTING':
                    return False, 'order list is not EXECUTING', facts
                if not position.tp_order_id or not position.sl_order_id:
                    return False, 'protective list leg identifiers are incomplete', facts
                for order_id in (position.tp_order_id, position.sl_order_id):
                    order = self._query_order(broker, position.symbol, order_id)
                    facts['orders'][str(order_id)] = order
                    if self._unsafe_exchange_fact(order) or self._order_status(order) not in {
                            'NEW', 'PARTIALLY_FILLED'}:
                        return False, f'protective leg {order_id} is not live', facts
                return True, 'order list and both protective legs are live', facts
            order_id = getattr(position, 'exit_order_id', None)
            if not order_id:
                return False, 'no protective order identifier', facts
            order = self._query_order(broker, position.symbol, order_id)
            facts['orders'][str(order_id)] = order
            if self._unsafe_exchange_fact(order):
                return False, 'protective order has unsafe expiry semantics', facts
            if self._order_status(order) not in {'NEW', 'PARTIALLY_FILLED'}:
                return False, 'protective order is not live', facts
            return True, 'protective order is live', facts
        except Exception as exc:
            return False, f'protection verification failed: {exc}', facts

    @staticmethod
    def _is_dust(position, total: Decimal, price: Decimal | None = None) -> bool:
        total = Decimal(str(total))
        if total <= 0:
            return True
        min_qty = Decimal(str(position.sym.min_qty or '0'))
        if min_qty > 0 and total < min_qty:
            return True
        min_notional = Decimal(str(position.sym.min_notional or '0'))
        return bool(price is not None and min_notional > 0 and total * price < min_notional)

    def _terminal_list_exit_evidence(self, broker, position) -> dict:
        result = {'ok': False, 'filled': False, 'fill_price': Decimal('0'),
                  'which': '', 'opposite_terminal': False, 'facts': {}}
        try:
            order_list = self._query_list(broker, position.order_list_id)
            result['facts']['order_list'] = order_list
            if self._unsafe_exchange_fact(order_list):
                result['detail'] = 'unsafe order-list expiry semantics'
                return result
            if str(order_list.get('listOrderStatus') or '').upper() != 'ALL_DONE':
                result['detail'] = 'order list is not ALL_DONE'
                return result
            if not position.tp_order_id or not position.sl_order_id:
                result['detail'] = 'opposite protective leg identifier is missing'
                return result
            legs = []
            for order_id, tag in ((position.tp_order_id, 'TP'), (position.sl_order_id, 'SL')):
                order = self._query_order(broker, position.symbol, order_id)
                result['facts'][tag] = order
                if self._unsafe_exchange_fact(order):
                    result['detail'] = f'{tag} has unsafe expiry semantics'
                    return result
                status = self._order_status(order)
                if status not in {'FILLED', 'CANCELED', 'EXPIRED', 'REJECTED'}:
                    result['detail'] = f'{tag} is not terminal'
                    return result
                legs.append((tag, status, order))
            filled = [leg for leg in legs if leg[1] == 'FILLED']
            if len(filled) > 1:
                result['detail'] = 'multiple exit legs report FILLED'
                return result
            balance = self.strict_balance_snapshot(position.sym.base, broker=broker, force=True)
            result['balance'] = balance
            if balance['status'] != 'KNOWN':
                result['detail'] = 'base total balance is UNKNOWN'
                return result
            total = Decimal(balance['total'])
            try:
                price = broker.price(position.symbol)
            except Exception:
                price = None
            if not self._is_dust(position, total, price):
                result['detail'] = f'base total {total} is not dust'
                return result
            result['opposite_terminal'] = True
            if filled:
                tag, _status, order = filled[0]
                executed = Decimal(str(order.get('executedQty') or '0'))
                quote = Decimal(str(order.get('cummulativeQuoteQty') or '0'))
                fill_price = quote / executed if executed > 0 and quote > 0 else Decimal(
                    str(order.get('price') or '0'))
                if executed <= 0 or fill_price <= 0:
                    result['detail'] = f'{tag} fill quantities are malformed'
                    return result
                result.update(filled=True, fill_price=fill_price, which=tag)
            result['ok'] = True
            result['detail'] = ('filled exit and opposite terminal verified'
                                if filled else 'zero inventory and both legs terminal verified')
            return result
        except Exception as exc:
            result['detail'] = f'terminal-list verification failed: {exc}'
            return result

    def _terminal_single_exit_evidence(self, broker, position, *, fact=None,
                                       client_order_id: str = '') -> dict:
        result = {'ok': False, 'filled': False, 'fill_price': Decimal('0'), 'fact': None}
        try:
            if fact is None:
                if client_order_id:
                    fact = broker.c.get_order(
                        symbol=position.symbol, origClientOrderId=client_order_id)
                    broker._sync_weight()
                    if not isinstance(fact, dict) or not fact.get('orderId'):
                        raise RuntimeError('malformed client-order-id lookup')
                elif getattr(position, 'exit_order_id', None):
                    fact = self._query_order(broker, position.symbol, position.exit_order_id)
                else:
                    raise RuntimeError('no exit order identifier')
            result['fact'] = fact
            if self._unsafe_exchange_fact(fact):
                result['detail'] = 'exit order has unsafe expiry semantics'
                return result
            status = self._order_status(fact)
            if status not in {'FILLED', 'CANCELED', 'EXPIRED', 'REJECTED'}:
                result['detail'] = f'exit order is not terminal ({status or "UNKNOWN"})'
                return result
            balance = self.strict_balance_snapshot(position.sym.base, broker=broker, force=True)
            result['balance'] = balance
            if balance['status'] != 'KNOWN':
                result['detail'] = 'base total balance is UNKNOWN'
                return result
            total = Decimal(balance['total'])
            try:
                price = broker.price(position.symbol)
            except Exception:
                price = None
            if not self._is_dust(position, total, price):
                result['detail'] = f'base total {total} is not dust'
                return result
            if status == 'FILLED':
                executed = Decimal(str(fact.get('executedQty') or '0'))
                quote = Decimal(str(fact.get('cummulativeQuoteQty') or '0'))
                order_price = Decimal(str(fact.get('price') or '0'))
                fill_price = quote / executed if executed > 0 and quote > 0 else order_price
                if (not executed.is_finite() or executed <= 0 or
                        not fill_price.is_finite() or fill_price <= 0):
                    result['detail'] = 'FILLED exit has malformed execution quantities'
                    return result
                result.update(filled=True, fill_price=fill_price)
            result['ok'] = True
            result['detail'] = ('filled single exit and total dust verified'
                                if result['filled'] else
                                'terminal single exit and zero inventory verified')
            return result
        except Exception as exc:
            result['detail'] = f'single-exit verification failed: {exc}'
            return result

    def _mark_reprotect_required(self, position, key: str, reason: str, kind='reprotect',
                                 details=None) -> None:
        position.replacing_protection = True
        position._sidecar_lifecycle = LifecycleState.REPROTECT_REQUIRED.value
        self._latch_safety(key, reason, position.symbol, kind, details)
        pf = getattr(self.trader, 'pf', None)
        if pf:
            try:
                pf.save()
            except Exception:
                pass
        self.mirror_positions('REPROTECT_REQUIRED')

    def _reconcile_position_interposed(self, portfolio, position) -> bool:
        broker = portfolio.b
        key = f'offline-fill:{position.symbol}'
        if position.bracket and position.order_list_id:
            try:
                order_list = self._query_list(broker, position.order_list_id)
            except Exception as exc:
                self._mark_reprotect_required(
                    position, key, f'offline order-list status UNKNOWN: {exc}', 'reconciliation')
                return True
            if self._unsafe_exchange_fact(order_list):
                position._sidecar_lifecycle = LifecycleState.RECONCILIATION_REQUIRED.value
                self._latch_safety(key, 'unsafe expiry semantics require reconciliation',
                                   position.symbol, 'reconciliation', order_list)
                return True
            if str(order_list.get('listOrderStatus') or '').upper() == 'ALL_DONE':
                evidence = self._terminal_list_exit_evidence(broker, position)
                if evidence['ok']:
                    if evidence['filled'] and position.entry_price > 0:
                        portfolio._book_close(position, evidence['fill_price'])
                    return False
                self._mark_reprotect_required(
                    position, key, evidence['detail'], 'reconciliation', evidence.get('facts'))
                return True
        exit_order_id = getattr(position, 'exit_order_id', None)
        if exit_order_id:
            try:
                exit_fact = self._query_order(broker, position.symbol, exit_order_id)
            except Exception as exc:
                self._mark_reprotect_required(
                    position, key,
                    f'offline known-order status UNKNOWN for {exit_order_id}: {exc}',
                    'reconciliation', {'order_id': exit_order_id})
                return True
            if self._unsafe_exchange_fact(exit_fact):
                position._sidecar_lifecycle = LifecycleState.RECONCILIATION_REQUIRED.value
                self._latch_safety(key, 'unsafe expiry semantics require reconciliation',
                                   position.symbol, 'reconciliation', exit_fact)
                return True
            if self._order_status(exit_fact) in {'FILLED', 'CANCELED', 'EXPIRED', 'REJECTED'}:
                evidence = self._terminal_single_exit_evidence(
                    broker, position, fact=exit_fact)
                if evidence['ok']:
                    position._sidecar_lifecycle = (
                        LifecycleState.EXIT_FILLED.value if evidence['filled']
                        else LifecycleState.RECONCILED.value)
                    if evidence['filled'] and position.entry_price > 0:
                        portfolio._book_close(position, evidence['fill_price'])
                    return False
                self._mark_reprotect_required(
                    position, key, evidence['detail'], 'reconciliation', evidence.get('fact'))
                return True
        for order_id in (getattr(position, 'entry_order_id', None),):
            if not order_id:
                continue
            try:
                fact = self._query_order(broker, position.symbol, order_id)
            except Exception as exc:
                self._mark_reprotect_required(
                    position, key,
                    f'offline known-order status UNKNOWN for {order_id}: {exc}',
                    'reconciliation', {'order_id': order_id})
                return True
            if self._unsafe_exchange_fact(fact):
                position._sidecar_lifecycle = LifecycleState.RECONCILIATION_REQUIRED.value
                self._latch_safety(key, 'unsafe expiry semantics require reconciliation',
                                   position.symbol, 'reconciliation', fact)
                return True
        keep = _ORIGINAL_RECONCILE_POSITION(portfolio, position)
        if keep and Decimal(str(getattr(position, 'filled_qty', 0) or 0)) > 0:
            live, detail, facts = self._verify_live_protection(broker, position)
            if not live:
                self._mark_reprotect_required(
                    position, key, 'offline entry/fill lacks verified live protection: ' + detail,
                    'reprotect', facts)
        return keep

    def _bracket_fill_interposed(self, trader, symbol: str, position) -> tuple:
        evidence = self._terminal_list_exit_evidence(trader.broker, position)
        if not evidence['ok'] or not evidence['filled'] or not evidence['opposite_terminal']:
            key = f'order-list:{position.symbol}'
            self._mark_reprotect_required(
                position, key, 'ALL_DONE is not sufficient close evidence: ' + evidence['detail'],
                'reconciliation', evidence.get('facts'))
            raise BalanceSnapshotUnavailable(evidence['detail'])
        return evidence['fill_price'], evidence['which']

    def _resolve_symbol_reprotect_halts(self, symbol: str, resolution: str) -> None:
        if not self.state_store:
            return
        for key, incident in list(self.state_store.safety_halts().items()):
            if str(incident.get('symbol') or '').upper() == symbol.upper() and \
                    str(incident.get('kind')) in {'reprotect', 'reconciliation', 'entry-timeout'}:
                self._resolve_safety(key, resolution)

    def _place_verified_rescue_trail(self, engine, position, key: str) -> bool:
        broker = engine.b
        balance = self.strict_balance_snapshot(position.sym.base, broker=broker, force=True)
        if balance['status'] != 'KNOWN':
            self._mark_reprotect_required(position, key, 'base balance UNKNOWN before rescue',
                                          'reprotect', balance)
            return False
        free = Decimal(balance['free'])
        locked = Decimal(balance['locked'])
        if locked > 0:
            self._mark_reprotect_required(
                position, key, f'base remains locked ({locked}) after confirmed cancellation',
                'reprotect', balance)
            return False
        quantity = legacy.round_down(min(free, position.filled_qty or free), position.sym.step)
        if quantity <= 0:
            self._mark_reprotect_required(position, key, 'no positively sellable quantity for rescue',
                                          'reprotect', balance)
            return False
        delta = broker.clamp_delta(
            position.sym, position.trail_delta or legacy.CFG.INITIAL_TRAIL_DELTA_BIPS)
        current = broker.price(position.symbol)
        limit_price = legacy.round_down(
            current * legacy.bips_mult(-(delta + legacy.CFG.LIMIT_FILL_BUFFER_BIPS)),
            position.sym.tick)
        coid = legacy._new_coid()
        params = {
            'symbol': position.symbol, 'side': 'SELL', 'type': 'STOP_LOSS_LIMIT',
            'quantity': legacy.dstr(quantity), 'price': legacy.dstr(limit_price),
            'trailingDelta': delta, 'timeInForce': 'GTC', 'newClientOrderId': coid,
        }
        self._validate_existing_request(broker, position.sym, quantity, 'order', params)
        self._validate_replacement_filters(broker, position.symbol, 'order', params)
        self._update_intent(key, stage='RESCUE_VALIDATED', client_order_id=coid,
                            endpoint='order', params=params)

        def send():
            value = broker.c.create_order(**params)
            broker._sync_weight()
            return value

        try:
            value = broker._place_idempotent(
                send, lambda: broker._find_order(position.symbol, coid),
                f'sidecar_rescue {position.symbol}')
        except Exception as exc:
            try:
                found = broker._find_order(position.symbol, coid)
            except Exception as lookup_exc:
                self._update_intent(
                    key, stage='RESCUE_SUBMISSION_UNKNOWN', client_order_id=coid,
                    submit_error=str(exc), lookup_error=str(lookup_exc))
                self._mark_reprotect_required(
                    position, key, 'rescue response and recovery lookup are UNKNOWN',
                    'reconciliation')
                return False
            if not found:
                self._mark_reprotect_required(
                    position, key, f'rescue placement outcome UNKNOWN: {exc}', 'reconciliation')
                return False
            value = found
        order_id = int(value.get('orderId') or 0) if isinstance(value, dict) else 0
        if not order_id:
            self._mark_reprotect_required(position, key, 'rescue response lacks order id',
                                          'reconciliation', value)
            return False
        position.exit_order_id = order_id
        position.order_list_id = None
        position.tp_order_id = position.sl_order_id = None
        position.bracket = False
        position.state = legacy.PosState.ARMED_TRAIL
        position._sidecar_lifecycle = LifecycleState.REPROTECT_REQUIRED.value
        position.replacing_protection = True
        try:
            self._update_intent(
                key, stage='RESCUE_ACCEPTED', order_id=order_id,
                client_order_id=coid, response=value)
            engine.pf.save()
        except Exception as exc:
            try:
                self._update_intent(
                    key, stage='RESCUE_ACCEPTED_PERSISTENCE_FAILED',
                    order_id=order_id, error=str(exc))
            except Exception:
                pass
            self._mark_reprotect_required(
                position, key, 'rescue accepted but local persistence failed',
                'reconciliation', {'order_id': order_id})
            return False
        live, detail, facts = self._verify_live_protection(broker, position)
        if not live:
            self._mark_reprotect_required(position, key, detail, 'reconciliation', facts)
            return False
        position.replacing_protection = False
        position._sidecar_lifecycle = LifecycleState.PROTECTION_ACTIVE.value
        engine.pf.save()
        self._resolve_safety(key, 'replacement protection verified live')
        self.mirror_positions('PROTECTION_REPLACED_VERIFIED')
        return True

    def _reprotect_interposed(self, engine, position, current):
        if not getattr(position, 'replacing_protection', False):
            return
        key = f'reprotect:{position.symbol}'
        live, detail, facts = self._verify_live_protection(engine.b, position)
        if live:
            position.replacing_protection = False
            position._sidecar_lifecycle = LifecycleState.PROTECTION_ACTIVE.value
            engine.pf.save()
            self._resolve_symbol_reprotect_halts(position.symbol, detail)
            return
        if position.bracket and position.order_list_id:
            evidence = self._terminal_list_exit_evidence(engine.b, position)
            if evidence['ok']:
                position.replacing_protection = False
                position.state = legacy.PosState.CLOSED
                position._sidecar_lifecycle = (
                    LifecycleState.EXIT_FILLED.value if evidence['filled']
                    else LifecycleState.RECONCILED.value)
                engine.pf.save()
                self._resolve_symbol_reprotect_halts(position.symbol, evidence['detail'])
                return
            self._mark_reprotect_required(position, key, evidence['detail'],
                                          'reconciliation', evidence.get('facts'))
            return
        exit_order_id = getattr(position, 'exit_order_id', None)
        exit_fact = (facts.get('orders') or {}).get(str(exit_order_id)) if exit_order_id else None
        if (not isinstance(exit_fact, dict) or self._unsafe_exchange_fact(exit_fact) or
                self._order_status(exit_fact) not in {
                    'FILLED', 'CANCELED', 'EXPIRED', 'REJECTED'}):
            self._mark_reprotect_required(
                position, key, 'protective-order terminal status is UNKNOWN; no blind replacement',
                'reconciliation', facts)
            return
        terminal_evidence = self._terminal_single_exit_evidence(
            engine.b, position, fact=exit_fact)
        if terminal_evidence['ok']:
            position.replacing_protection = False
            position.state = legacy.PosState.CLOSED
            position._sidecar_lifecycle = (
                LifecycleState.EXIT_FILLED.value if terminal_evidence['filled']
                else LifecycleState.RECONCILED.value)
            engine.pf.save()
            self._resolve_symbol_reprotect_halts(position.symbol, terminal_evidence['detail'])
            return
        if 'is not dust' not in terminal_evidence['detail']:
            self._mark_reprotect_required(
                position, key, terminal_evidence['detail'], 'reconciliation', exit_fact)
            return
        balance = self.strict_balance_snapshot(position.sym.base, broker=engine.b, force=True)
        if balance['status'] != 'KNOWN':
            self._mark_reprotect_required(position, key, 'base balance UNKNOWN during reprotect',
                                          'reconciliation', balance)
            return
        total, free, locked = (Decimal(balance[name]) for name in ('total', 'free', 'locked'))
        if locked > 0 or free < total:
            self._mark_reprotect_required(
                position, key, f'base is still locked ({locked}); no blind replacement',
                'reconciliation', balance)
            return
        if self._is_dust(position, total, Decimal(str(current))):
            position.replacing_protection = False
            position.state = legacy.PosState.CLOSED
            position._sidecar_lifecycle = (
                LifecycleState.EXIT_FILLED.value
                if self._order_status(exit_fact) == 'FILLED'
                else LifecycleState.RECONCILED.value)
            engine.pf.save()
            self._resolve_symbol_reprotect_halts(
                position.symbol, 'terminal exit and total dust verified')
            return
        try:
            self._latch_safety(key, 'verified re-protection required', position.symbol,
                               'reprotect', facts)
            return self._place_verified_rescue_trail(engine, position, key)
        except Exception as exc:
            self._mark_reprotect_required(position, key, str(exc), 'reprotect')

    def _reprice_interposed(self, engine, position):
        if not (position.bracket and position.order_list_id and position.filled_qty > 0):
            return _ORIGINAL_REPRICE(engine, position)
        key = f'cancel-reprice:{position.symbol}'
        try:
            self._mark_reprotect_required(
                position, key, 'partial OTOCO requires verified cancel before rescue',
                'entry-timeout', {'order_list_id': position.order_list_id})
            self._update_intent(key, stage='CANCEL_REQUESTED')
        except Exception:
            return
        confirmed, detail, facts = self._cancel_and_verify_known(engine.b, position)
        self._update_intent(key, stage='CANCEL_VERIFIED' if confirmed else 'CANCEL_UNCERTAIN',
                            verification=detail, facts=facts)
        if not confirmed:
            return
        return self._place_verified_rescue_trail(engine.ex, position, key)

    @staticmethod
    def _apply_entry_fill(position, fact: dict) -> Decimal:
        executed = Decimal(str(fact.get('executedQty') or '0'))
        quote = Decimal(str(fact.get('cummulativeQuoteQty') or '0'))
        if not executed.is_finite() or executed < 0 or not quote.is_finite() or quote < 0:
            raise ValueError('malformed entry fill quantities')
        if executed > 0:
            position.filled_qty = max(position.filled_qty, executed)
            if quote > 0:
                position.entry_price = quote / executed
        return executed

    def _apply_verified_cancel_entry_fact(self, position, facts: dict, key: str) -> bool:
        fact = (facts.get('orders') or {}).get(str(position.entry_order_id))
        if not isinstance(fact, dict) or fact.get('executedQty') in (None, ''):
            self._update_intent(
                key, stage='CANCEL_FACT_MALFORMED',
                error='verified entry-order fact lacks executedQty')
            return False
        try:
            executed = self._apply_entry_fill(position, fact)
            if executed > 0 and position.entry_price <= 0:
                raise ValueError('positive late fill lacks a usable average price')
            return True
        except Exception as exc:
            self._update_intent(key, stage='CANCEL_FACT_MALFORMED', error=str(exc), fact=fact)
            return False

    def _prevalidate_initial_protection(self, engine, position, delta: int, key: str) -> None:
        broker = engine.b
        quantity = legacy.round_down(position.filled_qty, position.sym.step)
        delta = broker.clamp_delta(position.sym, delta)
        activation = legacy.round_down(
            position.entry_price * legacy.bips_mult(
                delta + legacy.CFG.ACTIVATION_MARGIN_BIPS),
            position.sym.tick)
        current = broker.price(position.symbol)
        if current >= activation:
            limit_price = legacy.round_down(
                current * legacy.bips_mult(
                    -(delta + legacy.CFG.LIMIT_FILL_BUFFER_BIPS)),
                position.sym.tick)
            order_type = 'STOP_LOSS_LIMIT'
            params = {
                'symbol': position.symbol, 'side': 'SELL', 'type': order_type,
                'quantity': legacy.dstr(quantity), 'price': legacy.dstr(limit_price),
                'trailingDelta': delta, 'timeInForce': 'GTC',
            }
        else:
            worst_trigger = activation * legacy.bips_mult(-delta)
            limit_price = legacy.round_down(
                worst_trigger * legacy.bips_mult(-legacy.CFG.LIMIT_FILL_BUFFER_BIPS),
                position.sym.tick)
            order_type = 'TAKE_PROFIT_LIMIT'
            params = {
                'symbol': position.symbol, 'side': 'SELL', 'type': order_type,
                'quantity': legacy.dstr(quantity), 'price': legacy.dstr(limit_price),
                'stopPrice': legacy.dstr(activation), 'trailingDelta': delta,
                'timeInForce': 'GTC',
            }
        self._validate_existing_request(broker, position.sym, quantity, 'order', params)
        self._validate_replacement_filters(broker, position.symbol, 'order', params)
        self._update_intent(
            key, stage='INITIAL_PROTECTION_VALIDATED', endpoint='order', params=params)

    @staticmethod
    def _entry_trail_delta(engine, position) -> int:
        try:
            pressure_state, _ratio = legacy.pressure(engine.b, position.symbol)
            if pressure_state == 'STRONG':
                return legacy.CFG.PUMP_TRAIL_DELTA_BIPS
        except Exception:
            pass
        return legacy.CFG.INITIAL_TRAIL_DELTA_BIPS

    def _strict_trailing_sell(self, broker, sym, qty: Decimal, delta_bips: int,
                              *, activation: Decimal | None = None) -> dict:
        """Validate the exact initial trailing payload immediately before send."""
        quantity = legacy.round_down(qty, sym.step)
        delta = broker.clamp_delta(sym, delta_bips)
        coid = legacy._new_coid()
        if activation is None:
            current = broker.price(sym.symbol)
            limit_price = legacy.round_down(
                current * legacy.bips_mult(
                    -(delta + legacy.CFG.LIMIT_FILL_BUFFER_BIPS)),
                sym.tick)
            label = 'immediate_trail'
            params = {
                'symbol': sym.symbol, 'side': 'SELL', 'type': 'STOP_LOSS_LIMIT',
                'quantity': legacy.dstr(quantity),
                'price': legacy.dstr(limit_price), 'trailingDelta': delta,
                'timeInForce': 'GTC', 'newClientOrderId': coid,
            }
        else:
            activation = legacy.round_down(activation, sym.tick)
            worst_trigger = activation * legacy.bips_mult(-delta)
            limit_price = legacy.round_down(
                worst_trigger * legacy.bips_mult(
                    -legacy.CFG.LIMIT_FILL_BUFFER_BIPS),
                sym.tick)
            label = 'activation_trail'
            params = {
                'symbol': sym.symbol, 'side': 'SELL', 'type': 'TAKE_PROFIT_LIMIT',
                'quantity': legacy.dstr(quantity),
                'stopPrice': legacy.dstr(activation),
                'price': legacy.dstr(limit_price), 'trailingDelta': delta,
                'timeInForce': 'GTC', 'newClientOrderId': coid,
            }
        broker._check_lot(sym, quantity)
        self._validate_existing_request(broker, sym, quantity, 'order', params)
        self._validate_replacement_filters(broker, sym.symbol, 'order', params)

        def send():
            result = broker.c.create_order(**params)
            broker._sync_weight()
            return result

        return broker._place_idempotent(
            send, lambda: broker._find_order(sym.symbol, coid),
            f'{label} {sym.symbol}')

    def _entry_fill_ready(self, engine, position, key: str, delta: int | None = None) -> bool:
        if position.bracket:
            live, detail, facts = self._verify_live_protection(engine.b, position)
            if live:
                position.state = legacy.PosState.ARMED_TRAIL
                position.activation = position.entry_price
                position._sidecar_lifecycle = LifecycleState.PROTECTION_ACTIVE.value
                engine.pf.save()
                self._resolve_safety(key, detail)
                return True
            self._mark_reprotect_required(position, key, detail, 'reprotect', facts)
            return False
        try:
            self._mark_reprotect_required(
                position, key, 'filled entry awaiting verified initial protection',
                'reprotect', {'filled_qty': str(position.filled_qty)})
            delta = delta if delta is not None else self._entry_trail_delta(engine, position)
            self._prevalidate_initial_protection(engine, position, delta, key)
            engine.ex.place_initial(position, delta)
        except Exception as exc:
            self._mark_reprotect_required(position, key, str(exc), 'reprotect')
            return False
        live, detail, facts = self._verify_live_protection(engine.b, position)
        if not live:
            self._mark_reprotect_required(position, key, detail, 'reprotect', facts)
            return False
        position._sidecar_lifecycle = LifecycleState.PROTECTION_ACTIVE.value
        engine.pf.save()
        self._resolve_safety(key, detail)
        return True

    def _await_fill_interposed(self, engine, symbol: str):
        task = engine._pending.get(symbol)
        if not task:
            return
        position, started = task['p'], task['t0']
        key = f'entry-timeout:{symbol}'
        while time.time() - started < legacy.CFG.ENTRY_FILL_TIMEOUT_SEC:
            try:
                fact = self._query_order(engine.b, symbol, position.entry_order_id)
                if self._unsafe_exchange_fact(fact):
                    self._mark_reprotect_required(
                        position, key, 'entry has unsafe expiry semantics',
                        'reconciliation', fact)
                    return
                status = self._order_status(fact)
                executed = self._apply_entry_fill(position, fact)
                if status == 'FILLED':
                    self._entry_fill_ready(
                        engine, position, key, self._entry_trail_delta(engine, position))
                    engine._pending.pop(symbol, None)
                    return
                if status in {'CANCELED', 'REJECTED', 'EXPIRED'}:
                    if executed > 0 or position.filled_qty > 0 or (
                            position.bracket and position.order_list_id):
                        self._mark_reprotect_required(
                            position, key, f'entry/list terminal requires verified cancellation ({status})',
                            'entry-timeout', fact)
                        confirmed, detail, facts = self._cancel_and_verify_known(engine.b, position)
                        self._update_intent(key, stage='CANCEL_VERIFIED' if confirmed else 'CANCEL_UNCERTAIN',
                                            verification=detail, facts=facts)
                        if confirmed and not self._apply_verified_cancel_entry_fact(
                                position, facts, key):
                            return
                        if confirmed:
                            if position.filled_qty > 0:
                                if self._place_verified_rescue_trail(engine.ex, position, key):
                                    engine._pending.pop(symbol, None)
                            else:
                                engine.pf.close(symbol, Decimal('0'))
                                engine._pending.pop(symbol, None)
                                self._resolve_safety(
                                    key, 'entry/list cancellation and zero fill verified')
                        return
                    engine.pf.close(symbol, Decimal('0'))
                    engine._pending.pop(symbol, None)
                    return
                if time.time() - started > legacy.CFG.ENTRY_REPRICE_EVERY_SEC * (
                        task.get('repriced', 0) + 1):
                    self._reprice_interposed(engine, position)
                    task['repriced'] = task.get('repriced', 0) + 1
                time.sleep(2)
            except Exception as exc:
                audit('entry_fill_poll_unknown', severity='ERROR', details={
                    'symbol': symbol, 'error': str(exc),
                })
                time.sleep(2)

        try:
            self._mark_reprotect_required(
                position, key, 'entry fill timeout requires verified cancellation',
                'entry-timeout', {'filled_qty': str(position.filled_qty)})
            self._update_intent(key, stage='FINAL_QUERY')
        except Exception:
            return
        try:
            final_fact = self._query_order(engine.b, symbol, position.entry_order_id)
            if self._unsafe_exchange_fact(final_fact):
                self._update_intent(key, stage='UNSAFE_EXPIRY', fact=final_fact)
                return
            self._apply_entry_fill(position, final_fact)
            if self._order_status(final_fact) == 'FILLED':
                if self._entry_fill_ready(
                        engine, position, key, self._entry_trail_delta(engine, position)):
                    engine._pending.pop(symbol, None)
                return
        except Exception as exc:
            self._update_intent(key, stage='FINAL_QUERY_UNKNOWN', error=str(exc))
        confirmed, detail, facts = self._cancel_and_verify_known(engine.b, position)
        self._update_intent(key, stage='CANCEL_VERIFIED' if confirmed else 'CANCEL_UNCERTAIN',
                            verification=detail, facts=facts,
                            filled_qty=str(position.filled_qty))
        if not confirmed:
            return
        if not self._apply_verified_cancel_entry_fact(position, facts, key):
            return
        if position.filled_qty > 0:
            if self._place_verified_rescue_trail(engine.ex, position, key):
                engine._pending.pop(symbol, None)
            return
        engine.pf.close(symbol, Decimal('0'))
        engine._pending.pop(symbol, None)
        self._resolve_safety(key, 'entry cancellation and zero fill positively verified')

    def emergency_exit(self, symbol, reason='manual owner request') -> dict:
        """Halt, cancel, verify, then submit one filter-checked idempotent IOC."""
        symbol = str(symbol or '').upper()
        trader = self.trader
        if not trader.is_running() or not trader.pf or not trader.broker:
            return {'ok': False, 'stage': 'not-started', 'halt_persisted': False,
                    'detail': 'execution core is not started'}
        position = trader.pf.positions.get(symbol)
        if not position:
            return {'ok': False, 'stage': 'not-found', 'halt_persisted': False,
                    'detail': 'no such open position (already closed?)'}
        broker, key = trader.broker, f'emergency:{symbol}'
        try:
            self._latch_safety(key, f'emergency on {symbol}: {reason}', symbol,
                               'emergency', {'stage': 'HALTED_BEFORE_CANCEL'})
            position.state = legacy.PosState.EMERGENCY
            position.replacing_protection = True
            position._sidecar_lifecycle = LifecycleState.RECONCILIATION_REQUIRED.value
            trader.pf.save()
        except Exception as exc:
            audit('emergency_halt_persist_failed', severity='CRITICAL', details={
                'symbol': symbol, 'error': str(exc),
            })
            return {'ok': False, 'stage': 'halt-persist-failed', 'halt_persisted': False,
                    'detail': f'durable halt failed; no exchange action attempted: {exc}'}

        confirmed, detail, facts = self._cancel_and_verify_known(broker, position)
        try:
            self._update_intent(key, stage='CANCEL_VERIFIED' if confirmed else 'CANCEL_UNCERTAIN',
                                verification=detail, facts=facts)
        except Exception as exc:
            return {'ok': False, 'stage': 'intent-persist-failed', 'halt_persisted': True,
                    'detail': f'cancel outcome could not be persisted; no exit submitted: {exc}'}
        if not confirmed:
            return {'ok': False, 'stage': 'cancel-unverified', 'halt_persisted': True,
                    'detail': detail}

        balance = self.strict_balance_snapshot(position.sym.base, broker=broker, force=True)
        if balance['status'] != 'KNOWN':
            self._update_intent(key, stage='BALANCE_UNKNOWN', balance=balance)
            return {'ok': False, 'stage': 'balance-unknown', 'halt_persisted': True,
                    'balance': balance, 'detail': 'base free/locked/total is UNKNOWN'}
        free, locked, total = (Decimal(balance[name]) for name in ('free', 'locked', 'total'))
        try:
            current = broker.price(symbol)
        except Exception as exc:
            self._update_intent(key, stage='PRICE_UNKNOWN', error=str(exc))
            return {'ok': False, 'stage': 'price-unknown', 'halt_persisted': True,
                    'balance': balance, 'detail': str(exc)}
        if self._is_dust(position, total, current):
            position.replacing_protection = False
            position.state = legacy.PosState.CLOSED
            position._sidecar_lifecycle = LifecycleState.RECONCILED.value
            trader.pf.save()
            self._resolve_safety(key, 'known orders terminal and total base balance is dust')
            return {'ok': True, 'stage': 'already-exited', 'halt_persisted': True,
                    'balance': balance, 'detail': 'total base balance is verified dust'}
        if locked > 0 or free < total:
            self._update_intent(key, stage='BALANCE_LOCKED', balance=balance)
            return {'ok': False, 'stage': 'balance-locked', 'halt_persisted': True,
                    'balance': balance, 'detail': f'base remains locked: {locked}'}
        quantity = legacy.round_down(min(free, position.filled_qty or free), position.sym.step)
        if quantity <= 0:
            self._update_intent(key, stage='NOTHING_SELLABLE', balance=balance)
            return {'ok': False, 'stage': 'nothing-to-sell', 'halt_persisted': True,
                    'balance': balance, 'detail': 'no positively sellable base quantity'}

        existing = self.state_store.data.get('recovery_intents', {}).get(key, {})
        coid = str(existing.get('client_order_id') or legacy._new_coid())
        price = legacy.round_down(current * legacy.bips_mult(-100), position.sym.tick)
        params = {
            'symbol': symbol, 'quantity': legacy.dstr(quantity),
            'price': legacy.dstr(price), 'timeInForce': 'IOC',
            'newClientOrderId': coid,
        }
        try:
            self._validate_existing_request(broker, position.sym, quantity, 'order', params)
            filter_params = dict(params, side='SELL', type='LIMIT')
            self._validate_replacement_filters(broker, symbol, 'order', filter_params)
            self._update_intent(key, stage='EXIT_VALIDATED', client_order_id=coid,
                                endpoint='order', params=filter_params)
        except Exception as exc:
            return {'ok': False, 'stage': 'preflight-failed', 'halt_persisted': True,
                    'client_order_id': coid, 'balance': balance, 'detail': str(exc)}

        def send():
            value = broker.c.order_limit_sell(**params)
            broker._sync_weight()
            return value

        submitted, response, submit_error = False, None, ''
        try:
            response = broker._place_idempotent(
                send, lambda: broker._find_order(symbol, coid),
                f'sidecar_emergency {symbol}')
            submitted = True
        except Exception as exc:
            submit_error = str(exc)
            try:
                response = broker._find_order(symbol, coid)
                submitted = bool(response)
            except Exception as lookup_exc:
                self._update_intent(
                    key, stage='EXIT_SUBMISSION_UNKNOWN', client_order_id=coid,
                    submit_error=submit_error, lookup_error=str(lookup_exc))
                return {
                    'ok': False, 'stage': 'submission-unknown',
                    'halt_persisted': True, 'submitted': False,
                    'client_order_id': coid, 'balance_before': balance,
                    'detail': ('IOC response and recovery lookup are UNKNOWN; '
                               'no retry is permitted before reconciliation'),
                }
        order_id = int(response.get('orderId') or 0) if isinstance(response, dict) else 0
        if order_id:
            position.exit_order_id = order_id
            try:
                self._update_intent(
                    key, stage='EXIT_ACCEPTED', order_id=order_id,
                    client_order_id=coid, response=response)
                trader.pf.save()
            except Exception as exc:
                try:
                    self._update_intent(
                        key, stage='EXIT_ACCEPTED_PERSISTENCE_FAILED',
                        order_id=order_id, error=str(exc))
                except Exception:
                    pass
                return {
                    'ok': False, 'stage': 'accepted-persistence-failed',
                    'halt_persisted': True, 'submitted': True,
                    'client_order_id': coid, 'order_id': order_id,
                    'balance_before': balance,
                    'detail': ('IOC was accepted but local persistence failed; '
                               'reconcile by client order ID before any retry'),
                }
        try:
            final_fact = self._query_order(broker, symbol, order_id) if order_id else {}
            final_status = self._order_status(final_fact)
            filled_fact = False
            if final_status == 'FILLED':
                executed_value = Decimal(str(final_fact.get('executedQty') or '0'))
                quote_value = Decimal(str(final_fact.get('cummulativeQuoteQty') or '0'))
                price_value = Decimal(str(final_fact.get('price') or '0'))
                filled_fact = bool(
                    executed_value.is_finite() and executed_value > 0 and
                    quote_value.is_finite() and price_value.is_finite() and
                    (quote_value > 0 or price_value > 0))
            safe_terminal = bool(
                order_id and not self._unsafe_exchange_fact(final_fact) and
                (final_status in {'CANCELED', 'EXPIRED', 'REJECTED'} or filled_fact))
        except Exception as exc:
            final_fact, safe_terminal = {'error': str(exc)}, False
        final_balance = self.strict_balance_snapshot(position.sym.base, broker=broker, force=True)
        verified_dust = (final_balance['status'] == 'KNOWN' and
                         self._is_dust(position, Decimal(final_balance['total']), current))
        ok = bool(submitted and safe_terminal and verified_dust)
        status = self._order_status(final_fact) or 'UNKNOWN'
        executed = str(final_fact.get('executedQty') or '0') if isinstance(final_fact, dict) else '0'
        try:
            self._update_intent(key, stage='EXIT_VERIFIED' if ok else 'EXIT_UNCERTAIN',
                                order_id=order_id, order_fact=final_fact,
                                final_balance=final_balance, submitted=submitted)
        except Exception as exc:
            return {
                'ok': False, 'stage': 'verification-persist-failed',
                'halt_persisted': True, 'submitted': submitted,
                'client_order_id': coid, 'order_id': order_id,
                'balance_after': final_balance,
                'detail': f'exit verification could not be persisted: {exc}',
            }
        if ok:
            position.replacing_protection = False
            position.state = legacy.PosState.CLOSED
            position._sidecar_lifecycle = (
                LifecycleState.EXIT_FILLED.value if filled_fact
                else LifecycleState.RECONCILED.value)
            trader.pf.save()
            self._resolve_safety(key, 'IOC terminal fact and total dust balance verified')
        result = {
            'ok': ok, 'stage': 'verified-exit' if ok else 'partial-or-unverified',
            'halt_persisted': True, 'submitted': submitted,
            'client_order_id': coid, 'order_id': order_id, 'order_status': status,
            'executed_qty': executed, 'balance_before': balance,
            'balance_after': final_balance,
            'detail': ('exit and total dust verified; owner resume is still required' if ok else
                       f'exit remains uncertain ({submit_error or status}); reconciliation required'),
        }
        audit('emergency_exit_result', severity='INFO' if ok else 'CRITICAL', details=result)
        return result

    def mirror_positions(self, status='MATCHED'):
        if not self.state_store or not self.trader.pf:
            return 0
        count = 0
        for symbol, position in list(self.trader.pf.positions.items()):
            self.state_store.mirror_legacy_position(symbol, position)
            count += 1
        self.state_store.data['last_reconciliation_status'] = status
        self.state_store.data['last_reconciliation_at'] = time.time()
        self.state_store.save()
        return count

    def _service_reconcile_positions(self) -> list[str]:
        """Verify every held position has protection or terminal close proof."""
        issues = []
        trader = self.trader
        for symbol, position in list(getattr(trader.pf, 'positions', {}).items()):
            filled = Decimal(str(getattr(position, 'filled_qty', 0) or 0))
            if filled <= 0:
                continue
            emergency_incidents = []
            if self.state_store:
                emergency_incidents = [
                    (key, incident) for key, incident in self.state_store.safety_halts().items()
                    if str(incident.get('symbol') or '').upper() == symbol.upper()
                    and str(incident.get('kind')) == 'emergency'
                ]
            if emergency_incidents:
                emergency_ok = True
                emergency_evidence = None
                for key, _incident in emergency_incidents:
                    intent = self.state_store.data.get('recovery_intents', {}).get(key, {})
                    client_id = str(intent.get('client_order_id') or '')
                    emergency_evidence = self._terminal_single_exit_evidence(
                        trader.broker, position, client_order_id=client_id)
                    if not emergency_evidence['ok']:
                        self._update_intent(
                            key, stage='EMERGENCY_RECONCILIATION_REQUIRED',
                            verification=emergency_evidence['detail'])
                        issues.append(f'{symbol}: {emergency_evidence["detail"]}')
                        emergency_ok = False
                        break
                if emergency_ok and emergency_evidence:
                    position.state = legacy.PosState.CLOSED
                    position.replacing_protection = False
                    position._sidecar_lifecycle = (
                        LifecycleState.EXIT_FILLED.value if emergency_evidence['filled']
                        else LifecycleState.RECONCILED.value)
                    self.mirror_positions('EMERGENCY_EXIT_RECONCILED')
                    if emergency_evidence['filled']:
                        trader.pf.close(symbol, emergency_evidence['fill_price'])
                    else:
                        with trader.pf.lock:
                            trader.pf.positions.pop(symbol, None)
                            trader.pf.save()
                    for key, _incident in emergency_incidents:
                        self._resolve_safety(key, emergency_evidence['detail'])
                    continue
                continue
            live, detail, facts = self._verify_live_protection(trader.broker, position)
            if live:
                position.replacing_protection = False
                position._sidecar_lifecycle = LifecycleState.PROTECTION_ACTIVE.value
                self._resolve_symbol_reprotect_halts(symbol, detail)
                continue
            if position.bracket and position.order_list_id:
                evidence = self._terminal_list_exit_evidence(trader.broker, position)
                if evidence['ok'] and evidence['filled']:
                    trader.pf.close(symbol, evidence['fill_price'])
                    self._resolve_symbol_reprotect_halts(symbol, evidence['detail'])
                    continue
                if evidence['ok'] and not evidence['filled']:
                    position.state = legacy.PosState.CLOSED
                    position._sidecar_lifecycle = LifecycleState.RECONCILED.value
                    self.mirror_positions('ZERO_INVENTORY_RECONCILED')
                    with trader.pf.lock:
                        trader.pf.positions.pop(symbol, None)
                        trader.pf.save()
                    self._resolve_symbol_reprotect_halts(symbol, evidence['detail'])
                    continue
            if position.exit_order_id:
                evidence = self._terminal_single_exit_evidence(trader.broker, position)
                if evidence['ok']:
                    position.state = legacy.PosState.CLOSED
                    position.replacing_protection = False
                    position._sidecar_lifecycle = (
                        LifecycleState.EXIT_FILLED.value if evidence['filled']
                        else LifecycleState.RECONCILED.value)
                    self.mirror_positions('SINGLE_EXIT_RECONCILED')
                    if evidence['filled']:
                        trader.pf.close(symbol, evidence['fill_price'])
                    else:
                        with trader.pf.lock:
                            trader.pf.positions.pop(symbol, None)
                            trader.pf.save()
                    self._resolve_symbol_reprotect_halts(symbol, evidence['detail'])
                    continue
            key = f'reconcile:{symbol}'
            reason = 'held inventory lacks verified live protection: ' + detail
            self._mark_reprotect_required(position, key, reason, 'reconciliation', facts)
            issues.append(f'{symbol}: {detail}')
        try:
            trader.pf.save()
        except Exception:
            pass
        return issues

    def verified_reconcile(self) -> dict:
        """Endpoint-complete reconciliation (fixes H-005).

        The preserved broker's open_orders/open_order_lists helpers swallow
        every exception and return an empty list, which is indistinguishable
        from a genuinely empty exchange. This method enumerates through the
        underlying client directly so a failed endpoint is reported as a
        FAILURE, keeps entries impossible, and never claims success.
        """
        if not self.trader.pf or not self.trader.broker:
            return {'ok': False, 'detail': 'not started', 'endpoints': {}}
        broker = self.trader.broker
        endpoints: dict[str, dict] = {}

        def probe(name, call):
            try:
                value = call()
                broker._sync_weight()
                if not isinstance(value, list):
                    raise RuntimeError(f'unexpected {name} response type {type(value).__name__}')
                endpoints[name] = {'ok': True, 'count': len(value)}
                return value
            except Exception as exc:
                endpoints[name] = {'ok': False, 'error': str(exc)}
                return None

        open_orders = probe('openOrders', lambda: broker.c.get_open_orders())
        open_lists = probe('openOrderList', lambda: broker.c._get('openOrderList', True, data={}))
        try:
            account = broker.c.get_account()
            broker._sync_weight()
            balances = account.get('balances') if isinstance(account, dict) else None
            if not isinstance(balances, list):
                raise RuntimeError('unexpected account response')
            for item in balances:
                if not isinstance(item, dict) or not item.get('asset'):
                    raise RuntimeError('malformed account balance row')
                self._decimal(item.get('free'), f'{item["asset"]}.free')
                self._decimal(item.get('locked'), f'{item["asset"]}.locked')
            endpoints['account'] = {'ok': True, 'balances': len(balances)}
        except Exception as exc:
            endpoints['account'] = {'ok': False, 'error': str(exc)}

        endpoint_ok = all(entry.get('ok') for entry in endpoints.values())
        position_issues = []
        remaining_halts = self.state_store.safety_halts() if self.state_store else {}
        mirrored = 0
        if endpoint_ok:
            self.trader._uds_resync()
            time.sleep(0.15)
            try:
                position_issues = self._service_reconcile_positions()
            except Exception as exc:
                position_issues = [f'position reconciliation failed: {exc}']
            remaining_halts = self.state_store.safety_halts() if self.state_store else {}
            all_ok = not position_issues and not remaining_halts
            mirrored = self.mirror_positions(
                'RECONCILED_ENDPOINTS_VERIFIED' if all_ok else 'RECONCILIATION_REQUIRED')
        else:
            all_ok = False
        if all_ok:
            audit('reconciliation_verified', details={'endpoints': endpoints, 'mirrored': mirrored})
            return {'ok': True, 'endpoints': endpoints, 'mirrored': mirrored,
                    'open_orders': open_orders is not None and len(open_orders),
                    'open_order_lists': open_lists is not None and len(open_lists),
                    'detail': f'all endpoints verified; mirrored {mirrored} position(s)'}
        if self.state_store:
            try:
                self.state_store.data['last_reconciliation_status'] = 'RECONCILIATION_FAILED'
                self.state_store.data['last_reconciliation_at'] = time.time()
                self.state_store.save()
            except Exception:
                pass
        audit('reconciliation_failed', severity='CRITICAL', details={
            'endpoints': endpoints, 'position_issues': position_issues,
            'safety_halts': remaining_halts,
        })
        failed = sorted(name for name, entry in endpoints.items() if not entry.get('ok'))
        if position_issues:
            failed.append('positions')
        if remaining_halts:
            failed.append('safety_halts')
        return {'ok': False, 'endpoints': endpoints,
                'detail': 'reconciliation FAILED — exchange enumeration incomplete on: ' + ', '.join(failed)}

    def reconcile(self):
        result = self.verified_reconcile()
        return result['detail'] if result['ok'] else 'failed: ' + result['detail']

    @staticmethod
    def _validate_existing_request(broker, sym, qty: Decimal, endpoint: str, params: dict) -> None:
        """Validate a replacement request before the active protection is cancelled."""
        qty = Decimal(str(qty))
        tick = Decimal(str(sym.tick)) if str(sym.tick) not in ('', '0') else Decimal('0')
        step = Decimal(str(sym.step)) if str(sym.step) not in ('', '0') else Decimal('0')
        minimum = Decimal(str(sym.min_notional)) if str(sym.min_notional) not in ('', '0') else Decimal('0')
        if qty <= 0:
            raise ValueError('replacement quantity must be positive')
        if step > 0 and qty % step != 0:
            raise ValueError(f'replacement quantity {qty} is not a multiple of stepSize {sym.step}')
        broker._check_lot(sym, qty)

        price_fields = ['price'] if endpoint == 'order' else ['abovePrice', 'belowPrice']
        prices: list[Decimal] = []
        for field in price_fields:
            if field not in params:
                raise ValueError(f'missing replacement price field: {field}')
            price = Decimal(str(params[field]))
            if price <= 0:
                raise ValueError(f'replacement {field} must be positive')
            if tick > 0 and price % tick != 0:
                raise ValueError(f'replacement {field} {price} is not a multiple of tickSize {sym.tick}')
            if minimum > 0 and price * qty < minimum:
                raise ValueError(f'replacement {field} notional {price * qty} is below {sym.min_notional}')
            prices.append(price)

        for field in ('belowStopPrice',):
            if field in params:
                trigger = Decimal(str(params[field]))
                if trigger <= 0 or (tick > 0 and trigger % tick != 0):
                    raise ValueError(f'invalid replacement {field}: {trigger}')
        delta_value = params.get('trailingDelta', params.get('belowTrailingDelta'))
        if delta_value is not None:
            delta = int(delta_value)
            if not (int(sym.trail_min) <= delta <= int(sym.trail_max)):
                raise ValueError(
                    f'replacement trailingDelta {delta} outside [{sym.trail_min},{sym.trail_max}]'
                )

    def convert(self, symbol: str, mode: ProtectionMode, *, break_even=False, lock_profit_pct=None):
        """Best-effort cancel-and-replace protection conversion.

        Binance Spot has no atomic order-list conversion primitive. The method therefore:
        prevalidates, persists a replacement intent, pauses all entries, cancels the old
        protection, places the deterministic replacement, and invokes emergency re-protection
        on any failure. It never represents this transition as gap-free.
        """
        trader = self.trader
        position = trader.pf.positions.get(symbol) if trader.pf else None
        if not position:
            return False, 'position not found'
        broker = trader.broker
        exit_engine = trader.exit
        current = broker.price(symbol)
        entry = position.entry_price or current
        held = broker.free(position.sym.base)
        qty = legacy.round_down(min(held, position.filled_qty or held), position.sym.step)
        if qty <= 0:
            return False, 'no free quantity available for replacement'
        stop_pct = 0.0 if break_even else 2.0
        if lock_profit_pct is not None:
            stop_pct = -abs(float(lock_profit_pct))
        requested_stop = entry * (Decimal(1) - Decimal(str(stop_pct)) / 100)
        if mode is not ProtectionMode.TRAILING_ONLY and requested_stop >= current:
            return False, 'requested fixed stop is not below current price; conversion refused before cancellation'
        endpoint, params = self.factory.existing(
            mode, position.sym, qty, entry, current, legacy.CFG.OTOCO_TP_PCT,
            stop_pct, position.trail_delta or legacy.CFG.INITIAL_TRAIL_DELTA_BIPS
        )
        self._validate_existing_request(broker, position.sym, qty, endpoint, params)
        # V101-NEW-002: validate against the COMPLETE current exchange filter
        # set (price bands, notional max, order/list capacity, reference
        # price) BEFORE the active protection is cancelled. Unavailable or
        # stale filter data refuses the conversion — fail closed.
        self._validate_replacement_filters(broker, position.sym.symbol, endpoint, params)
        # C-001: durably persist the replacement intent BEFORE any cancel; if
        # this persistence fails the conversion is refused pre-cancellation.
        self._persist_replacement_intent(symbol, endpoint, '', mode.value)
        # Persist the full intended replacement before touching the active protection.
        with trader.pf.lock:
            trader.pf.autotrade_on = False
            legacy._set_entries_armed(False)
            position.replacing_protection = True
            trader.pf.save()
        audit('protection_replacement_intent', severity='WARNING', details={
            'symbol': symbol, 'mode': mode.value, 'endpoint': endpoint,
            'limitation': 'cancel-and-replace is not atomic on Binance Spot order lists'
        })
        replacement_accepted = False
        replacement_client_id = ''
        try:
            if position.order_list_id:
                broker.cancel_order_list(symbol, position.order_list_id)
            elif position.exit_order_id:
                broker.cancel(symbol, position.exit_order_id)
            time.sleep(0.25)
            broker.invalidate_balance_cache()
            available = broker.free(position.sym.base)
            qty = legacy.round_down(min(available, position.filled_qty or available), position.sym.step)
            if qty <= 0:
                raise RuntimeError('quantity unavailable after cancellation')
            # Rebuild with exact free quantity after cancellation.
            endpoint, params = self.factory.existing(
                mode, position.sym, qty, entry, broker.price(symbol), legacy.CFG.OTOCO_TP_PCT,
                stop_pct, position.trail_delta or legacy.CFG.INITIAL_TRAIL_DELTA_BIPS
            )
            self._validate_existing_request(broker, position.sym, qty, endpoint, params)
            # F23E-001: the pre-cancel filter check used the price and quantity
            # from BEFORE cancellation. Re-run the COMPLETE current-filter
            # validation (with a freshly fetched reference price) against the
            # rebuilt replacement immediately before submission. If the market
            # moved or the reference price changed, Binance would otherwise
            # reject the replacement and leave the position unprotected; a
            # failure here drops into emergency re-protection below instead.
            self._validate_replacement_filters(broker, position.sym.symbol, endpoint, params)
            coid = params.get('listClientOrderId') or params.get('newClientOrderId')
            replacement_client_id = str(coid or '')
            # C-001: the deterministic client id is durably recorded BEFORE the
            # network submission so recovery can always query exactly this id.
            self._persist_replacement_intent(symbol, endpoint, replacement_client_id, mode.value)
            def send():
                out = broker.c._post(endpoint, True, data=params)
                broker._sync_weight()
                return out
            lookup = (lambda: broker._find_list(coid)) if endpoint.startswith('orderList/') else (lambda: broker._find_order(symbol, coid))
            out = broker._place_idempotent(send, lookup, f'convert {mode.value} {symbol}')
            replacement_accepted = True
            position.list_client_id = str(out.get('listClientOrderId') or replacement_client_id)
            position.order_list_id = int(out.get('orderListId', 0)) or None
            if endpoint.startswith('orderList/'):
                _, tp_id, sl_id = broker.parse_otoco_ids(out)
            else:
                tp_id, sl_id = None, int(out.get('orderId', 0))
            position.tp_order_id = tp_id
            position.sl_order_id = sl_id
            position.exit_order_id = sl_id
            position.bracket = endpoint.startswith('orderList/')
            position.replacing_protection = False
            position.uncapped = mode is ProtectionMode.TRAILING_ONLY
            # Sidecar-only metadata is intentionally kept outside the preserved
            # V4.9.16 serializer. The SQLite state store remains authoritative
            # across process restarts.
            position._sidecar_protection_mode = mode.value
            position._sidecar_protected_quantity = str(qty)
            if break_even:
                lifecycle = 'BREAK_EVEN_ARMED'
                description = 'gross break-even fixed OCO (before fees and slippage)'
            elif lock_profit_pct is not None:
                lifecycle = 'PROFIT_LOCKED'
                description = f'fixed OCO locking {abs(float(lock_profit_pct)):.4g}% gross profit'
            elif mode in (ProtectionMode.TRAILING_ONLY, ProtectionMode.OCO_TRAILING):
                lifecycle = 'TRAILING_ACTIVE'
                description = mode.value
            else:
                lifecycle = 'PROTECTION_ACTIVE'
                description = mode.value
            position._sidecar_lifecycle = lifecycle
            trader.pf.save()
            self._clear_replacement_intent()
            self.mirror_positions('PROTECTION_REPLACED')
            audit('protection_converted', details={
                'symbol': symbol, 'mode': mode.value, 'lifecycle': lifecycle,
                'break_even_is_gross': bool(break_even),
            })
            return True, f'{symbol} protection changed to {description}'
        except Exception as exc:
            trader.pf.protection_halt = f'conversion failed for {symbol}: {exc}'
            if replacement_accepted:
                # The exchange placement returned successfully. A later parsing or
                # persistence failure must never trigger a blind second sell order.
                # Keep replacement intent latched and force exchange reconciliation.
                position.replacing_protection = True
                position.list_client_id = position.list_client_id or replacement_client_id
                try:
                    trader.pf.save()
                except Exception as persist_exc:
                    audit('accepted_replacement_persistence_failed', severity='CRITICAL', details={
                        'symbol': symbol, 'client_order_id': replacement_client_id,
                        'error': str(persist_exc),
                    })
                try:
                    trader._uds_resync()
                except Exception as reconcile_exc:
                    audit('accepted_replacement_reconcile_failed', severity='CRITICAL', details={
                        'symbol': symbol, 'client_order_id': replacement_client_id,
                        'error': str(reconcile_exc),
                    })
                audit('protection_conversion_outcome_uncertain', severity='CRITICAL', details={
                    'symbol': symbol, 'client_order_id': replacement_client_id, 'error': str(exc),
                    'action': 'no blind reprotection; exchange reconciliation required',
                })
                return False, 'replacement was accepted but local persistence is incomplete; reconcile before any retry'

            # C-001 fix: an exception from _place_idempotent does NOT prove the
            # replacement is absent — Binance may have accepted it while the
            # response (and the recovery lookup) was lost. Re-protection is
            # only safe after the exchange CONFIRMS the deterministic client
            # id does not exist; every other outcome is reconciliation-only.
            absent, absence_detail = self._replacement_confirmed_absent(
                broker, symbol, endpoint, replacement_client_id)
            if not absent:
                position.replacing_protection = True
                position.list_client_id = position.list_client_id or replacement_client_id
                try:
                    trader.pf.save()
                except Exception as persist_exc:
                    audit('uncertain_replacement_persistence_failed', severity='CRITICAL', details={
                        'symbol': symbol, 'client_order_id': replacement_client_id,
                        'error': str(persist_exc),
                    })
                try:
                    trader._uds_resync()
                except Exception as reconcile_exc:
                    audit('uncertain_replacement_reconcile_failed', severity='CRITICAL', details={
                        'symbol': symbol, 'error': str(reconcile_exc),
                    })
                audit('protection_conversion_outcome_uncertain', severity='CRITICAL', details={
                    'symbol': symbol, 'client_order_id': replacement_client_id, 'error': str(exc),
                    'absence_check': absence_detail,
                    'action': 'no blind reprotection; exchange reconciliation required',
                })
                return False, ('replacement outcome is uncertain (' + absence_detail +
                               '); no blind re-protection — reconcile before any retry')

            try:
                trader.pf.save()
            except Exception as persist_exc:
                audit('conversion_failure_state_persist_failed', severity='CRITICAL', details={
                    'symbol': symbol, 'error': str(persist_exc),
                })
            audit('protection_conversion_failed', severity='CRITICAL', details={
                'symbol': symbol, 'error': str(exc), 'absence_check': absence_detail,
            })
            try:
                exit_engine.reprotect_if_naked(position, broker.price(symbol))
            except Exception as reprotect_exc:
                audit('emergency_reprotect_failed', severity='CRITICAL', details={'symbol': symbol, 'error': str(reprotect_exc)})
            return False, f'conversion failed; entries remain paused: {exc}'

    def _validate_replacement_filters(self, broker, symbol: str, endpoint: str, params: dict):
        """Complete-filter preflight (V101-NEW-002); raises before any cancel."""
        validator = getattr(self, 'filter_validator', None)
        if validator is None:
            validator = SpotFilterValidator()
            self.filter_validator = validator

        def open_orders_for(sym_name):
            out = broker.c.get_open_orders(symbol=sym_name)
            broker._sync_weight()
            if not isinstance(out, list):
                raise RuntimeError('unexpected openOrders response')
            return out

        def open_lists():
            out = broker.c._get('openOrderList', True, data={})
            broker._sync_weight()
            if not isinstance(out, list):
                raise RuntimeError('unexpected openOrderList response')
            return out

        validator.validate_replacement(
            symbol, endpoint, params,
            open_orders_provider=open_orders_for,
            open_order_lists_provider=open_lists,
        )

    def _persist_replacement_intent(self, symbol: str, endpoint: str, client_id: str, mode: str):
        if not self.state_store:
            return
        try:
            self.state_store.data['replacement_intent'] = {
                'symbol': symbol, 'endpoint': endpoint,
                'client_order_id': client_id, 'mode': mode, 'at': time.time(),
            }
            self.state_store.save()
        except Exception as exc:
            raise RuntimeError(f'could not durably persist replacement intent: {exc}') from exc

    def _clear_replacement_intent(self):
        if not self.state_store:
            return
        try:
            self.state_store.data.pop('replacement_intent', None)
            self.state_store.save()
        except Exception as exc:
            audit('replacement_intent_clear_failed', severity='WARNING', details={'error': str(exc)})

    def _replacement_confirmed_absent(self, broker, symbol: str, endpoint: str,
                                      client_id: str) -> tuple[bool, str]:
        """CONFIRMED-ABSENT / EXISTS / UNCERTAIN classification for C-001."""
        if not client_id:
            return True, 'replacement was never submitted (no client id generated)'
        api_error = getattr(legacy, 'BinanceAPIException', None)
        try:
            if endpoint.startswith('orderList/'):
                found = broker.c._get('orderList', True, data={'origClientOrderId': client_id})
                exists = isinstance(found, dict) and found.get('orderListId')
            else:
                found = broker.c.get_order(symbol=symbol, origClientOrderId=client_id)
                exists = isinstance(found, dict) and found.get('orderId')
            broker._sync_weight()
            if exists:
                return False, 'replacement order EXISTS on the exchange'
            return False, 'lookup returned no identifier — treated as uncertain'
        except Exception as exc:
            code = getattr(exc, 'code', None)
            if api_error is not None and isinstance(exc, api_error) and code in (-2013, -2018):
                return True, f'exchange confirmed absence (code {code})'
            return False, f'absence could not be confirmed: {exc}'
