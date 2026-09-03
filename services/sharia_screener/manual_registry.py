"""Strict owner-maintained Sharia registry contract.

The manual registry is an operational allowlist, not a religious ruling.  It
contains only symbols that the owner has reviewed outside the trading
process.  The trading services never edit this file and never add a symbol.
"""
from __future__ import annotations

import hashlib
import json
import re
import argparse
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path


REGISTRY_SCHEMA_VERSION = 1
MANUAL_PROJECTION_MODE = 'manual-registry/v1'
MANUAL_STATUS_SOURCE = 'manual-owner-registry/v1'
SYMBOL_RE = re.compile(r'^[A-Z0-9]{1,16}USDT$')
VERSION_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$')
MAX_REVIEW_WINDOW_DAYS = 366
MANUAL_STATUS_SCHEMA_VERSION = 2


class ManualRegistryError(ValueError):
    """The owner-maintained registry is missing, stale or malformed."""


@dataclass(frozen=True)
class ManualRegistry:
    path: Path
    sha256: str
    version: str
    last_reviewed: date | None
    next_review: date | None
    symbols: tuple[str, ...]

    @property
    def bases(self) -> tuple[str, ...]:
        return tuple(symbol[:-4] for symbol in self.symbols)


def _parse_date(value: object, field: str) -> date:
    try:
        parsed = date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ManualRegistryError(f'{field} must be an ISO date (YYYY-MM-DD)') from exc
    return parsed


