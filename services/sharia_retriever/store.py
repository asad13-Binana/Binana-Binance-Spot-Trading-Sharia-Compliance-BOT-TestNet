"""Content-addressed storage for exact Sharia source response bytes."""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path

_DIGEST = re.compile(r'^[0-9a-f]{64}$')
_RELATIVE = re.compile(r'^sha256/[0-9a-f]{2}/[0-9a-f]{64}\.bin$')


class EvidenceStoreError(RuntimeError):
    """Evidence bytes could not be stored or verified safely."""


class EvidenceStore:
    """Store immutable response bytes under their SHA-256 digest.

    The returned path is always relative to ``root`` and contains no
    caller-controlled filename component. Existing objects are re-hashed
    before reuse so disk corruption cannot be silently accepted.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root)

    @staticmethod
    def relative_path(digest: str) -> str:
        if not _DIGEST.fullmatch(str(digest)):
            raise EvidenceStoreError('invalid SHA-256 digest')
        return f'sha256/{digest[:2]}/{digest}.bin'

    def _target(self, relative_path: str) -> Path:
        if not _RELATIVE.fullmatch(str(relative_path)):
            raise EvidenceStoreError('invalid content-addressed evidence path')
        root = self.root.resolve()
        target = (root / relative_path).resolve()
        if root not in target.parents:
            raise EvidenceStoreError('evidence path escapes the store root')
        return target

    def put(self, payload: bytes) -> tuple[str, str]:
        if not isinstance(payload, bytes):
            raise EvidenceStoreError('evidence payload must be bytes')
        digest = hashlib.sha256(payload).hexdigest()
        relative = self.relative_path(digest)
        target = self._target(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise EvidenceStoreError(
                    f'existing evidence object failed digest verification: {relative}')
            return digest, relative

        fd, temporary = tempfile.mkstemp(
            prefix=f'.{digest}.', suffix='.tmp', dir=target.parent)
        try:
            with os.fdopen(fd, 'wb') as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            try:
                directory_fd = os.open(
                    target.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
        finally:
            try:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            except OSError:
                pass
        if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            raise EvidenceStoreError('new evidence object failed post-write verification')
        return digest, relative

    def verify(self, digest: str, relative_path: str) -> bool:
        if not _DIGEST.fullmatch(str(digest)):
            return False
        try:
            target = self._target(relative_path)
            return (target.is_file() and
                    hashlib.sha256(target.read_bytes()).hexdigest() == digest)
        except (OSError, EvidenceStoreError):
            return False

    def read(self, digest: str, relative_path: str) -> bytes:
        if not self.verify(digest, relative_path):
            raise EvidenceStoreError('evidence object is missing or failed verification')
        return self._target(relative_path).read_bytes()
