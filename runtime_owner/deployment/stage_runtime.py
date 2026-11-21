"""Carry Python's extra shared libraries and their real Debian package records."""
from pathlib import Path
import shutil
import subprocess


root = Path('/runtime-extra')
# The distroless base already supplies libc, libgcc, libstdc++, SSL and zlib.
# SQLite, curses/tinfo and UUID come exclusively from the patched source stage.
packages = ('libbz2-1.0', 'libdb5.3t64', 'libffi8', 'libgdbm6t64',
            'libgdbm-compat4t64', 'libgomp1', 'liblzma5', 'libreadline8t64', 'libzstd1')
for package in packages:
    paths = subprocess.check_output(['dpkg-query', '-L', package], text=True).splitlines()
    for item in paths:
        source = Path(item)
        if source.is_file() and ('.so' in source.name or source.name == 'copyright'):
            destination = root / source.relative_to('/')
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination, follow_symlinks=False)
    record = subprocess.check_output(['dpkg-query', '-s', package], text=True)
    destination = root / 'var/lib/dpkg/status.d' / package
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(record)
