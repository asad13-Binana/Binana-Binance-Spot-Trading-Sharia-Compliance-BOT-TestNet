from __future__ import annotations

"""Non-core launcher that adds advisory Spot observation to the universe service."""

import logging

from services.market_context.service import start_background
from services.universe_service.scanner import main

if __name__ == "__main__":
    try:
        start_background()
    except Exception as exc:
        # Observation is deliberately non-authoritative. Configuration or
        # startup failure is visible in logs but cannot stop universe scanning.
        logging.getLogger("spot-market-context").exception(
            "market-context startup failed: %s", type(exc).__name__
        )
    main()
