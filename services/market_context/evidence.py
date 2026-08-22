from __future__ import annotations

"""Failure-isolated signal-time copy of advisory market-context evidence."""

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
SYMBOL_RE = re.compile(r"[A-Z0-9]{2,24}USDT")


def _epoch(value: object) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc).timestamp()


def capture_signal_evidence(
    path: str | Path,
    symbol: str,
    *,
    now: float | None = None,
) -> dict:
    """Return a bounded safe subset; never raise and never authorize a trade."""
    symbol = str(symbol or "").upper()
    base = {
        "advisory_only": True,
        "used_for_trade_decision": False,
        "symbol": symbol,
        "available": False,
        "fresh": False,
    }
    if not SYMBOL_RE.fullmatch(symbol):
        return dict(base, reason="invalid_symbol")
    try:
        source = Path(path)
        if source.stat().st_size > MAX_SNAPSHOT_BYTES:
            return dict(base, reason="snapshot_too_large")
        payload = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return dict(base, reason="snapshot_missing")
    except (OSError, UnicodeError, json.JSONDecodeError):
        return dict(base, reason="snapshot_unreadable")
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or payload.get("advisory_only") is not True
        or payload.get("spot_only") is not True
        or payload.get("can_trade") is not False
        or not isinstance(payload.get("symbols"), dict)
    ):
        return dict(base, reason="snapshot_invalid_contract")
    record = payload["symbols"].get(symbol)
    if not isinstance(record, dict) or record.get("symbol") != symbol:
        return dict(base, reason="symbol_unavailable")
    generated = _epoch(payload.get("generated_at"))
    observed_now = time.time() if now is None else now
    delta = None if generated is None else observed_now - generated
    age = None if delta is None or delta < -5 else max(0.0, delta)
    try:
        max_age = int(os.getenv("MARKET_CONTEXT_EVIDENCE_MAX_AGE_SECONDS", "30"))
    except (TypeError, ValueError):
        max_age = 30
    max_age = max(5, min(max_age, 300))
    flow = record.get("spot_aggressive_flow")
    book = record.get("top_of_book_liquidity")
    try:
        flow_age = int(flow.get("agg_trade_age_ms")) if isinstance(flow, dict) else None
        book_age = (
            int(book.get("book_ticker_age_ms")) if isinstance(book, dict) else None
        )
        record_max_age = int(payload.get("max_age_ms"))
    except (TypeError, ValueError, OverflowError):
        flow_age = book_age = record_max_age = None
    effective_flow_age = (
        None if age is None or flow_age is None else flow_age + round(age * 1000)
    )
    effective_book_age = (
        None if age is None or book_age is None else book_age + round(age * 1000)
    )
    fresh = bool(
        age is not None
        and age <= max_age
        and record.get("status") == "fresh"
        and record_max_age is not None
        and 250 <= record_max_age <= 60_000
        and effective_flow_age is not None
        and effective_flow_age <= record_max_age
        and effective_book_age is not None
        and effective_book_age <= record_max_age
    )
    return {
        **base,
        "available": True,
        "fresh": fresh,
        "reason": None if fresh else "stale_or_incomplete",
        "snapshot_generated_at": payload.get("generated_at"),
        "snapshot_age_seconds": None if age is None else round(age, 3),
        "signal_time_agg_trade_age_ms": effective_flow_age,
        "signal_time_book_ticker_age_ms": effective_book_age,
        "universe_snapshot_hash": payload.get("universe_snapshot_hash"),
        "record": {
            "status": record.get("status"),
            "spot_aggressive_flow": record.get("spot_aggressive_flow"),
            "top_of_book_liquidity": record.get("top_of_book_liquidity"),
        },
    }
