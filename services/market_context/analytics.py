from __future__ import annotations

"""Bounded, Decimal-safe analytics for Binance Spot public market streams."""

import re
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

SYMBOL_RE = re.compile(r"[A-Z0-9]{2,24}USDT")
WINDOWS = (10, 30, 60)
MAX_SYMBOLS = 50


class MarketDataError(ValueError):
    """A malformed, non-finite, stale, duplicate, or out-of-order event."""


def _decimal(value: object, field_name: str, *, allow_zero: bool = False) -> Decimal:
    if value is None or isinstance(value, bool):
        raise MarketDataError(f"{field_name} is missing or boolean")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise MarketDataError(f"{field_name} is not decimal") from exc
    if not parsed.is_finite() or parsed < 0 or (not allow_zero and parsed == 0):
        raise MarketDataError(f"{field_name} is not a permitted finite value")
    return parsed


def _integer(value: object, field_name: str, *, positive: bool = False) -> int:
    if isinstance(value, bool):
        raise MarketDataError(f"{field_name} is boolean")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise MarketDataError(f"{field_name} is not an integer") from exc
    if parsed < (1 if positive else 0):
        raise MarketDataError(f"{field_name} is outside the permitted range")
    return parsed


def _symbol(value: object) -> str:
    symbol = str(value or "")
    if not SYMBOL_RE.fullmatch(symbol):
        raise MarketDataError("invalid Spot/USDT symbol identity")
    return symbol


