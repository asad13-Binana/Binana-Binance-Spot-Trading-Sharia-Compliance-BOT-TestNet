from __future__ import annotations
import argparse, json
from datetime import datetime, timezone
from pathlib import Path
from services.universe_service.sharia_filter import ShariaFilter, VALID, ALLOWED, SCHEMA_VERSION


def validate(path: Path) -> list[str]:
    """Schema validation for the V19.1 status projection.

    V101-NEW-008 fix: the validator now applies the SAME future-date rule as
    the runtime filter, so release verification can never call a dataset valid
    that the runtime would exclude.
    """
    errors = []
    raw = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(raw, dict) or raw.get('schema_version') != SCHEMA_VERSION:
        return [f'schema_version must be {SCHEMA_VERSION} (legacy Sharia datasets are rejected)']
    if not str(raw.get('controller_sha256', '')).strip():
        errors.append('controller_sha256 binding is required')
    records = raw.get('records')
    if not isinstance(records, list) or not records:
        return errors + ['records must be a non-empty list']
    now = datetime.now(timezone.utc)
    seen = set()
    for i, row in enumerate(records):
        where = f'records[{i}]'
        if not isinstance(row, dict):
            errors.append(where + ' must be an object'); continue
        symbol = str(row.get('symbol', '')).upper()
        if not symbol or not symbol.isalnum(): errors.append(where + '.symbol invalid')
        if symbol in seen: errors.append(where + '.symbol duplicate')
        seen.add(symbol)
        status = str(row.get('status', '')).upper()
        if status not in VALID: errors.append(where + f'.status {status!r} is not a V19.1 final code')
        if not str(row.get('source', '')).strip(): errors.append(where + '.source required')
        try:
            reviewed = datetime.fromisoformat(str(row.get('reviewed_at', ''))[:10]).date()
            # Same rule as ShariaFilter.decision: a future review date is invalid.
            if reviewed > now.date():
                errors.append(where + '.reviewed_at is in the future')
        except Exception: errors.append(where + '.reviewed_at invalid')
        try:
            expiry = datetime.fromisoformat(str(row.get('expires_at', '')).replace('Z', '+00:00'))
            if expiry.tzinfo is None: expiry = expiry.replace(tzinfo=timezone.utc)
            if status in ALLOWED and expiry <= now:
                errors.append(where + f' {status} record is expired')
        except Exception: errors.append(where + '.expires_at invalid')
    if not errors:
        ShariaFilter(path)
    return errors


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('path', nargs='?', default='/app/shared/sharia/sharia_status.json')
    args = parser.parse_args()
    errors = validate(Path(args.path))
    if errors:
        print('\n'.join(errors)); raise SystemExit(1)
    print('Sharia dataset valid (schema only; this does not determine religious permissibility).')


if __name__ == '__main__': main()
