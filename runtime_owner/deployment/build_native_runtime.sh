#!/bin/sh
# Build-only stage. No compiler, source archive or administrative utility ships.
set -eu
mkdir -p /tmp/native-source /opt/native/lib /opt/native/share
cd /tmp/native-source
python - <<'PY'
import hashlib
import json
from pathlib import Path
import urllib.request

sources = [
    ('sqlite', '3.54.1', 'https://sqlite.org/2026/sqlite-autoconf-3540100.tar.gz',
     '926ef8d3eab93fbf6e06011901273eec076b25594a4e1cdadd50c987f4f6d64e'),
    ('ncurses', '6.6', 'https://ftp.gnu.org/gnu/ncurses/ncurses-6.6.tar.gz',
     '355b4cbbed880b0381a04c46617b7656e362585d52e9cf84a67e2009b749ff11'),
    ('util-linux', '2.42.4', 'https://www.kernel.org/pub/linux/utils/util-linux/v2.42/util-linux-2.42.4.tar.xz',
     'fbd62a100ab7bb8746ba0661255c3c48185b1e9021507c624da01fbc696330ec'),
]
manifest = []
for name, version, url, expected in sources:
    request = urllib.request.Request(url, headers={'User-Agent': 'BINANA-offline-build/1.0'})
    with urllib.request.urlopen(request, timeout=120) as response:
        raw = response.read(20_000_001)
    if hashlib.sha256(raw).hexdigest() != expected:
        raise SystemExit(f'Native source checksum mismatch: {name}')
    Path(url.rsplit('/', 1)[1]).write_bytes(raw)
    manifest.append(dict(name=name, version=version, url=url, sha256=expected))
Path('/opt/native/share/binana-native-runtime.json').write_text(
    json.dumps({'sources': manifest}, indent=2) + '\n')
PY
tar -xzf sqlite-autoconf-3540100.tar.gz
cd sqlite-autoconf-3540100
# Preserve the previous SQLite features used by Python and recovery databases.
cc -O2 -DNDEBUG -D_FORTIFY_SOURCE=2 -fstack-protector-strong -fPIC -shared \
    -Wl,-soname,libsqlite3.so.0 -Wl,-z,relro,-z,now \
    -DSQLITE_THREADSAFE=1 -DSQLITE_USE_URI=1 -DSQLITE_SECURE_DELETE \
    -DSQLITE_ENABLE_COLUMN_METADATA -DSQLITE_ENABLE_DBSTAT_VTAB \
    -DSQLITE_ENABLE_FTS3 -DSQLITE_ENABLE_FTS3_PARENTHESIS -DSQLITE_ENABLE_FTS4 \
    -DSQLITE_ENABLE_FTS5 -DSQLITE_ENABLE_RTREE -DSQLITE_ENABLE_MATH_FUNCTIONS \
    -DSQLITE_ENABLE_SESSION -DSQLITE_ENABLE_PREUPDATE_HOOK \
    -DSQLITE_ENABLE_UNLOCK_NOTIFY -DSQLITE_ENABLE_STMTVTAB \
    -DSQLITE_MAX_VARIABLE_NUMBER=250000 -DSQLITE_DEFAULT_AUTOVACUUM \
    -DSQLITE_SOUNDEX -DSQLITE_LIKE_DOESNT_MATCH_BLOBS \
    sqlite3.c -o /opt/native/lib/libsqlite3.so.3.54.1 -lm -ldl -pthread
ln -s libsqlite3.so.3.54.1 /opt/native/lib/libsqlite3.so.0
cd /tmp/native-source
tar -xzf ncurses-6.6.tar.gz
cd ncurses-6.6
export CFLAGS='-O2 -D_FORTIFY_SOURCE=2 -fstack-protector-strong'
export LDFLAGS='-Wl,-z,relro,-z,now'
./configure --prefix=/opt/native --libdir=/opt/native/lib --enable-widec \
    --with-shared --without-normal --without-debug --without-ada \
    --without-cxx-binding --without-tests --without-progs --without-manpages \
    --with-termlib=tinfo --with-versioned-syms
make -j2
make install.libs
cd /tmp/native-source
tar -xJf util-linux-2.42.4.tar.xz
cd util-linux-2.42.4
./configure --prefix=/opt/native --libdir=/opt/native/lib \
    --disable-all-programs --enable-libuuid --disable-static
make -j2
make install
python - <<'PY'
import hashlib
import json
from pathlib import Path

root = Path('/opt/native/lib')
# Retain only the compiled runtime libraries, never libtool metadata or headers.
for path in root.iterdir():
    if path.is_file() and '.so' not in path.name:
        path.unlink()
manifest = Path('/opt/native/share/binana-native-runtime.json')
data = json.loads(manifest.read_text())
data['libraries'] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in sorted(root.glob('*.so*'))}
manifest.write_text(json.dumps(data, indent=2) + '\n')
PY
