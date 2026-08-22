from __future__ import annotations

"""Failure-isolated signal-time recording of advisory Spot evidence."""

import json
import os
import re
from collections.abc import Callable
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any

from services.common import envelope
from services.common.atomic import atomic_write_json
from services.common.audit import audit
from services.common.retention import prune_files

from .evidence import capture_signal_evidence

SIGNAL_PRODUCER = "freqtrade-strategy"
SIGNAL_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")


def _enabled() -> bool:
    return os.getenv("CAPTURE_SIGNAL_MARKET_CONTEXT", "true").strip().lower() == "true"


def _prepare(path: Path) -> dict[str, Any]:
    """Read authenticated identity and freeze the current public observation."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    signal = envelope.verify_envelope(
        raw,
        purpose=envelope.BUS_SIGNAL,
        expected_producers={SIGNAL_PRODUCER},
    )
    signal_id = str(signal.get("signal_id", ""))
    symbol = str(signal.get("symbol", "")).upper()
    if not SIGNAL_ID_RE.fullmatch(signal_id):
        raise ValueError("invalid signal identity")
    context_file = Path(
        os.getenv("MARKET_CONTEXT_FILE", "/app/shared/market_context/current.json")
    )
    return {
        "schema_version": 1,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "signal_id": signal_id,
        "pair": str(signal.get("pair", "")).upper(),
        "symbol": symbol,
        "market_context": capture_signal_evidence(context_file, symbol),
    }


def _persist(observation: dict[str, Any], outcome: Any) -> None:
    observation = dict(observation)
    observation["signal_processing_outcome"] = str(outcome)
    signal_id = observation["signal_id"]
    target_dir = Path(
        os.getenv(
            "MARKET_CONTEXT_SIGNAL_EVIDENCE_DIR",
            "/app/shared/market_context/signal_evidence",
        )
    )
    atomic_write_json(target_dir / f"{signal_id}.json", observation)
    try:
        max_files = int(os.getenv("MARKET_CONTEXT_SIGNAL_EVIDENCE_MAX_FILES", "10000"))
    except (TypeError, ValueError):
        max_files = 10000
    prune_files(target_dir, "*.json", max_files=max(0, min(max_files, 100000)))
    market_context = observation["market_context"]
    audit(
        "signal_market_context_observed",
        details={
            "signal_id": signal_id,
            "pair": observation["pair"],
            "available": market_context.get("available"),
            "fresh": market_context.get("fresh"),
            "reason": market_context.get("reason"),
            "advisory_only": True,
            "used_for_trade_decision": False,
        },
    )


def install_signal_observer(order_manager_class: type) -> None:
    """Wrap process_signal once while preserving its return and exceptions."""
    original: Callable[..., Any] = order_manager_class.process_signal
    if getattr(original, "_binana_market_context_observer", False):
        return

    @wraps(original)
    def observed(self, path, *args, **kwargs):
        observation = None
        if _enabled():
            try:
                observation = _prepare(Path(path))
            except Exception as exc:  # noqa: BLE001 - advisory isolation boundary
                # The protected signal pipeline performs its own authoritative
                # verification. Observation failure cannot influence it.
                try:
                    audit(
                        "signal_market_context_unavailable",
                        severity="WARNING",
                        details={
                            "file": Path(path).name,
                            "reason": type(exc).__name__,
                            "advisory_only": True,
                            "used_for_trade_decision": False,
                        },
                    )
                except Exception:  # noqa: BLE001,S110 - must not affect signal path
                    pass
        outcome = original(self, path, *args, **kwargs)
        if observation is not None:
            try:
                _persist(observation, outcome)
            except Exception as exc:  # noqa: BLE001 - advisory isolation boundary
                audit(
                    "signal_market_context_persist_failed",
                    severity="WARNING",
                    details={
                        "signal_id": observation.get("signal_id"),
                        "reason": type(exc).__name__,
                        "advisory_only": True,
                        "used_for_trade_decision": False,
                    },
                )
        return outcome

    observed._binana_market_context_observer = True
    order_manager_class.process_signal = observed
