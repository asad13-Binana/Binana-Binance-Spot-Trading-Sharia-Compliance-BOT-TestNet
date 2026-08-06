from __future__ import annotations
"""Runtime package-mode interlock (V102-REM-001 / audit ISSUE 1).

The release ships as two packages that differ in the immutable RELEASE_MODE
file: ``testnet`` and ``live``. Before this interlock, the distinction was
enforced only by the installer and documentation — a user could edit the
private ``.env`` to ``EXECUTION_MODE=live`` and drive a *testnet* package
down the live execution path. This module makes the packaged mode a runtime
law inside the execution sidecar, checked before any authenticated Binance
client, user-data stream, reconciliation, order, or live-evidence code runs.

The mode is read from the RELEASE_MODE file shipped inside the package (and
baked read-only into the service image), NEVER from an environment variable,
so no ``.env`` edit, direct ``docker compose up``, or restored deployment
can bypass it.

Allowed execution modes per package:

  testnet package -> testnet, simulation   (simulation is strictly safer)
  live package    -> live, simulation      (cross-mode testnet deployment is
                                            rejected, matching the installer
                                            contract in
                                            docs/GITHUB_ORACLE_DEPLOYMENT.md)

The live package's own live-promotion evidence gates are unchanged — this
interlock runs in front of them and can only ever REMOVE capability from a
package, never add it.
"""
from pathlib import Path

VALID_PACKAGE_MODES = {'live', 'testnet'}
ALLOWED_EXECUTION_MODES = {
    'testnet': {'testnet', 'simulation'},
    'live': {'live', 'simulation'},
}
VALID_SHARIA_GATE_MODES = {'fresh', 'cached'}

# parents[2] of this file is the repository root on a development host and
# /app inside the service image (Dockerfile.services bakes RELEASE_MODE there
# with mode 0444). Tests may patch this constant; production cannot.
PACKAGE_MODE_FILE = Path(__file__).resolve().parents[2] / 'RELEASE_MODE'


def load_package_mode(path: str | Path | None = None) -> str:
    """Read and validate the shipped package mode. Fail closed on anything
    missing or malformed: a package that cannot prove its mode must not
    reach an exchange."""
    target = Path(path) if path is not None else PACKAGE_MODE_FILE
    try:
        mode = target.read_text(encoding='utf-8').strip().lower()
    except OSError as exc:
        raise SystemExit(
            f'PACKAGE MODE BLOCKED: RELEASE_MODE is unreadable at {target}: {exc}'
        ) from exc
    if mode not in VALID_PACKAGE_MODES:
        raise SystemExit(
            f'PACKAGE MODE BLOCKED: RELEASE_MODE {mode!r} is invalid '
            f'(expected one of {sorted(VALID_PACKAGE_MODES)})')
    return mode


def enforce_package_mode(execution_mode: str,
                         path: str | Path | None = None) -> str:
    """Block execution modes the shipped package does not permit.

    Returns the validated package mode so the caller can log/audit it.
    Raises SystemExit — the same fail-closed mechanism as the existing
    live interlock — before any authenticated action can occur.
    """
    package = load_package_mode(path)
    execution = str(execution_mode or '').strip().lower()
    allowed = ALLOWED_EXECUTION_MODES[package]
    if execution not in allowed:
        if package == 'testnet' and execution == 'live':
            raise SystemExit(
                'LIVE BLOCKED: this is the TESTNET package; EXECUTION_MODE=live '
                'is never permitted, regardless of .env contents or live '
                'evidence. Use the live-capable package and its documented '
                'promotion procedure.')
        raise SystemExit(
            f'PACKAGE MODE BLOCKED: the {package} package permits '
            f'EXECUTION_MODE {sorted(allowed)}; got {execution!r}')
    return package


def enforce_sharia_gate_mode(package_mode: str, gate_mode: str) -> str:
    """Make fresh signal-time screening immutable for the live package."""
    package = str(package_mode or '').strip().lower()
    gate = str(gate_mode or '').strip().lower()
    if package not in VALID_PACKAGE_MODES or gate not in VALID_SHARIA_GATE_MODES:
        raise SystemExit(
            f'SHARIA GATE BLOCKED: package={package!r}, gate={gate!r}; '
            f'expected package {sorted(VALID_PACKAGE_MODES)} and gate '
            f'{sorted(VALID_SHARIA_GATE_MODES)}')
    if package == 'live' and gate != 'fresh':
        raise SystemExit(
            'LIVE BLOCKED: the live package requires SHARIA_SIGNAL_GATE_MODE=fresh; '
            'cached screening is simulation/testnet-only')
    return gate
