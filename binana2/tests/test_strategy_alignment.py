import numpy as np
import pandas as pd
from binana2.strategy.v1_original import OriginalV1Strategy

def candles(start,periods,freq):
    idx=pd.date_range(start,periods=periods,freq=freq,tz="UTC"); close=np.linspace(100.0,130.0,periods); return pd.DataFrame({"date":idx,"open":close-0.05,"high":close+0.2,"low":close-0.2,"close":close,"volume":np.linspace(1000,2000,periods)})

def test_five_minute_features_are_available_only_after_candle_close():
    strategy=OriginalV1Strategy(); one=candles("2026-01-01T00:00:00Z",220,"1min"); five=candles("2026-01-01T00:00:00Z",220,"5min"); features=strategy.features(one,five); row=features.loc[features["date"]==pd.Timestamp("2026-01-01T00:04:00Z")].iloc[0]; assert row["available_at"]==pd.Timestamp("2026-01-01T00:05:00Z")
