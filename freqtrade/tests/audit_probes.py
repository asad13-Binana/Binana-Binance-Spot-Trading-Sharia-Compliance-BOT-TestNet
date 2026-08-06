from __future__ import annotations

import ast
import copy
import json
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path


ROOT = Path(__file__).parent.parent
STRATEGY = ROOT / "user_data" / "strategies" / "IctSmcStrategy.py"
CONFIG = ROOT / "user_data" / "config.json"
BACKTEST = ROOT / "scripts" / "backtest.sh"


def load_strategy_class():
    import talib.abstract as talib_abstract

    freqtrade = types.ModuleType("freqtrade")
    strategy_module = types.ModuleType("freqtrade.strategy")

    class IStrategy:
        pass

    def informative(_timeframe):
        def decorate(function):
            return function
        return decorate

    strategy_module.IStrategy = IStrategy
    strategy_module.informative = informative
    vendor = types.ModuleType("freqtrade.vendor")
    qtpylib_pkg = types.ModuleType("freqtrade.vendor.qtpylib")
    indicators = types.ModuleType("freqtrade.vendor.qtpylib.indicators")
    def rolling_vwap(dataframe, window=14, min_periods=None):
        min_periods = window if min_periods is None else min_periods
        typical = (dataframe["high"] + dataframe["low"] + dataframe["close"]) / 3
        weighted = (typical * dataframe["volume"]).rolling(
            window=window, min_periods=min_periods
        ).sum()
        volume = dataframe["volume"].rolling(
            window=window, min_periods=min_periods
        ).sum()
        return weighted / volume

    indicators.rolling_vwap = rolling_vwap
    qtpylib_pkg.indicators = indicators
    talib = types.ModuleType("talib")
    talib.abstract = talib_abstract

    sys.modules.update(
        {
            "freqtrade": freqtrade,
            "freqtrade.strategy": strategy_module,
            "freqtrade.vendor": vendor,
            "freqtrade.vendor.qtpylib": qtpylib_pkg,
            "freqtrade.vendor.qtpylib.indicators": indicators,
            "talib": talib,
            "talib.abstract": talib_abstract,
        }
    )
    namespace = {"__file__": str(STRATEGY), "__name__": "audit_strategy"}
    exec(compile(STRATEGY.read_bytes(), str(STRATEGY), "exec"), namespace)
    return namespace["IctSmcStrategy"]


def strategy_smoke():
    import numpy as np
    import pandas as pd

    cls = load_strategy_class()
    strategy = cls.__new__(cls)
    count = 6000
    rng = np.random.default_rng(20260713)
    dates = pd.date_range("2025-01-01", periods=count, freq="min", tz="UTC")
    trend = np.linspace(100.0, 145.0, count)
    wave = 1.8 * np.sin(np.arange(count) / 42.0) + 0.5 * np.sin(np.arange(count) / 7.0)
    close = trend + wave
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) + 0.20
    low = np.minimum(open_, close) - 0.20
    volume = 100.0 + rng.uniform(0, 20, count)
    volume[::37] *= 3.0
    raw = pd.DataFrame(
        {"date": dates, "open": open_, "high": high, "low": low, "close": close, "volume": volume}
    )

    one = strategy.populate_indicators(raw.copy(), {"pair": "BTC/USDT"})
    five_raw = (
        raw.set_index("date")
        .resample("5min")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
        .dropna()
        .reset_index()
    )
    five = strategy.populate_indicators_5m(five_raw.copy(), {"pair": "BTC/USDT"})
    informative = five[["date", "ema9", "ema21", "ema50", "macdhist"]].copy()
    informative["date"] = informative["date"] + pd.Timedelta(minutes=5)
    informative = informative.rename(
        columns={name: f"{name}_5m" for name in ["ema9", "ema21", "ema50", "macdhist"]}
    )
    merged = pd.merge_asof(
        one.sort_values("date"), informative.sort_values("date"), on="date", direction="backward"
    )
    entered = strategy.populate_entry_trend(merged.copy(), {"pair": "BTC/USDT"})
    exited = strategy.populate_exit_trend(merged.copy(), {"pair": "BTC/USDT"})

    # 1-minute macdhist: computed for reference only; the entry condition is
    # deliberately commented out in the strategy, so forcing it must NOT change
    # the entry count (docs/STRATEGY_NOTES.md).
    negative = merged.copy()
    negative["macdhist"] = -999.0
    negative = strategy.populate_entry_trend(negative, {"pair": "BTC/USDT"})
    positive = merged.copy()
    positive["macdhist"] = 999.0
    positive = strategy.populate_entry_trend(positive, {"pair": "BTC/USDT"})

    # A-001 fix: macdhist_5m IS the active hard gate in populate_entry_trend.
    # Forcing it negative must zero out entries; forcing it positive must not
    # reduce them. The previous probe only forced the inactive 1m column, so
    # the gate was never actually exercised.
    negative_5m = merged.copy()
    negative_5m["macdhist_5m"] = -999.0
    negative_5m = strategy.populate_entry_trend(negative_5m, {"pair": "BTC/USDT"})
    positive_5m = merged.copy()
    positive_5m["macdhist_5m"] = 999.0
    positive_5m = strategy.populate_entry_trend(positive_5m, {"pair": "BTC/USDT"})

    prefix = strategy.populate_indicators(raw.iloc[:5000].copy(), {"pair": "BTC/USDT"})
    comparison_columns = ["ema9", "ema21", "ema50", "rsi", "vwap", "rvol", "adx"]
    max_diffs = {}
    for column in comparison_columns:
        left = one.loc[:4999, column].to_numpy(dtype=float)
        right = prefix[column].to_numpy(dtype=float)
        max_diffs[column] = float(np.nanmax(np.abs(left - right)))

    return {
        "candles": count,
        "entry_signals": int(entered.get("enter_long", pd.Series(dtype=float)).fillna(0).sum()),
        "exit_signals": int(exited.get("exit_long", pd.Series(dtype=float)).fillna(0).sum()),
        "entries_macd_forced_negative": int(negative.get("enter_long", pd.Series(dtype=float)).fillna(0).sum()),
        "entries_macd_forced_positive": int(positive.get("enter_long", pd.Series(dtype=float)).fillna(0).sum()),
        "entries_macd5m_forced_negative": int(negative_5m.get("enter_long", pd.Series(dtype=float)).fillna(0).sum()),
        "entries_macd5m_forced_positive": int(positive_5m.get("enter_long", pd.Series(dtype=float)).fillna(0).sum()),
        "prefix_max_abs_diffs": max_diffs,
    }