def load_manual_registry(path: str | Path, *, today: date | None = None) -> ManualRegistry:
    """Load and strictly validate ``halal_coins.json``.

    An empty list is a valid deny-all bootstrap and may omit review dates.  A
    non-empty list requires explicit current review dates.  Symbols are exact
    uppercase Binance-style Spot/USDT identifiers such as ``SOLUSDT``.
    """
    source = Path(path)
    try:
        raw_bytes = source.read_bytes()
        payload = json.loads(raw_bytes.decode('utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ManualRegistryError(f'cannot read manual registry {source}: {exc}') from exc
    if not isinstance(payload, dict):
        raise ManualRegistryError('manual registry must be a JSON object')
    if payload.get('schema_version') != REGISTRY_SCHEMA_VERSION:
        raise ManualRegistryError(
            f'manual registry schema_version must be {REGISTRY_SCHEMA_VERSION}')
    version = str(payload.get('version', '')).strip()
    if VERSION_RE.fullmatch(version) is None:
        raise ManualRegistryError('manual registry version is missing or invalid')
    symbols = payload.get('symbols')
    if not isinstance(symbols, list):
        raise ManualRegistryError('manual registry symbols must be an array')

    normalized: list[str] = []
    seen: set[str] = set()
    for index, value in enumerate(symbols):
        if not isinstance(value, str) or SYMBOL_RE.fullmatch(value) is None:
            raise ManualRegistryError(
                f'symbols[{index}] must be an exact uppercase Spot/USDT symbol')
        if value in seen:
            raise ManualRegistryError(f'duplicate manual registry symbol: {value}')
        seen.add(value)
        normalized.append(value)
    if normalized != sorted(normalized):
        raise ManualRegistryError('manual registry symbols must be sorted')

    reviewed_raw = payload.get('last_reviewed')
    next_raw = payload.get('next_review')
    if not normalized and reviewed_raw in (None, '') and next_raw in (None, ''):
        reviewed = None
        next_review = None
    else:
        reviewed = _parse_date(reviewed_raw, 'last_reviewed')
        next_review = _parse_date(next_raw, 'next_review')
        current = today or datetime.now(timezone.utc).date()
        if reviewed > current:
            raise ManualRegistryError('last_reviewed cannot be in the future')
        if next_review <= current:
            raise ManualRegistryError('manual registry review has expired')
        if next_review <= reviewed:
            raise ManualRegistryError('next_review must be after last_reviewed')
        if (next_review - reviewed).days > MAX_REVIEW_WINDOW_DAYS:
            raise ManualRegistryError(
                f'manual registry review window exceeds {MAX_REVIEW_WINDOW_DAYS} days')

    return ManualRegistry(
        path=source,
        sha256=hashlib.sha256(raw_bytes).hexdigest(),
        version=version,
        last_reviewed=reviewed,
        next_review=next_review,
        symbols=tuple(normalized),
    )


def build_manual_decision_report(registry: ManualRegistry, base: str) -> dict:
    base = str(base).upper().strip()
    symbol = base + 'USDT'
    if symbol not in registry.symbols:
        raise ManualRegistryError(f'{symbol} is not present in the manual registry')
    if registry.last_reviewed is None or registry.next_review is None:
        raise ManualRegistryError('a non-empty registry requires review dates')
    return {
        'schema_version': REGISTRY_SCHEMA_VERSION,
        'report_type': 'manual-owner-approved-asset',
        'symbol': base,
        'pair': f'{base}/USDT',
        'status': 'GREEN',
        'registry_file': registry.path.name,
        'registry_sha256': registry.sha256,
        'registry_version': registry.version,
        'last_reviewed': registry.last_reviewed.isoformat(),
        'next_review': registry.next_review.isoformat(),
        'owner_maintained': True,
        'research_only_not_fatwa': True,
    }


def validate_manual_decision_report(report: object, *, expected_base: str,
                                    expected_registry_sha256: str = '') -> dict:
    if not isinstance(report, dict):
        raise ManualRegistryError('manual registry report must be an object')
    base = str(expected_base).upper().strip()
    if report.get('schema_version') != REGISTRY_SCHEMA_VERSION:
        raise ManualRegistryError('manual registry report schema is invalid')
    if report.get('report_type') != 'manual-owner-approved-asset':
        raise ManualRegistryError('manual registry report type is invalid')
    if str(report.get('symbol', '')).upper() != base:
        raise ManualRegistryError('manual registry report symbol binding mismatch')
    if str(report.get('pair', '')).upper() != f'{base}/USDT':
        raise ManualRegistryError('manual registry report pair binding mismatch')
    if report.get('status') != 'GREEN':
        raise ManualRegistryError('manual registry report status must be GREEN')
    registry_sha = str(report.get('registry_sha256', '')).lower()
    if re.fullmatch(r'[0-9a-f]{64}', registry_sha) is None:
        raise ManualRegistryError('manual registry report hash is invalid')
    if expected_registry_sha256 and registry_sha != expected_registry_sha256:
        raise ManualRegistryError('manual registry report hash binding mismatch')
    if VERSION_RE.fullmatch(str(report.get('registry_version', '')).strip()) is None:
        raise ManualRegistryError('manual registry report version is invalid')
    reviewed = _parse_date(report.get('last_reviewed'), 'last_reviewed')
    next_review = _parse_date(report.get('next_review'), 'next_review')
    if next_review <= reviewed:
        raise ManualRegistryError('manual registry report review dates are invalid')
    if report.get('owner_maintained') is not True:
        raise ManualRegistryError('manual registry report lacks owner-maintained binding')
    if report.get('research_only_not_fatwa') is not True:
        raise ManualRegistryError('manual registry report lacks research-only disclaimer')
    return report


def build_manual_bootstrap_status(registry: ManualRegistry) -> dict:
    """Build a deny-all projection for installation before the signer starts.

    The document binds the current registry hash but deliberately contains no
    approved records.  It therefore lets a clean-host installer validate the
    owner file without creating an unsigned trading permission.  The isolated
    projector replaces it with signed records after service startup.
    """
    return {
        '_comment': (
            'Deny-all bootstrap projection for the owner-maintained '
            'halal_coins.json registry. The isolated manual registry service '
            'creates signed approved records after startup. No automatic '
            'Sharia research is performed.'),
        'schema_version': MANUAL_STATUS_SCHEMA_VERSION,
        'projection_mode': MANUAL_PROJECTION_MODE,
        'registry_valid': True,
        'registry_sha256': registry.sha256,
        'registry_version': registry.version,
        'projection_complete': False,
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'records': [],
    }


def write_manual_bootstrap_status(registry_path: str | Path,
                                  status_path: str | Path) -> ManualRegistry:
    """Validate the owner file and atomically install a deny-all projection."""
    from services.common.atomic import atomic_write_json

    registry = load_manual_registry(registry_path)
    atomic_write_json(status_path, build_manual_bootstrap_status(registry))
    return registry


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    subparsers = parser.add_subparsers(dest='command', required=True)
    validate = subparsers.add_parser(
        'validate', help='validate the manual registry without changing files')
    validate.add_argument('registry_path')
    bootstrap = subparsers.add_parser(
        'bootstrap-status',
        help='validate the manual registry and write a deny-all status projection',
    )
    bootstrap.add_argument('registry_path')
    bootstrap.add_argument('status_path')
    args = parser.parse_args(argv)
    try:
        if args.command == 'validate':
            registry = load_manual_registry(args.registry_path)
        else:
            registry = write_manual_bootstrap_status(
                args.registry_path, args.status_path)
    except ManualRegistryError as exc:
        parser.exit(2, f'MANUAL SHARIA REGISTRY BLOCKED: {exc}\n')
    action = ('validated' if args.command == 'validate'
              else 'validated; deny-all bootstrap installed')
    print('manual Sharia registry ' + action +
          f' (version={registry.version}, symbols={len(registry.symbols)})')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
