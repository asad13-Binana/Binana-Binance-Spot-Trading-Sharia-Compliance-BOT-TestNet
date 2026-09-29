from __future__ import annotations

class BinanaConfigError(ValueError):
    pass

def validate_runtime_contract(config: dict) -> None:
    policy=config.get("binana") or {}
    if policy.get("enabled") is not True:
        raise BinanaConfigError("BINANA extension must be enabled")
    if policy.get("environment") != "testnet":
        raise BinanaConfigError("BINANA environment must be testnet")
    if policy.get("spot_only") is not True:
        raise BinanaConfigError("BINANA spot_only must be true")
    if policy.get("allow_live") not in (False, None):
        raise BinanaConfigError("BINANA live trading is forbidden")
    if policy.get("single_halal_decision") is not True:
        raise BinanaConfigError("BINANA requires one halal decision per new intent")
    if int(policy.get("allocation_usdt", 0)) != 1000:
        raise BinanaConfigError("BINANA allocation must equal 1000 USDT")
    if not str(policy.get("testnet_epoch_id", "")).strip():
        raise BinanaConfigError("BINANA testnet_epoch_id is required")
    if config.get("trading_mode") != "spot":
        raise BinanaConfigError("BINANA requires spot mode")
    if config.get("margin_mode") not in (None, ""):
        raise BinanaConfigError("BINANA forbids margin mode")
    if config.get("stake_currency") != "USDT":
        raise BinanaConfigError("BINANA requires USDT stake currency")
    if config.get("stake_amount") != 250:
        raise BinanaConfigError("BINANA requires nominal 250-USDT stake")
    if config.get("max_open_trades") != 4:
        raise BinanaConfigError("BINANA requires four maximum open trades")
    if config.get("timeframe") != "5m":
        raise BinanaConfigError("BINANA NFI profile requires 5m timeframe")
    if config.get("force_entry_enable", False) is not False:
        raise BinanaConfigError("Force entry is forbidden")
    if config.get("position_adjustment_enable", False) is not False:
        raise BinanaConfigError("Position adjustment is forbidden")
    ex=config.get("exchange") or {}
    if ex.get("name") != "binance":
        raise BinanaConfigError("BINANA requires Binance")

def assert_spot_pair(pair: str, *, side: str, leverage: float) -> None:
    if side != "long":
        raise BinanaConfigError("BINANA permits long entries only")
    if float(leverage) != 1.0:
        raise BinanaConfigError("BINANA leverage must equal 1")
    if not pair.endswith("/USDT") or ":" in pair:
        raise BinanaConfigError("BINANA permits USDT Spot pairs only")
