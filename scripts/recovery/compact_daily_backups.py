"""Verify and archive previous daily copies before the next scheduled backup.

The previous backup remains recoverable as a verified tar.gz even if creation
of the next backup fails. Run under the backup service's existing root boundary.
Never touches the live shared tree, rehearsal receipts or rollout backups.
"""
import fcntl
import os
from pathlib import Path
import re

import compact_old_binana_backups_20261007 as archive


def main():
    if os.geteuid()!=0:raise RuntimeError('ROOT_REQUIRED')
    os.umask(0o077)
    root=archive.ROOT
    if root.resolve()!=root or root.is_symlink():raise RuntimeError('BACKUP_ROOT_CHANGED')
    with (root/'.backup.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        archive.NAMES=sorted(p.name for p in root.iterdir()
            if re.fullmatch(r'20\d{6}T\d{6}Z',p.name) and p.is_dir())
        for name in archive.NAMES:archive.compact(name)


if __name__=='__main__':main()
