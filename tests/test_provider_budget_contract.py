from __future__ import annotations

import unittest
from pathlib import Path

from services.common.provider_budget_contract import (
    ProviderBudgetContractError,
    enforce_provider_budget_contract,
    evaluate_provider_budget_contract,
)


def _env(**updates) -> dict[str, str]:
    values = {
        'SHARIA_AUTO_SOURCE_DISCOVERY_ENABLED': 'true',
        'ENABLE_COINGECKO_SIGNALS': 'true',
        'ENABLE_CMC_TRENDING': 'true',
        'COINGECKO_API_KEY': 'configured-but-never-returned',
        'COINMARKETCAP_API_KEY': 'configured-but-never-returned',
        'COINGECKO_PER_MINUTE_LIMIT': '84',
        'COINGECKO_MONTHLY_LIMIT': '4800',
        'SHARIA_COINGECKO_PER_MINUTE_LIMIT': '12',
        'SHARIA_COINGECKO_MONTHLY_LIMIT': '4800',
        'CMC_PER_MINUTE_LIMIT': '42',
        'CMC_MONTHLY_LIMIT': '11400',
        'SHARIA_CMC_PER_MINUTE_LIMIT': '6',
        'SHARIA_CMC_MONTHLY_LIMIT': '3000',
    }
    values.update({key: str(value) for key, value in updates.items()})
    return values


class ProviderBudgetContractTests(unittest.TestCase):
    def test_partitioned_defaults_exactly_fit_safe_shared_ceilings(self):
        result = evaluate_provider_budget_contract(_env())
        self.assertEqual(result['coingecko']['per_minute'], 96)
        self.assertEqual(result['coingecko']['per_month'], 9600)
        self.assertEqual(result['coinmarketcap']['per_minute'], 48)
        self.assertEqual(result['coinmarketcap']['per_month'], 14400)
        self.assertNotIn('configured-but-never-returned', repr(result))

    def test_any_aggregate_overage_refuses_startup(self):
        for key, value, provider in (
            ('COINGECKO_PER_MINUTE_LIMIT', 85, 'coingecko'),
            ('SHARIA_COINGECKO_MONTHLY_LIMIT', 4801, 'coingecko'),
            ('CMC_PER_MINUTE_LIMIT', 43, 'coinmarketcap'),
            ('CMC_MONTHLY_LIMIT', 11401, 'coinmarketcap'),
        ):
            with self.subTest(key=key), self.assertRaisesRegex(
                    ProviderBudgetContractError, provider):
                evaluate_provider_budget_contract(_env(**{key: value}))

    def test_disabled_consumers_do_not_reserve_quota(self):
        result = evaluate_provider_budget_contract(_env(
            ENABLE_COINGECKO_SIGNALS='false',
            ENABLE_CMC_TRENDING='false',
        ))
        self.assertEqual(result['coingecko']['per_minute'], 12)
        self.assertEqual(result['coinmarketcap']['per_minute'], 6)

    def test_keyless_sharia_is_clamped_and_shared_keyless_use_blocks(self):
        values = _env(
            COINGECKO_API_KEY='', ENABLE_COINGECKO_SIGNALS='false')
        result = enforce_provider_budget_contract(values)
        self.assertEqual(result['coingecko']['per_minute'], 5)
        self.assertEqual(values['SHARIA_COINGECKO_PER_MINUTE_LIMIT'], '5')

        with self.assertRaisesRegex(
                ProviderBudgetContractError, 'coingecko aggregate'):
            evaluate_provider_budget_contract(_env(COINGECKO_API_KEY=''))

    def test_malformed_boolean_or_budget_is_rejected(self):
        with self.assertRaisesRegex(ProviderBudgetContractError, 'exactly'):
            evaluate_provider_budget_contract(_env(
                ENABLE_COINGECKO_SIGNALS='maybe'))
        with self.assertRaisesRegex(ProviderBudgetContractError, 'positive'):
            evaluate_provider_budget_contract(_env(
                SHARIA_CMC_PER_MINUTE_LIMIT='0'))

    def test_compose_wires_guard_and_exact_package_execution_contract(self):
        root = Path(__file__).resolve().parents[1]
        compose = (root / 'docker-compose.yml').read_text(encoding='utf-8')
        release_mode = (root / 'RELEASE_MODE').read_text(
            encoding='utf-8').strip()
        expected_base = {
            'testnet': 'https://testnet.binance.vision',
            'live': 'https://api.binance.com',
        }[release_mode]
        self.assertIn(
            'command: python -m services.sharia_screener.guarded_main',
            compose)
        self.assertIn(
            'BINANCE_PUBLIC_BASE: '
            f'${{BINANCE_EXECUTION_PUBLIC_BASE:-{expected_base}}}',
            compose)
        self.assertIn(
            'COINGECKO_MONTHLY_LIMIT: '
            '${COINGECKO_MONTHLY_LIMIT:-4800}',
            compose)


if __name__ == '__main__':
    unittest.main()
