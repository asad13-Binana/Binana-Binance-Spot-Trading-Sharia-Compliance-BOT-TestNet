"""Failure-safe, read-only metric collectors for the V10.1 deployment."""
from __future__ import annotations

import datetime as dt
import json
import math
import re
import shutil
import statistics
import time
from pathlib import Path

from .configuration import CONFIG
from .database import query, status as database_status
from .log_redaction import redact, redact_obj

try:
    import psutil
except ImportError:  # pragma: no cover - deliberately supported degradation
    psutil = None


_PROCESS_STARTED = time.time()
_LOG_TS = re.compile(
    r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?)"
)


def _iso(epoch: float | None = None) -> str:
    moment = dt.datetime.fromtimestamp(
        time.time() if epoch is None else epoch, tz=dt.timezone.utc
    )
    return moment.isoformat().replace("+00:00", "Z")


def _parse_time(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000.0
        return number
    except (TypeError, ValueError):
        pass
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.timestamp()
    except (TypeError, ValueError):
        return None


def _safe_float(value, default=0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _safe_json(path: Path):
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value, None
    except FileNotFoundError:
        return None, "missing"
    except (OSError, json.JSONDecodeError) as exc:
        return None, type(exc).__name__


def file_freshness(path: Path) -> dict:
    try:
        stat = Path(path).stat()
        return {
            "present": True,
            "age_seconds": max(0, round(time.time() - stat.st_mtime, 3)),
            "size_bytes": stat.st_size,
            "modified_at": _iso(stat.st_mtime),
        }
    except FileNotFoundError:
        return {"present": False, "reason": "missing"}
    except OSError as exc:
        return {"present": False, "reason": type(exc).__name__}


def _health(path: Path, max_age: int) -> dict:
    data, error = _safe_json(path)
    if error:
        return {"available": False, "fresh": False, "reason": error}
    stamp = data.get("ts") if isinstance(data, dict) else None
    if stamp is None and isinstance(data, dict):
        stamp = data.get("generated_at")
    epoch = _parse_time(stamp)
    age = None if epoch is None else max(0, time.time() - epoch)
    return {
        "available": True,
        "fresh": age is not None and age <= max_age,
        "age_seconds": None if age is None else round(age, 3),
        "data": redact_obj(data),
    }


def runtime_health() -> dict:
    components = {
        "execution_sidecar": _health(CONFIG.sidecar_health_path, 30),
        "telegram_broker": _health(CONFIG.telegram_health_path, 150),
        "sharia_screener": _health(CONFIG.sharia_health_path, 180),
        "universe": _health(CONFIG.universe_health_path, 1800),
    }
    available = [value for value in components.values() if value["available"]]
    overall = "healthy" if available and all(value["fresh"] for value in available) else "degraded"
    if not available:
        overall = "unavailable"
    return {"status": overall, "components": components}


def container_state() -> dict:
    data, error = _safe_json(CONFIG.container_status_path)
    if error:
        return {"status": "unavailable", "reason": f"snapshot_{error}"}
    generated = _parse_time(data.get("generated_at")) if isinstance(data, dict) else None
    age = None if generated is None else max(0, time.time() - generated)
    containers = data.get("containers", []) if isinstance(data, dict) else []
    states = [str(item.get("status", "unknown")) for item in containers if isinstance(item, dict)]
    health = [str(item.get("health", "none")) for item in containers if isinstance(item, dict)]
    expected = {"universe", "sharia-screener", "freqtrade", "execution-sidecar", "telegram-broker"}
    present = {str(item.get("service")) for item in containers if isinstance(item, dict)}
    ok = expected.issubset(present) and all(state == "running" for state in states)
    ok = ok and all(value in {"healthy", "none"} for value in health)
    return {
        "status": "healthy" if ok else "degraded",
        "snapshot_age_seconds": None if age is None else round(age, 3),
        "snapshot_fresh": age is not None and age <= 90,
        "expected_services": sorted(expected),
        "containers": redact_obj(containers),
    }


def system_resources() -> dict:
    disk_path = CONFIG.shared_root if CONFIG.shared_root.exists() else Path("/")
    try:
        usage = shutil.disk_usage(disk_path)
        out = {
            "disk_path": str(disk_path),
            "disk_used_pct": round(100 * usage.used / usage.total, 1),
            "disk_free_mb": usage.free // (1024 * 1024),
            "monitor_process_uptime_seconds": int(time.time() - _PROCESS_STARTED),
        }
    except OSError as exc:
        out = {"disk_error": type(exc).__name__}
    if psutil is None:
        out["psutil"] = False
        return out
    try:
        memory = psutil.virtual_memory()
        load = psutil.getloadavg() if hasattr(psutil, "getloadavg") else None
        out.update({
            "psutil": True,
            "cpu_pct": psutil.cpu_percent(interval=0.2),
            "mem_pct": memory.percent,
            "mem_available_mb": memory.available // (1024 * 1024),
            "load_average": list(load) if load else None,
        })
    except Exception as exc:  # psutil can fail on restricted hosts
        out.update({"psutil": True, "error": type(exc).__name__})
    return out


def binance_latency(samples: int = 5) -> dict:
    try:
        import httpx
    except ImportError:
        return {"reachable": False, "error": "httpx_not_installed"}
    samples = max(1, min(int(samples), CONFIG.MAX_LATENCY_SAMPLES))
    durations: list[float] = []
    errors = 0
    for _ in range(samples):
        started = time.perf_counter()
        try:
            response = httpx.get(f"{CONFIG.binance_base}/api/v3/ping", timeout=5)
            response.raise_for_status()
            durations.append((time.perf_counter() - started) * 1000)
        except Exception:
            errors += 1
    if not durations:
        return {
            "reachable": False, "endpoint": CONFIG.binance_base,
            "samples": samples, "errors": errors,
        }
    ordered = sorted(durations)

    def percentile(percent: int) -> float:
        index = min(len(ordered) - 1, math.ceil(percent / 100 * len(ordered)) - 1)
        return ordered[max(0, index)]

    return {
        "reachable": True,
        "endpoint": CONFIG.binance_base,
        "samples": len(ordered),
        "errors": errors,
        "median_ms": round(statistics.median(ordered), 1),
        "p95_ms": round(percentile(95), 1),
        "p99_ms": round(percentile(99), 1),
    }


def _tail_text(path: Path, lines: int, max_bytes: int):
    """Read only a bounded suffix of a file; never load the whole log."""
    try:
        size = Path(path).stat().st_size
        take = min(size, max_bytes)
        with Path(path).open("rb") as handle:
            handle.seek(size - take)
            payload = handle.read(take)
        truncated = size > take
        text = payload.decode("utf-8", errors="ignore")
        values = text.splitlines()
        if truncated and values:
            values = values[1:]  # discard a likely partial first line
        return values[-lines:], truncated, None
    except FileNotFoundError:
        return [], False, "missing"
    except OSError as exc:
        return [], False, type(exc).__name__


def tail_log(lines: int):
    lines = max(1, min(int(lines), CONFIG.MAX_LOG_LINES))
    return _tail_text(CONFIG.log_path, lines, CONFIG.MAX_LOG_SCAN_BYTES)


def log_freshness() -> dict:
    return file_freshness(CONFIG.log_path)


def error_lines(lines: int = 200) -> dict:
    raw, truncated, error = tail_log(lines)
    if error:
        return {"error": f"log_{error}", "error_count": 0, "recent": []}
    keywords = ("ERROR", "CRITICAL", "Traceback", "Exception")
    hits = [redact(line.rstrip()) for line in raw if any(key in line for key in keywords)]
    return {
        "error_count": len(hits),
        "rejected_lines": sum("reject" in line.lower() for line in raw),
        "reconnect_lines": sum("reconnect" in line.lower() for line in raw),
        "scan_truncated": truncated,
        "recent": hits[-40:],
    }


def _log_line_epoch(line: str) -> float | None:
    match = _LOG_TS.match(line)
    return _parse_time(match.group(1).replace(",", ".")) if match else None


def crash_blocks(hours: int = 24) -> dict:
    hours = max(1, min(int(hours), CONFIG.MAX_CRASH_HOURS))
    raw, truncated, error = _tail_text(
        CONFIG.log_path, 50_000, CONFIG.MAX_LOG_SCAN_BYTES
    )
    if error:
        return {"error": f"log_{error}", "window_hours": hours, "crash_count": 0, "latest": []}
    cutoff = time.time() - hours * 3600
    blocks: list[list[str]] = []
    current: list[str] = []
    current_epoch: float | None = None
    earliest: float | None = None
    for line in raw:
        epoch = _log_line_epoch(line)
        if epoch is not None:
            current_epoch = epoch
            earliest = epoch if earliest is None else min(earliest, epoch)
        if current_epoch is not None and current_epoch < cutoff:
            continue
        if "Traceback (most recent call last)" in line:
            if current:
                blocks.append(current)
            current = [line.rstrip()]
        elif current:
            current.append(line.rstrip())
            if re.search(r"(?:Error|Exception):", line):
                blocks.append(current)
                current = []
    if current:
        blocks.append(current)
    latest = ["\n".join(redact(line) for line in block[-16:]) for block in blocks[-3:]]
    return {
        "window_hours": hours,
        "crash_count": len(blocks),
        "latest": latest,
        "scan_truncated": truncated,
        "window_complete": not truncated or (earliest is not None and earliest <= cutoff),
    }


def databases() -> dict:
    execution = database_status(CONFIG.execution_db_path)
    signal = database_status(CONFIG.signal_db_path)
    execution["role"] = "authoritative_execution_state"
    signal["role"] = "signal_only_freqtrade"
    execution["freshness"] = file_freshness(CONFIG.execution_db_path)
    signal["freshness"] = file_freshness(CONFIG.signal_db_path)
    return {"execution": execution, "signal_engine": signal}


def execution_state(limit: int = 50) -> dict:
    limit = max(1, min(int(limit), CONFIG.MAX_TRADES))
    active, error = query(
        CONFIG.execution_db_path,
        """SELECT trade_id,pair,lifecycle_state,filled_quantity,protected_quantity,
                  average_entry_price,protection_mode,last_event_time,
                  reconciliation_status,updated_at
           FROM trade_records
           WHERE lifecycle_state NOT IN (?,?)
           ORDER BY updated_at DESC LIMIT ?""",
        ("EXIT_FILLED", "ERROR", limit),
    )
    if error:
        return {"source": "execution_state.sqlite", "error": error, "open_positions": []}
    terminal, terminal_error = query(
        CONFIG.execution_db_path,
        """SELECT lifecycle_state,COUNT(*) AS count FROM trade_records
           WHERE lifecycle_state IN (?,?) GROUP BY lifecycle_state""",
        ("EXIT_FILLED", "ERROR"),
    )
    return {
        "source": "authoritative_execution_state.sqlite",
        "open_count": len(active),
        "open_positions": active,
        "terminal_counts": terminal if not terminal_error else [],
    }


def _read_jsonl_tail(path: Path, max_bytes: int):
    lines, truncated, error = _tail_text(path, 1_000_000, max_bytes)
    records = []
    for line in lines:
        if not line.strip():
            continue
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                records.append(value)
        except json.JSONDecodeError:
            continue
    return records, truncated, error


def performance(days: int = 1) -> dict:
    days = max(1, min(int(days), CONFIG.MAX_REPORT_DAYS))
    records, truncated, error = _read_jsonl_tail(
        CONFIG.pnl_ledger_path, CONFIG.MAX_LEDGER_SCAN_BYTES
    )
    state = execution_state(CONFIG.MAX_TRADES)
    out = {
        "window_days": days,
        "source": "execution_pnl_ledger_and_execution_state",
        "units": "percentage_points_per_closed_trade",
        "open_trades": state.get("open_count"),
        "open_positions": state.get("open_positions", []),
    }
    if error:
        out.update({"error": f"pnl_ledger_{error}", "closed_trades": 0})
        return out
    cutoff = time.time() - days * 86400
    closed = [row for row in records if _safe_float(row.get("ts"), -1) >= cutoff]
    pnl = [_safe_float(row.get("pnl_pct")) for row in closed]
    wins = [value for value in pnl if value > 0]
    losses = [value for value in pnl if value <= 0]
    equity = peak = max_drawdown = 0.0
    for value in pnl:
        equity += value
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    oldest = min((_safe_float(row.get("ts"), time.time()) for row in records), default=None)
    gross_profit = sum(wins)
    gross_loss = sum(losses)
    out.update({
        "closed_trades": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(100 * len(wins) / len(closed), 2) if closed else None,
        "gross_profit_pct": round(gross_profit, 4),
        "gross_loss_pct": round(gross_loss, 4),
        "net_pnl_pct": round(gross_profit + gross_loss, 4),
        "profit_factor": round(gross_profit / abs(gross_loss), 3) if gross_loss < 0 else None,
        "max_drawdown_pct": round(max_drawdown, 4) if closed else None,
        "scan_truncated": truncated,
        "window_complete": not truncated or (oldest is not None and oldest <= cutoff),
    })
    return out


def recent_trades(limit: int = 20) -> dict:
    limit = max(1, min(int(limit), CONFIG.MAX_TRADES))
    records, truncated, error = _read_jsonl_tail(
        CONFIG.pnl_ledger_path, CONFIG.MAX_LEDGER_SCAN_BYTES
    )
    if error:
        return {"source": "execution_pnl_ledger", "error": f"pnl_ledger_{error}", "trades": []}
    records.sort(key=lambda row: _safe_float(row.get("ts")), reverse=True)
    safe = [
        {key: row.get(key) for key in ("utc", "symbol", "entry", "exit", "pnl_pct", "tag")}
        for row in records[:limit]
    ]
    return {"source": "execution_pnl_ledger", "scan_truncated": truncated, "trades": safe}


def signal_performance(days: int = 1) -> dict:
    """Freqtrade signal-engine results, explicitly not real execution P/L."""
    days = max(1, min(int(days), CONFIG.MAX_REPORT_DAYS))
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    closed, error = query(
        CONFIG.signal_db_path,
        """SELECT pair,close_profit_abs,close_profit,open_date,close_date
           FROM trades
           WHERE is_open=0 AND datetime(close_date) >= datetime(?)
           ORDER BY datetime(close_date)""",
        (since,),
    )
    if error:
        return {"source": "freqtrade_signal_only", "error": error}
    pnl = [_safe_float(row.get("close_profit_abs")) for row in closed]
    return {
        "source": "freqtrade_signal_only_not_real_orders",
        "window_days": days,
        "closed_signals": len(closed),
        "net_signal_pnl": round(sum(pnl), 4),
        "wins": sum(value > 0 for value in pnl),
    }


def order_quality(days: int = 1) -> dict:
    days = max(1, min(int(days), CONFIG.MAX_REPORT_DAYS))
    cutoff_epoch = time.time() - days * 86400
    since = dt.datetime.fromtimestamp(cutoff_epoch, tz=dt.timezone.utc).isoformat()
    events, error = query(
        CONFIG.execution_db_path,
        """SELECT event_type,event_time,payload_json FROM exchange_events
           ORDER BY id DESC LIMIT 5000""",
        (),
    )
    rejected = canceled = expired = malformed = 0
    if not error:
        for row in events:
            # event_time may be ISO text or Binance epoch milliseconds.
            event_epoch = _parse_time(row.get("event_time"))
            if event_epoch is not None and event_epoch < cutoff_epoch:
                continue
            try:
                payload = json.loads(row.get("payload_json") or "{}")
            except json.JSONDecodeError:
                malformed += 1
                continue
            status = str(payload.get("X") or payload.get("orderStatus") or "").upper()
            rejected += status == "REJECTED"
            canceled += status == "CANCELED"
            expired += status == "EXPIRED"
    signals, signal_error = query(
        CONFIG.execution_db_path,
        """SELECT result,COUNT(*) AS count FROM processed_signals
           WHERE datetime(processed_at) >= datetime(?) GROUP BY result""",
        (since,),
    )
    rejected_signals = sum(
        int(row.get("count") or 0)
        for row in signals
        if any(word in str(row.get("result", "")).lower() for word in ("reject", "fail", "error"))
    ) if not signal_error else None
    return {
        "window_days": days,
        "exchange_events_error": error,
        "rejected_orders": rejected,
        "canceled_orders": canceled,
        "expired_orders": expired,
        "malformed_events": malformed,
        "rejected_signals": rejected_signals,
        "signal_query_error": signal_error,
    }


def websocket_status() -> dict:
    health = _health(CONFIG.user_stream_health_path, 90)
    if not health["available"]:
        return health
    data = health.get("data", {})
    return {
        "available": True,
        "fresh": health["fresh"],
        "age_seconds": health["age_seconds"],
        "connected": bool(data.get("connected")),
        "subscribed": bool(data.get("subscribed")),
        "reconnect_count": int(data.get("reconnect_count") or 0),
        "last_event_at": data.get("last_event_at"),
        "last_message_at": data.get("last_message_at"),
        "endpoint_mode": data.get("mode"),
        "last_error": redact(str(data.get("last_error", ""))) or None,
    }


def sharia_status() -> dict:
    data, error = _safe_json(CONFIG.sharia_status_path)
    health = _health(CONFIG.sharia_health_path, 180)
    if error:
        return {"available": False, "reason": error, "service": health}
    records = data.get("records", []) if isinstance(data, dict) else []
    counts: dict[str, int] = {}
    latest = None
    for record in records:
        if not isinstance(record, dict):
            continue
        state = str(record.get("final_code") or record.get("status") or "UNKNOWN").upper()
        counts[state] = counts.get(state, 0) + 1
        reviewed = record.get("reviewed_at") or record.get("screened_at")
        if reviewed and (latest is None or str(reviewed) > str(latest)):
            latest = reviewed
    service_data = health.get("data", {}) if health.get("available") else {}
    return {
        "available": True,
        "controller_sha256": data.get("controller_sha256") if isinstance(data, dict) else None,
        "status_counts": counts,
        "total_records": len(records),
        "latest_review": latest,
        "service": health,
        "queue": service_data.get("queue"),
        "latest_successful_scan": service_data.get("last_done"),
        "latest_failed_scan": service_data.get("last_failed"),
    }


def deployment_info() -> dict:
    out = {"package_mode": None, "release_tag": None, "release_sha256": None, "git_commit": None}
    for key, name in (
        ("package_mode", "RELEASE_MODE"),
        ("release_tag", ".release-tag"),
        ("release_sha256", "RELEASE_SHA256.txt"),
        ("git_commit", ".git-commit"),
    ):
        try:
            value = (CONFIG.bot_dir / name).read_text(encoding="utf-8").strip()
            out[key] = value.split()[0] if value else None
        except OSError:
            pass
    status, status_error = _safe_json(CONFIG.deploy_status_path)
    validation, validation_error = _safe_json(CONFIG.validation_status_path)
    out["deployment_status"] = status if status_error is None else {"available": False, "reason": status_error}
    out["validation"] = validation if validation_error is None else {"available": False, "reason": validation_error}
    if isinstance(status, dict):
        out["last_deploy"] = status.get("at") if status.get("status") == "DEPLOYED" else None
        out["last_rollback"] = status.get("at") if str(status.get("status", "")).startswith("ROLLED_BACK") else None
    return redact_obj(out)


def recent_security_warnings(limit: int = 50) -> dict:
    limit = max(1, min(int(limit), 200))
    records, truncated, error = _read_jsonl_tail(
        CONFIG.security_audit_path, CONFIG.MAX_LOG_SCAN_BYTES
    )
    if error:
        return {"available": False, "reason": error, "warnings": []}
    warnings = [
        record for record in records
        if str(record.get("severity", "INFO")).upper() in {"WARNING", "ERROR", "CRITICAL"}
    ]
    return {
        "available": True,
        "scan_truncated": truncated,
        "warnings": redact_obj(warnings[-limit:]),
    }
