from __future__ import annotations
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
from .package_profile import PACKAGE_ENVIRONMENT

class BinanaConfigError(ValueError):
    pass


def package_environment() -> str:
    if PACKAGE_ENVIRONMENT not in {'testnet', 'live'}:
        raise BinanaConfigError('INVALID_PACKAGE_ENVIRONMENT')
    return PACKAGE_ENVIRONMENT


def execution_rest_endpoint() -> str:
    return ('https://testnet.binance.vision/api/v3' if package_environment() == 'testnet'
            else 'https://api.binance.com/api/v3')


def private_stream_endpoint() -> str:
    return ('wss://ws-api.testnet.binance.vision/ws-api/v3' if package_environment() == 'testnet'
            else 'wss://ws-api.binance.com:443/ws-api/v3')


def execution_epoch(config: dict) -> str:
    key = 'testnet_epoch_id' if package_environment() == 'testnet' else 'exchange_epoch_id'
    return str(config.get('binana', {}).get(key, '')).strip()


def account_scope(config: dict) -> str:
    fallback = 'owner-testnet-account-1' if package_environment() == 'testnet' else ''
    return str(config.get('binana', {}).get('account_scope') or fallback).strip()


def execution_scope(config: dict) -> str:
    return f'binance-{package_environment()}|{account_scope(config)}|{execution_epoch(config)}'


def fence_ccxt_transport(api, *, public_endpoint: str):
    """Recheck route and host before every sync/async CCXT HTTP operation.

    Returning the original awaitable also preserves the async client contract.
    No network I/O occurs during construction of this fence.
    """
    from freqtrade.exceptions import OperationalException
    request = api.request
    expected = {'private': execution_rest_endpoint(), 'public': public_endpoint,
                'v1': 'https://api.binance.com/api/v1'}

    def fenced_request(path, api='public', method='GET', *args, **kwargs):
        if (not isinstance(api, str) or api not in expected
                or client.urls.get('api', {}).get(api) != expected[api]
                or (api != 'private' and str(method).upper() != 'GET')):
            raise OperationalException('BINANA_SPOT_TRANSPORT_FENCE')
        return request(path, api, method, *args, **kwargs)

    client = api
    client.request = fenced_request
    # Spot currencies are derived from exchangeInfo; SAPI wallet discovery is
    # outside this owner's contract and must not run during load_markets().
    client.has['fetchCurrencies'] = False
    return client


EXCLUDED_BASE_ASSETS = frozenset({'BTC', 'BNB', 'XRP', 'SOL', 'ETH'})
STABLE_BASE_ASSETS = frozenset({'USDT', 'USDC', 'FDUSD', 'TUSD', 'USDP', 'DAI',
    'EUR', 'AEUR', 'BUSD', 'USDE', 'USDS', 'PYUSD', 'USD1', 'USDG', 'RLUSD', 'EURC', 'EURI'})


def resolve_stake_amount(config: dict, *, override_path: Path) -> Decimal:
    """One exact stake contract for admission and the strategy callback."""
    value = config.get('stake_amount')
    try:
        raw = json.loads(override_path.read_text(), parse_float=Decimal)
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        raise BinanaConfigError('INVALID_TRADE_SIZE_OVERRIDE') from exc
    else:
        if not isinstance(raw, dict) or 'usdt' not in raw or set(raw) - {'usdt', 'updated_at', 'source'}:
            raise BinanaConfigError('INVALID_TRADE_SIZE_SCHEMA')
        value = raw['usdt']
    try:
        if isinstance(value, bool):
            raise ValueError('boolean stake')
        amount = Decimal(str(value))
        allocation = Decimal(str(config['binana']['allocation_usdt']))
        if not amount.is_finite() or amount <= 0 or not allocation.is_finite() or amount > allocation:
            raise ValueError('stake outside allocation')
    except (ValueError, InvalidOperation, KeyError) as exc:
        raise BinanaConfigError('INVALID_TRADE_SIZE') from exc
    return amount

def validate_runtime_contract(config: dict) -> None:
    policy=config.get("binana") or {}
    if policy.get("enabled") is not True:
        raise BinanaConfigError("BINANA extension must be enabled")
    mode = package_environment()
    if policy.get("environment") != mode:
        raise BinanaConfigError("BINANA package/environment mismatch")
    if policy.get("spot_only") is not True:
        raise BinanaConfigError("BINANA spot_only must be true")
    if mode == 'testnet':
        if policy.get("allow_live") not in (False, None):
            raise BinanaConfigError("BINANA live trading is forbidden")
    else:
        if (policy.get('allow_live') is not True
                or policy.get('live_activation') != 'I_AUTHORIZE_BINANA_LIVE_SPOT'):
            raise BinanaConfigError('LIVE_ACTIVATION_REQUIRED')
        for value in (execution_epoch(config), account_scope(config)):
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{7,127}', value) or 'REPLACE' in value:
                raise BinanaConfigError('LIVE_RECONCILED_ACCOUNT_AND_EPOCH_REQUIRED')
        if config.get('initial_state') != 'stopped' or config.get('dry_run') is not False:
            raise BinanaConfigError('LIVE_OWNER_MUST_START_STOPPED')
        if policy.get('testnet_epoch_id'):
            raise BinanaConfigError('LIVE_FORBIDS_TESTNET_EPOCH')
    if policy.get("single_halal_decision") is not True:
        raise BinanaConfigError("BINANA requires one halal decision per new intent")
    try:
        allocation = Decimal(str(policy.get("allocation_usdt")))
    except InvalidOperation as exc:
        raise BinanaConfigError("BINANA allocation must equal 1000 USDT") from exc
    if not allocation.is_finite() or allocation != Decimal('1000'):
        raise BinanaConfigError("BINANA allocation must equal 1000 USDT")
    if not execution_epoch(config):
        raise BinanaConfigError("BINANA exchange epoch is required")
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
    if mode == 'live' and ex.get('binana_testnet') is not False:
        raise BinanaConfigError('LIVE_EXCHANGE_MODE_MISMATCH')
    if mode == 'testnet' and ex.get('binana_testnet', True) is not True:
        raise BinanaConfigError('TESTNET_EXCHANGE_MODE_MISMATCH')

def assert_spot_pair(pair: str, *, side: str, leverage: float) -> None:
    if side != "long":
        raise BinanaConfigError("BINANA permits long entries only")
    if float(leverage) != 1.0:
        raise BinanaConfigError("BINANA leverage must equal 1")
    if not pair.endswith("/USDT") or ":" in pair:
        raise BinanaConfigError("BINANA permits USDT Spot pairs only")
    if pair.split('/')[0] in EXCLUDED_BASE_ASSETS | STABLE_BASE_ASSETS:
        raise BinanaConfigError('EXCLUDED_BASE_ASSET')
