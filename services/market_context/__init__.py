"""Read-only Binance Spot microstructure observations.

This package has no exchange credentials and no order methods.  It publishes
advisory evidence only; the protected strategy and execution gates never
consume it as a trading condition.
"""

from .analytics import SpotMicrostructureAnalytics

__all__ = ["SpotMicrostructureAnalytics"]
