"""Measure the owner's actual persistence filesystem before new entries."""
import os
from pathlib import Path
import shutil
import tempfile
from time import time


def persistence_readiness(database_path):
    directory = Path(database_path).resolve().parent
    result = {'ok': False, 'checked_at': time(), 'blockers': []}
    try:
        usage = shutil.disk_usage(directory)
        result.update(free_bytes=usage.free, total_bytes=usage.total)
        if usage.total <= 0 or usage.free < 5 * 1024**3 or usage.used / usage.total >= .90:
            result['blockers'].append('PERSISTENCE_DISK_CRITICAL')
        if hasattr(os, 'statvfs'):
            fs = os.statvfs(directory)
            result.update(free_inodes=fs.f_favail, total_inodes=fs.f_files)
            if fs.f_files and (fs.f_favail < 1024 or fs.f_favail / fs.f_files < .01):
                result['blockers'].append('PERSISTENCE_INODES_CRITICAL')
        # Verify a write and fsync as this process on the same filesystem.
        with tempfile.NamedTemporaryFile(dir=directory, prefix='.entry-readiness-') as stream:
            stream.write(b'1'); stream.flush(); os.fsync(stream.fileno())
    except (OSError, ValueError) as exc:
        result['blockers'].append('PERSISTENCE_UNAVAILABLE:' + type(exc).__name__)
    result['ok'] = not result['blockers']
    return result
