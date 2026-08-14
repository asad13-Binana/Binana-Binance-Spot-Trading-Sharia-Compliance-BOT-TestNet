from __future__ import annotations

from dataclasses import dataclass
from binana2.config import Settings
from binana2.exchange.binance_spot import BinanceSpotAdapter
from binana2.risk.engine import RiskEngine
from binana2.sharia.engine import V191Gate
from binana2.state.database import Database
from binana2.state.repositories import StateRepository
from binana2.strategy.v1_original import OriginalV1Strategy
from binana2.trading.order_manager import OrderManager
from binana2.trading.reconciliation import Reconciler

@dataclass
class Application:
    settings: Settings; db: Database; state: StateRepository; exchange: BinanceSpotAdapter; sharia: V191Gate; risk: RiskEngine; strategy: OriginalV1Strategy; orders: OrderManager
    async def close(self) -> None:
        await self.exchange.__aexit__(None,None,None); self.db.checkpoint(); self.db.close()

async def build_application(settings: Settings | None = None) -> Application:
    settings=settings or Settings.from_env(); db=Database(settings.db_path); state=StateRepository(db)
    if not settings.entries_enabled: state.set_entry_pause(True,"ENTRIES_ENABLED=false")
    exchange=BinanceSpotAdapter(api_key=settings.binance_api_key,api_secret=settings.binance_api_secret,rest_base=settings.binance_rest_base,market_ws_base=settings.binance_ws_stream_base,ws_api_base=settings.binance_ws_api_base,recv_window_ms=settings.recv_window_ms)
    await exchange.__aenter__(); sharia=V191Gate(settings.sharia_status_path); risk=RiskEngine(state,max_positions=settings.max_positions,max_signal_age_seconds=settings.max_signal_age_seconds,max_candle_age_seconds=settings.max_candle_age_seconds); strategy=OriginalV1Strategy(); orders=OrderManager(exchange,state); await Reconciler(exchange,state,orders).startup(); return Application(settings,db,state,exchange,sharia,risk,strategy,orders)
