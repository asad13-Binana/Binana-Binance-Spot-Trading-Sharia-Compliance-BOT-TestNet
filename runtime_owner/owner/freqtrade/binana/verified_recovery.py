"""Authenticated Freqtrade-owner reconciliation. Mutations are durably recorded first."""
from dataclasses import replace
from decimal import Decimal
import json
from time import time
from .recovery_contract import RecoveryBlocked, TERMINAL, dec, verify_child, live_protection, residual_quantity, executable_quantity, recovery_protection_quantity, recovery_boundary_action, classify_executability, protectable_quantity, order_history_state
from .binance_order_lists import _client
from .protection_plan import provisional_plan, trailing_plan
from .operator_exit import OPERATOR_PRE_SUBMISSION
from .submission_absence import verify_absent_replacement, explicit_absence

RESOLVED_PREFIXES = ("alldone-", "partial-exit-", "promotion-qty-", "promotion-old-fill-", "promote-cancel-", "promote-submit-", "recovery-", "partial-")

class VerifiedRecovery:
    def _recovery_block(self, iid, pair, reason):
        self.store.set_intent_state(iid, "UNKNOWN")
        self.store.incident(incident_id="recovery-"+iid, intent_id=iid, pair=pair,
                            code="PROTECTION_RECONCILIATION_BLOCKED", detail=str(reason)[:1000])

    def _recovery_clear(self, iid):
        for prefix in RESOLVED_PREFIXES:
            self.store.close_incident(prefix+iid)

    def _owned_child(self, pair, gen, key, side="sell"):
        oid, cid = gen.get(key+"_order_id"), gen.get(key+"_client_id")
        if not oid or not cid:
            raise RecoveryBlocked("OWNED_ORDER_IDENTITY_MISSING")
        try:
            order = self.exchange.fetch_order(str(oid), pair)
        except Exception as exc:
            policy=self.config.get("binana",{})
            state=order_history_state(exc,testnet=str(policy.get("environment","")).lower()=="testnet")
            if state:
                raise RecoveryBlocked(state) from exc
            raise
        return verify_child(order, order_id=oid, client_id=cid, pair=pair, side=side)

    def reconcile_trade(self, trade):
        if trade.id is None:
            return None
        iid = self.intent_for_trade(int(trade.id))
        if not iid:
            raise RecoveryBlocked("CANONICAL_TRADE_HAS_NO_INTENT")
        try:
            return self._reconcile_verified(trade, iid)
        except Exception as exc:
            # A failed read must never leave an apparently verified ACTIVE label.
            latest = self.store.latest_generation(iid)
            explicit = str(exc) if isinstance(exc,RecoveryBlocked) else ""
            if latest and explicit == "ORDER_HISTORY_UNAVAILABLE":
                self.store.update_generation_status(iid, int(latest["generation"]), "ORDER_HISTORY_UNAVAILABLE")
            elif latest and latest["status"] in {"TRAILING_ACTIVE","FIXED_ACTIVE"}:
                self.store.update_generation_status(iid, int(latest["generation"]), "VERIFICATION_UNKNOWN")
            self._recovery_block(iid, trade.pair, type(exc).__name__+(":"+explicit if explicit else ""))
            return None

    def _reconcile_verified(self, trade, iid):
        pair = trade.pair
        with self.store._connect() as conn:
            generations = [dict(r) for r in conn.execute(
                "SELECT * FROM protection WHERE intent_id=? ORDER BY generation", (iid,))]
        if not generations:
            raise RecoveryBlocked("PROTECTION_GENERATION_MISSING")
        latest = generations[-1]
        operator_request = latest if latest['mode'] == 'OPERATOR_EXIT' and latest['status'] in OPERATOR_PRE_SUBMISSION else None
        histories = []
        all_owned_ids = set()
        buy_total = sell_total = base_fees = Decimal(0)
        base_asset = pair.split("/")[0]
        original = None
        pending_exit = None
        for gen in generations:
            if gen['mode'] == 'OPERATOR_EXIT' and gen['status'] in OPERATOR_PRE_SUBMISSION | {'OPERATOR_REMAINDER_DUST','DUST_PENDING','DUST_RETAINED'}:
                continue
            if not any(gen.get(k+"_order_id") for k in ("working","tp","sl")):
                if gen['status'] == 'SUBMISSION_ABSENT':
                    # Revalidate the historical IDs. A later owned generation
                    # may now protect this symbol; the aggregate ownership
                    # check below still rejects every unrelated open order.
                    verify_absent_replacement(self, gen, pair, require_empty_symbol=False)
                    continue
                # These phases are persisted before cancellation and prove no replacement POST.
                if gen["status"] in {"PROMOTION_INTENT","CANCEL_UNKNOWN","ABORTED_OLD_FILL",
                                     "FILL_RECONCILIATION_UNKNOWN","RESIDUAL_PLAN_INVALID",
                                     "NOT_NEEDED_POSITION_EXITED"}:
                    continue
                if gen["mode"] in {"TARGET_EXIT","STOP_EXIT","OPERATOR_EXIT"}:
                    key="sl" if gen["mode"]=="STOP_EXIT" else "tp"
                    cid=gen[key+"_client_id"]
                    raw=self.exchange._api.privateGetOrder({"symbol":pair.replace("/",""),"origClientOrderId":cid})
                    if gen['mode']=='OPERATOR_EXIT' and (raw.get('clientOrderId')!=cid
                            or raw.get('symbol')!=pair.replace('/','') or raw.get('side')!='SELL'):
                        raise RecoveryBlocked('OPERATOR_RECOVERED_IDENTITY_MISMATCH')
                    self.store.put_generation(intent_id=iid,generation=gen["generation"],pair=pair,
                        mode=gen["mode"],status="SUBMITTED_UNVERIFIED",
                        ids={key+"_order_id":str(raw["orderId"]),key+"_client_id":cid},
                        expected_qty=gen["expected_qty"],payload=json.loads(gen['payload_json']))
                    gen=self.store.generation(iid,gen["generation"])
                else:
                    try:
                        response = self.order_lists.query_list(list_client_id=gen["list_client_id"])
                    except Exception as exc:
                        # Replacement OCO submission can fail before Binance creates
                        # anything.  Prove absence by deterministic list + both child
                        # client IDs + current open-order snapshot before allowing a
                        # new recovery action.  Initial OTOCO entry submission remains
                        # fail-closed and is never classified this way.
                        text=str(exc or "")
                        replacement_pending=(
                            gen.get("status")=="SUBMISSION_PENDING"
                            and not gen.get("working_client_id")
                            and explicit_absence(exc, order_list=True)
                        )
                        if not replacement_pending:
                            raise
                        proof = verify_absent_replacement(self, gen, pair, require_record=False)
                        self.store.update_generation_status(
                            iid, int(gen['generation']), 'SUBMISSION_ABSENT', payload=proof)
                        continue
                    ids = self.order_lists._extract_ids(response, list_client_id=gen["list_client_id"],
                        working_client_id=gen.get("working_client_id"), tp_client_id=gen.get("tp_client_id"),
                        sl_client_id=gen.get("sl_client_id"))
                    self.store.put_generation(intent_id=iid,generation=gen["generation"],pair=pair,
                        mode=gen["mode"],status="SUBMITTED_UNVERIFIED",ids=ids.as_dict(),
                        expected_qty=gen["expected_qty"],payload={"recovered_by_client_id":True})
                    gen = self.store.generation(iid,gen["generation"])
            children = {}
            for key in ("working", "tp", "sl"):
                if key == "working" and not gen.get("working_order_id"):
                    continue
                if key == "sl" and gen["mode"] in {"TARGET_EXIT","OPERATOR_EXIT"}:
                    continue
                if key == "tp" and gen["mode"] == "STOP_EXIT":
                    continue
                order = self._owned_child(pair, gen, key, "buy" if key == "working" else "sell")
                self.store.bind_order_identity(scope_id=self.scope_id,intent_id=iid,
                    generation=int(gen['generation']),pair=pair,
                    role='CANONICAL_EXIT' if gen['mode']=='OPERATOR_EXIT' else {'working':'WORKING','tp':'TAKE_PROFIT','sl':'STOP'}[key],
                    client_id=gen[key+'_client_id'],order_id=str(order['id']))
                children[key] = order
                oid = str(order["id"])
                if oid in all_owned_ids:
                    raise RecoveryBlocked("ORDER_REUSED_ACROSS_GENERATIONS")
                all_owned_ids.add(oid)
                filled = dec(order.get("filled") or 0)
                if filled:
                    actual = self._record_order_trades(intent_id=iid,pair=pair,order_id=oid,
                                                       side="BUY" if key=="working" else "SELL")
                    if actual != filled:
                        raise RecoveryBlocked("EXECUTION_HISTORY_INCOMPLETE:"+oid)
                    # This list is de-duplicated by exchange execution identity, independently
                    # of legacy aggregate ledger rows which are kept as historical evidence.
                    fills = self._verified_order_fills
                    base_fees += sum((dec((r.get("fee") or {}).get("cost") or 0) for r in fills
                        if (r.get("fee") or {}).get("currency")==base_asset),Decimal(0))
                if key == "working":
                    buy_total += filled
                    if order["status"] not in TERMINAL:
                        raise RecoveryBlocked("ENTRY_STILL_PENDING")
                else:
                    sell_total += filled
                    if filled and not self._freqtrade_exit_already_applied(trade,oid,filled):
                        pending_exit = pending_exit or {"exit_order":order,
                            "exit_reason":"binana_operator_exit" if gen['mode']=='OPERATOR_EXIT' else ("binana_tp" if key=="tp" else "binana_protection")}
            if int(gen["generation"]) == 1:
                original = dec(children["tp"]["amount"])
                if original != dec(children["sl"]["amount"]):
                    raise RecoveryBlocked("INITIAL_PROTECTION_BASIS_MISMATCH")
            sell_keys=("tp",) if gen["mode"] in {"TARGET_EXIT","OPERATOR_EXIT"} else (("sl",) if gen["mode"]=="STOP_EXIT" else ("tp","sl"))
            if all(children.get(k,{}).get("status") in TERMINAL for k in sell_keys):
                self.store.update_generation_status(iid,int(gen["generation"]),"TERMINAL",
                    payload={"verified_at":time(),"tp_status":children.get("tp",{}).get("status"),"sl_status":children.get("sl",{}).get("status")})
            histories.append((gen,children))
        if pending_exit:
            self._recovery_block(iid,pair,"CANONICAL_EXIT_IMPORT_PENDING")
            return pending_exit
        if original is None:
            raise RecoveryBlocked("IMMUTABLE_ENTRY_PROTECTION_BASIS_UNAVAILABLE")
        if operator_request:
            self.order_lists._metadata.pop(pair.replace('/',''), None)
        filters = self.order_lists.symbol_info(pair).get("filters",[])
        lot = next((r for r in filters if r.get("filterType")=="LOT_SIZE"),None)
        if not lot:
            raise RecoveryBlocked("LOT_SIZE_UNAVAILABLE")
        step = dec(lot["stepSize"])
        canonical = dec(trade.amount or 0)
        owned = buy_total-sell_total-base_fees
        if abs(owned-canonical) > step/2:
            raise RecoveryBlocked("CANONICAL_OWNERSHIP_MISMATCH")
        with self.store.tx() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS protection_basis(intent_id TEXT PRIMARY KEY, original_qty TEXT NOT NULL, evidence_json TEXT NOT NULL, created_ts REAL NOT NULL)")
            basis = conn.execute("SELECT original_qty FROM protection_basis WHERE intent_id=?",(iid,)).fetchone()
            if basis and dec(basis[0]) != original:
                raise RecoveryBlocked("IMMUTABLE_PROTECTION_BASIS_CHANGED")
            conn.execute("INSERT OR IGNORE INTO protection_basis VALUES(?,?,?,?)",
                (iid,str(original),json.dumps({"generation":1,"source":"authenticated_owned_children"}),time()))
        open_orders = self.exchange._api.fetch_open_orders(pair)
        ticker = self.order_lists.public.publicGetTickerBookTicker({'symbol':pair.replace('/','')})
        price = dec(ticker.get("bidPrice") or 0)
        quantity = protectable_quantity(original,sell_total,canonical,price,filters)
        if any(str(o.get("id")) not in all_owned_ids for o in open_orders):
            raise RecoveryBlocked("UNRECONCILED_OPEN_ORDER_ON_SYMBOL")
        if operator_request:
            execution_class, _ = classify_executability(canonical,price,filters)
            if not open_orders and execution_class != 'EXECUTABLE':
                self.store.update_generation_status(iid,operator_request['generation'],'OPERATOR_REMAINDER_DUST')
            else:
                return self.reconcile_operator_exit(trade,iid,operator_request,histories,
                    all_owned_ids,filters,price,open_orders)
        active_histories = [(g,ch) for g,ch in histories if any(
            ch.get(k,{}).get("status")=="open" for k in ("tp","sl"))]
        if active_histories:
            if len(active_histories)!=1:
                raise RecoveryBlocked("MULTIPLE_ACTIVE_PROTECTION_GENERATIONS")
            gen,ch = active_histories[0]
            if gen["mode"] in {"TARGET_EXIT","STOP_EXIT","OPERATOR_EXIT"}:
                raise RecoveryBlocked(gen["mode"]+"_NOT_TERMINAL")
            if not live_protection(ch["tp"],ch["sl"],quantity):
                # A single surviving OCO child is incomplete protection, not a
                # quantity mismatch.  First prove both children still represent
                # exactly the verified protectable quantity.  Then durably mark
                # cancellation before mutating Binance.  The next reconciliation
                # pass re-reads both children/fills and performs the existing
                # STOP_EXIT, TARGET_EXIT, or replacement-OCO recovery path.
                q=dec(quantity)
                tp_remaining=dec(ch["tp"].get("amount",0))-dec(ch["tp"].get("filled",0))
                sl_remaining=dec(ch["sl"].get("amount",0))-dec(ch["sl"].get("filled",0))
                if tp_remaining != q or sl_remaining != q:
                    raise RecoveryBlocked("PROTECTION_CHILD_QUANTITY_MISMATCH")
                live_keys=[k for k in ("tp","sl") if ch[k].get("status")=="open"]
                terminal_keys=[k for k in ("tp","sl") if ch[k].get("status") in TERMINAL]
                if len(live_keys)!=1 or len(terminal_keys)!=1:
                    raise RecoveryBlocked("INCOMPLETE_PROTECTION_STATE_UNSAFE")
                generation=int(gen["generation"])
                self.store.update_generation_status(iid,generation,"REPAIR_CANCEL_INTENT")
                self._recovery_block(iid,pair,
                    "INCOMPLETE_PROTECTION_CANCEL_PENDING:tp="+
                    str(ch["tp"].get("status"))+",sl="+str(ch["sl"].get("status")))
                try:
                    self.order_lists.cancel_list(symbol=pair,
                        order_list_id=gen.get("order_list_id"),
                        list_client_id=gen.get("list_client_id"))
                except Exception as exc:
                    self.store.update_generation_status(iid,generation,"CANCEL_UNKNOWN")
                    self._recovery_block(iid,pair,
                        "INCOMPLETE_PROTECTION_CANCEL_UNKNOWN:"+type(exc).__name__)
                    return None
                self.store.update_generation_status(iid,generation,"REPAIR_CANCELLED")
                return None
            self.store.update_generation_status(iid,int(gen["generation"]),
                "TRAILING_ACTIVE" if gen["mode"]=="TRAILING_OCO" else "FIXED_ACTIVE",
                payload={"verified_at":time(),"verified_quantity":str(quantity)})
            self.store.set_intent_state(iid,"OPEN")
            self._recovery_clear(iid)
            if int(gen["generation"])==int(latest["generation"]) and sell_total==0:
                self.maybe_promote(trade,iid,self.store.generation(iid,int(gen["generation"])))
            return None
        if open_orders:
            raise RecoveryBlocked("OPEN_ORDER_SNAPSHOT_CONFLICT")
        # No surviving exchange obligation: determine dust from current native filters.
        execution_class, canonical_rounded = classify_executability(canonical,price,filters)
        executable = execution_class == "EXECUTABLE"
        if not executable:
            filled_dates = [order.order_filled_date for order in trade.orders
                            if order.filled and order.order_filled_date]
            evidence={"quantity":str(canonical),"cost_basis":str(trade.stake_amount),
                "classification":execution_class,"non_executable":True,
                "price":str(price),"original_protection_qty":str(original),"sold_qty":str(sell_total),
                "base_commission":str(base_fees),"verified_at":time(),"exchange_open_orders":0,
                "filters":filters,"scope":self.scope_id,
                "settled_at":max(filled_dates).isoformat() if filled_dates else None}
            self.store.update_generation_status(iid,int(latest["generation"]),"DUST_PENDING",payload=evidence)
            self.store.set_intent_state(iid,"EXIT_PENDING")
            return {"dust_retained":evidence}
        quantity = recovery_protection_quantity(original,sell_total,canonical,price,filters)
        if quantity is None:
            raise RecoveryBlocked("CANONICAL_EXECUTABILITY_CHANGED_DURING_RECOVERY")
        balance = self.exchange._api.fetch_balance()
        if dec((balance.get(base_asset) or {}).get("free") or 0) < quantity:
            raise RecoveryBlocked("INSUFFICIENT_FREE_OWNED_ASSET")
        cfg = self.config["binana"]
        profile = json.loads(generations[0].get("payload_json") or "{}").get("profile")
        stop = cfg["defensive_stop_fraction"] if profile=="DEFENSIVE" else cfg["normal_stop_fraction"]
        fixed = provisional_plan(intent_id=iid,pair=pair,quantity=quantity,entry_limit=trade.open_rate,
            tick_size=self._tick_size(pair),target_fraction=cfg["fixed_target_fraction"],stop_fraction=stop,
            stop_limit_buffer_fraction=cfg["stop_limit_buffer_fraction"],
            estimated_exit_fee_fraction=cfg.get("estimated_exit_fee_fraction","0"))
        new_generation = int(latest["generation"])+1
        boundary_action=recovery_boundary_action(price,fixed.stop_trigger,fixed.tp_price)
        if boundary_action == "STOP_EXIT":
            cid=_client("R",iid,new_generation,"stop")
            self.store.put_generation(intent_id=iid,generation=new_generation,pair=pair,
                mode="STOP_EXIT",status="SUBMISSION_PENDING",ids={"sl_client_id":cid},
                expected_qty=str(quantity),payload={"reason":"EXISTING_STOP_ALREADY_BREACHED",
                    "market_price":str(price),"stop_trigger":str(fixed.stop_trigger),
                    "immutable_basis":str(original)})
            self._recovery_block(iid,pair,"STOP_EXIT_VERIFICATION_PENDING")
            order=self.exchange._api.create_order(pair,"market","sell",float(quantity),None,
                params={"newClientOrderId":cid})
            if not order.get("id"):
                raise RecoveryBlocked("STOP_EXIT_ID_UNAVAILABLE")
            self.store.put_generation(intent_id=iid,generation=new_generation,pair=pair,
                mode="STOP_EXIT",status="SUBMITTED_UNVERIFIED",
                ids={"sl_order_id":str(order["id"]),"sl_client_id":cid},expected_qty=str(quantity))
            self.store.bind_order_identity(scope_id=self.scope_id,intent_id=iid,generation=new_generation,
                pair=pair,role="STOP",client_id=cid,order_id=str(order["id"]))
            return None
        if any(g["mode"]=="TRAILING_OCO" for g in generations):
            lo,hi=self._trailing_bounds(pair)
            plan=trailing_plan(fixed,market_price=price,trailing_delta_bips=int(cfg["trailing_delta_bips"]),
                               min_bips=lo,max_bips=hi,generation=new_generation)
        else:
            plan=replace(fixed,generation=new_generation)
        if boundary_action == "TARGET_EXIT":
            # The configured TP is already executable. Preserve its minimum price
            # through the Freqtrade owner, without raising the target or inventing a fill.
            cid=_client("R",iid,new_generation,"target")
            self.store.put_generation(intent_id=iid,generation=new_generation,pair=pair,
                mode="TARGET_EXIT",status="SUBMISSION_PENDING",
                ids={"tp_client_id":cid},expected_qty=str(quantity),
                payload={"reason":"EXISTING_TAKE_PROFIT_REACHED","limit_price":str(plan.tp_price),
                         "immutable_basis":str(original)})
            self._recovery_block(iid,pair,"TARGET_EXIT_VERIFICATION_PENDING")
            order=self.exchange._api.create_order(pair,"limit","sell",float(quantity),
                float(plan.tp_price),params={"timeInForce":"IOC","newClientOrderId":cid})
            if not order.get("id"):
                raise RecoveryBlocked("TARGET_EXIT_ID_UNAVAILABLE")
            self.store.put_generation(intent_id=iid,generation=new_generation,pair=pair,
                mode="TARGET_EXIT",status="SUBMITTED_UNVERIFIED",
                ids={"tp_order_id":str(order["id"]),"tp_client_id":cid},expected_qty=str(quantity))
            self.store.bind_order_identity(scope_id=self.scope_id,intent_id=iid,generation=new_generation,
                pair=pair,role="TAKE_PROFIT",client_id=cid,order_id=str(order["id"]))
            return None
        ids={"list_client_id":_client("L",iid,new_generation,"list"),
             "tp_client_id":_client("T",iid,new_generation,"takeprofit"),
             "sl_client_id":_client("S",iid,new_generation,"stop")}
        # SUBMISSION_PENDING is committed before POST. After any crash/timeout,
        # only a read by these exact IDs is permitted; no blind repeat POST.
        self.store.put_generation(intent_id=iid,generation=new_generation,pair=pair,mode=plan.mode,
            status="SUBMISSION_PENDING",ids=ids,expected_qty=str(quantity),
            payload={"immutable_basis":str(original),"cumulative_sell":str(sell_total),
                     "plan":json.loads(json.dumps(plan.__dict__,default=str))})
        self._recovery_block(iid,pair,"REPLACEMENT_VERIFICATION_PENDING")
        response,newids=self.order_lists.submit_oco(plan)
        self.store.put_generation(intent_id=iid,generation=new_generation,pair=pair,mode=plan.mode,
            status="SUBMITTED_UNVERIFIED",ids=newids.as_dict(),expected_qty=str(quantity))
        for role,oid,cid in (("TAKE_PROFIT",newids.tp_order_id,newids.tp_client_id),
                             ("STOP",newids.sl_order_id,newids.sl_client_id)):
            self.store.bind_order_identity(scope_id=self.scope_id,intent_id=iid,generation=new_generation,
                pair=pair,role=role,client_id=cid,order_id=oid)
        fresh = self.store.generation(iid,new_generation)
        tp,sl=self._owned_child(pair,fresh,"tp"),self._owned_child(pair,fresh,"sl")
        if not live_protection(tp,sl,quantity):
            raise RecoveryBlocked("NEW_PROTECTION_NOT_LIVE")
        self.store.update_generation_status(iid,new_generation,
            "TRAILING_ACTIVE" if plan.mode=="TRAILING_OCO" else "FIXED_ACTIVE",
            payload={"verified_at":time(),"verified_quantity":str(quantity),"immutable_basis":str(original)})
        self.store.set_intent_state(iid,"OPEN")
        self._recovery_clear(iid)
        return None

    def acknowledge_imported_exit(self, trade, order):
        """Clear the recovery blocker only after Freqtrade persisted an owned exit."""
        if getattr(trade, "is_open", True):
            return False
        iid=self.intent_for_trade(int(trade.id))
        if not iid:
            raise RecoveryBlocked("CLOSED_TRADE_HAS_NO_INTENT")
        oid=str((order or {}).get("id") or "")
        filled=dec((order or {}).get("filled") or 0)
        if not oid or filled <= 0:
            raise RecoveryBlocked("IMPORTED_EXIT_EVIDENCE_INCOMPLETE")
        with self.store._connect() as conn:
            owned=conn.execute(
                "SELECT 1 FROM protection WHERE intent_id=? AND (tp_order_id=? OR sl_order_id=? OR working_order_id=?) LIMIT 1",
                (iid,oid,oid,oid)).fetchone()
        if not owned:
            raise RecoveryBlocked("IMPORTED_EXIT_NOT_OWNED")
        if not self._freqtrade_exit_already_applied(trade,oid,filled):
            raise RecoveryBlocked("CANONICAL_EXIT_NOT_APPLIED")
        if self.exchange._api.fetch_open_orders(trade.pair):
            raise RecoveryBlocked("OPEN_ORDER_REMAINS_AFTER_CANONICAL_EXIT")
        self.store.set_intent_state(iid,"EXIT_FILLED")
        self._recovery_clear(iid)
        return True

    def acknowledge_retained_dust(self, trade, evidence):
        """Called only after Freqtrade's canonical custom-data transaction commits."""
        iid=self.intent_for_trade(int(trade.id))
        actual=trade.get_custom_data(key="binana_retained_dust")
        if not actual or actual["quantity"]!=evidence["quantity"] or actual["cost_basis"]!=evidence["cost_basis"]:
            raise RecoveryBlocked("CANONICAL_DUST_ACK_MISMATCH")
        gen=self.store.latest_generation(iid)
        self.store.update_generation_status(iid,int(gen["generation"]),"DUST_RETAINED",payload=evidence)
        self.store.set_intent_state(iid,"DUST_RETAINED")
        self._recovery_clear(iid)

        # Freqtrade retains the unsold amount and cost. Its supported query
        # projection excludes verified retained dust from executable trade slots.
        # No close rate or synthetic sale is assigned to retained inventory.
