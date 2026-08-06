"""External market-signal enrichment (CoinGecko + CoinMarketCap) for the
active universe service.

The byte-preserved legacy core (legacy_core/binance_bot_V4.9.16_ALL_IN_ONE.py)
contains its own CoinGecko/CMC code that the containerized stack never calls.
This package re-implements those signals for the ACTIVE scanner without
touching the preserved file, exactly as the external audit required:
separate client modules, env-controlled enablement, persisted free-tier
budgets, caching, and fail-safe degradation to Binance-only data.
"""
from services.universe_service.external_signals.enrichment import ExternalSignals

__all__ = ['ExternalSignals']
