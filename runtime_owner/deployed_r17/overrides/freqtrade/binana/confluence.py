"""Closed-candle confluence for existing BINANA candidates; no order methods.

Uses the modular indicator idea illustrated by Crypto-Signal. The VWAP here
is explicitly a rolling 20-bar HLC3 VWAP, with no future or open candle data.
"""
import math
from datetime import datetime,timezone


def score_confluence(frame, *, now=None, policy=None):
    import numpy as np
    import pandas as pd
    import talib.abstract as ta
    policy=policy or {}
    result={'status':'BLOCKED','reason':'CONFLUENCE_DATA_UNAVAILABLE','score':0,'maximum':7,'votes':{}}
    try:
        if frame is None or len(frame)<60:return result
        df=frame.tail(256).copy()
        dates=pd.to_datetime(df['date'],utc=True)
        current=pd.Timestamp(now or datetime.now(timezone.utc))
        if current.tzinfo is None:current=current.tz_localize('UTC')
        if not dates.is_monotonic_increasing or dates.duplicated().any():return result
        if (dates.diff().dropna()!=pd.Timedelta(minutes=5)).any():return result
        age=(current-(dates.iloc[-1]+pd.Timedelta(minutes=5))).total_seconds()
        if not 0<=age<=600:
            result['reason']='CONFLUENCE_CANDLE_NOT_CLOSED_OR_STALE';return result
        for name in ['open','high','low','close','volume']:
            df[name]=pd.to_numeric(df[name],errors='raise')
        if not np.isfinite(df[['open','high','low','close','volume']].to_numpy()).all():return result
        if (df[['open','high','low','close']]<=0).any().any() or (df['volume']<0).any():return result
        if (df['high']<df[['open','close','low']].max(axis=1)).any() or (df['low']>df[['open','close','high']].min(axis=1)).any():return result
        rsi=ta.RSI(df,timeperiod=14);mfi=ta.MFI(df,timeperiod=14)
        obv=ta.OBV(df);macd=ta.MACD(df,fastperiod=12,slowperiod=26,signalperiod=9)
        momentum=ta.MOM(df,timeperiod=10);ema=ta.EMA(df,timeperiod=20)
        volume=df['volume'].rolling(20).sum()
        vwap=(((df['high']+df['low']+df['close'])/3)*df['volume']).rolling(20).sum()/volume
        values={'close':float(df['close'].iloc[-1]),'vwap_20':float(vwap.iloc[-1]),
            'obv_delta_5':float(obv.iloc[-1]-obv.iloc[-6]),'mfi_14':float(mfi.iloc[-1]),
            'rsi_14':float(rsi.iloc[-1]),'macd_hist':float(macd['macdhist'].iloc[-1]),
            'momentum_10':float(momentum.iloc[-1]),'ema_20':float(ema.iloc[-1]),
            'ema_20_delta':float(ema.iloc[-1]-ema.iloc[-2])}
        if not all(math.isfinite(x) for x in values.values()):return result
        votes={'vwap':values['close']>values['vwap_20'], 'obv':values['obv_delta_5']>0,
            'mfi':50<=values['mfi_14']<=80, 'rsi':50<=values['rsi_14']<=68,
            'macd':values['macd_hist']>0,'momentum':values['momentum_10']>0,
            'ema_context':values['close']>values['ema_20'] and values['ema_20_delta']>=0}
        score=sum(votes.values());volume_votes=sum(votes[k] for k in ['vwap','obv','mfi']);momentum_votes=sum(votes[k] for k in ['rsi','macd','momentum'])
        accepted=score>=int(policy.get('min_score',5)) and volume_votes>=2 and momentum_votes>=2
        result.update(status='QUALIFIED' if accepted else 'BLOCKED',reason='CONFLUENCE_QUALIFIED' if accepted else 'CONFLUENCE_NOT_CONFIRMED',
            score=score,votes=votes,values=values,volume_votes=volume_votes,momentum_votes=momentum_votes,candle_time=dates.iloc[-1].isoformat())
        return result
    except Exception as exc:
        result['error_type']=type(exc).__name__;return result
