# IctSmcStrategy.py — your ICT/SMC scalping logic, ported to Freqtrade.
#
# WHAT THIS IS: a STARTING POINT that moves your strategy onto Freqtrade's
# battle-tested engine (durable order journal, fill ledger, crash recovery,
# exchange-native trailing stop, dry-run, backtesting, Telegram control).
#
# WHAT YOU MUST DO BEFORE ANY REAL MONEY:
#   1) freqtrade backtesting --strategy IctSmcStrategy --timeframe 1m ...
#   2) freqtrade trade --dry-run   (paper trade on live data for days)
#   3) only then, tiny live.
# The engine is now solid; the STRATEGY still has to prove an edge in backtest.
# Every real backtest of this logic so far has LOST money — treat a positive
# result with suspicion and verify (no look-ahead, realistic fees/slippage).
#
# Install: https://www.freqtrade.io/en/stable/installation/
# Put this file in user_data/strategies/.

import logging
from pathlib import Path
import os, json, hashlib, tempfile
from datetime import datetime, timezone

import numpy as np
from pandas import DataFrame

from freqtrade.strategy import IStrategy, informative
import talib.abstract as ta
import freqtrade.vendor.qtpylib.indicators as qtpylib


logger = logging.getLogger(__name__)


class IctSmcStrategy(IStrategy):
    # ---- core config ----
    timeframe = "1m"
    can_short = False                 # SPOT only
    process_only_new_candles = True
    startup_candle_count = 210        # enough history for EMA200 context
    use_exit_signal = True
    exit_profit_only = False

    # ---- risk / exits (exchange-native, like your OCO trailing) ----
    # Hard stop (absolute floor). Trailing takes over once in profit.
    stoploss = -0.02                  # -2% hard floor; TUNE from backtests
    trailing_stop = True
    trailing_stop_positive = 0.004    # trail 0.4% behind price...
    trailing_stop_positive_offset = 0.006   # ...once +0.6% in profit
    trailing_only_offset_is_reached = True

    # minimal_roi = time-based FULL-exit thresholds (age_minutes -> min profit).
    # It exits the WHOLE position when reached — it is NOT a partial-TP ladder.
    minimal_roi = {
        "0": 0.012,    # take 1.2% immediately if available
        "20": 0.006,   # after 20 min, 0.6% is enough
        "45": 0.003,   # after 45 min, 0.3%
        "90": 0.0,     # after 90 min, exit at break-even+
    }

    # Place the stop ON the exchange (survives bot downtime), like your design.
    # Binance SPOT supports only STOP_LOSS_LIMIT for on-exchange stops
    # (verified in freqtrade source: stoploss_order_types = {"limit": ...}),
    # so "stoploss" MUST be "limit" here. Note the honest limitation of a
    # stop-LIMIT: in a violent gap through both stop and limit price it can
    # remain unfilled — the limit_ratio 0.99 gives a 1% fill buffer.
    # force_exit is MARKET so your Telegram /forceexit closes IMMEDIATELY.
    order_types = {
        "entry": "limit",
        "exit": "limit",
        "force_entry": "limit",
        "force_exit": "market",
        "emergency_exit": "market",
        "stoploss": "limit",
        "stoploss_on_exchange": True,
        "stoploss_on_exchange_interval": 60,
        "stoploss_on_exchange_limit_ratio": 0.99,
    }
    order_time_in_force = {"entry": "GTC", "exit": "GTC"}

    # Portfolio protections (tunable): pause after a stop-out, halt the pair
    # after repeated stops, and stand down on drawdown.
    @property
    def protections(self):
        return [
            {"method": "CooldownPeriod", "stop_duration_candles": 5},
            {
                "method": "StoplossGuard",
                "lookback_period_candles": 60,
                "trade_limit": 3,
                "stop_duration_candles": 60,
                "only_per_pair": True,
            },
            {
                "method": "MaxDrawdown",
                "lookback_period_candles": 1440,
                "trade_limit": 10,
                "max_allowed_drawdown": 0.1,
                "stop_duration_candles": 720,
            },
        ]

    # ---- tunables (your thresholds) ----
    RVOL_MIN = 1.5
    RSI_MIN = 50.0

    # ---- fail-closed live interlock + durable pause (v4, audit-hardened) ----
    # confirm_trade_entry is Freqtrade's LAST gate before an entry order is sent.
    #  * ANY error/uncertainty in this gate  -> entry REFUSED (fail-closed).
    #  * user_data/PAUSE exists              -> no new entries, any mode; survives
    #    restarts. (It does NOT cancel an already-open entry order — for that use
    #    /stopentry + /forceexit, or stop the bot.)
    #  * LIVE mode additionally requires:
    #      - db_url is NOT the dry-run database (histories must never mix), and
    #      - user_data/LIVE_OK containing THIS release's approval hash — a
    #        SHA-256 over the strategy file + the security-relevant effective
    #        config (exchange, whitelist, stake, max trades, db_url). Any change
    #        to those invalidates a stale LIVE_OK. The bot logs the expected
    #        hash at startup in live mode; write it deliberately:
    #            echo <hash> > user_data/LIVE_OK
    def _live_approval_hash(self) -> str:
        import hashlib, json as _json
        ex = self.config.get("exchange", {})
        relevant = {
            # identity + universe
            "exchange": ex.get("name", ""),
            "pair_whitelist": ex.get("pair_whitelist", []),
            "pair_blacklist": ex.get("pair_blacklist", []),
            "trading_mode": self.config.get("trading_mode", ""),
            "timeframe": self.config.get("timeframe", ""),
            # sizing / risk
            "stake_currency": self.config.get("stake_currency", ""),
            "stake_amount": str(self.config.get("stake_amount", "")),
            "max_open_trades": self.config.get("max_open_trades", 0),
            "tradable_balance_ratio": self.config.get("tradable_balance_ratio", 1),
            # config-level STRATEGY OVERRIDES (freqtrade lets config override these)
            "stoploss": self.config.get("stoploss"),
            "minimal_roi": self.config.get("minimal_roi"),
            "trailing_stop": self.config.get("trailing_stop"),
            "order_types": self.config.get("order_types"),
            "unfilledtimeout": self.config.get("unfilledtimeout"),
            # operational switches
            "force_entry_enable": self.config.get("force_entry_enable"),
            "initial_state": self.config.get("initial_state", ""),
            "db_url": self.config.get("db_url", ""),
        }
        h = hashlib.sha256()
        h.update(Path(__file__).read_bytes())
        h.update(_json.dumps(relevant, sort_keys=True).encode())
        return h.hexdigest()[:16]


    # ---- Active fail-closed Sharia gate + Freqtrade-to-sidecar signal seam ----
    _halal_cache = (None, None)

    def _sharia_path(self) -> Path:
        return Path(os.getenv('SHARIA_FILE', '/freqtrade/shared/sharia/sharia_status.json'))

    def _halal_allowed(self):
        """Trade-eligible bases under the V19.1 projection (fail-closed).

        Only the schema_version-2 status file written by the sharia-screener
        service is accepted; the legacy HALAL/HARAM vocabulary is rejected so
        the previous Sharia definition can never gate entries again. Only a
        current GREEN or GREEN_AVOID_OPTIONAL record is trade-eligible.
        Returns (allowed_bases, code_by_base); errors return empty sets.
        """
        try:
            p=self._sharia_path(); st=p.stat(); cache_key=(st.st_mtime_ns,st.st_size)
            if self._halal_cache[1] is not None and self._halal_cache[0]==cache_key:
                return self._halal_cache[1]
            raw=json.loads(p.read_text()); now=datetime.now(timezone.utc)
            if not isinstance(raw,dict) or raw.get('schema_version')!=2:
                raise ValueError('legacy or unknown Sharia dataset (V19.1 schema_version 2 required)')
            records=raw.get('records',[])
            if not isinstance(records,list) or not records: raise ValueError('empty Sharia records')
            valid={'GREEN','GREEN_AVOID_OPTIONAL','NO_TRADE_INFO','NO_TRADE_YIELD','DOUBTFUL','HARAM','TECH_STOP'}
            eligible={'GREEN','GREEN_AVOID_OPTIONAL'}
            allowed=set(); codes={}; seen=set()
            for r in records:
                if not isinstance(r,dict): raise ValueError('malformed Sharia record')
                symbol=str(r.get('symbol','')).upper().replace('/','')
                if symbol.endswith('USDT'): symbol=symbol[:-4]
                if not symbol or not symbol.isalnum() or symbol in seen:
                    raise ValueError('invalid or duplicate Sharia symbol')
                seen.add(symbol)
                status=str(r.get('status','')).upper()
                if status not in valid: raise ValueError('non-V19.1 Sharia status: '+status)
                if not str(r.get('source','')).strip(): raise ValueError('Sharia source missing')
                try: reviewed=datetime.fromisoformat(str(r.get('reviewed_at',''))[:10]).date()
                except Exception as exc: raise ValueError('malformed reviewed_at') from exc
                if reviewed>now.date(): raise ValueError('future reviewed_at')
                try: exp=datetime.fromisoformat(str(r.get('expires_at','')).replace('Z','+00:00'))
                except Exception as exc: raise ValueError('malformed expires_at') from exc
                if exp.tzinfo is None: exp=exp.replace(tzinfo=timezone.utc)
                codes[symbol]=status
                if status in eligible and exp>now: allowed.add(symbol)
            result=(allowed,codes)
            type(self)._halal_cache=(cache_key,result)
            return result
        except Exception as exc:
            self._seam_error('Sharia dataset unreadable (%s) — FAIL-CLOSED.'%exc)
            return (set(),{})

    def _is_halal(self,pair: str)->bool:
        allowed,_=self._halal_allowed()
        return pair.split('/')[0].upper() in allowed

    def _emit_signal(self,pair: str,row) -> None:
        inbox=Path(os.getenv('SIGNAL_INBOX','/freqtrade/shared/signals/inbox')); inbox.mkdir(parents=True,exist_ok=True)
        candle=row.get('date'); candle_time=candle.isoformat() if hasattr(candle,'isoformat') else str(candle)
        universe=Path(os.getenv('UNIVERSE_FILE','/freqtrade/shared/universe/current_pairlist.json'))
        universe_hash=''
        try:
            _u=json.loads(universe.read_text()); universe_hash=_u.get('snapshot_hash') or _u.get('configuration_hash','')
        except Exception: pass
        token=hashlib.sha256(f'{pair}|{candle_time}|IctSmcStrategy|ema_vwap_pullback'.encode()).hexdigest()[:24]
        target=inbox/f'{token}.json'
        processed=Path(os.getenv('SIGNAL_PROCESSED','/freqtrade/shared/signals/processed'))
        rejected=Path(os.getenv('SIGNAL_REJECTED','/freqtrade/shared/signals/rejected'))
        # The sidecar moves files out of inbox. Check all three archives so the
        # same closed candle is not recreated on every bot loop.
        if target.exists() or any((folder/f'{token}.json').exists() for folder in (processed,rejected)):
            return
        if any(folder.exists() and next(folder.glob(f'{token}.*.json'),None) for folder in (processed,rejected)):
            return
        _,codes=self._halal_allowed()
        payload={'signal_id':token,'pair':pair,'symbol':pair.replace('/',''),'candle_time':candle_time,
          'generated_at':datetime.now(timezone.utc).isoformat(),'strategy':'IctSmcStrategy','entry_tag':str(row.get('enter_tag','')),
          'universe_hash':universe_hash,'sharia_status':codes.get(pair.split('/')[0].upper(),''),'payload':{
          'close':float(row.get('close',0) or 0),'rsi':float(row.get('rsi',0) or 0),'rvol':float(row.get('rvol',0) or 0),
          'adx':float(row.get('adx',0) or 0),'macdhist_5m':float(row.get('macdhist_5m',0) or 0)}}
        # V101-NEW-001: signals are HMAC-signed envelopes; the sidecar rejects
        # anything unsigned. If signing is unavailable, NO signal is emitted.
        try:
            from services.common.envelope import BUS_SIGNAL, sign_envelope
            signed=sign_envelope(producer='freqtrade-strategy',purpose=BUS_SIGNAL,payload=payload,
                                 ttl_seconds=int(os.getenv('MAX_SIGNAL_AGE_SECONDS','180'))+60)
        except Exception as exc:
            self._seam_error('signal signing unavailable — FAIL-CLOSED, no emission: %s'%exc)
            return
        fd,tmp=tempfile.mkstemp(prefix=token+'.',suffix='.tmp',dir=inbox)
        try:
            with os.fdopen(fd,'w') as f: json.dump(signed,f,sort_keys=True); f.flush(); os.fsync(f.fileno())
            os.replace(tmp,target)
            type(self)._seam_state['emitted']=type(self)._seam_state.get('emitted',0)+1
        finally:
            try:
                if os.path.exists(tmp): os.unlink(tmp)
            except OSError: pass

    # ---- signal-seam observability (V101-NEW-004) ----
    _seam_state={'last_error':'','count':0,'last_log':0.0,'emitted':0}

    def _seam_error(self,message: str)->None:
        """Escalating, rate-limited seam error reporting (never DEBUG-only)."""
        state=type(self)._seam_state; import time as _t
        state['count']=state['count']+1 if state['last_error']==message else 1
        state['last_error']=message
        if _t.time()-state['last_log']>60:
            state['last_log']=_t.time()
            log_fn=logger.error if state['count']>=5 else logger.warning
            log_fn('signal seam: %s (repeat x%d)',message,state['count'])

    def _seam_heartbeat(self,ok: bool,whitelist: int,halal: int,error: str='')->None:
        """Durable heartbeat consumed by the container healthcheck."""
        try:
            hb=Path(os.getenv('SIGNAL_HEARTBEAT','/freqtrade/shared/freqtrade/signal_seam_heartbeat.json'))
            hb.parent.mkdir(parents=True,exist_ok=True)
            state=type(self)._seam_state
            payload={'ts':datetime.now(timezone.utc).isoformat(),'ok':bool(ok),
                     'whitelist':int(whitelist),'halal':int(halal),
                     'emitted_total':int(state.get('emitted',0)),
                     'last_error':error or state.get('last_error',''),
                     'error_repeat':int(state.get('count',0))}
            fd,tmp=tempfile.mkstemp(dir=hb.parent,suffix='.tmp')
            with os.fdopen(fd,'w') as f: json.dump(payload,f,sort_keys=True); f.flush(); os.fsync(f.fileno())
            os.replace(tmp,hb)
        except Exception:
            pass

    def bot_loop_start(self,current_time=None,**kwargs)->None:
        wl_count=halal_count=0
        try:
            wl=self.dp.current_whitelist(); halal=[p for p in wl if self._is_halal(p)]
            wl_count,halal_count=len(wl),len(halal)
            key=','.join(halal); last_key,last_ts=getattr(self,'_wl_state',('',0.0)); import time as _t
            if key!=last_key or (_t.time()-last_ts)>1800:
                self._wl_state=(key,_t.time())
                if key!=last_key:self.dp.send_msg(f'Universe V19.1-filtered ({len(halal)}/{len(wl)}): '+(', '.join(x.split('/')[0] for x in halal) or 'NONE'))
            for pair in halal:
                df,_=self.dp.get_analyzed_dataframe(pair,self.timeframe)
                if df is None or df.empty: continue
                row=df.iloc[-1]
                if int(row.get('enter_long',0) or 0)==1:self._emit_signal(pair,row)
            self._seam_heartbeat(True,wl_count,halal_count)
        except Exception as exc:
            # V101-NEW-004 fix: a persistent seam failure is escalated and
            # visible to the healthcheck instead of vanishing at DEBUG level.
            self._seam_error(str(exc))
            self._seam_heartbeat(False,wl_count,halal_count,error=str(exc))

    def bot_start(self,**kwargs)->None:
        logger.warning('V10.1 signal-engine mode: Freqtrade order submission is disabled; execution sidecar owns Binance orders.')

    def confirm_trade_entry(self,pair: str,order_type: str,amount:float,rate:float,time_in_force:str,current_time,entry_tag,side:str,**kwargs)->bool:
        # Freqtrade is permanently signal-only in V8.1. It must never compete with the sidecar for order ownership.
        return False

    # ================= 5-minute trend filter (#9) =================
    @informative("5m")
    def populate_indicators_5m(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema9"] = ta.EMA(dataframe, timeperiod=9)
        dataframe["ema21"] = ta.EMA(dataframe, timeperiod=21)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["ema200"] = ta.EMA(dataframe, timeperiod=200)   # context/backdrop
        macd = ta.MACD(dataframe, fastperiod=12, slowperiod=26, signalperiod=9)
        dataframe["macdhist"] = macd["macdhist"]
        dataframe["macd"] = macd["macd"]
        dataframe["macdsignal"] = macd["macdsignal"]
        return dataframe

    # ================= 1-minute entry logic (#1) =================
    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema9"] = ta.EMA(dataframe, timeperiod=9)
        dataframe["ema21"] = ta.EMA(dataframe, timeperiod=21)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)

        # RSI as a MOMENTUM filter (>50 & rising), not oversold
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["rsi_rising"] = dataframe["rsi"] > dataframe["rsi"].shift(1)

        # 1m MACD fast 5/13/6 (soft confirmation / boost)
        macd = ta.MACD(dataframe, fastperiod=5, slowperiod=13, signalperiod=6)
        dataframe["macdhist"] = macd["macdhist"]

        # Rolling VWAP over the last 200 bars (an approximation — NOT a true
        # session-anchored VWAP; treat it as a dynamic value baseline)
        dataframe["vwap"] = qtpylib.rolling_vwap(dataframe, window=200)

        # RVOL = current volume / 20-bar average
        dataframe["vol_ma"] = dataframe["volume"].rolling(20).mean()
        dataframe["rvol"] = dataframe["volume"] / dataframe["vol_ma"]

        dataframe["adx"] = ta.ADX(dataframe)

        # pullback: a recent low tagged the EMA9/EMA21 value zone
        zone = dataframe[["ema9", "ema21"]].max(axis=1)
        # a pullback = any of the last 3 bars traded down into its own EMA zone
        touched = (dataframe["low"] <= zone)
        dataframe["pullback"] = touched.rolling(3).max() > 0
        dataframe["ema9_rising"] = dataframe["ema9"] >= dataframe["ema9"].shift(1)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        conditions = [
            # --- 5m HARD trend gate (#9) ---
            (dataframe["ema9_5m"] > dataframe["ema21_5m"]),
            (dataframe["ema21_5m"] > dataframe["ema50_5m"]),
            (dataframe["close"] > dataframe["ema50_5m"]),
            (dataframe["macdhist_5m"] > 0),                 # 5m MACD bullish (hard)
            # --- 1m entry (#1) ---
            (dataframe["close"] > dataframe["vwap"]),       # above VWAP value
            (dataframe["pullback"]),                        # pulled back to EMA9/21
            (dataframe["close"] > dataframe["ema9"]),       # reclaim close
            (dataframe["ema9_rising"]),
            # --- momentum + participation ---
            (dataframe["rsi"] > self.RSI_MIN),
            (dataframe["rsi_rising"]),
            (dataframe["rvol"] >= self.RVOL_MIN),
            # 1m MACD (5/13/6) is computed for reference/analysis only — it has
            # NO effect on entries in this version. To make it a gate, add:
            #   (dataframe["macdhist"] > 0),
            (dataframe["adx"] > 20),
            (dataframe["volume"] > 0),
        ]
        dataframe.loc[
            np.logical_and.reduce(conditions),
            ["enter_long", "enter_tag"],
        ] = (1, "ema_vwap_pullback")
        # Sharia gate at signal level too (consistent in backtests): a pair not
        # in halal_list.json produces NO entry signals. (Skipped only when run
        # bare, without a freqtrade config.)
        if getattr(self, "config", None) and not self._is_halal(metadata.get("pair", "")):
            dataframe["enter_long"] = 0
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Exits are handled by minimal_roi + trailing stop + exchange stoploss.
        # A light structural exit: price loses VWAP AND 5m turns bearish.
        dataframe.loc[
            (
                (dataframe["close"] < dataframe["vwap"])
                & (dataframe["macdhist_5m"] < 0)
            ),
            ["exit_long", "exit_tag"],
        ] = (1, "lost_vwap_5m_bear")
        return dataframe

    # OPTIONAL next step — BTC regime gate. To require BTC bullish before any alt
    # entry, add BTC/USDT to informative_pairs() and merge its trend here:
    #
    # def informative_pairs(self):
    #     return [("BTC/USDT", "5m"), ("BTC/USDT", "1m")]
    #
    # then in populate_indicators, use self.dp.get_pair_dataframe("BTC/USDT","5m")
    # to compute a BTC GREEN/RED flag and AND it into populate_entry_trend.