def _text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _iso(epoch: float) -> str:
    return (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


@dataclass
class _FlowBucket:
    second: int
    buy_quote: Decimal = Decimal(0)
    sell_quote: Decimal = Decimal(0)
    trade_count: int = 0


@dataclass
class _SymbolState:
    buckets: deque[_FlowBucket] = field(default_factory=deque)
    last_agg_id: int | None = None
    last_agg_event_ms: int | None = None
    last_agg_received_mono: float | None = None
    last_agg_received_wall: float | None = None
    cvd_quote_session: Decimal = Decimal(0)
    session_started_wall: float | None = None
    book_update_id: int | None = None
    bid: Decimal | None = None
    bid_qty: Decimal | None = None
    ask: Decimal | None = None
    ask_qty: Decimal | None = None
    book_received_mono: float | None = None
    book_received_wall: float | None = None


class SpotMicrostructureAnalytics:
    """Aggregate one-second flow buckets for at most fifty active symbols.

    One-second buckets bound memory independently of trade rate.  The public
    feed can emit many aggregate trades per second; retaining every event for
    fifty symbols would make an Oracle Free deployment unnecessarily fragile.
    """

    def __init__(
        self,
        *,
        max_age_ms: int = 5_000,
        max_event_lag_ms: int = 120_000,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        if not 250 <= int(max_age_ms) <= 60_000:
            raise ValueError("max_age_ms must be within 250-60000")
        if not 1_000 <= int(max_event_lag_ms) <= 600_000:
            raise ValueError("max_event_lag_ms must be within 1000-600000")
        self.max_age_ms = int(max_age_ms)
        self.max_event_lag_ms = int(max_event_lag_ms)
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._active: set[str] = set()
        self._states: dict[str, _SymbolState] = {}
        self._lock = threading.RLock()
        self._stats = {
            "accepted_agg_trades": 0,
            "accepted_book_tickers": 0,
            "malformed_messages": 0,
            "duplicate_messages": 0,
            "out_of_order_messages": 0,
            "inactive_symbol_messages": 0,
        }

    def set_symbols(
        self, symbols: set[str] | list[str] | tuple[str, ...]
    ) -> tuple[str, ...]:
        normalized = {_symbol(value) for value in symbols}
        if len(normalized) > MAX_SYMBOLS:
            raise ValueError(
                f"Spot microstructure supports at most {MAX_SYMBOLS} symbols"
            )
        with self._lock:
            self._active = normalized
            for symbol in normalized:
                self._states.setdefault(symbol, _SymbolState())
            for symbol in set(self._states) - normalized:
                del self._states[symbol]
        return tuple(sorted(normalized))

    def _state(self, symbol: str) -> _SymbolState:
        if symbol not in self._active:
            self._stats["inactive_symbol_messages"] += 1
            raise MarketDataError("event belongs to an inactive symbol")
        return self._states.setdefault(symbol, _SymbolState())

    @staticmethod
    def _purge(state: _SymbolState, now_mono: float) -> None:
        oldest = int(now_mono) - 65
        while state.buckets and state.buckets[0].second < oldest:
            state.buckets.popleft()

    def ingest_agg_trade(
        self,
        payload: object,
        *,
        received_mono: float | None = None,
        received_wall: float | None = None,
    ) -> bool:
        now_mono = float(self._monotonic() if received_mono is None else received_mono)
        now_wall = float(self._wall_clock() if received_wall is None else received_wall)
        try:
            if not isinstance(payload, dict) or payload.get("e") != "aggTrade":
                raise MarketDataError("not an aggTrade event")
            symbol = _symbol(payload.get("s"))
            aggregate_id = _integer(payload.get("a"), "aggregate trade id")
            event_ms = _integer(
                payload.get("T"), "aggregate trade event time", positive=True
            )
            maker_flag = payload.get("m")
            if not isinstance(maker_flag, bool):
                raise MarketDataError("buyer-maker flag must be boolean")
            price = _decimal(payload.get("p"), "price")
            quantity = _decimal(payload.get("q"), "quantity")
            event_age_ms = now_wall * 1000 - event_ms
            if event_age_ms < -5_000 or event_age_ms > self.max_event_lag_ms:
                raise MarketDataError(
                    "aggTrade event timestamp is stale or in the future"
                )
            quote = price * quantity
            with self._lock:
                state = self._state(symbol)
                if state.last_agg_id is not None and aggregate_id <= state.last_agg_id:
                    key = (
                        "duplicate_messages"
                        if aggregate_id == state.last_agg_id
                        else "out_of_order_messages"
                    )
                    self._stats[key] += 1
                    return False
                if (
                    state.last_agg_event_ms is not None
                    and event_ms < state.last_agg_event_ms
                ):
                    self._stats["out_of_order_messages"] += 1
                    return False
                self._purge(state, now_mono)
                second = int(now_mono)
                if not state.buckets or state.buckets[-1].second != second:
                    state.buckets.append(_FlowBucket(second=second))
                bucket = state.buckets[-1]
                # Binance m=true means the buyer was maker, so the aggressive
                # (taker) side was SELL.  m=false means aggressive BUY.
                if maker_flag:
                    bucket.sell_quote += quote
                    state.cvd_quote_session -= quote
                else:
                    bucket.buy_quote += quote
                    state.cvd_quote_session += quote
                bucket.trade_count += 1
                state.last_agg_id = aggregate_id
                state.last_agg_event_ms = event_ms
                state.last_agg_received_mono = now_mono
                state.last_agg_received_wall = now_wall
                if state.session_started_wall is None:
                    state.session_started_wall = now_wall
                self._stats["accepted_agg_trades"] += 1
            return True
        except MarketDataError:
            with self._lock:
                self._stats["malformed_messages"] += 1
            return False

    def ingest_book_ticker(
        self,
        payload: object,
        *,
        received_mono: float | None = None,
        received_wall: float | None = None,
    ) -> bool:
        now_mono = float(self._monotonic() if received_mono is None else received_mono)
        now_wall = float(self._wall_clock() if received_wall is None else received_wall)
        try:
            if not isinstance(payload, dict):
                raise MarketDataError("bookTicker event must be an object")
            symbol = _symbol(payload.get("s"))
            update_id = _integer(payload.get("u"), "bookTicker update id")
            bid = _decimal(payload.get("b"), "best bid")
            bid_qty = _decimal(payload.get("B"), "best bid quantity", allow_zero=True)
            ask = _decimal(payload.get("a"), "best ask")
            ask_qty = _decimal(payload.get("A"), "best ask quantity", allow_zero=True)
            if ask < bid:
                raise MarketDataError("crossed bookTicker payload")
            with self._lock:
                state = self._state(symbol)
                if (
                    state.book_update_id is not None
                    and update_id <= state.book_update_id
                ):
                    key = (
                        "duplicate_messages"
                        if update_id == state.book_update_id
                        else "out_of_order_messages"
                    )
                    self._stats[key] += 1
                    return False
                state.book_update_id = update_id
                state.bid = bid
                state.bid_qty = bid_qty
                state.ask = ask
                state.ask_qty = ask_qty
                # Spot bookTicker has no exchange event timestamp.  Freshness
                # is intentionally based on this local monotonic receive time.
                state.book_received_mono = now_mono
                state.book_received_wall = now_wall
                self._stats["accepted_book_tickers"] += 1
            return True
        except MarketDataError:
            with self._lock:
                self._stats["malformed_messages"] += 1
            return False

    @staticmethod
    def _window(
        state: _SymbolState, now_mono: float, seconds: int
    ) -> tuple[Decimal, Decimal, int]:
        threshold = now_mono - seconds
        selected = [bucket for bucket in state.buckets if bucket.second >= threshold]
        return (
            sum((bucket.buy_quote for bucket in selected), Decimal(0)),
            sum((bucket.sell_quote for bucket in selected), Decimal(0)),
            sum(bucket.trade_count for bucket in selected),
        )

    @staticmethod
    def _range_count(
        state: _SymbolState, now_mono: float, newer: int, older: int
    ) -> int:
        return sum(
            bucket.trade_count
            for bucket in state.buckets
            if newer <= now_mono - bucket.second < older
        )

    def _symbol_snapshot(
        self, symbol: str, state: _SymbolState, now_mono: float
    ) -> dict:
        self._purge(state, now_mono)
        agg_age_ms = (
            None
            if state.last_agg_received_mono is None
            else max(0, round((now_mono - state.last_agg_received_mono) * 1000))
        )
        book_age_ms = (
            None
            if state.book_received_mono is None
            else max(0, round((now_mono - state.book_received_mono) * 1000))
        )
        flow_fresh = agg_age_ms is not None and agg_age_ms <= self.max_age_ms
        book_fresh = book_age_ms is not None and book_age_ms <= self.max_age_ms
        flow: dict[str, object] = {
            "status": "fresh"
            if flow_fresh
            else ("stale" if agg_age_ms is not None else "unavailable"),
            "agg_trade_age_ms": agg_age_ms,
            "last_agg_trade_event_time_ms": state.last_agg_event_ms,
            "last_agg_trade_received_at": (
                _iso(state.last_agg_received_wall)
                if state.last_agg_received_wall is not None
                else None
            ),
            "cvd_quote_session": _text(state.cvd_quote_session),
            "cvd_session_started_at": (
                _iso(state.session_started_wall)
                if state.session_started_wall is not None
                else None
            ),
        }
        for window in WINDOWS:
            buy, sell, count = self._window(state, now_mono, window)
            total = buy + sell
            flow[f"aggressive_buy_quote_{window}s"] = _text(buy)
            flow[f"aggressive_sell_quote_{window}s"] = _text(sell)
            flow[f"taker_buy_ratio_{window}s"] = _text(buy / total) if total else None
            flow[f"cvd_quote_{window}s"] = _text(buy - sell)
            flow[f"trade_count_{window}s"] = count
            flow[f"trade_intensity_per_second_{window}s"] = _text(
                Decimal(count) / Decimal(window)
            )
            flow[f"quote_flow_per_second_{window}s"] = _text(total / Decimal(window))

        current_count = self._range_count(state, now_mono, 0, 10)
        prior_count = self._range_count(state, now_mono, 10, 30)
        current_rate = Decimal(current_count) / Decimal(10)
        prior_rate = Decimal(prior_count) / Decimal(20)
        flow["trade_flow_acceleration_ratio_10s_vs_prior_20s"] = (
            _text(current_rate / prior_rate) if prior_rate else None
        )
        flow["trade_flow_acceleration_delta_tps"] = _text(current_rate - prior_rate)

        spread = mid = spread_bps = pressure = None
        if state.bid is not None and state.ask is not None:
            spread = state.ask - state.bid
            mid = (state.ask + state.bid) / Decimal(2)
            spread_bps = spread / mid * Decimal(10_000) if mid else None
        if state.bid_qty is not None and state.ask_qty is not None:
            total_qty = state.bid_qty + state.ask_qty
            pressure = (
                (state.bid_qty - state.ask_qty) / total_qty if total_qty else None
            )
        book = {
            "status": "fresh"
            if book_fresh
            else ("stale" if book_age_ms is not None else "unavailable"),
            "book_ticker_age_ms": book_age_ms,
            "last_book_ticker_received_at": (
                _iso(state.book_received_wall)
                if state.book_received_wall is not None
                else None
            ),
            "exchange_event_time": None,
            "best_bid": _text(state.bid),
            "best_bid_qty": _text(state.bid_qty),
            "best_ask": _text(state.ask),
            "best_ask_qty": _text(state.ask_qty),
            "spread": _text(spread),
            "spread_bps": _text(spread_bps),
            "top_of_book_quantity_pressure": _text(pressure),
        }
        if flow_fresh and book_fresh:
            status = "fresh"
        elif agg_age_ms is None and book_age_ms is None:
            status = "unavailable"
        else:
            status = "stale"
        return {
            "symbol": symbol,
            "status": status,
            "advisory_only": True,
            "spot_aggressive_flow": flow,
            "top_of_book_liquidity": book,
        }

    def snapshot(
        self,
        *,
        now_mono: float | None = None,
        now_wall: float | None = None,
    ) -> dict:
        observed_mono = float(self._monotonic() if now_mono is None else now_mono)
        observed_wall = float(self._wall_clock() if now_wall is None else now_wall)
        with self._lock:
            symbols = {
                symbol: self._symbol_snapshot(
                    symbol, self._states[symbol], observed_mono
                )
                for symbol in sorted(self._active)
            }
            stats = dict(self._stats)
        fresh = sum(value["status"] == "fresh" for value in symbols.values())
        return {
            "schema_version": 1,
            "generated_at": _iso(observed_wall),
            "advisory_only": True,
            "spot_only": True,
            "can_trade": False,
            "features": [
                "aggTrade",
                "bookTicker_local_monotonic_freshness",
                "aggressive_buy_sell_windows",
                "spot_cvd",
                "trade_flow_intensity_acceleration",
                "signal_time_liquidity_evidence",
            ],
            "max_age_ms": self.max_age_ms,
            "symbol_count": len(symbols),
            "fresh_symbol_count": fresh,
            "symbols": symbols,
            "statistics": stats,
        }
