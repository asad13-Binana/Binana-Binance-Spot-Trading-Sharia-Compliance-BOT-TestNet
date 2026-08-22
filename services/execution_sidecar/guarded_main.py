"""Execution-sidecar launcher with non-core Binance contract hardening."""

from __future__ import annotations

from services.execution_sidecar import core_adapter, order_manager
from services.execution_sidecar.binance_contract_guard import (
    install_binance_contract_guard,
)
from services.market_context.execution_observer import install_signal_observer

install_binance_contract_guard(core_adapter.CoreAdapter)
install_signal_observer(order_manager.OrderManager)

from services.execution_sidecar.main import main

if __name__ == "__main__":
    main()