def interlock_probes():
    cls = load_strategy_class()
    base = json.loads(CONFIG.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="v4-interlock-") as tmp:
        ud = Path(tmp)
        (ud / "halal_list.json").write_text(
            json.dumps({"allowed": ["BTC"], "denied": []}), encoding="utf-8")

        def instance(config=None):
            obj = cls.__new__(cls)
            obj.config = copy.deepcopy(config or base)
            obj.config["user_data_dir"] = str(ud)
            return obj

        def confirm(obj):
            return obj.confirm_trade_entry(
                "BTC/USDT", "limit", 0.001, 1.0, "GTC", None, None, "long"
            )

        dry = instance()
        results = {"dry_run_without_pause": confirm(dry)}
        results["nonhalal_pair_refused"] = not dry.confirm_trade_entry(
            "DOGE/USDT", "limit", 0.001, 1.0, "GTC", None, None, "long")
        (ud / "halal_list.json").unlink()
        type(dry)._halal_cache = (0.0, None)
        results["halal_file_missing_fail_closed"] = not confirm(dry)
        (ud / "halal_list.json").write_text(
            json.dumps({"allowed": ["BTC"], "denied": []}), encoding="utf-8")
        type(dry)._halal_cache = (0.0, None)
        (ud / "PAUSE").touch()
        results["dry_run_with_pause"] = confirm(dry)
        (ud / "PAUSE").unlink()

        live_config = copy.deepcopy(base)
        live_config["dry_run"] = False
        live_config["db_url"] = "sqlite:////freqtrade/user_data/tradesv3.sqlite"
        live = instance(live_config)
        results["live_missing_marker"] = confirm(live)
        (ud / "LIVE_OK").write_text("wrong", encoding="utf-8")
        results["live_wrong_marker"] = confirm(live)
        (ud / "LIVE_OK").write_text(live._live_approval_hash(), encoding="utf-8")
        results["live_correct_marker"] = confirm(live)

        dry_db_config = copy.deepcopy(live_config)
        dry_db_config["db_url"] = "sqlite:////freqtrade/user_data/tradesv3.dryrun.sqlite"
        dry_db = instance(dry_db_config)
        (ud / "LIVE_OK").write_text(dry_db._live_approval_hash(), encoding="utf-8")
        results["live_lowercase_dryrun_db"] = confirm(dry_db)

        for label, db_url in {
            "live_paper_db": "sqlite:////freqtrade/user_data/paper.sqlite",
            "live_uppercase_DRYRUN_db": "sqlite:////freqtrade/user_data/DRYRUN.sqlite",
        }.items():
            cfg = copy.deepcopy(live_config)
            cfg["db_url"] = db_url
            obj = instance(cfg)
            (ud / "LIVE_OK").write_text(obj._live_approval_hash(), encoding="utf-8")
            results[label] = confirm(obj)

        error_obj = instance(live_config)
        del error_obj.config["user_data_dir"]
        results["interlock_exception"] = confirm(error_obj)

        base_hash = live._live_approval_hash()
        mutations = {
            "trading_mode": "futures",
            "stake_currency": "BTC",
            "tradable_balance_ratio": 0.5,
            "force_entry_enable": False,
            "initial_state": "running",
            "pair_blacklist": ["ETH/USDT"],
            "unfilledtimeout": {"entry": 99, "exit": 99, "unit": "minutes"},
            "stoploss": -0.99,
            "minimal_roi": {"0": 9.0},
            "order_types": {"entry": "market", "exit": "market"},
        }
        hash_binding = {}
        for key, value in mutations.items():
            cfg = copy.deepcopy(live_config)
            if key == "pair_blacklist":
                cfg["exchange"][key] = value
            else:
                cfg[key] = value
            hash_binding[key] = instance(cfg)._live_approval_hash() != base_hash

        bound_mutations = {
            "exchange_name": ("exchange", "name", "kraken"),
            "pair_whitelist": ("exchange", "pair_whitelist", ["BTC/USDT"]),
            "stake_amount": (None, "stake_amount", 25),
            "max_open_trades": (None, "max_open_trades", 1),
            "db_url": (None, "db_url", "sqlite:///other.sqlite"),
        }
        for label, (parent, key, value) in bound_mutations.items():
            cfg = copy.deepcopy(live_config)
            if parent:
                cfg[parent][key] = value
            else:
                cfg[key] = value
            hash_binding[label] = instance(cfg)._live_approval_hash() != base_hash

        return results, hash_binding, len(base_hash)


