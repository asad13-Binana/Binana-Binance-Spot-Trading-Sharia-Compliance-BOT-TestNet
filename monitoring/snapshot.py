#!/usr/bin/env python3
"""Root-owned helper that emits sanitized Docker status for the botmon user.

The monitor service itself never receives Docker-socket access.  This fixed,
root-owned helper has no order or container-mutation subcommands.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
import math
import os
import re
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
_OWNER_SOURCE = "freqtrade/runtime/owner_readiness.json"
_OWNER_OUTPUT = "runtime/owner_monitor_readiness.json"
_OWNER_READ_LIMIT = 128 * 1024


def _open_directory_nofollow(path: Path) -> int:
    """Anchor every absolute path component without resolving a symlink."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    absolute = path.absolute()
    descriptor = os.open(absolute.anchor, flags)
    try:
        for name in absolute.parts[1:]:
            child = os.open(name, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _owner_readiness_fields(root_descriptor: int) -> dict:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    with ExitStack() as stack:
        directory = root_descriptor
        for name in ("freqtrade", "runtime"):
            directory = os.open(name, flags, dir_fd=directory)
            stack.callback(os.close, directory)
        descriptor = os.open(
            "owner_readiness.json",
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=directory,
        )
        with os.fdopen(descriptor, "rb") as handle:
            source = os.fstat(handle.fileno())
            if (not stat.S_ISREG(source.st_mode) or source.st_nlink != 1
                    or source.st_uid not in {0, os.fstat(root_descriptor).st_uid}
                    or source.st_size > _OWNER_READ_LIMIT):
                raise ValueError("invalid owner readiness source")
            raw = handle.read(_OWNER_READ_LIMIT + 1)
            if len(raw) > _OWNER_READ_LIMIT:
                raise ValueError("oversized owner readiness source")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("invalid owner readiness object")
    if (value.get("order_owner") != "FREQTRADE"
            or value.get("execution_environment") not in ("TESTNET", "LIVE")
            or type(value.get("release_approved")) is not bool):
        raise ValueError("invalid owner readiness identity")
    generated, valid = value.get("generated_at"), value.get("valid_until")
    if (type(generated) not in (int, float) or type(valid) not in (int, float)
            or not math.isfinite(generated) or not math.isfinite(valid)
            or not 0 < valid - generated <= 60):
        raise ValueError("invalid owner readiness validity")
    blockers = value.get("blockers")
    if (not isinstance(blockers, list) or len(blockers) > 100
            or any(not isinstance(item, str)
                   or re.fullmatch(r"[A-Za-z0-9_:-]{1,160}", item) is None
                   for item in blockers)):
        raise ValueError("invalid owner readiness blockers")
    return {key: value[key] for key in (
        "order_owner", "execution_environment", "generated_at", "valid_until",
        "release_approved", "blockers",
    )}


def publish_owner_readiness(root: Path = SHARED_ROOT) -> None:
    """Copy only bounded public readiness; leave the private source untouched."""
    root_descriptor = _open_directory_nofollow(Path(root))
    try:
        try:
            value = _owner_readiness_fields(root_descriptor)
        except (OSError, ValueError, TypeError, ArithmeticError, RecursionError):
            value = {"available": False, "reason": "owner_readiness_unavailable"}
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        created = False
        try:
            directory = os.open("runtime", flags, dir_fd=root_descriptor)
        except FileNotFoundError:
            os.mkdir("runtime", mode=0o750, dir_fd=root_descriptor)
            directory = os.open("runtime", flags, dir_fd=root_descriptor)
            created = True
        try:
            if created:
                os.fchown(directory, 0, os.fstat(root_descriptor).st_gid)
                os.fchmod(directory, 0o750)
            # The checked directory remains pinned across path renames. Reuse
            # the existing root-owned 0640 atomic writer through this open fd;
            # no untrusted filesystem symlink is followed for publication.
            atomic_write(
                Path(f"/proc/self/fd/{directory}") / "owner_monitor_readiness.json", value,
            )
        finally:
            os.close(directory)
    finally:
        os.close(root_descriptor)


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
    parser.add_argument("--owner-readiness-only", action="store_true")
    args = parser.parse_args(argv)
    if args.owner_readiness_only:
        publish_owner_readiness(SHARED_ROOT)
        return 0
    resolved = args.output.resolve()
    allowed = DEFAULT_OUTPUT.parent.resolve()
    if resolved.parent != allowed:
        raise SystemExit(f"output must be directly inside {allowed}")
    published = publish_monitor_permissions()
    if (os.path.lexists(SHARED_ROOT / _OWNER_SOURCE)
            or os.path.lexists(SHARED_ROOT / _OWNER_OUTPUT)):
        publish_owner_readiness(SHARED_ROOT)
    value = collect()
    value["readonly_sources_published"] = published
    atomic_write(resolved, value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
