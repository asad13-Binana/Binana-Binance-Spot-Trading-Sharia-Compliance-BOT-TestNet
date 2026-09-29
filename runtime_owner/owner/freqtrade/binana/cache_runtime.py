"""Portable cache lifecycle integration. No trading keys or exchange calls."""
from __future__ import annotations
from pathlib import Path
import json
import os
import shutil
import time
from .cache import CacheManager

_next_sweep = 0.0
_manager = None


def manager() -> CacheManager:
    global _manager
    if _manager is None:
        root = Path(os.environ.get('SHARED_ROOT', '/app/shared'))
        _manager = CacheManager(root/'cache'/'binana-v1', root/'runtime'/'cache-service')
    return _manager


def maintenance(atomic_write, audit) -> None:
    global _next_sweep
    now = time.monotonic()
    if now < _next_sweep:
        return
    _next_sweep = now + 300
    root = Path(os.environ.get('SHARED_ROOT', '/app/shared'))
    try:
        result = manager().sweep()
        usage = shutil.disk_usage(root)
        data = {'ok': result.get('errors',0) == 0, 'ts': time.time(),
                'disk_percent': round(100 * usage.used / usage.total, 2),
                'last_cleanup': result, 'cache': manager().status()}
        if result.get('deleted_files') or result.get('errors'):
            audit('safe_cache_retention', details=result)
    except Exception as exc:
        data = {'ok': False, 'ts': time.time(), 'error_type': type(exc).__name__}
        audit('safe_cache_retention_failed', severity='WARNING', details={'error_type': type(exc).__name__})
    atomic_write(root/'runtime'/'cache_health.json', data)


def render_status() -> str:
    m = manager()
    data = m.status()
    disk = shutil.disk_usage(m.root)
    return '\n'.join([
        'BINANA SAFE CACHE', '',
        f"Managed disposable files: {data['registered_files']}",
        f"Managed size: {data['registered_bytes']/1024/1024:.2f} MiB",
        f"Expired, unpinned files: {data['expired_unpinned_files']}",
        f"Disk used: {100*disk.used/disk.total:.1f}%",
        f"Disk available: {disk.free/1024/1024/1024:.2f} GiB", '',
        'Automatic expiry: every 5 minutes while controller is running.',
        'Only completed registered disposable objects can be cleared.',
        'Trade DB, protection, registry, config, NFI state, backtests and Bitcoin are excluded.',
        'Unknown, changed, pinned or currently-read files are retained.',
        'This is not a Docker/system-wide cleanup.',
    ])
