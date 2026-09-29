from __future__ import annotations
"""Signed live-promotion evidence gate (fixes C-003 and H-007).

The preserved core's live interlock accepts presence-only marker files and a
backtest JSON produced by its own legacy engine — evidence that is forgeable
with `touch` and that does not prove the EXACT Freqtrade strategy was ever
replayed. The preserved gate still runs (the legacy source is untouched), but
live mode now ALSO requires this sidecar gate, which only passes on an
HMAC-signed, expiring evidence envelope that binds:

  * the installed release hash;
  * the canonical fingerprints of the four protected strategy methods;
  * the immutable V19.1 controller hash;
  * an exact-strategy Freqtrade backtest artifact (name, hash, trade count);
  * explicit Testnet / Oracle / clean-pass assertions.

The signing key (LIVE_EVIDENCE_KEY) is operator-held and must never be
present in the repository or image. Without a valid envelope, live mode is
impossible regardless of any legacy marker files.
"""
import os
import re
from pathlib import Path

from services.common.envelope import BUS_LIVE_EVIDENCE, EnvelopeError, read_verified_file
from services.common.paths import RUNTIME
from services.common.sharia_v19 import V19_CONTROLLER_SHA256

EVIDENCE_PRODUCER = 'release-certifier'
REQUIRED_ASSERTIONS = (
    'binance_spot_testnet_lifecycle_passed',
    'oracle_deployment_soak_completed',
    'step1_clean_passes_completed',
    'final_clean_passes_completed',
)
_HEX64 = re.compile(r'^[0-9a-f]{64}$')


class LiveEvidenceError(RuntimeError):
    pass


def evidence_path() -> Path:
    return Path(os.getenv('LIVE_EVIDENCE_FILE', str(RUNTIME / 'LIVE_EVIDENCE.json')))


def verify_live_evidence(*, release_hash: str, strategy_fingerprints: dict,
                         controller_sha256: str = V19_CONTROLLER_SHA256,
                         path: str | Path | None = None) -> dict:
    target = Path(path) if path else evidence_path()
    if not target.exists():
        raise LiveEvidenceError(f'live evidence envelope missing: {target}')
    try:
        payload = read_verified_file(
            target, purpose=BUS_LIVE_EVIDENCE, expected_producers={EVIDENCE_PRODUCER})
    except EnvelopeError as exc:
        raise LiveEvidenceError(f'live evidence envelope rejected: {exc}') from exc

    if str(payload.get('release_hash', '')) != release_hash:
        raise LiveEvidenceError('live evidence is bound to a different release hash')
    if str(payload.get('controller_sha256', '')) != controller_sha256:
        raise LiveEvidenceError('live evidence is bound to a different V19.1 controller')
    if payload.get('strategy_fingerprints') != strategy_fingerprints:
        raise LiveEvidenceError('live evidence strategy fingerprints do not match the installed strategy')

    backtest = payload.get('freqtrade_backtest')
    if not isinstance(backtest, dict):
        raise LiveEvidenceError('live evidence lacks the exact-strategy Freqtrade backtest record')
    if str(backtest.get('strategy', '')) != 'IctSmcStrategy':
        raise LiveEvidenceError('backtest evidence is not for IctSmcStrategy')
    if not _HEX64.match(str(backtest.get('artifact_sha256', ''))):
        raise LiveEvidenceError('backtest evidence lacks a valid artifact sha256')
    minimum_trades = int(os.getenv('LIVE_MIN_BACKTEST_TRADES', '100'))
    if int(backtest.get('trades', 0) or 0) < minimum_trades:
        raise LiveEvidenceError(f'backtest evidence has fewer than {minimum_trades} trades')
    if not str(backtest.get('timerange', '')).strip():
        raise LiveEvidenceError('backtest evidence lacks a timerange')

    assertions = payload.get('assertions')
    if not isinstance(assertions, dict):
        raise LiveEvidenceError('live evidence lacks the assertions object')
    for name in REQUIRED_ASSERTIONS:
        if assertions.get(name) is not True:
            raise LiveEvidenceError(f'live evidence assertion not satisfied: {name}')
    return payload
