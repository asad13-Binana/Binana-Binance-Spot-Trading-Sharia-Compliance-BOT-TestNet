from __future__ import annotations
import hashlib, json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.common.strategy_fingerprint import fingerprints  # noqa: E402

EXCLUDE = {
    'RELEASE_MANIFEST.json', 'RELEASE_SHA256.txt',
}
EXCLUDE_PARTS = {'.git', '__pycache__', '.pytest_cache', '.ruff_cache'}

SIGNAL_METHODS = [
    'populate_indicators_5m', 'populate_indicators',
    'populate_entry_trend', 'populate_exit_trend',
]
# Canonical source/token fingerprints (interpreter-independent; see
# services/common/strategy_fingerprint.py). These correspond to the exact
# reviewed method text of the original strategy; the V8.1 ast.dump() hashes
# were retired because Python 3.12 changed the AST representation of
# unchanged source (release blocker in BINANCE_BOT_FINAL_VERDICT_IMP.md).
EXPECTED_SIGNAL_FINGERPRINTS = {
    'populate_indicators_5m': {
        'source_sha256': '3c01ceda9807efbcf63b32297879c04af6cc65744387dd4821a2ed1328025969',
        'token_sha256': '071a394a70a2370b05700ba8e58bfbbeec6d66fb48ca78995dc8fc5dd98e265b',
    },
    'populate_indicators': {
        'source_sha256': '11c39597e4c7f535808e36db290f36f8908dc0e3b98578d9d92dd1e8abd93526',
        'token_sha256': 'ab8b017314652ed1d08f9c23813eade8a79d5a67c0831a624095f315ef6ecd59',
    },
    'populate_entry_trend': {
        'source_sha256': '277b321d40430842a0462182df665ca1e4f0a1546ced7bf2ffc75eb5b7b8c11e',
        'token_sha256': '954d39c9f92fc3a7ce744cc2ef0269e38d32949612fea4ba6baed07049e0c8ad',
    },
    'populate_exit_trend': {
        'source_sha256': 'fdd2c099edf44b4db4408a5aec183f2c493c95db34d66975697dc6a50c50c196',
        'token_sha256': 'dc79eb4e19e4c68db8c3e877c671a115edb11f87f40cf1a9b34fbb0c457ccd7f',
    },
}


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
    strategy = ROOT / 'freqtrade/user_data/strategies/IctSmcStrategy.py'
    manifest = {
        'release': f'{release_version}-{package_mode.upper()}',
        'package_mode': package_mode,
        'lineage': 'V8.1-MERGED-AUDITED (safety baseline) + reviewed V10 operational imports',
        'manifest_format': 2,
        'safety_default': 'testnet' if package_mode == 'testnet' else 'simulation',
        'live_certified': False,
        'source_archives': {
            'binance_bot_V4.9.16_COMPLETE-1.zip': 'e89c4025ce423bcaea67be226dbc7243d929c2da7a7e5e3a44152ab570fef41a',
            'ict_smc_freqtrade_DEPLOY_v7_MERGED-2.zip': 'e4812f3af69a1c55a633861fbbd243e1c612161770c5c7cad033842aff1a54d5',
        },
        'preservation': {
            'legacy_core_sha256': sha(ROOT / 'legacy_core/binance_bot_V4.9.16_ALL_IN_ONE.py'),
            'expected_legacy_core_sha256': '70b1d67cc0092b5b8db4a68b343cf893641bde1aae580e9ef51e2adec1062459',
            'strategy_signal_fingerprints': fingerprints(strategy, 'IctSmcStrategy', SIGNAL_METHODS),
            'expected_strategy_signal_fingerprints': EXPECTED_SIGNAL_FINGERPRINTS,
            'fingerprint_method': 'canonical source segment + logical token stream '
                                  '(services/common/strategy_fingerprint.py); '
                                  'interpreter-independent, Python 3.10-3.13',
        },
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