def extract_gate_code():
    text = BACKTEST.read_text(encoding="utf-8")
    return text.split("python3 - << 'PYGATE'", 1)[1].split("\nPYGATE", 1)[0].lstrip("\n")


def run_gate_case(stats):
    gate = extract_gate_code()
    with tempfile.TemporaryDirectory(prefix="v4-gate-") as tmp:
        work = Path(tmp)
        results = work / "user_data" / "backtest_results"
        results.mkdir(parents=True)
        artifact = "result.json"
        (results / ".last_result.json").write_text(
            json.dumps({"latest_backtest": artifact}), encoding="utf-8"
        )
        (results / artifact).write_text(
            json.dumps({"strategy": {"IctSmcStrategy": stats}}), encoding="utf-8"
        )
        proc = subprocess.run(
            [sys.executable, "-c", gate], cwd=work, text=True, capture_output=True
        )
        return proc.returncode, proc.stdout.strip().splitlines()


def backtest_probes():
    cases = {
        "catastrophic_drawdown": {
            "profit_factor": 1.16,
            "total_trades": 100,
            "profit_total": 0.01,
            "max_drawdown_account": 0.99,
        },
        "missing_drawdown": {
            "profit_factor": 1.16,
            "total_trades": 100,
            "profit_total": 0.01,
        },
        "pf_boundary": {
            "profit_factor": 1.15,
            "total_trades": 100,
            "profit_total": 0.01,
            "max_drawdown_account": 0.01,
        },
        "negative_profit": {
            "profit_factor": 2.0,
            "total_trades": 100,
            "profit_total": -0.01,
            "max_drawdown_account": 0.01,
        },
    }
    return {name: run_gate_case(stats)[0] for name, stats in cases.items()}


def inventory():
    files = sorted(
        path
        for path in ROOT.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    )
    tree = ast.parse(STRATEGY.read_text(encoding="utf-8"), filename=str(STRATEGY))
    functions = [
        {"name": node.name, "line": node.lineno, "end_line": node.end_lineno}
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    return [str(path.relative_to(ROOT)) for path in files], sorted(functions, key=lambda x: x["line"])


def main():
    files, functions = inventory()
    interlock, binding, hash_length = interlock_probes()
    report = {
        "files": files,
        "file_count": len(files),
        "functions": functions,
        "function_count": len(functions),
        "interlock": interlock,
        "hash_binding_changed": binding,
        "approval_hash_hex_length": hash_length,
        "backtest_gate_exit_codes": backtest_probes(),
        "strategy_smoke": strategy_smoke(),
    }
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
