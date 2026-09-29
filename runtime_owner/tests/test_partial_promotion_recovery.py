from __future__ import annotations
from registry_fixture import registry_document
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from freqtrade.binana.execution_manager import BinanaExecutionManager


class Api:
    def __init__(self):
        self.oco_calls = []
        self.cancel_response = {"orderReports": []}
    def publicGetExchangeInfo(self, params):
        return {'symbols':[{'symbol':'ADAUSDT','status':'TRADING','ocoAllowed':True,'otoAllowed':True,
            'allowTrailingStop':True,'filters':[
                {'filterType':'PRICE_FILTER','minPrice':'0.0001','maxPrice':'1000','tickSize':'0.0001'},
                {'filterType':'LOT_SIZE','minQty':'0.01','maxQty':'1000000','stepSize':'0.01'},
                {'filterType':'NOTIONAL','minNotional':'0.1','maxNotional':'1000000000'},
                {'filterType':'PERCENT_PRICE_BY_SIDE','bidMultiplierDown':'0.2','bidMultiplierUp':'5',
                 'askMultiplierDown':'0.2','askMultiplierUp':'5'},
                {'filterType':'TRAILING_DELTA','minTrailingBelowDelta':10,'maxTrailingBelowDelta':2000},
                {'filterType':'MAX_NUM_ORDERS','maxNumOrders':100},
                {'filterType':'MAX_NUM_ORDER_LISTS','maxNumOrderLists':100},
                {'filterType':'MAX_NUM_ALGO_ORDERS','maxNumAlgoOrders':100},
            ]}]}
    def publicGetAvgPrice(self, params): return {'price':'1'}
    def privateGetOpenOrders(self, params): return []
    def privateGetOpenOrderList(self, params=None): return []
    def fetch_open_orders(self, pair): return []
    def publicGetTickerBookTicker(self, params): return {'bidPrice':'1.01','askPrice':'1.02'}
    def fetch_balance(self): return {'ADA': {'free': 1000.0}}
    def fetch_my_trades(self, pair, params=None):
        oid=str((params or {}).get("orderId"))
        if oid == "10":
            return [{"id":"100","order":"10","symbol":pair,"timestamp":900,"side":"buy",
                     "price":1.0,"amount":10.0,"cost":10.0,
                     "fee":{"currency":"USDT","cost":0.0}}]
        if oid == "11":
            return [{"id": "101", "order": "11", "symbol":pair,"timestamp": 1000, "side": "sell",
                     "price": 1.02, "amount": 1.0, "cost": 1.02,
                     "fee": {"currency": "USDT", "cost": 0.001}}]
        return []
    def privateDeleteOrderList(self, params):
        return self.cancel_response
    def privatePostOrderListOco(self, params):
        self.oco_calls.append(params)
        return {"orderListId": 22, "orders": [
            {"orderId": 23, "clientOrderId": params["aboveClientOrderId"]},
            {"orderId": 24, "clientOrderId": params["belowClientOrderId"]},
        ]}


class Exchange:
    def __init__(self):
        self._api = Api()
        self.markets = {"ADA/USDT": {"active": True, "spot": True, "precision": {"price": "0.0001"}, "info": {
            "ocoAllowed": True, "otoAllowed": True, "allowTrailingStop": True,
            "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.0001"},
                {"filterType": "LOT_SIZE", "minQty": "0.01", "maxQty": "1000000", "stepSize": "0.01"},
                {"filterType": "NOTIONAL", "minNotional": "0.1", "maxNotional": "1000000000"},
                {"filterType": "TRAILING_DELTA", "minTrailingBelowDelta": 10, "maxTrailingBelowDelta": 2000},
            ],
        }}}
    def amount_to_precision(self, pair, amount):
        return str(amount)
    def price_to_precision(self, pair, price):
        return str(price)
    def fetch_order(self, order_id, pair):
        oid=str(order_id)
        if oid == '10':
            return {'id':'10','clientOrderId':'W','symbol':pair,'side':'buy','status':'closed',
                    'amount':10.0,'filled':10.0,'remaining':0.0,'price':1.0,'average':1.0,
                    'cost':10.0,'info':{'clientOrderId':'W'}}
        if oid in {'23','24'} and self._api.oco_calls:
            params=self._api.oco_calls[-1]
            client=params['aboveClientOrderId'] if oid=='23' else params['belowClientOrderId']
            qty=float(params['quantity'])
            return {'id':oid,'clientOrderId':client,'symbol':pair,'side':'sell','status':'open',
                    'amount':qty,'filled':0.0,'remaining':qty,
                    'price':float(params.get('abovePrice') or params.get('belowPrice') or 1),
                    'cost':0.0,'info':{'clientOrderId':client}}
        report=next((r for r in self._api.cancel_response.get('orderReports',[])
                     if str(r.get('orderId'))==oid),None)
        status=str((report or {}).get('status') or 'CANCELED').lower()
        if status in {'cancelled','partially_filled'}: status='canceled'
        filled=float((report or {}).get('executedQty') or 0)
        client=(report or {}).get('clientOrderId') or ('TP' if oid=='11' else 'SL')
        return {'id':oid,'clientOrderId':client,'symbol':pair,'side':'sell','status':status,
                'amount':9.98,'filled':filled,'remaining':max(0.0,9.98-filled),
                'price':1.02 if oid=='11' else 0.99,'average':1.02 if filled else None,
                'cost':filled*1.02,'info':report or {'clientOrderId':client}}
    def _trades_contracts_to_amount(self, trades):
        return trades


