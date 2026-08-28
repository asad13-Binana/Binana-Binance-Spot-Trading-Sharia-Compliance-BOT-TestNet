"""Sharia launcher with aggregate free-provider quota validation."""

from __future__ import annotations

from services.common.provider_budget_contract import (
    ProviderBudgetContractError,
    enforce_provider_budget_contract,
)


def main() -> None:
    try:
        enforce_provider_budget_contract()
    except ProviderBudgetContractError as exc:
        raise SystemExit(f'PROVIDER BUDGET CONTRACT BLOCKED: {exc}') from exc
    # Import only after the wrapper has applied the keyless safety clamp.
    from services.sharia_screener.service import main as service_main
    service_main()


if __name__ == '__main__':
    main()
