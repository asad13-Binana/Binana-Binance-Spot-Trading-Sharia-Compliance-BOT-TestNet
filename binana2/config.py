from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return default if raw is None else int(raw)


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return default if raw is None else float(raw)


@dataclass(frozen=True)
class Settings:
    environment: str
    db_path: Path
    binance_api_key: str
    binance_api_secret: str
    binance_rest_base: str
    binance_ws_stream_base: str
    binance_ws_api_base: str
    recv_window_ms: int
    trade_size_usdt: float
    max_positions: int
    pair_cooldown_seconds: int
    max_stopouts_pair_day: int
    max_stopouts_global_day: int
    max_signal_age_seconds: int
    max_candle_age_seconds: int
    sharia_status_path: Path
    telegram_token: str
    telegram_owner_chat_id: int | None
    entries_enabled: bool

    @classmethod
    def from_env(cls) -> "Settings":
        testnet = _bool("BINANCE_TESTNET", True)
        environment = os.getenv("BOT_ENVIRONMENT", "TESTNET").upper()
        # Foundation-stage hard interlock: this package is incapable of
        # selecting Binance production endpoints. Live promotion is not a
        # configuration toggle and requires a separately reviewed code change.
        if environment != "TESTNET" or not testnet:
            raise ValueError("Binana 2.0 foundation is Binance Spot Testnet-only")

        rest = "https://testnet.binance.vision"
        ws_stream = "wss://stream.testnet.binance.vision:9443"
        ws_api = "wss://ws-api.testnet.binance.vision/ws-api/v3"

        recv = _int("BINANCE_RECV_WINDOW_MS", 5000)
        if not 1 <= recv <= 5000:
            raise ValueError("BINANCE_RECV_WINDOW_MS must be 1..5000")

        trade_size = _float("TRADE_SIZE_USDT", 100.0)
        max_positions = _int("MAX_POSITIONS", 2)
        if trade_size <= 0 or max_positions <= 0:
            raise ValueError("trade size and max positions must be positive")

        owner = os.getenv("TELEGRAM_OWNER_CHAT_ID", "").strip()
        return cls(
            environment=environment,
            db_path=Path(os.getenv("BINANA_DB_PATH", "/app/data/binana.sqlite3")),
            binance_api_key=os.getenv("BINANCE_API_KEY", ""),
            binance_api_secret=os.getenv("BINANCE_API_SECRET", ""),
            binance_rest_base=rest,
            binance_ws_stream_base=ws_stream,
            binance_ws_api_base=ws_api,
            recv_window_ms=recv,
            trade_size_usdt=trade_size,
            max_positions=max_positions,
            pair_cooldown_seconds=_int("PAIR_COOLDOWN_SECONDS", 60),
            max_stopouts_pair_day=_int("MAX_STOPOUTS_PER_PAIR_DAY", 3),
            max_stopouts_global_day=_int("MAX_STOPOUTS_GLOBAL_DAY", 10),
            max_signal_age_seconds=_int("MAX_SIGNAL_AGE_SECONDS", 180),
            max_candle_age_seconds=_int("MAX_CANDLE_AGE_SECONDS", 180),
            sharia_status_path=Path(os.getenv("SHARIA_FILE", "/app/data/sharia_status.json")),
            telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            telegram_owner_chat_id=int(owner) if owner else None,
            entries_enabled=_bool("ENTRIES_ENABLED", False),
        )
