#!/usr/bin/env python3
"""Root-owned helper that emits sanitized Docker status for the botmon user.

The monitor service itself never receives Docker-socket access.  This fixed,
root-owned helper has no order or container-mutation subcommands.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path


PROJECT = os.getenv("BINANA_COMPOSE_PROJECT", "binana-freqtrade-v101")
DEFAULT_OUTPUT = Path(
    os.getenv(
        "BINANA_CONTAINER_STATUS_PATH",
        "/var/lib/binana-freqtrade-v101/shared/runtime/container_status.json",
    )
)
SHARED_ROOT = Path("/var/lib/binana-freqtrade-v101/shared")
# Fixed, credential-free sources consumed by monitoring/api/metrics.py.  No
# queues, HMAC envelopes, Sharia evidence bytes, private configuration, or
# recovery state may be added to this allowlist.
READONLY_MONITOR_SOURCES = (
    "runtime/sidecar_health.json",
    "runtime/telegram_health.json",
    "runtime/telegram_alert_outbox_health.json",
    "runtime/user_stream_health.json",
    "runtime/market_context/health.json",
    "runtime/sharia_screener/health.json",
    "runtime/deployment_status.json",
    "runtime/release_validation.json",
    "runtime/offhost_backup_status.json",
    "runtime/api_readiness_status.json",
    "runtime/execution_state.sqlite",
    "runtime/execution_state.sqlite-wal",
    "runtime/execution_state.sqlite-shm",
    "sharia/sharia_status.json",
    "universe/status.json",
    "market_context/current.json",
    "freqtrade/tradesv3.signal-only.sqlite",
    "freqtrade/tradesv3.signal-only.sqlite-wal",
    "freqtrade/tradesv3.signal-only.sqlite-shm",
    "freqtrade/logs/freqtrade.log",
    "legacy_runtime/logs/pnl_ledger.jsonl",
    "audit/events.jsonl",
)


def collect() -> dict:
    docker = "/usr/bin/docker"
    if not Path(docker).is_file():
        return {"generated_at": datetime.now(timezone.utc).isoformat(), "error": "docker_missing", "containers": []}
    listed = subprocess.run(
        [docker, "ps", "-aq", "--filter", f"label=com.docker.compose.project={PROJECT}"],
        capture_output=True, text=True, timeout=10, check=False,
    )
    if listed.returncode != 0:
        return {"generated_at": datetime.now(timezone.utc).isoformat(), "error": "docker_query_failed", "containers": []}
    identifiers = listed.stdout.split()
    if not identifiers:
        return {"generated_at": datetime.now(timezone.utc).isoformat(), "containers": []}
    inspected = subprocess.run(
        [docker, "inspect", *identifiers], capture_output=True, text=True,
        timeout=15, check=False,
    )
    if inspected.returncode != 0:
        return {"generated_at": datetime.now(timezone.utc).isoformat(), "error": "docker_inspect_failed", "containers": []}
    try:
        raw = json.loads(inspected.stdout)
    except json.JSONDecodeError:
        raw = []
    containers = []
    for item in raw:
        state = item.get("State") or {}
        labels = (item.get("Config") or {}).get("Labels") or {}
        health = (state.get("Health") or {}).get("Status") or "none"
        containers.append({
            "service": labels.get("com.docker.compose.service"),
            "name": str(item.get("Name") or "").lstrip("/"),
            "status": state.get("Status") or "unknown",
            "health": health,
            "restart_count": int(item.get("RestartCount") or 0),
            "started_at": state.get("StartedAt"),
        })
    containers.sort(key=lambda value: str(value.get("service")))
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "containers": containers}


def publish_monitor_permissions(root: Path = SHARED_ROOT) -> list[str]:
    """Grant group-read only to the fixed monitoring sources.

    The helper is root-owned and sandboxed by systemd.  Every file is opened
    without following links and is rejected when it is not a regular,
    single-link file owned by root or the dedicated bot account.
    """
    root = root.resolve(strict=True)
    root_stat = root.stat()
    published = []
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    for relative in READONLY_MONITOR_SOURCES:
        path = root / relative
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError:
            continue
        except OSError:
            continue
        try:
            current = os.fstat(descriptor)
            if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1:
                continue
            if current.st_uid not in {0, root_stat.st_uid}:
                continue
            os.fchown(descriptor, current.st_uid, root_stat.st_gid)
            os.fchmod(descriptor, 0o640)
            published.append(relative)
        finally:
            os.close(descriptor)
    return published


def atomic_write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".container-status.", dir=path.parent)
    try:
        os.fchown(descriptor, 0, path.parent.stat().st_gid)
        os.fchmod(descriptor, 0o640)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, separators=(",", ":"), sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    resolved = args.output.resolve()
    allowed = DEFAULT_OUTPUT.parent.resolve()
    if resolved.parent != allowed:
        raise SystemExit(f"output must be directly inside {allowed}")
    published = publish_monitor_permissions()
    value = collect()
    value["readonly_sources_published"] = published
    atomic_write(resolved, value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
