"""Run the packaged BinanaNfiSpot and exercise its real admission policy offline."""
from datetime import datetime, timezone
from importlib.metadata import version
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

import numpy as np
import pandas as pd
import talib


def main():
    root = Path("/freqtrade")
    sys.path[:0] = [str(root), str(root / "binana_tests"), str(root / "recovery_validation")]
    from validate_image import configure_test_environment

    expected = os.environ["BINANA_TEST_EXPECTED_MODE"]
    manifest = json.loads((root / "recovery_validation/PROVENANCE.json").read_bytes())
    assert configure_test_environment(root, manifest) == expected
    assert sys.version_info[:2] == (3, 14) and version("freqtrade") == "2026.10"

    from test_freqtrade_202610_upgrade import Upstream202610OverlayAdmission
    from test_strategy_contract import StrategyContractTests

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(StrategyContractTests)
    suite.addTest(Upstream202610OverlayAdmission(
        "test_real_nfi_instances_reject_api_policy_override_without_state_leak"))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.testsRun < 7 or result.skipped or not result.wasSuccessful():
        raise SystemExit("Required packaged strategy contracts failed or skipped")

    fixture = StrategyContractTests()
    fixture.setUp()
    try:
        strategy = fixture.build()
        close = np.arange(1.0, 101.0)
        candles = pd.DataFrame({"close": close, "ema": talib.EMA(close, timeperiod=5)})
        assert np.isfinite(candles.ema.iloc[-1])
        last = float(candles.close.iloc[-1])

        def analyzed(pair, timeframe):
            assert (pair, timeframe) == ("ADA/USDT", "5m")
            return candles.copy(), None

        # Supply candles only. The packaged parent strategy evaluates the slippage.
        strategy.dp = SimpleNamespace(get_analyzed_dataframe=analyzed)
        now = datetime.now(timezone.utc)

        def entry(rate, tag="1", side="long"):
            return strategy.confirm_trade_entry("ADA/USDT", "limit", 1.0, rate, "FOK",
                                                now, tag, side)

        assert strategy.config["binana"]["environment"] == expected
        assert 0 < strategy.max_slippage < 1
        assert entry(last) is True
        assert entry(last * (1 + 2 * strategy.max_slippage)) is False
        assert entry(last, tag="force_entry") is False
        assert entry(last, side="short") is False
        print(f"{expected}: {strategy.version()} accepted close and rejected excess slippage")
    finally:
        fixture.tearDown()


if __name__ == "__main__":
    main()
