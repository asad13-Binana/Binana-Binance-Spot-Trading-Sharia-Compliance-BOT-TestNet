"""Typed Binance Spot Testnet order-list transport owned by the Freqtrade fork."""
from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
from typing import Any

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
        filters=info.get("filters") or []
        price_rule=self._filter(filters,"PRICE_FILTER"); qty_rule=self._filter(filters,"LOT_SIZE")
        notional_rule=self._filter(filters,"NOTIONAL") or self._filter(filters,"MIN_NOTIONAL")
        pct_rule=self._filter(filters,"PERCENT_PRICE_BY_SIDE")
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
        avg=self._decimal((self.public.publicGetAvgPrice({"symbol":symbol}) or {}).get("price"))
        if pct_rule:
            if otoco:
                lo=avg*self._decimal(pct_rule.get("bidMultiplierDown")); hi=avg*self._decimal(pct_rule.get("bidMultiplierUp"))
                if not lo <= self._decimal(plan.reference_price) <= hi: raise OrderListError("PERCENT_PRICE_BY_SIDE working price outside BUY range")
            slo=avg*self._decimal(pct_rule.get("askMultiplierDown")); shi=avg*self._decimal(pct_rule.get("askMultiplierUp"))
            for label,value in [("take-profit",plan.tp_price)]+([("stop-limit",plan.stop_limit)] if plan.mode=="FIXED_OCO" else []):
                if not slo <= self._decimal(value) <= shi: raise OrderListError(f"PERCENT_PRICE_BY_SIDE {label} outside SELL range")
        open_orders=self.api.privateGetOpenOrders({"symbol":symbol}) or []
        open_lists=[row for row in (self.api.privateGetOpenOrderList() or []) if row.get("symbol")==symbol]
        additional_orders=3 if otoco else 2
        max_orders=self._count_limit(self._filter(filters,"MAX_NUM_ORDERS"),"maxNumOrders")
        if max_orders is not None and len(open_orders)+additional_orders > max_orders: raise OrderListError("MAX_NUM_ORDERS capacity exceeded")
        max_lists=self._count_limit(self._filter(filters,"MAX_NUM_ORDER_LISTS"),"maxNumOrderLists")
        if max_lists is not None and len(open_lists)+1 > max_lists: raise OrderListError("MAX_NUM_ORDER_LISTS capacity exceeded")
        algo_types={"STOP_LOSS","STOP_LOSS_LIMIT","TAKE_PROFIT","TAKE_PROFIT_LIMIT"}
        algo_open=sum(str(row.get("type") or "").upper() in algo_types for row in open_orders)
        max_algo=self._count_limit(self._filter(filters,"MAX_NUM_ALGO_ORDERS"),"maxNumAlgoOrders")
        if max_algo is not None and algo_open+1 > max_algo: raise OrderListError("MAX_NUM_ALGO_ORDERS capacity exceeded")
        return {"symbol":symbol,"additional_orders":additional_orders,"open_orders":len(open_orders),"open_order_lists":len(open_lists),"open_algo_orders":algo_open,"average_price":str(avg)}

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
