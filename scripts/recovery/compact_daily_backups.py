"""Verify and archive previous daily copies before the next scheduled backup.

The previous backup remains recoverable as a verified tar.gz even if creation
of the next backup fails. Run under the backup service's existing root boundary.
Never touches the live shared tree, rehearsal receipts or rollout backups.
"""
import os
import json
from pathlib import Path
import re
import time

import compact_old_binana_backups_20261007 as archive


def prune_archives(root, *, keep):
    """Retain complete verified daily archives; named repair evidence is excluded."""
    archive.require(2 <= keep <= 90, 'BACKUP_RETAIN must be 2..90')
    archive.require(root.resolve()==root and not root.is_symlink(), 'Backup root changed')
    paths=sorted((p for p in root.iterdir()
        if re.fullmatch(r'20\d{6}T\d{6}Z\.verified\.tar\.gz',p.name)),reverse=True)
    if len(paths)<=keep:return
    verified=[]
    # Verify retained copies as well, before deleting anything. Each receipt is
    # root-owned evidence of the full per-file archive verification at creation.
    for path in paths:
        archive.require(path.is_file() and not path.is_symlink() and path.resolve().parent==root,
                        'Unexpected archive path')
        receipt_path=root/(path.name.removesuffix('.verified.tar.gz')+'.compression-receipt.json')
        archive.require(receipt_path.is_file() and not receipt_path.is_symlink(), 'Archive receipt missing')
        try:receipt=json.loads(receipt_path.read_text())
        except (ValueError,OSError):raise RuntimeError('Archive receipt unreadable') from None
        source=root/path.name.removesuffix('.verified.tar.gz')
        # Two historical receipt schemas predate duplicate_removed and use
        # different field names. Original-tree absence is verified directly.
        archive.require((receipt.get('verified') is True or receipt.get('all_file_content_and_modes_verified') is True)
            and receipt.get('duplicate_removed',True) is True
            and receipt.get('source')==str(source) and not source.exists() and not source.is_symlink()
            and receipt.get('archive')==str(path) and receipt.get('files',receipt.get('verified_files',0))>0
            and receipt.get('archive_bytes')==path.stat().st_size
            and receipt.get('archive_sha256',receipt.get('sha256'))==archive.digest(path), 'Archive verification failed')
        verified.append((path,path.stat(),receipt_path,receipt))
    for path,before,receipt_path,receipt in verified[keep:]:
        archive.unmounted(path)
        current=path.stat()
        archive.require(not path.is_symlink() and path.resolve().parent==root
            and (current.st_dev,current.st_ino,current.st_size,current.st_mtime_ns)==
                (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns), 'Archive changed before retention')
        path.unlink()
        receipt.update(retention_removed=True,retention_removed_at=time.time(),retained_daily_copies=keep)
        with receipt_path.open('w') as stream:
            stream.write(json.dumps(receipt,indent=2)+'\n');stream.flush();os.fsync(stream.fileno())
        print(json.dumps({'retention_removed':str(path),'retained_daily_copies':keep}),flush=True)


def main():
    import fcntl
    if os.geteuid()!=0:raise RuntimeError('ROOT_REQUIRED')
    os.umask(0o077)
    root=archive.ROOT
    if root.resolve()!=root or root.is_symlink():raise RuntimeError('BACKUP_ROOT_CHANGED')
    with (root/'.backup.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        archive.NAMES=sorted(p.name for p in root.iterdir()
            if re.fullmatch(r'20\d{6}T\d{6}Z',p.name) and p.is_dir())
        for name in archive.NAMES:archive.compact(name)
        prune_archives(root,keep=int(os.environ.get('BACKUP_RETAIN','14')))


if __name__=='__main__':main()
