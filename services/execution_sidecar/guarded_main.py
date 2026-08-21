"""Execution-sidecar launcher with non-core Binance contract hardening."""

from __future__ import annotations

from services.execution_sidecar import core_adapter
from services.execution_sidecar.binance_contract_guard import (
    install_binance_contract_guard,
)

install_binance_contract_guard(core_adapter.CoreAdapter)

from services.execution_sidecar.main import main

if __name__ == '__main__':
    main()