class GoodMarket:
    def refresh(self, pair, quote_budget=None):
        return SimpleNamespace(state="GOOD", flow_status="fresh", taker_buy_ratio_60s="0.70",
            cvd_quote_60s="1000", best_bid="1.01", to_dict=lambda: {"state": "GOOD"})


def cfg(td, registry):
    return {"binana": {"enabled": True, "environment": "testnet", "spot_only": True, "allow_live": False,
        "single_halal_decision": True, "allocation_usdt": 1000, "testnet_epoch_id": "epoch-A",
        "halal_registry_path": str(registry), "state_db_path": str(Path(td) / "ext.sqlite"),
        "market_context_path": str(Path(td) / "context.json"), "liquidity": {},
        "flow_policy": {"promote_buy_ratio": 0.58, "promotion_min_profit_fraction": 0.004},
        "pending_base_fee_reserve_fraction": "0.002", "defensive_stop_fraction": "0.006",
        "normal_stop_fraction": "0.01", "fixed_target_fraction": "0.015",
        "stop_limit_buffer_fraction": "0.0015", "estimated_exit_fee_fraction": "0.001",
        "trailing_delta_bips": 75}, "dry_run": False, "trading_mode": "spot", "margin_mode": None,
        "stake_currency": "USDT", "stake_amount": 250, "max_open_trades": 4, "timeframe": "5m",
        "force_entry_enable": False, "position_adjustment_enable": False,
        "exchange": {"name": "binance", "key": "key-A"}}


class PartialPromotionRecovery(unittest.TestCase):
    def test_partial_old_tp_fill_is_recorded_and_only_residual_is_reprotected(self):
        with TemporaryDirectory() as td:
            registry = Path(td) / "halal.json"
            registry.write_text(json.dumps(registry_document(['ADAUSDT'])))
            ex = Exchange()
            manager = BinanaExecutionManager(cfg(td, registry), ex)
            manager.market_data = GoodMarket()
            iid = "intent"
            manager.store.put_intent(intent_id=iid, pair="ADA/USDT", signal_id="x", halal_allowed=True,
                registry_sha256="h", nominal_usdt="250", state="OPEN")
            ids = {"order_list_id": "10", "list_client_id": "L", "tp_order_id": "11",
                   "tp_client_id": "TP", "sl_order_id": "12", "sl_client_id": "SL"}
            manager.store.put_generation(intent_id=iid, generation=1, pair="ADA/USDT", mode="FIXED_OCO",
                status="FIXED_ACTIVE", ids=ids, expected_qty="9.98")
            trade = SimpleNamespace(id=1, pair="ADA/USDT", amount=10.0, open_rate=1.0)
            ex._api.cancel_response = {"orderReports": [
                {"orderId": 11, "clientOrderId": "TP", "status": "CANCELED", "executedQty": "1.0"}
            ]}

            self.assertIsNone(manager.maybe_promote(trade, iid, manager.store.latest_generation(iid)))

            self.assertEqual(ex._api.oco_calls[0]["quantity"], "8.98")
            fills = manager.store.fill_rows(iid)
            self.assertEqual(len(fills), 1)
            self.assertEqual((fills[0]["side"], fills[0]["base_qty"], fills[0]["fee_asset"]),
                             ("SELL", "1.0", "USDT"))
            gen2 = manager.store.latest_generation(iid)
            # Replacement is deliberately not trusted as ACTIVE until a later
            # authenticated child-order verification pass.
            self.assertEqual((gen2["generation"], gen2["status"], gen2["expected_qty"]),
                             (2, "SUBMITTED_UNVERIFIED", "8.98"))
            self.assertTrue(manager.store.has_blockers())


if __name__ == "__main__":
    unittest.main()
