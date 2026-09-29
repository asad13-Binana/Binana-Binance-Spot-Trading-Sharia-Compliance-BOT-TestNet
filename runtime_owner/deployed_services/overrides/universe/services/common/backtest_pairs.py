"""Resolve offline backtest pairs from the owner-maintained Sharia registry.

The manual file is the approval source, while the ordinary universe policy
remains an additional gate.  This helper never adds or approves a symbol and
fails closed when the registry is missing, malformed, expired or empty.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from services.sharia_screener.manual_registry import (
    ManualRegistryError,
    load_manual_registry,
)

DEFAULT_STABLECOINS = 'USDC,FDUSD,TUSD,USDP,DAI,EUR,AEUR,BUSD'
EXCLUDED_BASES = frozenset({'BTC', 'BNB'})
LEVERAGED_SUFFIXES = ('UP', 'DOWN', 'BULL', 'BEAR')


class BacktestPairError(ValueError):
    """The generated V19.1 compatibility view is not safe to consume."""


def _stablecoins() -> frozenset[str]:
    return frozenset(
        item.strip().upper()
        for item in os.getenv('STABLECOINS', DEFAULT_STABLECOINS).split(',')
        if item.strip()
    )


def load_pairs(path: str | Path) -> list[str]:
    source = Path(path)
    try:
        registry = load_manual_registry(source)
    except ManualRegistryError as exc:
        raise BacktestPairError(str(exc)) from exc
    if not registry.symbols:
        raise BacktestPairError('owner-maintained manual Sharia registry is empty')

    stablecoins = _stablecoins()
    pairs: list[str] = []
    for symbol in registry.symbols:
        base = symbol[:-4]
        if (base in EXCLUDED_BASES or base in stablecoins
                or any(base.endswith(suffix) for suffix in LEVERAGED_SUFFIXES)):
            continue
        pairs.append(f'{base}/USDT')
    if not pairs:
        raise BacktestPairError(
            'manual registry contains no symbol permitted by universe policy')
    return sorted(pairs)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        '--compat-file',
        default='../shared/sharia/halal_coins.json',
        help='owner-maintained manual Sharia registry',
    )
    args = parser.parse_args(argv)
    try:
        pairs = load_pairs(args.compat_file)
    except BacktestPairError as exc:
        parser.exit(2, f'BACKTEST PAIR GATE BLOCKED: {exc}\n')
    for pair in pairs:
        print(pair)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
