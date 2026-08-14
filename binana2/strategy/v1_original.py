from __future__ import annotations

from datetime import datetime, timezone
import pandas as pd
from .base import Signal
from .indicators import adx, ema, macd, rolling_vwap, rsi_wilder


class OriginalV1Strategy:
    name = "v1_original"; RSI_MIN = 50.0; RVOL_MIN = 1.5; STARTUP_CANDLES_1M = 210; STARTUP_CANDLES_5M = 210

    @staticmethod
    def _prepare(frame: pd.DataFrame) -> pd.DataFrame:
        needed = {"date", "open", "high", "low", "close", "volume"}; missing = needed - set(frame.columns)
        if missing: raise ValueError(f"missing candle columns: {sorted(missing)}")
        out = frame.copy().sort_values("date"); out["date"] = pd.to_datetime(out["date"], utc=True)
        for col in ("open", "high", "low", "close", "volume"): out[col] = pd.to_numeric(out[col], errors="raise")
        return out

    def indicators_5m(self, frame: pd.DataFrame) -> pd.DataFrame:
        out = self._prepare(frame); out["ema9_5m"] = ema(out["close"],9); out["ema21_5m"] = ema(out["close"],21); out["ema50_5m"] = ema(out["close"],50); out["ema200_5m"] = ema(out["close"],200)
        out["macd_5m"], out["macdsignal_5m"], out["macdhist_5m"] = macd(out["close"],12,26,9); out["available_at"] = out["date"] + pd.Timedelta(minutes=5); return out

    def indicators_1m(self, frame: pd.DataFrame) -> pd.DataFrame:
        out = self._prepare(frame); out["ema9"] = ema(out["close"],9); out["ema21"] = ema(out["close"],21); out["ema50"] = ema(out["close"],50)
        out["rsi"] = rsi_wilder(out["close"],14); out["rsi_rising"] = out["rsi"] > out["rsi"].shift(1); out["macd"], out["macdsignal"], out["macdhist"] = macd(out["close"],5,13,6)
        out["vwap"] = rolling_vwap(out,200); out["vol_ma"] = out["volume"].rolling(20).mean(); out["rvol"] = out["volume"] / out["vol_ma"]; out["adx"] = adx(out)
        zone = out[["ema9","ema21"]].max(axis=1); out["pullback"] = (out["low"] <= zone).rolling(3).max() > 0; out["ema9_rising"] = out["ema9"] >= out["ema9"].shift(1); out["decision_at"] = out["date"] + pd.Timedelta(minutes=1); return out

    def features(self, one_minute: pd.DataFrame, five_minute: pd.DataFrame) -> pd.DataFrame:
        one = self.indicators_1m(one_minute); five = self.indicators_5m(five_minute)[["available_at","ema9_5m","ema21_5m","ema50_5m","ema200_5m","macdhist_5m"]]
        return pd.merge_asof(one.sort_values("decision_at"), five.sort_values("available_at"), left_on="decision_at", right_on="available_at", direction="backward")

    def evaluate(self, symbol: str, one_minute: pd.DataFrame, five_minute: pd.DataFrame) -> Signal:
        features = self.features(one_minute, five_minute)
        if len(one_minute) < self.STARTUP_CANDLES_1M or len(five_minute) < self.STARTUP_CANDLES_5M or features.empty:
            now = datetime.now(timezone.utc); candle = pd.to_datetime(one_minute.iloc[-1]["date"], utc=True).to_pydatetime() if not one_minute.empty else now; return Signal(self.name,symbol,candle,now,False,False)
        row = features.iloc[-1]; required = ["ema9_5m","ema21_5m","ema50_5m","macdhist_5m","vwap","ema9","rsi","rvol","adx"]; ready = all(pd.notna(row.get(k)) for k in required)
        enter = bool(ready and all([row["ema9_5m"]>row["ema21_5m"],row["ema21_5m"]>row["ema50_5m"],row["close"]>row["ema50_5m"],row["macdhist_5m"]>0,row["close"]>row["vwap"],bool(row["pullback"]),row["close"]>row["ema9"],bool(row["ema9_rising"]),row["rsi"]>self.RSI_MIN,bool(row["rsi_rising"]),row["rvol"]>=self.RVOL_MIN,row["adx"]>20,row["volume"]>0]))
        exit_signal = bool(ready and row["close"] < row["vwap"] and row["macdhist_5m"] < 0); candle = pd.Timestamp(row["date"]).to_pydatetime()
        return Signal(self.name,symbol,candle,datetime.now(timezone.utc),enter,exit_signal,"ema_vwap_pullback" if enter else "","lost_vwap_5m_bear" if exit_signal else "")
