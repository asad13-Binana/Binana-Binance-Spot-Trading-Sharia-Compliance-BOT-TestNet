"""Freqtrade-owned BINANA admission and entry-order coordinator."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from hashlib import sha256
import json
from pathlib import Path
from typing import Any

from .binance_order_lists import BinanceOrderLists, OrderListError, OrderListUnknown, _client
from .environment import assert_spot_pair, validate_runtime_contract, resolve_stake_amount, BinanaConfigError
from .market_data import MarketDataService, MarketSnapshot
from .protection_plan import provisional_plan
from .registry import OwnerRegistry, RegistryError
from .state_store import StateStore


class AdmissionRejected(RuntimeError):
    pass


@dataclass(frozen=True)
class PreparedAdmission:
    intent_id: str
    pair: str
    enter_tag: str
    profile: str
    market: MarketSnapshot
    registry_sha256: str
    nominal_usdt: Decimal


from .verified_recovery import VerifiedRecovery
from .entry_recovery import EntryRecovery
from .canonical_receipts import CanonicalReceipts
from .retained_inventory import RetainedInventory
from .closed_recovery import ClosedRecovery
from .operator_exit import OperatorExit

class BinanaExecutionManager(OperatorExit, ClosedRecovery, RetainedInventory, CanonicalReceipts, EntryRecovery, VerifiedRecovery):
    TERMINAL_INTENT_STATES={"EXIT_FILLED","DUST_RETAINED","CLOSED_NO_FILL","CLOSED"}

    def __init__(self, config: dict, exchange):
        self.config=config; self.exchange=exchange
        validate_runtime_contract(config)
        policy=config['binana']
        self.registry=OwnerRegistry(policy['halal_registry_path'])
        self.store=StateStore(policy['state_db_path'])
        self.market_data=MarketDataService(exchange,policy['market_context_path'],policy.get('liquidity',{}))
        self.order_lists=BinanceOrderLists(exchange._api)
        account_label=str(policy.get('account_scope') or 'owner-testnet-account-1').strip()
        if not account_label:
            raise AdmissionRejected('MISSING_ACCOUNT_SCOPE')
        self.scope_id=f"binance-testnet|{account_label}|{policy['testnet_epoch_id']}"
        self.legacy_scope_ids={f"binance-testnet|{policy['testnet_epoch_id']}|no-key"}
        self.prepared: dict[str,PreparedAdmission]={}
        self._trade_intent: dict[int,str]={}
        self._promotion_last: dict[int,float]={}
        self.user_stream = None
        self._rest_health = {}
        self._retained_health = {}
        self._rest_checked_at = 0.0
        self.canonical_reconciliation_ready = False

    def start_user_stream(self):
        from .user_stream import PrivateUserStream
        if self.user_stream is None:
            self.user_stream = PrivateUserStream(self.exchange._api.apiKey,
                self.exchange._api.secret, scope=self.scope_id,
                runtime=Path(self.config['binana']['state_db_path']).parent/'runtime')
        self.user_stream.start()

    def stop_user_stream(self):
        if self.user_stream is not None:
            self.user_stream.stop()

    def acknowledge_canonical_reconciliation(self, trade):
        """Resolve a prior import failure only after verified ownership recovered."""
        intent_id = self.intent_for_trade(int(trade.id))
        intent = self.store.get_intent(intent_id) if intent_id else None
        if intent and intent['state'] in {'OPEN', 'EXIT_FILLED', 'DUST_RETAINED'}:
            self.store.close_incident(f'canonical-trade-{trade.id}')
            latest = self.store.latest_generation(intent_id)
            if latest and (intent['state']=='DUST_RETAINED' or (
                    intent['state']=='OPEN' and latest['mode'] in {'FIXED_OCO','TRAILING_OCO'}
                    and latest['status'] in {'FIXED_ACTIVE','TRAILING_ACTIVE'})):
                self.store.close_incident('operator-exit-'+intent_id)

    def refresh_runtime_evidence(self):
        """Read exchange ownership in the owner loop; never repair foreign orders."""
        from time import time
        now = time()
        if now - self._rest_checked_at < 20:
            return
        self._rest_checked_at = now
        self._rest_health = {'ok':False, 'checked_at':now}
        try:
            api = self.exchange._api
            if api.urls['api']['private'] != 'https://testnet.binance.vision/api/v3':
                raise AdmissionRejected('EXECUTION_ENDPOINT_MISMATCH')
            account = api.privateGetAccount()
            if account.get('accountType') != 'SPOT' or account.get('canTrade') is not True:
                raise AdmissionRejected('ACCOUNT_SPOT_TRADING_UNAVAILABLE')
            orders = api.privateGetOpenOrders()
            lists = api.privateGetOpenOrderList()
            with self.store._connect() as connection:
                identities = connection.execute('SELECT scope_id,pair,order_id,client_id FROM order_identity').fetchall()
                protections = connection.execute('SELECT pair,order_list_id,list_client_id FROM protection').fetchall()
            allowed = self.legacy_scope_ids | {self.scope_id}
            owned = {(r['pair'].replace('/',''), str(r['order_id']), r['client_id'])
                     for r in identities if r['scope_id'] in allowed}
            owned_lists = {(r['pair'].replace('/',''), str(r['order_list_id']), r['list_client_id'])
                           for r in protections}
            if any((r['symbol'], str(r['orderId']), r['clientOrderId']) not in owned for r in orders):
                raise AdmissionRejected('ACCOUNT_ORDER_OWNERSHIP_MISMATCH')
            if any((r['symbol'], str(r['orderListId']), r['listClientOrderId']) not in owned_lists for r in lists):
                raise AdmissionRejected('ACCOUNT_LIST_OWNERSHIP_MISMATCH')
            from freqtrade.persistence import Trade
            self.refresh_retained_inventory(Trade.get_trades_proxy(), orders)
            self._rest_health = {'ok':True, 'checked_at':time(),
                                 'open_orders':len(orders), 'open_lists':len(lists)}
        except Exception as exc:
            self._rest_health['reason'] = str(exc) if isinstance(exc, AdmissionRejected) else type(exc).__name__

    def release_status(self) -> dict:
        from .release_gate import runtime_bindings, certificate_blockers, load_certificate
        from time import time
        bindings = runtime_bindings(self.config)
        bindings['account_key_sha256'] = sha256(str(self.exchange._api.apiKey).encode()).hexdigest()
        runtime = Path(self.config['binana']['state_db_path']).parent / 'runtime'
        certificate = load_certificate(runtime / 'freqtrade_owner_patch_ready.json')
        blockers = certificate_blockers(certificate, bindings)
        from .user_stream import health_is_fresh
        if self.user_stream is None or not health_is_fresh(self.user_stream.health()):
            blockers.append('PRIVATE_USER_STREAM_UNAVAILABLE')
        if not self.canonical_reconciliation_ready:
            blockers.append('CANONICAL_RECONCILIATION_UNAVAILABLE')
        rest = self._rest_health
        if not rest.get('ok') or not 0 <= time()-rest.get('checked_at',0) < 35:
            blockers.append('AUTHENTICATED_RECONCILIATION_UNAVAILABLE')
        retained = getattr(self, '_retained_health', {})
        if not retained.get('ok') or not 0 <= time()-retained.get('checked_at',0) < 35:
            blockers.append('RETAINED_INVENTORY_VERIFICATION_UNAVAILABLE')
        elif retained.get('executable_pairs'):
            blockers.append('AGGREGATE_RETAINED_INVENTORY_EXECUTABLE')
        try:
            context = json.loads(self.market_data.context_file.read_text())
            generated = datetime.fromisoformat(context['generated_at'].replace('Z','+00:00')).timestamp()
            if not 0 <= time()-generated < 30 or context.get('fresh_symbol_count',0) <= 0:
                raise ValueError('market unavailable')
        except (OSError, ValueError, TypeError, KeyError):
            blockers.append('PUBLIC_SPOT_DATA_UNAVAILABLE')
        if self.store.has_blockers():
            blockers.append('RECONCILIATION_BLOCKER_OPEN')
        return {**bindings, 'generated_at':time(), 'valid_until':time()+30,
                'order_owner':'FREQTRADE', 'scope_id':self.scope_id, 'execution_environment':'TESTNET',
                'market_data_environment':'PRODUCTION_PUBLIC_SPOT',
                'authenticated_reconciliation':dict(rest),
                'retained_inventory':dict(retained),
                'release_approved':not blockers, 'blockers':blockers}

    def assert_release_ready(self) -> None:
        try:
            status = self.release_status()
        except Exception as exc:
            raise AdmissionRejected('OWNER_RELEASE_EVIDENCE_UNAVAILABLE') from exc
        if status['blockers']:
            raise AdmissionRejected('OWNER_RELEASE_BLOCKED:' + ','.join(status['blockers']))

    def publish_release_status(self) -> None:
        import os
        self.refresh_runtime_evidence()
        runtime = Path(self.config['binana']['state_db_path']).parent / 'runtime'
        runtime.mkdir(parents=True, exist_ok=True)
        destination = runtime / 'owner_readiness.json'
        temporary = destination.with_suffix('.json.tmp')
        temporary.write_text(json.dumps(self.release_status(), sort_keys=True))
        os.replace(temporary, destination)

    def _nominal_usdt(self) -> str:
        return str(resolve_stake_amount(self.config,
            override_path=Path('/freqtrade/shared/runtime/trade_size.json')))

    def _requested_nominal_usdt(self) -> Decimal:
        try:
            value=Decimal(self._nominal_usdt())
        except BinanaConfigError as exc:
            raise AdmissionRejected(str(exc)) from exc
        if not value.is_finite() or value <= 0:
            raise AdmissionRejected('INVALID_TRADE_SIZE')
        return value

    def _intent_id(self, pair: str, enter_tag: str, candle_date) -> str:
        if hasattr(candle_date,'isoformat'): stamp=candle_date.isoformat()
        else: stamp=str(candle_date)
        return sha256(f"{self.scope_id}|{pair}|{enter_tag}|{stamp}".encode()).hexdigest()[:32]

    @staticmethod
    def _execution_rejection_evidence(snapshot: MarketSnapshot, policy: dict) -> dict[str,Any]:
        def text(value):
            return None if value is None else str(value)
        depths=[Decimal(str(value)) for value in (snapshot.bid_depth_20_quote,snapshot.ask_depth_20_quote) if value is not None]
        return {
            'code': snapshot.reason,
            'market_state': snapshot.state,
            'spread_bps': text(snapshot.spread_bps),
            'hard_max_spread_bps': text(policy.get('hard_max_spread_bps',100)),
            'min_depth_20_quote': text(min(depths) if depths else None),
            'hard_min_depth_quote': text(policy.get('hard_min_depth_quote',250)),
            'buy_slippage_bps': text(snapshot.buy_250_slippage_bps),
            'exit_slippage_bps': text(snapshot.exit_250_slippage_bps),
            'slippage_quote_budget': text(snapshot.slippage_quote_budget),
            'hard_max_slippage_bps': text(policy.get('hard_max_slippage_bps',75)),
            'quote_volume_24h': text(snapshot.quote_volume_24h),
            'min_quote_volume_24h': text(policy.get('min_quote_volume_24h',1000000)),
        }

    @staticmethod
    def _flow_rejection_evidence(snapshot: MarketSnapshot, policy: dict, code: str) -> dict[str,Any]:
        return {
            'code': code,
            'flow_status': snapshot.flow_status,
            'taker_buy_ratio_60s': None if snapshot.taker_buy_ratio_60s is None else str(snapshot.taker_buy_ratio_60s),
            'minimum_buy_ratio': str(policy.get('normal_buy_ratio',0.52)),
            'cvd_quote_60s': None if snapshot.cvd_quote_60s is None else str(snapshot.cvd_quote_60s),
            'cvd_requirement': '>0',
        }

    @staticmethod
    def _sequence_rejection_evidence(snapshot: MarketSnapshot) -> dict[str,Any]:
        return {
            'code': 'SEQUENCE_BOOK_BULLISH_NOT_CONFIRMED',
            'sequence_book_status': snapshot.sequence_book_status,
            'required_status': 'fresh',
            'book_imbalance_20': None if snapshot.book_imbalance_20 is None else str(snapshot.book_imbalance_20),
            'imbalance_requirement': '0 < value <= 1',
        }

    def _capability_check(self,pair: str) -> None:
        market=self.exchange.markets.get(pair)
        if not market or market.get('active') is False or market.get('spot') is False:
            raise AdmissionRejected('MARKET_NOT_ACTIVE_SPOT')
        try:
            info=self.order_lists.symbol_info(pair)
        except Exception as exc:
            raise AdmissionRejected('TESTNET_METADATA_UNAVAILABLE') from exc
        if info.get('status') != 'TRADING':
            raise AdmissionRejected('TESTNET_MARKET_NOT_TRADING')
        for key,code in [('ocoAllowed','OCO_UNSUPPORTED'),('otoAllowed','OTO_UNSUPPORTED'),('allowTrailingStop','TRAILING_UNSUPPORTED')]:
            if info.get(key) is not True:
                raise AdmissionRejected(code)

    @staticmethod
    def _profile(snapshot: MarketSnapshot, policy: dict) -> str:
        if snapshot.state=='ACCEPTABLE': return 'DEFENSIVE'
        if snapshot.state!='GOOD': raise AdmissionRejected(snapshot.reason)
        if snapshot.flow_status!='fresh' or snapshot.taker_buy_ratio_60s is None:
            return 'DEFENSIVE'
        ratio=Decimal(snapshot.taker_buy_ratio_60s); cvd=Decimal(snapshot.cvd_quote_60s or '0')
        strong=Decimal(str(policy.get('strong_buy_ratio',0.62)))
        normal=Decimal(str(policy.get('normal_buy_ratio',0.52)))
        if ratio>=strong and cvd>0: return 'CONTINUATION'
        if ratio>=normal: return 'NORMAL'
        return 'DEFENSIVE'

    def prepare_candidate(self, *, pair: str, enter_tag: str | None, candle_date, side: str='long', candles=None) -> PreparedAdmission:
        from .durable_journal import append_event
        event={'scope':self.scope_id,'pair':pair,'entry_tag':enter_tag,
               'candle_time':str(candle_date),'source':'freqtrade_admission'}
        journal=Path(self.config['binana']['state_db_path']).parent/'runtime/freqtrade_lifecycle_events.jsonl'
        try:
            prepared=self._prepare_candidate(pair=pair,enter_tag=enter_tag,candle_date=candle_date,side=side,candles=candles)
        except Exception as exc:
            event.update(type='ADMISSION_REJECTED',reason=str(exc).split(':')[0][:160],error_type=type(exc).__name__)
            append_event(journal,event)
            raise
        event.update(type='ADMISSION_ACCEPTED',reason='ACCEPTED',intent_id=prepared.intent_id,
                     profile=prepared.profile,market=prepared.market.to_dict(),registry_sha256=prepared.registry_sha256,
                     nominal_usdt=str(prepared.nominal_usdt))
        append_event(journal,event)
        return prepared

    def _prepare_candidate(self, *, pair: str, enter_tag: str | None, candle_date, side: str='long', candles=None) -> PreparedAdmission:
        if not enter_tag: raise AdmissionRejected('MISSING_ENTRY_TAG')
        assert_spot_pair(pair,side=side,leverage=1)
        if self.store.has_blockers():
            raise AdmissionRejected('RECONCILIATION_BLOCKER_OPEN')
        intent_id=self._intent_id(pair,enter_tag,candle_date)
        requested_nominal=self._requested_nominal_usdt()
        existing=self.store.get_intent(intent_id)
        if existing is not None:
            if not bool(existing['halal_allowed']): raise AdmissionRejected('HALAL_NOT_ALLOWED')
            state=str(existing.get('state') or '')
            if state.startswith('REJECTED'):
                try:
                    replay=json.loads(existing.get('admission_json') or '{}')
                    code=str(replay.get('code') or '')
                    reason=code if code and code!='HALAL_ALLOWED' else state
                except Exception:
                    reason=state
                raise AdmissionRejected(reason)
            if state in self.TERMINAL_INTENT_STATES:
                raise AdmissionRejected('SIGNAL_ALREADY_TERMINAL')
            # A repeated signal must not overwrite an existing trade or order lifecycle.
            if existing.get('freqtrade_trade_id') is not None or self.store.latest_generation(intent_id) is not None:
                raise AdmissionRejected('SIGNAL_ALREADY_OWNED')
            nominal=Decimal(str(existing['nominal_usdt']))
            if not nominal.is_finite() or nominal <= 0:
                raise AdmissionRejected('CANONICAL_NOMINAL_INVALID')
        else:
            # Exactly one authoritative membership decision for this new intent.
            try:
                decision=self.registry.decide(intent_id=intent_id,pair=pair)
            except RegistryError as exc:
                raise AdmissionRejected('OWNER_REGISTRY_UNAVAILABLE_OR_INVALID') from exc
            if not decision.allowed:
                self.store.put_intent(intent_id=intent_id,pair=pair,signal_id=enter_tag,halal_allowed=False,
                    registry_sha256=decision.registry_sha256,nominal_usdt=str(requested_nominal),state='REJECTED_HALAL',
                    admission={'code':'HALAL_NOT_ALLOWED'})
                raise AdmissionRejected('HALAL_NOT_ALLOWED')
            max_slots=int(self.config.get('max_open_trades',4))
            if self.store.occupied_count()>=max_slots:
                self.store.put_intent(intent_id=intent_id,pair=pair,signal_id=enter_tag,halal_allowed=True,
                    registry_sha256=decision.registry_sha256,nominal_usdt=str(requested_nominal),state='REJECTED_MAX_SLOTS',
                    admission={'code':'MAX_SLOTS_REACHED','max_slots':max_slots})
                raise AdmissionRejected('MAX_SLOTS_REACHED')
            allocation=Decimal(str(self.config['binana'].get('allocation_usdt',0)))
            if not allocation.is_finite() or allocation <= 0:
                raise AdmissionRejected('ALLOCATION_CONFIG_INVALID')
            used=self.store.occupied_nominal_usdt()
            if requested_nominal > allocation or used + requested_nominal > allocation:
                self.store.put_intent(intent_id=intent_id,pair=pair,signal_id=enter_tag,halal_allowed=True,
                    registry_sha256=decision.registry_sha256,nominal_usdt=str(requested_nominal),state='REJECTED_ALLOCATION',
                    admission={'code':'ALLOCATION_LIMIT_EXCEEDED','allocation_usdt':str(allocation),
                               'occupied_nominal_usdt':str(used),'requested_nominal_usdt':str(requested_nominal)})
                raise AdmissionRejected('ALLOCATION_LIMIT_EXCEEDED')
            nominal=requested_nominal
            registry_sha=decision.registry_sha256
            # Persist the one authoritative halal decision immediately. Any later
            # failure or process restart must reuse this durable decision instead
            # of consulting the owner registry a second time for the same intent.
            self.store.put_intent(intent_id=intent_id,pair=pair,signal_id=enter_tag,halal_allowed=True,
                registry_sha256=registry_sha,nominal_usdt=str(nominal),state='HALAL_DECIDED',
                admission={'code':'HALAL_ALLOWED','nominal_usdt':str(nominal)})
            existing=self.store.get_intent(intent_id)
        try:
            self._capability_check(pair)
        except AdmissionRejected as exc:
            self.store.set_intent_state(intent_id,'REJECTED_CAPABILITY',admission={'code':str(exc).split(':')[0]})
            raise
        snapshot=self.market_data.refresh(pair, quote_budget=nominal)  # bind liquidity evidence to admitted notional
        liquidity_policy=self.config['binana'].get('liquidity',{})
        flow_policy=self.config['binana'].get('flow_policy',{})
        if snapshot.reason == 'PAIR_NOT_READY':
            self.store.set_intent_state(
                intent_id,'PAIR_NOT_READY',
                admission=self._flow_rejection_evidence(snapshot,flow_policy,'PAIR_NOT_READY')
            )
            raise AdmissionRejected('PAIR_NOT_READY')
        if snapshot.state in {'DANGEROUS','UNKNOWN'}:
            self.store.set_intent_state(intent_id,'REJECTED_EXECUTION',admission=self._execution_rejection_evidence(snapshot,liquidity_policy))
            raise AdmissionRejected(snapshot.reason)
        from .quality_gate import bullish_flow_reason
        flow_reason=bullish_flow_reason(snapshot,flow_policy)
        if flow_reason:
            self.store.set_intent_state(intent_id,'REJECTED_FLOW',admission=self._flow_rejection_evidence(snapshot,flow_policy,flow_reason))
            raise AdmissionRejected(flow_reason)
        quality_policy=self.config['binana'].get('confluence_policy',{})
        if quality_policy.get('enabled') is True:
            from .confluence import score_confluence
            quality=score_confluence(candles,policy=quality_policy)
            from .durable_journal import append_event
            append_event(Path(self.config['binana']['state_db_path']).parent/'runtime/freqtrade_lifecycle_events.jsonl',
                {'scope':self.scope_id,'type':'CONFLUENCE_REVIEW','pair':pair,'intent_id':intent_id,'source':'freqtrade_admission',**quality})
            if quality['status']!='QUALIFIED':
                admission={'code':quality['reason'],
                    'score':quality.get('score'),'min_score':quality_policy.get('min_score',5),
                    'volume_votes':quality.get('volume_votes'),'minimum_volume_votes':quality_policy.get('minimum_volume_votes',2),
                    'momentum_votes':quality.get('momentum_votes'),'minimum_momentum_votes':quality_policy.get('minimum_momentum_votes',2),
                    'votes':quality.get('votes'),'values':quality.get('values')}
                self.store.set_intent_state(intent_id,'REJECTED_CONFLUENCE',admission=admission);raise AdmissionRejected(quality['reason'])
        if self.config['binana'].get('liquidity',{}).get('require_sequence_book') is True:
            try:
                imbalance=Decimal(str(snapshot.book_imbalance_20))
                book_ok=snapshot.sequence_book_status=='fresh' and imbalance.is_finite() and Decimal('0')<imbalance<=Decimal('1')
            except Exception:book_ok=False
            if not book_ok:
                self.store.set_intent_state(intent_id,'REJECTED_SEQUENCE_BOOK',admission=self._sequence_rejection_evidence(snapshot));raise AdmissionRejected('SEQUENCE_BOOK_BULLISH_NOT_CONFIRMED')
        profile=self._profile(snapshot,self.config['binana'].get('flow_policy',{}))
        registry_sha=existing['registry_sha256'] if existing is not None else registry_sha
        accepted_admission={'code':'ADMISSION_ACCEPTED','profile':profile,'market':snapshot.to_dict(),
                            'nominal_usdt':str(nominal)}
        self.store.set_intent_state(intent_id,'RESERVED',admission=accepted_admission)
        prepared=PreparedAdmission(intent_id,pair,enter_tag,profile,snapshot,registry_sha,nominal)
        self.prepared[pair]=prepared
        return prepared

    def _tick_size(self,pair: str) -> Decimal:
        info=self.order_lists.symbol_info(pair)
        for f in info.get('filters',[]) or []:
            if f.get('filterType')=='PRICE_FILTER' and f.get('tickSize'):
                return Decimal(str(f['tickSize']))
        raise AdmissionRejected('MISSING_PRICE_FILTER')

    def _quantity_step(self, pair: str) -> Decimal:
        for rule in self.order_lists.symbol_info(pair).get('filters',[]):
            if rule.get('filterType')=='LOT_SIZE':
                step=Decimal(str(rule.get('stepSize')))
                if step.is_finite() and step>0:
                    return step
        raise AdmissionRejected('MISSING_QUANTITY_FILTER')

    def entry_parameters(self, pair: str, stake_amount: float) -> tuple[float,float,float]:
        try:
            snapshot=self.market_data.refresh(pair,quote_budget=Decimal(str(stake_amount)))
            if snapshot.state not in {'GOOD','ACCEPTABLE'} or snapshot.flow_status!='fresh':
                raise AdmissionRejected('TESTNET_EXECUTION_QUOTE_UNAVAILABLE')
            tick=self._tick_size(pair)
            step=self._quantity_step(pair)
            price=(Decimal(snapshot.best_ask)/tick).to_integral_value(rounding=ROUND_UP)*tick
            if not price.is_finite() or price<=0:
                raise AdmissionRejected('TESTNET_EXECUTION_QUOTE_INVALID')
            return float(price),float(step),float(tick)
        except AdmissionRejected:
            raise
        except Exception as exc:
            raise AdmissionRejected('TESTNET_EXECUTION_QUOTE_UNAVAILABLE') from exc

    def entry_commission_reserve(self, pair: str) -> Decimal:
        """Reserve the undiscounted BUY/taker fee from authenticated symbol rates."""
        try:
            rates=self.exchange._api.privateGetAccountCommission({'symbol':pair.replace('/','')})
            if rates.get('symbol') != pair.replace('/',''):
                raise ValueError('commission symbol mismatch')
            reserve=Decimal(0)
            for kind in ('standardCommission','specialCommission','taxCommission'):
                for role in ('taker','buyer'):
                    value=Decimal(str(rates[kind][role]))
                    if not value.is_finite() or value<0:
                        raise ValueError('invalid commission rate')
                    reserve+=value
            if reserve>=1:
                raise ValueError('commission exceeds received amount')
            return reserve
        except Exception as exc:
            raise AdmissionRejected('ACCOUNT_COMMISSION_UNAVAILABLE') from exc

    def _trailing_bounds(self,pair: str) -> tuple[int,int]:
        info=self.order_lists.symbol_info(pair)
        for f in info.get('filters',[]) or []:
            if f.get('filterType')=='TRAILING_DELTA':
                return int(f['minTrailingBelowDelta']),int(f['maxTrailingBelowDelta'])
        raise AdmissionRejected('MISSING_TRAILING_DELTA_FILTER')

    def abort_prepared(self, *, pair: str, reason: str) -> bool:
        """Release a pre-submit reservation only when exchange mutation is provably absent."""
        prepared=self.prepared.get(pair)
        if prepared is None:
            return False
        intent=self.store.get_intent(prepared.intent_id)
        if intent is None or str(intent.get('state') or '') not in {'HALAL_DECIDED','RESERVED'}:
            return False
        if self.store.has_generation(prepared.intent_id):
            return False
        self.store.set_intent_state(
            prepared.intent_id,'CLOSED_NO_FILL',
            admission={'code':'PRE_SUBMIT_ABORT','reason':str(reason)[:160],
                       'nominal_usdt':str(prepared.nominal_usdt)},
        )
        self.prepared.pop(pair,None)
        return True

    def entry_no_fill(self, *, pair: str) -> None:
        prepared=self.prepared.get(pair)
        if prepared is None:
            return
        self.store.set_intent_state(prepared.intent_id,'CLOSED_NO_FILL')
        gen=self.store.latest_generation(prepared.intent_id)
        if gen and str(gen.get('status') or '').upper() in {'SUBMISSION_PENDING','ENTRY_PENDING'}:
            self.store.update_generation_status(prepared.intent_id,int(gen['generation']),'CLOSED_NO_FILL')
        self.prepared.pop(pair,None)

    def submit_entry(self, *, pair: str, amount: float, rate: float, enter_tag: str) -> dict[str,Any]:
        prepared=self.prepared.get(pair)
        if prepared is None or prepared.enter_tag!=enter_tag:
            raise AdmissionRejected('NO_PREPARED_ADMISSION')
        assert_spot_pair(pair,side='long',leverage=1)
        cfg=self.config['binana']
        try:
            step=self._quantity_step(pair);tick=self._tick_size(pair)
            q=(Decimal(str(amount))/step).to_integral_value(rounding=ROUND_DOWN)*step
            px=(Decimal(str(rate))/tick).to_integral_value(rounding=ROUND_UP)*tick
        except Exception as exc:
            raise AdmissionRejected('TESTNET_EXECUTION_FILTERS_UNAVAILABLE') from exc
        if q <= 0 or px <= 0:
            self.abort_prepared(pair=pair,reason='INVALID_ORDER_NOTIONAL')
            raise AdmissionRejected('INVALID_ORDER_NOTIONAL')
        actual_notional=q*px
        if actual_notional > prepared.nominal_usdt + Decimal('0.01'):
            self.abort_prepared(pair=pair,reason='ORDER_NOTIONAL_EXCEEDS_ADMITTED')
            raise AdmissionRejected('ORDER_NOTIONAL_EXCEEDS_ADMITTED')
        fee_buffer=self.entry_commission_reserve(pair)
        protected=(q*(Decimal('1')-fee_buffer)/step).to_integral_value(rounding=ROUND_DOWN)*step
        stop_fraction=Decimal(str(cfg['defensive_stop_fraction'] if prepared.profile=='DEFENSIVE' else cfg['normal_stop_fraction']))
        plan=provisional_plan(intent_id=prepared.intent_id,pair=pair,quantity=q,entry_limit=px,
            tick_size=self._tick_size(pair),target_fraction=cfg['fixed_target_fraction'],stop_fraction=stop_fraction,
            stop_limit_buffer_fraction=cfg['stop_limit_buffer_fraction'],estimated_exit_fee_fraction=cfg.get('estimated_exit_fee_fraction','0'))
        ids={'list_client_id':_client('L',plan.intent_id,plan.generation,'list'),
             'working_client_id':_client('W',plan.intent_id,plan.generation,'working'),
             'tp_client_id':_client('T',plan.intent_id,plan.generation,'takeprofit'),
             'sl_client_id':_client('S',plan.intent_id,plan.generation,'stop')}
        entry_evidence={'profile':prepared.profile,'plan':json.loads(json.dumps(plan.__dict__,default=str)),
            'admitted_nominal_usdt':str(prepared.nominal_usdt),'actual_order_notional_usdt':str(actual_notional),
            'authenticated_base_fee_reserve':str(fee_buffer)}
        self.store.put_generation(intent_id=plan.intent_id,generation=1,pair=pair,mode='FIXED_OCO',status='SUBMISSION_PENDING',
                                  ids=ids,expected_qty=str(protected),payload=entry_evidence)
        self.store.set_intent_state(plan.intent_id,'ENTRY_PENDING')
        try:
            response,list_ids=self.order_lists.submit_fixed_otoco(plan,pending_quantity=protected)
        except OrderListUnknown as exc:
            # Deterministic list ID was persisted before mutation. Recover it before any retry.
            try:
                response=self.order_lists.query_list(list_client_id=ids['list_client_id'])
                list_ids=self.order_lists._extract_ids(response,list_client_id=ids['list_client_id'],
                    working_client_id=ids['working_client_id'],tp_client_id=ids['tp_client_id'],sl_client_id=ids['sl_client_id'])
            except Exception as reconcile_exc:
                self.store.set_intent_state(plan.intent_id,'UNKNOWN')
                self.store.incident(incident_id='submit-'+plan.intent_id,intent_id=plan.intent_id,pair=pair,
                    code='ENTRY_SUBMISSION_UNKNOWN',detail=f'{exc}; reconcile={type(reconcile_exc).__name__}:{reconcile_exc}')
                raise AdmissionRejected('ENTRY_SUBMISSION_UNKNOWN') from exc
        except OrderListError as exc:
            # Preflight happens before the transport POST; no exchange mutation.
            self.entry_no_fill(pair=pair)
            raise AdmissionRejected('ENTRY_PREFLIGHT_REJECTED') from exc
        self.store.put_generation(intent_id=plan.intent_id,generation=1,pair=pair,mode='FIXED_OCO',status='ENTRY_PENDING',
                                  ids=list_ids.as_dict(),expected_qty=str(protected),payload=entry_evidence)
        for role,oid,cid in (('WORKING',list_ids.working_order_id,list_ids.working_client_id),
                             ('TAKE_PROFIT',list_ids.tp_order_id,list_ids.tp_client_id),
                             ('STOP',list_ids.sl_order_id,list_ids.sl_client_id)):
            if cid:
                self.store.bind_order_identity(scope_id=self.scope_id,intent_id=plan.intent_id,generation=1,pair=pair,
                                               role=role,client_id=cid,order_id=oid)
        return self._working_ccxt_order(response,ids['working_client_id'],pair,q,px)

    @staticmethod
    def _working_ccxt_order(response: dict[str,Any], client_id: str, pair: str, amount: Decimal, price: Decimal) -> dict[str,Any]:
        rows=(response.get('orderReports') or [])
        row=next((r for r in rows if r.get('clientOrderId')==client_id),None)
        if row is None:
            order=next((r for r in response.get('orders',[]) if r.get('clientOrderId')==client_id),{})
            row=order
        status_map={'NEW':'open','PARTIALLY_FILLED':'open','FILLED':'closed','CANCELED':'canceled','CANCELLED':'canceled','REJECTED':'rejected','EXPIRED':'expired','PENDING_NEW':'open'}
        filled=Decimal(str(row.get('executedQty','0'))); cost=Decimal(str(row.get('cummulativeQuoteQty','0')))
        avg=(cost/filled) if filled>0 and cost>0 else None
        oid=row.get('orderId')
        if oid is None: raise AdmissionRejected('OTOCO_RESPONSE_MISSING_WORKING_ORDER')
        return {'id':str(oid),'clientOrderId':client_id,'symbol':pair,'status':status_map.get(str(row.get('status','NEW')).upper(),'open'),
                'side':'buy','type':'limit','price':float(Decimal(str(row.get('price',price)))),'amount':float(Decimal(str(row.get('origQty',amount)))),
                'filled':float(filled),'remaining':float(max(Decimal('0'),amount-filled)),'cost':float(cost),'average':float(avg) if avg is not None else None,
                'timestamp':row.get('transactTime') or response.get('transactionTime'),'info':row}

    def _record_order_trades(self, *, intent_id: str, pair: str, order_id: str, side: str) -> Decimal:
        """Persist immutable exchange fills for one owned order and return total base quantity."""
        try:
            # Binance accepts orderId by itself for /api/v3/myTrades. Do not combine
            # it with Freqtrade's since/startTime wrapper, which Testnet rejects.
            trades=self.exchange._api.fetch_my_trades(pair, params={'orderId':int(order_id)})
            if hasattr(self.exchange, '_trades_contracts_to_amount'):
                trades=self.exchange._trades_contracts_to_amount(trades)
        except Exception as exc:
            raise AdmissionRejected(f'FILL_RECONCILIATION_FAILED:{type(exc).__name__}') from exc
        total=Decimal('0')
        self._verified_order_fills=[]
        seen={}
        for row in trades or []:
            row_order = row.get('order') if row.get('order') is not None else row.get('orderId')
            if str(row_order) != str(order_id):
                continue
            identity='' if row.get('id') is None else str(row['id'])
            if not identity:
                raise AdmissionRejected('FILL_EXECUTION_ID_MISSING')
            if row.get('symbol') != pair or str(row.get('side')).upper() != side.upper():
                raise AdmissionRejected('FILL_PAIR_OR_SIDE_MISMATCH')
            amount=Decimal(str(row.get('amount')))
            quote=Decimal(str(row.get('cost')))
            fee=row.get('fee') if isinstance(row.get('fee'),dict) else {}
            fee_amount=Decimal(str(fee.get('cost')))
            if (not all(n.is_finite() for n in (amount,quote,fee_amount))
                    or amount<=0 or quote<=0 or fee_amount<0 or not fee.get('currency')):
                raise AdmissionRejected('INVALID_FILL_ECONOMICS')
            signature=(amount,quote,fee.get('currency'),fee_amount,row.get('timestamp'))
            if identity in seen:
                if seen[identity] != signature:
                    raise AdmissionRejected('CONFLICTING_EXECUTION_REPLAY')
                continue
            seen[identity]=signature
            self._verified_order_fills.append(row)
            total += amount
            price=row.get('price') or ((quote/amount) if quote>0 else None)
            trade_id=identity
            fill_key=sha256(f"{self.scope_id}|{pair}|{order_id}|{identity}".encode()).hexdigest()
            legacy_keys=[sha256(f"{scope}|{pair}|{order_id}|{identity}".encode()).hexdigest() for scope in self.legacy_scope_ids]
            self.store.record_fill(fill_key=fill_key,legacy_fill_keys=legacy_keys,scope_id=self.scope_id,intent_id=intent_id,pair=pair,
                order_id=str(order_id),side=side.upper(),base_qty=str(amount),quote_qty=str(quote),
                average_price=str(price) if price is not None else None,fee_asset=fee.get('currency'),
                fee_amount=str(fee.get('cost')) if fee.get('cost') is not None else None,
                occurred_ts=(float(row.get('timestamp'))/1000 if row.get('timestamp') else None),raw=row,
                exchange_trade_id=trade_id,source_kind='exchange_trade')
        return total

    @staticmethod
    def _freqtrade_exit_already_applied(trade, order_id: str | None, filled: Decimal | None = None) -> bool:
        if not order_id:
            return False
        for existing in getattr(trade, 'orders', []) or []:
            if str(getattr(existing, 'order_id', '') or '') != str(order_id):
                continue
            existing_filled=Decimal(str(getattr(existing, 'filled', 0) or 0))
            if existing_filled <= 0:
                return False
            if filled is not None and existing_filled < filled:
                return False
            if getattr(existing, 'ft_is_open', False):
                return False
            return True
        return False

    def maybe_promote(self, trade, intent_id: str, gen: dict[str,Any]) -> dict[str,Any] | None:
        """One-way FIXED_OCO -> TRAILING_OCO promotion with durable mutation intent."""
        import time as _time
        if gen.get('mode')!='FIXED_OCO' or gen.get('status')!='FIXED_ACTIVE' or int(gen.get('generation') or 0)!=1:
            return None
        tid=int(trade.id)
        now=_time.time(); cooldown=float(self.config['binana'].get('promotion_check_seconds',10))
        if now-self._promotion_last.get(tid,0)<cooldown: return None
        self._promotion_last[tid]=now
        snap=self.market_data.refresh(trade.pair, quote_budget=Decimal(str(getattr(trade,'stake_amount',None) or self.config.get('stake_amount',250))))
        if snap.state!='GOOD' or snap.flow_status!='fresh' or snap.taker_buy_ratio_60s is None: return None
        policy=self.config['binana'].get('flow_policy',{})
        if Decimal(snap.taker_buy_ratio_60s)<Decimal(str(policy.get('promote_buy_ratio',0.58))): return None
        if Decimal(snap.cvd_quote_60s or '0')<=0: return None
        market=Decimal(snap.best_bid or '0'); entry=Decimal(str(trade.open_rate or 0))
        if market<=0 or entry<=0: return None
        profit=(market-entry)/entry
        if profit<Decimal(str(policy.get('promotion_min_profit_fraction',0.004))): return None
        cfg=self.config['binana']; tick=self._tick_size(trade.pair)
        stop_fraction=Decimal(str(cfg['normal_stop_fraction']))
        protected_qty=Decimal(str(gen.get('expected_qty') or trade.amount))
        if protected_qty<=0 or protected_qty>Decimal(str(trade.amount)):
            self.store.incident(incident_id='promotion-qty-'+intent_id,intent_id=intent_id,pair=trade.pair,
                code='PROMOTION_QUANTITY_INVALID',detail=f'protected={protected_qty} trade_amount={trade.amount}')
            return None
        fixed=provisional_plan(intent_id=intent_id,pair=trade.pair,quantity=str(protected_qty),entry_limit=str(entry),
            tick_size=tick,target_fraction=cfg['fixed_target_fraction'],stop_fraction=stop_fraction,
            stop_limit_buffer_fraction=cfg['stop_limit_buffer_fraction'],estimated_exit_fee_fraction=cfg.get('estimated_exit_fee_fraction','0'))
        min_b,max_b=self._trailing_bounds(trade.pair); delta=int(cfg['trailing_delta_bips'])
        from .protection_plan import trailing_plan
        try: newplan=trailing_plan(fixed,market_price=market,trailing_delta_bips=delta,min_bips=min_b,max_bips=max_b,generation=2)
        except Exception: return None
        ids={'list_client_id':_client('L',intent_id,2,'list'),'tp_client_id':_client('T',intent_id,2,'takeprofit'),'sl_client_id':_client('S',intent_id,2,'stop')}
        self.store.put_generation(intent_id=intent_id,generation=2,pair=trade.pair,mode='TRAILING_OCO',status='PROMOTION_INTENT',ids=ids,
            expected_qty=str(protected_qty),payload={'market':snap.to_dict(),'from_generation':1,'plan':json.loads(json.dumps(newplan.__dict__,default=str))})
        self.store.update_generation_status(intent_id,1,'REPLACING')
        try:
            cancel_response=self.order_lists.cancel_list(symbol=trade.pair,order_list_id=gen.get('order_list_id'),list_client_id=gen.get('list_client_id'))
        except OrderListUnknown as exc:
            self.store.update_generation_status(intent_id,2,'CANCEL_UNKNOWN')
            self.store.set_intent_state(intent_id,'UNKNOWN')
            self.store.incident(incident_id='promote-cancel-'+intent_id,intent_id=intent_id,pair=trade.pair,
                                code='PROMOTION_CANCEL_UNKNOWN',detail=str(exc))
            return None
        # Cancellation can race with an exchange fill. Reconcile immutable trades,
        # then replace protection only for the unsold portion of the old protected quantity.
        # Verify both old children after cancellation, including fills absent from
        # the cancellation response. No new SELL may overlap an unresolved child.
        reports=[]
        try:
            for key in ('tp','sl'):
                child=self._owned_child(trade.pair,gen,key)
                if child['status'] not in {'closed','canceled','cancelled','expired','rejected'}:
                    raise AdmissionRejected('OLD_CHILD_STILL_LIVE')
                reports.append({'orderId':child['id'],'clientOrderId':child['clientOrderId'],
                    'executedQty':str(child.get('filled') or 0),'status':'FILLED' if child['status']=='closed' else child['status'].upper()})
        except Exception as exc:
            self.store.update_generation_status(intent_id,2,'CANCEL_UNKNOWN')
            self._recovery_block(intent_id,trade.pair,'CANCEL_CHILD_VERIFICATION_FAILED:'+type(exc).__name__)
            return None
        old_sold=Decimal('0')
        old_base_commission=Decimal('0')
        saw_old_fill=False
        for child in reports:
            executed=Decimal(str(child.get('executedQty') or '0'))
            if executed<=0:
                continue
            saw_old_fill=True
            cid=str(child.get('clientOrderId') or '')
            oid=str(child.get('orderId') or '')
            side_label='TAKE_PROFIT' if cid==str(gen.get('tp_client_id') or '') else 'STOP'
            try:
                reconciled=self._record_order_trades(intent_id=intent_id,pair=trade.pair,order_id=oid,side='SELL') if oid else Decimal('0')
            except Exception:
                reconciled=Decimal('0')
            if reconciled!=executed:
                self.store.update_generation_status(intent_id,2,'FILL_RECONCILIATION_UNKNOWN')
                self.store.set_intent_state(intent_id,'UNKNOWN')
                self.store.incident(incident_id='promotion-old-fill-'+intent_id,intent_id=intent_id,pair=trade.pair,
                    code='PROMOTION_OLD_CHILD_FILL_UNVERIFIED',detail=json.dumps(child,default=str)[:4000])
                return None
            old_sold += reconciled
            old_base_commission += sum((Decimal(str((row.get('fee') or {}).get('cost') or 0))
                for row in self._verified_order_fills
                if (row.get('fee') or {}).get('currency') == trade.pair.split('/')[0]), Decimal(0))
            self.store.update_generation_status(intent_id,1,'TERMINAL' if str(child.get('status','')).upper()=='FILLED' else 'PARTIAL_EXIT',
                payload={'filled_child':side_label,'order_id':oid,'filled_qty':str(reconciled)})
        if saw_old_fill:
            step = self._quantity_step(trade.pair)
            residual = (max(Decimal('0'), protected_qty-old_sold-old_base_commission) / step).to_integral_value(rounding=ROUND_DOWN) * step
            if residual<=0:
                self.store.update_generation_status(intent_id,2,'NOT_NEEDED_POSITION_EXITED')
                self._recovery_block(intent_id, trade.pair, 'PROMOTION_EXIT_CANONICAL_IMPORT_PENDING')
                return None
            fixed=provisional_plan(intent_id=intent_id,pair=trade.pair,quantity=residual,entry_limit=str(entry),
                tick_size=tick,target_fraction=cfg['fixed_target_fraction'],stop_fraction=stop_fraction,
                stop_limit_buffer_fraction=cfg['stop_limit_buffer_fraction'],estimated_exit_fee_fraction=cfg.get('estimated_exit_fee_fraction','0'))
            try:
                newplan=trailing_plan(fixed,market_price=market,trailing_delta_bips=delta,min_bips=min_b,max_bips=max_b,generation=2)
            except Exception:
                self.store.update_generation_status(intent_id,2,'RESIDUAL_PLAN_INVALID')
                self.store.set_intent_state(intent_id,'UNKNOWN')
                self.store.incident(incident_id='promotion-old-fill-'+intent_id,intent_id=intent_id,pair=trade.pair,
                    code='PROMOTION_RESIDUAL_PLAN_INVALID',detail=f'residual={residual}')
                return None
            self.store.put_generation(intent_id=intent_id,generation=2,pair=trade.pair,mode='TRAILING_OCO',status='REPLACEMENT_PENDING',
                ids=ids,expected_qty=str(residual),payload={'market':snap.to_dict(),'from_generation':1,'residual_after_old_fill':str(residual)})
        else:
            self.store.update_generation_status(intent_id,2,'REPLACEMENT_PENDING')
        try:
            response,newids=self.order_lists.submit_oco(newplan)
        except OrderListUnknown as exc:
            try:
                response=self.order_lists.query_list(list_client_id=ids['list_client_id'])
                newids=self.order_lists._extract_ids(response,list_client_id=ids['list_client_id'],working_client_id=None,
                                                       tp_client_id=ids['tp_client_id'],sl_client_id=ids['sl_client_id'])
            except Exception as rec:
                self.store.update_generation_status(intent_id,2,'SUBMISSION_UNKNOWN')
                self.store.set_intent_state(intent_id,'UNKNOWN')
                self.store.incident(incident_id='promote-submit-'+intent_id,intent_id=intent_id,pair=trade.pair,
                    code='TRAILING_SUBMISSION_UNKNOWN',detail=f'{exc}; reconcile={type(rec).__name__}:{rec}')
                return None
        final_qty=str(newplan.quantity)
        self.store.put_generation(intent_id=intent_id,generation=2,pair=trade.pair,mode='TRAILING_OCO',status='SUBMITTED_UNVERIFIED',
                                  ids=newids.as_dict(),expected_qty=final_qty,payload={'market':snap.to_dict(),'from_generation':1})
        self._recovery_block(intent_id,trade.pair,'PROMOTION_CHILD_VERIFICATION_PENDING')
        for role,oid,cid in (('TAKE_PROFIT',newids.tp_order_id,newids.tp_client_id),('STOP',newids.sl_order_id,newids.sl_client_id)):
            if cid:
                self.store.bind_order_identity(scope_id=self.scope_id,intent_id=intent_id,generation=2,pair=trade.pair,
                                               role=role,client_id=cid,order_id=oid)
        self.store.update_generation_status(intent_id,1,'REPLACED')
        return None

    def bind_trade(self, *, pair: str, trade_id: int) -> None:
        prepared=self.prepared.get(pair)
        if prepared:
            self.store.set_trade_id(prepared.intent_id,trade_id)
            self._trade_intent[int(trade_id)]=prepared.intent_id
            self.prepared.pop(pair,None)

    def intent_for_trade(self, trade_id: int) -> str | None:
        if int(trade_id) in self._trade_intent: return self._trade_intent[int(trade_id)]
        # Restore mapping after restart from durable state.
        with self.store._connect() as c:
            row=c.execute('SELECT intent_id FROM intents WHERE freqtrade_trade_id=?',(int(trade_id),)).fetchone()
            if row:
                self._trade_intent[int(trade_id)]=row[0]; return row[0]
        return None
