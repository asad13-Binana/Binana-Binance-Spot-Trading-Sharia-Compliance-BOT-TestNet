"""BINANA's pinned NFI-derived Spot/Testnet strategy profile."""
from __future__ import annotations
import hashlib
import importlib.util
from pathlib import Path
import json
import sys
import time

from freqtrade.binana.environment import assert_spot_pair, validate_runtime_contract

_SOURCE=Path(__file__).parent/'binana_vendor'/'NostalgiaForInfinityX8.py'
_SHA256='fcf9f622a3ad797bd143561d1017f3930220f22c793905af929ff1d33bbde686'
if hashlib.sha256(_SOURCE.read_bytes()).hexdigest()!=_SHA256:
    raise ImportError('Pinned NFI X8 source checksum mismatch')
_NAME='_binana_nfi_x8_e9e165f8_runtime'
_spec=importlib.util.spec_from_file_location(_NAME,_SOURCE)
if _spec is None or _spec.loader is None:
    raise ImportError('Cannot load pinned NFI source')
_vendor=importlib.util.module_from_spec(_spec); sys.modules[_NAME]=_vendor; _spec.loader.exec_module(_vendor)

_SIGNAL_JOURNAL = Path('/freqtrade/shared/runtime/freqtrade_lifecycle_events.jsonl')
_SEEN_SIGNAL_IDS: set[str] = set()

def _journal_strategy_event(event_type: str, pair: str, **fields) -> None:
    from freqtrade.binana.durable_journal import append_event
    payload={'ts':time.time(),'type':event_type,'source':'binana_wrapper','pair':str(pair).upper()}
    payload.update({k:v for k,v in fields.items() if v is not None})
    try:
        append_event(_SIGNAL_JOURNAL,payload)
    except Exception as exc:
        import logging
        logging.getLogger("binana-signal-journal").error("Signal evidence persistence failed: %s",type(exc).__name__)

def _journal_latest_signal(df, pair: str) -> None:
    if df is None or len(df) == 0 or 'enter_long' not in df.columns:
        return
    row = df.iloc[-1]
    if not bool(row.get('enter_long', 0)):
        return
    candle = row.get('date', '')
    tag = str(row.get('enter_tag') or '')
    _journal_strategy_event('STRATEGY_SIGNAL', pair, candle_time=str(candle),
        entry_tag=tag, close=row.get('close'), rsi=row.get('RSI_14'),
        mfi=row.get('MFI_14'), cmf=row.get('CMF_20'))


class BinanaNfiSpot(_vendor.NostalgiaForInfinityX8):
    INTERFACE_VERSION=3
    can_short=False
    timeframe='5m'
    position_adjustment_enable=False
    use_custom_stoploss=False
    trailing_stop=False
    hold_support_enabled=False
    use_exit_signal=False
    order_time_in_force={'entry':'FOK','exit':'GTC'}
    order_types={'entry':'limit','exit':'limit','emergency_exit':'market','force_exit':'market',
                 'force_entry':'market','stoploss':'market','stoploss_on_exchange':False}

    def __init__(self, config: dict) -> None:
        validate_runtime_contract(config)
        super().__init__(config)
        # NFI can change config-derived behavior during construction. Re-assert
        # the execution contract afterwards and pin the adapted behavior.
        validate_runtime_contract(self.config)
        self.can_short=False
        self.is_futures_mode=False
        self.position_adjustment_enable=False
        self.hold_support_enabled=False
        self.use_custom_stoploss=False
        self.trailing_stop=False
        self.use_exit_signal=False
        self.order_time_in_force={'entry':'FOK','exit':'GTC'}
        self.order_types={'entry':'limit','exit':'limit','emergency_exit':'market','force_exit':'market',
                          'force_entry':'market','stoploss':'market','stoploss_on_exchange':False}
        # Exchange protection belongs to BINANA ProtectionManager, not NFI's
        # fallback -99% stop. Keep a finite logical emergency bound only.
        self.stoploss=-float(self.config['binana']['logical_emergency_stop_fraction'])
        # Normal profit-taking is represented by the exchange ProtectionPlan.
        # Keep Freqtrade ROI effectively disabled in this first order-list profile.
        self.minimal_roi={"0": float(self.config['binana']['logical_roi_fraction'])}

    def populate_entry_trend(self, df, metadata: dict):
        df = super().populate_entry_trend(df, metadata)
        if self.config.get('binana', {}).get('environment') != 'testnet':
            return df
        # Stabilization mode: NFI remains the sole strategy signal authority.
        # BINANA safety/liquidity/admission checks run after the native NFI signal;
        # this wrapper must not manufacture independent STRONG/NORMAL/EARLY entries.
        _journal_latest_signal(df, str(metadata.get('pair', '')))
        return df

    def version(self) -> str:
        return 'BINANA-X8-v18.0.26-testnet-owner-5'

    def custom_stake_amount(self, pair, current_time, current_rate, proposed_stake,
                            min_stake, max_stake, leverage, entry_tag, side, **kwargs):
        try:
            assert_spot_pair(pair,side=side,leverage=leverage)
        except Exception:
            return 0.0
        nominal=float(self.config.get('stake_amount', proposed_stake))
        try:
            override=json.loads(Path('/freqtrade/shared/runtime/trade_size.json').read_text())
            requested=float(override.get('usdt'))
            if requested > 0:
                nominal=requested
        except Exception:
            pass
        if min_stake is not None and nominal < float(min_stake): return 0.0
        if max_stake is not None and nominal > float(max_stake): return 0.0
        return nominal

    def adjust_trade_position(self,*args,**kwargs):
        return None

    def confirm_trade_entry(self,pair,order_type,amount,rate,time_in_force,
                            current_time,entry_tag,side,**kwargs):
        try:
            assert_spot_pair(pair,side=side,leverage=1)
            validate_runtime_contract(self.config)
        except Exception as exc:
            _journal_strategy_event('TRADE_REJECTED', pair, entry_tag=entry_tag, reason='runtime_contract', detail=type(exc).__name__)
            return False
        if not entry_tag or entry_tag=='force_entry':
            _journal_strategy_event('TRADE_REJECTED', pair, entry_tag=entry_tag, reason='invalid_entry_tag')
            return False
        unsupported=set(self.long_rebuy_mode_tags)|set(self.long_grind_mode_tags)|set(self.long_btc_mode_tags)
        if set(entry_tag.split()) & unsupported:
            _journal_strategy_event('TRADE_REJECTED', pair, entry_tag=entry_tag, reason='unsupported_nfi_mode')
            return False
        accepted = super().confirm_trade_entry(pair,order_type,amount,rate,time_in_force,current_time,entry_tag,side,**kwargs)
        if not accepted:
            _journal_strategy_event('TRADE_REJECTED', pair, entry_tag=entry_tag, reason='nfi_confirm_trade_entry')
        return accepted

    def custom_exit(self,*args,**kwargs):
        return None

    def confirm_trade_exit(self,pair,trade,order_type,amount,rate,time_in_force,
                           exit_reason,current_time,**kwargs):
        # Exchange protection owned by BINANA is the sole automatic stop/target
        # authority. A separate Freqtrade stop SELL can race the live OCO children.
        if exit_reason in {'stop_loss','trailing_stop_loss','stoploss_on_exchange'}:
            return False
        # Explicit operator/emergency exits remain available.
        if exit_reason in {'force_exit','emergency_exit'}:
            return True
        # Normal NFI/ROI exits are intentionally adapted into the OCO target policy
        # instead of issuing a second independent SELL beside exchange protection.
        return False
