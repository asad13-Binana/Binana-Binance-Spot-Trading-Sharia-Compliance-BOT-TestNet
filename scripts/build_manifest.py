from __future__ import annotations
import hashlib, json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.common.owner_source_integrity import verify  # noqa: E402

EXCLUDE = {
    'RELEASE_MANIFEST.json', 'RELEASE_SHA256.txt',
}
EXCLUDE_PARTS = {'.git', '__pycache__', '.pytest_cache', '.ruff_cache', '.hypothesis'}

def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    mode_path = ROOT / 'RELEASE_MODE'
    package_mode = mode_path.read_text(encoding='utf-8').strip() if mode_path.is_file() else 'unconfigured'
    if package_mode not in {'testnet', 'live'}:
        raise SystemExit('RELEASE_MODE must be testnet or live before building the manifest')
    # V102-REM-009: the release label comes from RELEASE_VERSION metadata.
    version_path = ROOT / 'RELEASE_VERSION'
    release_version = (version_path.read_text(encoding='utf-8').strip()
                       if version_path.is_file() else 'V10.2')
    files = {}
    for path in sorted(ROOT.rglob('*')):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT).as_posix()
        if rel in EXCLUDE or any(part in EXCLUDE_PARTS for part in path.parts):
            continue
        files[rel] = {'sha256': sha(path), 'size': path.stat().st_size}
    protected = verify(ROOT)
    manifest = {
        'release': f'{release_version}-{package_mode.upper()}',
        'package_mode': package_mode,
        'lineage': 'Freqtrade-owned Oracle bundle; historical source identities retained in Git',
        'manifest_format': 2,
        'safety_default': 'stopped',
        'live_certified': False,
        'source_archives': {
            'binance_bot_V4.9.16_COMPLETE-1.zip': 'e89c4025ce423bcaea67be226dbc7243d929c2da7a7e5e3a44152ab570fef41a',
            'ict_smc_freqtrade_DEPLOY_v7_MERGED-2.zip': 'e4812f3af69a1c55a633861fbbd243e1c612161770c5c7cad033842aff1a54d5',
        },
        'preservation': {'execution_owner': 'BinanaNfiSpot', 'protected_source_sha256': protected},
        'files': files,
    }
    payload = json.dumps(manifest, indent=2, sort_keys=True) + '\n'
    # Write raw bytes so the on-disk manifest matches the hashed payload on
    # every OS. Path.write_text uses text mode and would translate '\n' to
    # '\r\n' on Windows, making sha256(file) != sha256(payload).
    (ROOT / 'RELEASE_MANIFEST.json').write_bytes(payload.encode('utf-8'))
    release_hash = hashlib.sha256(payload.encode('utf-8')).hexdigest()
    (ROOT / 'RELEASE_SHA256.txt').write_bytes(
        (release_hash + '  RELEASE_MANIFEST.json\n').encode('utf-8'))
    print(release_hash)

if __name__ == '__main__':
    main()
