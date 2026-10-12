"""Typed Binance Spot Testnet order-list transport owned by the Freqtrade fork."""
from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
from typing import Any
import json

from .protection_plan import ProtectionPlan


class OrderListError(RuntimeError):
    pass


class OrderListUnknown(OrderListError):
    """Exchange result is ambiguous and MUST be reconciled before retry."""


def _client(prefix: str, intent_id: str, generation: int, role: str) -> str:
    digest=sha256(f"{intent_id}|{generation}|{role}".encode()).hexdigest()[:16]
    # Binance client IDs allow a wider set; conservative alphanumeric form is portable.
    return f"B{prefix}{generation}{role[:1].upper()}{digest}"[:32]


def _symbol(pair: str) -> str:
    return pair.replace("/", "").replace(":USDT", "")


@dataclass(frozen=True)
class ListIds:
    order_list_id: str | None
    list_client_id: str
    working_order_id: str | None = None
    working_client_id: str | None = None
    tp_order_id: str | None = None
    tp_client_id: str | None = None
    sl_order_id: str | None = None
    sl_client_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class BinanceOrderLists:
    def __init__(self, ccxt_api):
        self.api=ccxt_api
        self.public = getattr(ccxt_api, 'binana_testnet_public', ccxt_api)
        self._metadata = {}

    def symbol_info(self, pair):
        from time import monotonic
        symbol = _symbol(pair)
        cached = self._metadata.get(symbol)
        if cached is not None and monotonic()-cached[0] < 60:
            return cached[1]
        rows = self.public.publicGetExchangeInfo({'symbol':symbol}).get('symbols') or []
        if len(rows) != 1 or rows[0].get('symbol') not in (None, symbol):
            raise OrderListError('Testnet exchangeInfo symbol unavailable')
        self._metadata[symbol] = (monotonic(), rows[0])
        return rows[0]

    @staticmethod
    def _decimal(value) -> Decimal:
        result=Decimal(str(value))
        if not result.is_finite():
            raise OrderListError("non-finite exchange filter value")
        return result

    @staticmethod
    def _filter(filters: list[dict[str,Any]], kind: str) -> dict[str,Any] | None:
        return next((row for row in filters if row.get("filterType")==kind),None)

    @staticmethod
    def _aligned(value: Decimal, step: Decimal) -> bool:
        return step <= 0 or value % step == 0

    def _validate_price(self, value, rule, label: str) -> None:
        if not rule:
            raise OrderListError("PRICE_FILTER unavailable")
        price=self._decimal(value); minimum=self._decimal(rule.get("minPrice",0)); maximum=self._decimal(rule.get("maxPrice",0)); tick=self._decimal(rule.get("tickSize",0))
        if minimum and price < minimum: raise OrderListError(f"PRICE_FILTER {label} below minimum")
        if maximum and price > maximum: raise OrderListError(f"PRICE_FILTER {label} above maximum")
        if tick and not self._aligned(price,tick): raise OrderListError(f"PRICE_FILTER {label} violates tickSize")

    def _validate_quantity(self, value, rule, label: str) -> None:
        if not rule:
            raise OrderListError("LOT_SIZE unavailable")
        qty=self._decimal(value); minimum=self._decimal(rule.get("minQty",0)); maximum=self._decimal(rule.get("maxQty",0)); step=self._decimal(rule.get("stepSize",0))
        if minimum and qty < minimum: raise OrderListError(f"LOT_SIZE {label} below minimum")
        if maximum and qty > maximum: raise OrderListError(f"LOT_SIZE {label} above maximum")
        if step and not self._aligned(qty,step): raise OrderListError(f"LOT_SIZE {label} violates stepSize")

    def _validate_notional(self, price, qty, rule, label: str) -> None:
        if not rule: raise OrderListError("NOTIONAL unavailable")
        value=self._decimal(price)*self._decimal(qty)
        minimum=self._decimal(rule.get("minNotional",0)); maximum=self._decimal(rule.get("maxNotional",0))
        if minimum and value < minimum: raise OrderListError(f"NOTIONAL {label} below minimum")
        if maximum and value > maximum: raise OrderListError(f"NOTIONAL {label} above maximum")

    @staticmethod
    def _count_limit(rule: dict[str,Any] | None, key: str) -> int | None:
        if not rule or rule.get(key) is None: return None
        return int(rule[key])

    @staticmethod
    def _error_code(exc):
        code = getattr(exc, 'code', None)
        if code is not None:
            return code
        # CCXT includes the structured Binance JSON in its exception string.
        # Parse the JSON object, never a substring such as '-2043' in free text.
        text = str(exc)
        start = text.find('{')
        if start >= 0:
            try:
                value, _ = json.JSONDecoder().raw_decode(text[start:])
                return value.get('code') if isinstance(value, dict) else None
            except ValueError:
                pass
        return None

    def _reference_price(self, symbol):
        method = getattr(self.public, 'publicGetReferencePrice', None)
        try:
            if callable(method):
                response = method({'symbol':symbol})
            else:
                # CCXT 4.5.76 has no generated binding. Use its existing rate-
                # limited transport and explicitly restrict this route to Testnet.
                if self.public.urls['api']['public'] != 'https://testnet.binance.vision/api/v3':
                    raise OrderListError('referencePrice transport is not Testnet')
                response = self.public.request('referencePrice', 'public', 'GET',
                    {'symbol':symbol}, config={'cost':2})
        except Exception as exc:
            if self._error_code(exc) == -2043:
                return None
            raise
        if isinstance(response, dict) and response.get('code') == -2043:
            return None
        if not isinstance(response, dict) or response.get('symbol') != symbol or 'referencePrice' not in response:
            raise OrderListError('referencePrice response invalid')
        if response['referencePrice'] is None:
            return None
        value = self._decimal(response['referencePrice'])
        if value <= 0:
            raise OrderListError('referencePrice must be positive')
        return value

    def _percent_basis(self, symbol, rule, reference):
        if reference is not None:
            return reference
        minutes = int(rule['avgPriceMins'])
        if minutes < 0:
            raise OrderListError('avgPriceMins invalid')
        if minutes == 0:
            response = self.public.publicGetTickerPrice({'symbol':symbol})
            if response.get('symbol') != symbol:
                raise OrderListError('last-price symbol mismatch')
        else:
            response = self.public.publicGetAvgPrice({'symbol':symbol})
            if response.get('mins') != minutes:
                raise OrderListError('average price window mismatch')
        value = self._decimal(response.get('price'))
        if value <= 0:
            raise OrderListError('percent price basis invalid')
        return value

    def _account_filters(self, symbol):
        method=getattr(self.api,'privateGetMyFilters',None)
        if callable(method):
            response=method({'symbol':symbol})
        else:
            if self.api.urls['api']['private'] != 'https://testnet.binance.vision/api/v3':
                raise OrderListError('myFilters transport is not Testnet')
            response=self.api.request('myFilters','private','GET',{'symbol':symbol},config={'cost':40})
        if not isinstance(response,dict) or any(not isinstance(response.get(key),list)
                for key in ('symbolFilters','exchangeFilters','assetFilters')):
            raise OrderListError('myFilters response invalid')
        if any(not isinstance(row,dict) or not row.get('filterType')
                for key in ('symbolFilters','exchangeFilters','assetFilters') for row in response[key]):
            raise OrderListError('myFilters rule invalid')
        return response

    def _current_sell_geometry(self, symbol, plan):
        last=self.public.publicGetTickerPrice({'symbol':symbol})
        book=self.public.publicGetTickerBookTicker({'symbol':symbol})
        if not isinstance(last,dict) or last.get('symbol')!=symbol or not isinstance(book,dict) or book.get('symbol')!=symbol:
            raise OrderListError('current Testnet price identity unavailable')
        price=self._decimal(last['price']);bid=self._decimal(book['bidPrice']);ask=self._decimal(book['askPrice'])
        if min(price,bid,ask)<=0 or bid>ask:
            raise OrderListError('current Testnet price invalid')
        if self._decimal(plan.tp_price)<=max(price,bid):
            raise OrderListError('OCO take-profit crosses current Testnet price')
        if plan.mode=='FIXED_OCO' and self._decimal(plan.stop_trigger)>=price:
            raise OrderListError('OCO stop would trigger at current Testnet price')
        return {'last':str(price),'bid':str(bid),'ask':str(ask)}

    def preflight(self, plan: ProtectionPlan, *, pending_quantity=None, otoco: bool=False) -> dict[str,Any]:
        try:
            return self._preflight(plan,pending_quantity=pending_quantity,otoco=otoco)
        except OrderListError:
            raise
        except Exception as exc:
            raise OrderListError('Testnet preflight unavailable: '+type(exc).__name__) from exc

    def _preflight(self, plan: ProtectionPlan, *, pending_quantity=None, otoco: bool=False) -> dict[str,Any]:
        plan.ensure_sell_geometry()
        symbol=_symbol(plan.pair)
        info=self.symbol_info(plan.pair)
        if info.get("status")!="TRADING": raise OrderListError("symbol is not TRADING")
        if not info.get("ocoAllowed"): raise OrderListError("OCO not allowed")
        if otoco and not info.get("otoAllowed"): raise OrderListError("OTOCO/OTO not allowed")
        if plan.mode=="TRAILING_OCO" and not info.get("allowTrailingStop"):
            raise OrderListError("trailing stop not allowed")
        account=self._account_filters(symbol)
        # myFilters is the authenticated account-specific view. Its symbol
        # rules override the public defaults, including account-specific limits.
        merged={row['filterType']:row for row in info.get('filters') or []}
        merged.update({row['filterType']:row for row in account['symbolFilters']})
        filters=list(merged.values())
        price_rule=self._filter(filters,"PRICE_FILTER"); qty_rule=self._filter(filters,"LOT_SIZE")
        notional_rule=self._filter(filters,"NOTIONAL") or self._filter(filters,"MIN_NOTIONAL")
        pct_rules=[r for r in filters if r.get('filterType') in {'PERCENT_PRICE','PERCENT_PRICE_BY_SIDE'}]
        trailing_rule=self._filter(filters,"TRAILING_DELTA")
        protected=self._decimal(pending_quantity if pending_quantity is not None else plan.quantity)
        self._validate_quantity(plan.quantity,qty_rule,"working")
        self._validate_quantity(protected,qty_rule,"protected")
        self._validate_price(plan.tp_price,price_rule,"take-profit")
        self._validate_notional(plan.tp_price,protected,notional_rule,"take-profit")
        if otoco:
            self._validate_price(plan.reference_price,price_rule,"working")
            self._validate_notional(plan.reference_price,plan.quantity,notional_rule,"working")
        if plan.mode=="FIXED_OCO":
            self._validate_price(plan.stop_trigger,price_rule,"stop-trigger")
            self._validate_price(plan.stop_limit,price_rule,"stop-limit")
            self._validate_notional(plan.stop_limit,protected,notional_rule,"stop-limit")
        elif plan.mode=="TRAILING_OCO":
            if not trailing_rule: raise OrderListError("TRAILING_DELTA unavailable")
            delta=int(plan.trailing_delta_bips or 0)
            low=int(trailing_rule.get("minTrailingBelowDelta",0)); high=int(trailing_rule.get("maxTrailingBelowDelta",0))
            if delta < low or (high and delta > high): raise OrderListError("TRAILING_DELTA outside SELL stop range")
        current_market=self._current_sell_geometry(symbol,plan) if not otoco else None
        base_asset,quote_asset=plan.pair.split('/')
        for rule in account['assetFilters']:
            if rule['filterType']!='MAX_ASSET':
                raise OrderListError('unsupported account asset filter')
            maximum=self._decimal(rule['limit'])
            if maximum<=0: raise OrderListError('invalid MAX_ASSET limit')
            if rule['asset']==base_asset:
                quantities=[protected]+([self._decimal(plan.quantity)] if otoco else [])
                if max(quantities)>maximum: raise OrderListError('MAX_ASSET base quantity exceeded')
            elif rule['asset']==quote_asset:
                if plan.mode=='TRAILING_OCO':
                    raise OrderListError('MAX_ASSET market stop notional cannot be proven')
                notionals=[protected*self._decimal(plan.tp_price),protected*self._decimal(plan.stop_limit)]
                if otoco: notionals.append(self._decimal(plan.quantity)*self._decimal(plan.reference_price))
                if max(notionals)>maximum: raise OrderListError('MAX_ASSET quote notional exceeded')
            else:
                raise OrderListError('MAX_ASSET symbol mismatch')
        reference = self._reference_price(symbol) if pct_rules else None
        bases = {}
        for pct_rule in pct_rules:
            avg = self._percent_basis(symbol, pct_rule, reference)
            kind = pct_rule['filterType']
            bases[kind] = str(avg)
            by_side = kind == 'PERCENT_PRICE_BY_SIDE'
            if otoco:
                lo=avg*self._decimal(pct_rule.get('bidMultiplierDown' if by_side else 'multiplierDown')); hi=avg*self._decimal(pct_rule.get('bidMultiplierUp' if by_side else 'multiplierUp'))
                if not lo <= self._decimal(plan.reference_price) <= hi: raise OrderListError("PERCENT_PRICE_BY_SIDE working price outside BUY range")
            slo=avg*self._decimal(pct_rule.get('askMultiplierDown' if by_side else 'multiplierDown')); shi=avg*self._decimal(pct_rule.get('askMultiplierUp' if by_side else 'multiplierUp'))
            for label,value in [("take-profit",plan.tp_price)]+([("stop-limit",plan.stop_limit)] if plan.mode=="FIXED_OCO" else []):
                if not slo <= self._decimal(value) <= shi: raise OrderListError(f"PERCENT_PRICE_BY_SIDE {label} outside SELL range")
        all_orders=self.api.privateGetOpenOrders({})
        all_lists=self.api.privateGetOpenOrderList()
        if not isinstance(all_orders,list) or not isinstance(all_lists,list):
            raise OrderListError('account open-order evidence unavailable')
        open_orders=[row for row in all_orders if row.get('symbol')==symbol]
        open_lists=[row for row in all_lists if row.get('symbol')==symbol]
        position=self._filter(filters,'MAX_POSITION')
        if position and otoco:
            balances=self.api.privateGetAccount({}).get('balances')
            if not isinstance(balances,list): raise OrderListError('MAX_POSITION balances unavailable')
            matches=[row for row in balances if row.get('asset')==base_asset]
            if len(matches)!=1: raise OrderListError('MAX_POSITION base balance unavailable')
            held=self._decimal(matches[0]['free'])+self._decimal(matches[0]['locked'])
            pending=sum((self._decimal(row['origQty'])-self._decimal(row['executedQty'])
                for row in open_orders if row.get('side')=='BUY'),Decimal(0))
            maximum=self._decimal(position['maxPosition'])
            if min(held,pending)<0 or maximum<=0 or held+pending+self._decimal(plan.quantity)>maximum:
                raise OrderListError('MAX_POSITION capacity exceeded')
        additional_orders=3 if otoco else 2
        max_orders=self._count_limit(self._filter(filters,"MAX_NUM_ORDERS"),"maxNumOrders")
        if max_orders is not None and len(open_orders)+additional_orders > max_orders: raise OrderListError("MAX_NUM_ORDERS capacity exceeded")
        max_lists=self._count_limit(self._filter(filters,"MAX_NUM_ORDER_LISTS"),"maxNumOrderLists")
        if max_lists is not None and len(open_lists)+1 > max_lists: raise OrderListError("MAX_NUM_ORDER_LISTS capacity exceeded")
        algo_types={"STOP_LOSS","STOP_LOSS_LIMIT","TAKE_PROFIT","TAKE_PROFIT_LIMIT"}
        algo_open=sum(str(row.get("type") or "").upper() in algo_types for row in open_orders)
        max_algo=self._count_limit(self._filter(filters,"MAX_NUM_ALGO_ORDERS"),"maxNumAlgoOrders")
        if max_algo is not None and algo_open+1 > max_algo: raise OrderListError("MAX_NUM_ALGO_ORDERS capacity exceeded")
        counts={'EXCHANGE_MAX_NUM_ORDERS':('maxNumOrders',len(all_orders)+additional_orders),
                'EXCHANGE_MAX_NUM_ALGO_ORDERS':('maxNumAlgoOrders',sum(str(r.get('type','')).upper() in algo_types for r in all_orders)+1),
                'EXCHANGE_MAX_NUM_ORDER_LISTS':('maxNumOrderLists',len(all_lists)+1)}
        for rule in account['exchangeFilters']:
            kind=rule['filterType']
            if kind=='EXCHANGE_MAX_NUM_ICEBERG_ORDERS': continue # no iceberg orders emitted
            if kind not in counts: raise OrderListError('unsupported exchange capacity filter')
            key,count=counts[kind];limit=self._count_limit(rule,key)
            if limit is None or limit<0: raise OrderListError('invalid exchange capacity limit')
            if count>limit: raise OrderListError(kind+' capacity exceeded')
        return {"symbol":symbol,"additional_orders":additional_orders,"open_orders":len(open_orders),"open_order_lists":len(open_lists),"open_algo_orders":algo_open,"percent_price_bases":bases,"reference_price":str(reference) if reference is not None else None,"current_market":current_market}

    @staticmethod
    def _format(value) -> str:
        return format(value, "f")

    @staticmethod
    def _extract_ids(response: dict[str, Any], *, list_client_id: str,
                     working_client_id: str | None, tp_client_id: str, sl_client_id: str) -> ListIds:
        by_client={}
        for row in response.get("orders", []) or []:
            cid=row.get("clientOrderId")
            if cid: by_client[cid]=row
        for row in response.get("orderReports", []) or []:
            cid=row.get("clientOrderId")
            if cid: by_client[cid]=row
        def oid(cid):
            row=by_client.get(cid or "") or {}
            value=row.get("orderId")
            return str(value) if value is not None else None
        list_id=response.get("orderListId")
        return ListIds(str(list_id) if list_id is not None else None,list_client_id,
                       oid(working_client_id),working_client_id,
                       oid(tp_client_id),tp_client_id,oid(sl_client_id),sl_client_id)

    def submit_fixed_otoco(self, plan: ProtectionPlan, *, pending_quantity=None) -> tuple[dict[str, Any], ListIds]:
        plan.ensure_sell_geometry()
        if plan.mode != "FIXED_OCO": raise OrderListError("OTOCO entry requires FIXED_OCO plan")
        self.preflight(plan,pending_quantity=pending_quantity,otoco=True)
        lid=_client("L",plan.intent_id,plan.generation,"list")
        wid=_client("W",plan.intent_id,plan.generation,"working")
        tid=_client("T",plan.intent_id,plan.generation,"takeprofit")
        sid=_client("S",plan.intent_id,plan.generation,"stop")
        protected_qty = pending_quantity if pending_quantity is not None else plan.quantity
        params={
            "symbol":_symbol(plan.pair),"listClientOrderId":lid,"newOrderRespType":"FULL",
            "workingType":"LIMIT","workingSide":"BUY","workingClientOrderId":wid,
            "workingPrice":self._format(plan.reference_price),"workingQuantity":self._format(plan.quantity),
            "workingTimeInForce":"FOK","pendingSide":"SELL","pendingQuantity":self._format(protected_qty),
            "pendingAboveType":"LIMIT_MAKER","pendingAboveClientOrderId":tid,
            "pendingAbovePrice":self._format(plan.tp_price),
            "pendingBelowType":"STOP_LOSS_LIMIT","pendingBelowClientOrderId":sid,
            "pendingBelowPrice":self._format(plan.stop_limit),"pendingBelowStopPrice":self._format(plan.stop_trigger),
            "pendingBelowTimeInForce":"GTC",
        }
        try:
            response=self.api.privatePostOrderListOtoco(params)
        except Exception as exc:
            # Network/timeouts/5xx are not safe to blindly retry. The caller persists
            # deterministic IDs before submission and reconciles them on any exception.
            raise OrderListUnknown(f"OTOCO submission outcome unknown: {type(exc).__name__}: {exc}") from exc
        return response,self._extract_ids(response,list_client_id=lid,working_client_id=wid,tp_client_id=tid,sl_client_id=sid)

    def submit_oco(self, plan: ProtectionPlan) -> tuple[dict[str, Any], ListIds]:
        plan.ensure_sell_geometry()
        self.preflight(plan,otoco=False)
        lid=_client("L",plan.intent_id,plan.generation,"list")
        tid=_client("T",plan.intent_id,plan.generation,"takeprofit")
        sid=_client("S",plan.intent_id,plan.generation,"stop")
        params={"symbol":_symbol(plan.pair),"listClientOrderId":lid,"side":"SELL",
                "quantity":self._format(plan.quantity),"newOrderRespType":"FULL",
                "aboveType":"LIMIT_MAKER","aboveClientOrderId":tid,"abovePrice":self._format(plan.tp_price),
                "belowType":"STOP_LOSS_LIMIT","belowClientOrderId":sid,"belowTimeInForce":"GTC"}
        if plan.mode == "FIXED_OCO":
            params.update({"belowPrice":self._format(plan.stop_limit),"belowStopPrice":self._format(plan.stop_trigger)})
        elif plan.mode == "TRAILING_OCO":
            # First Testnet profile uses a conditional MARKET stop for the trailing leg.
            # Binance STOP_LOSS + trailingDelta avoids falsely implying a static limit
            # price follows the trail. This policy is explicit and tested separately.
            params["belowType"]="STOP_LOSS"
            params.pop("belowTimeInForce",None)
            params["belowTrailingDelta"]=int(plan.trailing_delta_bips)
        else:
            raise OrderListError("unsupported OCO mode")
        try:
            response=self.api.privatePostOrderListOco(params)
        except Exception as exc:
            raise OrderListUnknown(f"OCO submission outcome unknown: {type(exc).__name__}: {exc}") from exc
        return response,self._extract_ids(response,list_client_id=lid,working_client_id=None,tp_client_id=tid,sl_client_id=sid)

    def query_list(self, *, order_list_id: str | None = None, list_client_id: str | None = None) -> dict[str, Any]:
        if not order_list_id and not list_client_id: raise OrderListError("order-list identity required")
        params={}
        if order_list_id: params["orderListId"]=order_list_id
        else: params["origClientOrderId"]=list_client_id
        return self.api.privateGetOrderList(params)

    def cancel_list(self, *, symbol: str, order_list_id: str | None = None, list_client_id: str | None = None) -> dict[str, Any]:
        if not order_list_id and not list_client_id: raise OrderListError("order-list identity required")
        params={"symbol":_symbol(symbol)}
        if order_list_id: params["orderListId"]=order_list_id
        else: params["listClientOrderId"]=list_client_id
        try:
            return self.api.privateDeleteOrderList(params)
        except Exception as exc:
            raise OrderListUnknown(f"order-list cancellation outcome unknown: {type(exc).__name__}: {exc}") from exc
