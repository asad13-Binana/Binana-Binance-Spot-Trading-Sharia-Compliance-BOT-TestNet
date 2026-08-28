"""Cross-service free-provider quota contract.

CoinGecko and CoinMarketCap are consumed by two separate processes with two
durable ledgers.  Each ledger is safe on its own, but independent maxima can
exceed one shared free plan.  This startup contract validates the aggregate
configured maxima without logging, hashing or otherwise exposing API keys.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, MutableMapping

COINGECKO_KEYED_PER_MINUTE = 96
COINGECKO_KEYLESS_PER_MINUTE = 5
COINGECKO_MONTHLY = 9_600
CMC_PER_MINUTE = 48
CMC_MONTHLY = 14_400


class ProviderBudgetContractError(ValueError):
    """Enabled services can collectively exceed a shared provider plan."""


def _enabled(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = str(env.get(name, 'true' if default else 'false')).strip().lower()
    if raw not in {'true', 'false'}:
        raise ProviderBudgetContractError(
            f'{name} must be exactly true or false')
    return raw == 'true'


def _budget(
        env: Mapping[str, str], name: str, default: int,
        maximum: int) -> int:
    raw = str(env.get(name, default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ProviderBudgetContractError(
            f'{name} must be a positive integer') from exc
    if value <= 0:
        raise ProviderBudgetContractError(
            f'{name} must be a positive integer')
    # The two underlying clients already clamp to these hard ceilings. Use
    # their effective values here so this guard preserves that behaviour.
    return min(value, maximum)


def evaluate_provider_budget_contract(env: Mapping[str, str]) -> dict:
    discovery = _enabled(
        env, 'SHARIA_AUTO_SOURCE_DISCOVERY_ENABLED', True)
    universe_cg = _enabled(env, 'ENABLE_COINGECKO_SIGNALS', False)
    universe_cmc = _enabled(env, 'ENABLE_CMC_TRENDING', False)
    has_cg_key = bool(str(env.get('COINGECKO_API_KEY', '')).strip())
    has_cmc_key = bool(str(
        env.get('COINMARKETCAP_API_KEY', '')
        or env.get('CMC_API_KEY', '')).strip())

    cg_minute_ceiling = (
        COINGECKO_KEYED_PER_MINUTE if has_cg_key
        else COINGECKO_KEYLESS_PER_MINUTE)
    external_cg_minute = (_budget(
        env, 'COINGECKO_PER_MINUTE_LIMIT', 84,
        COINGECKO_KEYED_PER_MINUTE) if universe_cg else 0)
    external_cg_month = (_budget(
        env, 'COINGECKO_MONTHLY_LIMIT', 4_800,
        COINGECKO_MONTHLY) if universe_cg else 0)
    sharia_cg_minute = (_budget(
        env, 'SHARIA_COINGECKO_PER_MINUTE_LIMIT', 12,
        COINGECKO_KEYED_PER_MINUTE) if discovery else 0)
    if sharia_cg_minute and not has_cg_key:
        sharia_cg_minute = min(
            sharia_cg_minute, COINGECKO_KEYLESS_PER_MINUTE)
    sharia_cg_month = (_budget(
        env, 'SHARIA_COINGECKO_MONTHLY_LIMIT', 4_800,
        COINGECKO_MONTHLY) if discovery else 0)

    external_cmc_minute = (_budget(
        env, 'CMC_PER_MINUTE_LIMIT', 42, CMC_PER_MINUTE)
        if universe_cmc and has_cmc_key else 0)
    external_cmc_month = (_budget(
        env, 'CMC_MONTHLY_LIMIT', 11_400, CMC_MONTHLY)
        if universe_cmc and has_cmc_key else 0)
    sharia_cmc_minute = (_budget(
        env, 'SHARIA_CMC_PER_MINUTE_LIMIT', 6, CMC_PER_MINUTE)
        if discovery and has_cmc_key else 0)
    sharia_cmc_month = (_budget(
        env, 'SHARIA_CMC_MONTHLY_LIMIT', 3_000, CMC_MONTHLY)
        if discovery and has_cmc_key else 0)

    totals = {
        'coingecko': {
            'per_minute': external_cg_minute + sharia_cg_minute,
            'per_month': external_cg_month + sharia_cg_month,
            'per_minute_ceiling': cg_minute_ceiling,
            'per_month_ceiling': COINGECKO_MONTHLY,
            'key_configured': has_cg_key,
        },
        'coinmarketcap': {
            'per_minute': external_cmc_minute + sharia_cmc_minute,
            'per_month': external_cmc_month + sharia_cmc_month,
            'per_minute_ceiling': CMC_PER_MINUTE,
            'per_month_ceiling': CMC_MONTHLY,
            'key_configured': has_cmc_key,
        },
    }
    violations: list[str] = []
    for provider, values in totals.items():
        if values['per_minute'] > values['per_minute_ceiling']:
            violations.append(
                f'{provider} aggregate per-minute budget '
                f'{values["per_minute"]} exceeds '
                f'{values["per_minute_ceiling"]}')
        if values['per_month'] > values['per_month_ceiling']:
            violations.append(
                f'{provider} aggregate monthly budget '
                f'{values["per_month"]} exceeds '
                f'{values["per_month_ceiling"]}')
    if violations:
        raise ProviderBudgetContractError('; '.join(violations))
    return totals


def enforce_provider_budget_contract(
        env: MutableMapping[str, str] | None = None) -> dict:
    target = env if env is not None else os.environ
    result = evaluate_provider_budget_contract(target)
    # The Sharia process owns this wrapper, so it can enforce the keyless
    # CoinGecko clamp before SourceDiscovery constructs its durable budget.
    if (not result['coingecko']['key_configured']
            and _enabled(target, 'SHARIA_AUTO_SOURCE_DISCOVERY_ENABLED', True)):
        configured = _budget(
            target, 'SHARIA_COINGECKO_PER_MINUTE_LIMIT', 12,
            COINGECKO_KEYED_PER_MINUTE)
        target['SHARIA_COINGECKO_PER_MINUTE_LIMIT'] = str(min(
            configured, COINGECKO_KEYLESS_PER_MINUTE))
    return result
