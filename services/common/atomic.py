from __future__ import annotations
import json, os, tempfile
from pathlib import Path
from typing import Any


def atomic_write_json(path: str | Path, payload: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=target.name + '.', suffix='.tmp', dir=target.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(payload, f, indent=2, sort_keys=True, default=str)
            f.flush(); os.fsync(f.fileno())
        os.replace(tmp, target)
        # Persist the directory entry as well as the file contents. This reduces
        # the chance of losing a just-replaced state file during a host crash.
        try:
            dir_fd = os.open(target.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            # Some filesystems/platforms do not support directory fsync.
            pass
    finally:
        try:
            if os.path.exists(tmp): os.unlink(tmp)
        except OSError:
            pass


def read_json(path: str | Path, default=None):
    try:
        with Path(path).open(encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default
