from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def rsi_wilder(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff(); gains = delta.clip(lower=0); losses = -delta.clip(upper=0)
    avg_gain = gains.ewm(alpha=1 / period, adjust=False, min_periods=period).mean(); avg_loss = losses.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan); out = 100 - (100 / (1 + rs)); return out.where(avg_loss != 0, 100.0)


def macd(series: pd.Series, fast: int, slow: int, signal: int) -> tuple[pd.Series, pd.Series, pd.Series]:
    fast_ema = ema(series, fast); slow_ema = ema(series, slow); line = fast_ema - slow_ema
    signal_line = line.ewm(span=signal, adjust=False, min_periods=signal).mean(); return line, signal_line, line - signal_line


def rolling_vwap(frame: pd.DataFrame, window: int = 200) -> pd.Series:
    typical = (frame["high"] + frame["low"] + frame["close"]) / 3.0; value = typical * frame["volume"]
    denominator = frame["volume"].rolling(window).sum(); return value.rolling(window).sum() / denominator.replace(0, np.nan)


def adx(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = frame["high"], frame["low"], frame["close"]; up = high.diff(); down = -low.diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0); minus_dm = down.where((down > up) & (down > 0), 0.0); prev_close = close.shift(1)
    tr = pd.concat([(high-low).abs(), (high-prev_close).abs(), (low-prev_close).abs()], axis=1).max(axis=1); atr = tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / atr; minus_di = 100 * minus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / atr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan); return dx.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
