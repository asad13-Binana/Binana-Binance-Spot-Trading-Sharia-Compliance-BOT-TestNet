"""Archive five obsolete BINANA-only copies, verify, then remove duplicate trees."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile


ROOT = Path('/var/backups/binana-testnet')
NAMES = ['20260919T202653Z', '20260919T202747Z', '20260923T013209Z',
         '20260923T111404Z', '20260923T120758Z']


def require(condition, reason):
    if not condition:
        raise RuntimeError(reason)


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def inventory(source):
    entries = sorted(source.rglob('*'))
    require(all(not p.is_symlink() and (p.is_file() or p.is_dir()) for p in entries),
            'Non-regular entry in backup')
    return {str(p.relative_to(ROOT)): (p.stat().st_size, digest(p), p.stat().st_mtime_ns)
            for p in entries if p.is_file()}


def unmounted(source):
    ids = subprocess.check_output(['docker', 'ps', '-aq', '--filter', 'name=binana-testnet'], text=True).split()
    require(bool(ids), 'Expected BINANA containers missing')
    containers = json.loads(subprocess.check_output(['docker', 'inspect', *ids]))
    mounts = [Path(m['Source']).resolve() for c in containers for m in c['Mounts']]
    require(not any(p == source or source in p.parents or p in source.parents for p in mounts),
            'Backup overlaps a container mount')


def compact(name):
    require(ROOT.resolve() == ROOT and name in NAMES, 'Unexpected backup root/name')
    source = ROOT/name
    require(source.is_dir() and not source.is_symlink() and source.resolve().parent == ROOT,
            'Unexpected source')
    unmounted(source)
    before = inventory(source)
    require(bool(before), 'Empty backup')
    archive = ROOT/(name+'.verified.tar.gz')
    temporary = ROOT/(name+'.partial.tar.gz')
    require(not archive.exists() and not temporary.exists(), 'Archive already exists')
    with temporary.open('xb') as stream:
        os.fchmod(stream.fileno(), 0o600)
        with tarfile.open(fileobj=stream, mode='w:gz', compresslevel=6) as tar:
            tar.add(source, arcname=name, recursive=True)
        stream.flush()
        os.fsync(stream.fileno())
    seen = set()
    with tarfile.open(temporary, 'r:gz') as tar:
        for entry in tar:
            require(entry.name == name or entry.name.startswith(name+'/'), 'Unexpected archive path')
            require(entry.isfile() or entry.isdir(), 'Unexpected archive member')
            if entry.isfile():
                require(entry.name in before and entry.name not in seen, 'Unexpected duplicate/file')
                require(entry.size == before[entry.name][0], 'Size mismatch')
                with tar.extractfile(entry) as stream:
                    require(hashlib.file_digest(stream, 'sha256').hexdigest() == before[entry.name][1],
                            'Archive content mismatch')
                seen.add(entry.name)
    require(seen == set(before) and inventory(source) == before, 'Source changed or archive incomplete')
    os.replace(temporary, archive)
    receipt = {'source': str(source), 'archive': str(archive), 'files': len(before),
               'original_bytes': sum(row[0] for row in before.values()), 'archive_bytes': archive.stat().st_size,
               'archive_sha256': digest(archive), 'verified': True, 'duplicate_removed': False}
    target = ROOT/(name+'.compression-receipt.json')
    with target.open('w') as stream:
        stream.write(json.dumps(receipt, indent=2)+'\n')
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(ROOT, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    unmounted(source)
    require(source.resolve() == ROOT/name and source.parent == ROOT and not source.is_symlink(),
            'Final deletion boundary mismatch')
    require(inventory(source) == before, 'Source changed before deletion')
    shutil.rmtree(source)
    receipt['duplicate_removed'] = True
    target.write_text(json.dumps(receipt, indent=2)+'\n')
    print(json.dumps(receipt), flush=True)


if __name__ == '__main__':
    os.umask(0o077)
    for backup in NAMES:
        compact(backup)
