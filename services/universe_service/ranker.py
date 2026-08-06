from __future__ import annotations

import math


def _finite_number(row: dict, field: str) -> float:
    try:
        value = float(row[field])
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f'ranking row has invalid {field}: {row.get(field)!r}') from exc
    if not math.isfinite(value):
        raise ValueError(f'ranking row has non-finite {field}: {row.get(field)!r}')
    return value


def _identity(row: dict) -> tuple[str, str]:
    symbol = str(row.get('symbol') or '').strip().upper()
    pair = str(row.get('pair') or '').strip().upper()
    if not symbol and pair.endswith('/USDT'):
        symbol = pair.replace('/', '')
    return symbol, pair


def rank(rows: list[dict], limit: int = 50) -> list[dict]:
    """Rank deterministically, including an ascending identity tie-break."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise ValueError('ranking limit must be an integer within 1-50')
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError('ranking rows must be a list of objects')
    identities = [_identity(row) for row in rows]
    symbols = [symbol for symbol, _pair in identities]
    pairs = [pair for _symbol, pair in identities]
    if len(symbols) != len(set(symbols)) or len(pairs) != len(set(pairs)):
        raise ValueError('ranking rows contain duplicate symbol/pair identity')
    ranked = sorted(
        (dict(row) for row in rows),
        key=lambda row: (
            -_finite_number(row, 'change_pct'),
            -_finite_number(row, 'quote_volume'),
            _finite_number(row, 'spread_ratio'),
            *_identity(row),
        ),
    )[:limit]
    for index, row in enumerate(ranked, 1):
        row['rank'] = index
    return ranked
