"""Unit tests for M001: validate fetch_open_orders response is an actual list.

Only an empty JSON array [] proves absence of open orders.
None, error envelope, non-list, or non-dict items fail closed.
Non-empty list containing our client IDs -> REPLACEMENT_OPEN_ORDER_EXISTS.
Non-empty list with only foreign orders -> REPLACEMENT_OPEN_ORDER_ABSENCE_UNVERIFIED.
"""

from __future__ import annotations

from types import SimpleNamespace
import unittest

from binana.recovery_contract import order_history_state
from binana.submission_absence import (
    _is_valid_open_orders,
    _our_order_in_list,
    explicit_absence,
    verify_absent_replacement,
)


class TestIsValidOpenOrders(unittest.TestCase):
    """M001: response-type validation for GET /api/v3/openOrders."""

    def test_none_is_invalid(self):
        self.assertIsNone(_is_valid_open_orders(None))

    def test_false_is_invalid(self):
        self.assertIsNone(_is_valid_open_orders(False))

    def test_zero_is_invalid(self):
        self.assertIsNone(_is_valid_open_orders(0))

    def test_dict_is_invalid(self):
        self.assertIsNone(_is_valid_open_orders({}))

    def test_string_is_invalid(self):
        self.assertIsNone(_is_valid_open_orders(""))

    def test_tuple_is_invalid(self):
        self.assertIsNone(_is_valid_open_orders(()))

    def test_empty_list_is_valid(self):
        self.assertEqual(_is_valid_open_orders([]), [])

    def test_list_of_dicts_is_valid(self):
        result = _is_valid_open_orders([{"id": "1", "clientOrderId": "abc"}])
        self.assertEqual(result, [{"id": "1", "clientOrderId": "abc"}])

    def test_list_with_non_dict_is_invalid(self):
        self.assertIsNone(_is_valid_open_orders([{"id": "1"}, "not-a-dict"]))


class TestOurOrderInList(unittest.TestCase):
    """M001: detect whether any open order carries one of our client IDs."""

    def test_our_client_id_present(self):
        orders = [
            {"id": "1", "clientOrderId": "our-list-cid"},
            {"id": "2", "clientOrderId": "foreign-cid"},
        ]
        self.assertTrue(_our_order_in_list(orders, ["our-list-cid", "tp-cid", "sl-cid"]))

    def test_our_order_id_present(self):
        orders = [{"id": "7", "clientOrderId": "foreign-cid"}]
        self.assertTrue(_our_order_in_list(orders, ["7", "tp-cid", "sl-cid"]))

    def test_foreign_orders_only(self):
        orders = [{"id": "2", "clientOrderId": "foreign-cid"}]
        self.assertFalse(_our_order_in_list(orders, ["our-cid", "tp-cid", "sl-cid"]))

    def test_empty_ids(self):
        orders = [{"id": "1", "clientOrderId": "anything"}]
        self.assertFalse(_our_order_in_list(orders, []))

    def test_orig_client_order_id_key(self):
        orders = [{"origClientOrderId": "our-cid", "id": "3"}]
        self.assertTrue(_our_order_in_list(orders, ["our-cid"]))

    def test_non_dict_items_skipped(self):
        orders = ["not-a-dict", {"id": "1", "clientOrderId": "our-cid"}]
        self.assertTrue(_our_order_in_list(orders, ["our-cid"]))


def _raise_2013(*args, **kwargs):
    raise RuntimeError('binance {"code":-2013,"msg":"Order does not exist."}')


def _raise_2018(*args, **kwargs):
    raise RuntimeError('binance {"code":-2018,"msg":"Order list does not exist."}')


def _make_manager(open_orders_result, list_raises_2013=True, child_raises_2013=True):
    """Build a manager that proves list+child absence via documented codes."""
    order_lists = SimpleNamespace()
    order_lists.query_list = _raise_2013 if list_raises_2013 else lambda *a, **k: None

    exchange = SimpleNamespace()
    exchange._api = SimpleNamespace()
    exchange._api.fetch_open_orders = lambda pair: open_orders_result
    exchange._api.privateGetOrder = _raise_2013 if child_raises_2013 else lambda *a, **k: None

    return SimpleNamespace(
        config={"binana": {"environment": "testnet"}},
        order_lists=order_lists,
        exchange=exchange,
        scope_id="test-scope",
    )


_GEN = {
    "mode": "FIXED_OCO", "generation": 2, "pair": "ETH/USDT",
    "working_client_id": None, "order_list_id": None, "working_order_id": None,
    "tp_order_id": None, "sl_order_id": None,
    "list_client_id": "list-cid", "tp_client_id": "tp-cid", "sl_client_id": "sl-cid",
    "payload_json": '{"verified_absent_at": 1, "list_client_id": "list-cid"}',
}


class TestVerifyAbsentReplacementOpenOrders(unittest.TestCase):
    """M001: verify_absent_replacement open-orders gate behavior."""

    def test_empty_open_orders_proves_absence(self):
        mgr = _make_manager([])
        result = verify_absent_replacement(mgr, dict(_GEN), "ETH/USDT")
        self.assertIsNotNone(result)
        self.assertEqual(result["list_client_id"], "list-cid")

    def test_none_open_orders_fail_closed(self):
        mgr = _make_manager(None)
        with self.assertRaisesRegex(Exception, "REPLACEMENT_OPEN_ORDER_ABSENCE_UNVERIFIED"):
            verify_absent_replacement(mgr, dict(_GEN), "ETH/USDT")

    def test_error_envelope_open_orders_fail_closed(self):
        mgr = _make_manager({"code": -1007, "msg": "Timeout"})
        with self.assertRaisesRegex(Exception, "REPLACEMENT_OPEN_ORDER_ABSENCE_UNVERIFIED"):
            verify_absent_replacement(mgr, dict(_GEN), "ETH/USDT")

    def test_our_order_in_open_orders_raises_exists(self):
        open_orders = [{"id": "9", "clientOrderId": "tp-cid"}]
        mgr = _make_manager(open_orders)
        with self.assertRaisesRegex(Exception, "REPLACEMENT_OPEN_ORDER_EXISTS"):
            verify_absent_replacement(mgr, dict(_GEN), "ETH/USDT")

    def test_foreign_open_orders_fail_closed(self):
        open_orders = [{"id": "9", "clientOrderId": "foreign-cid"}]
        mgr = _make_manager(open_orders)
        with self.assertRaisesRegex(Exception, "REPLACEMENT_OPEN_ORDER_ABSENCE_UNVERIFIED"):
            verify_absent_replacement(mgr, dict(_GEN), "ETH/USDT")

    def test_api_timeout_fail_closed(self):
        def boom(pair):
            raise RuntimeError('binance {"code":-1007,"msg":"Timeout"}')

        order_lists = SimpleNamespace()
        order_lists.query_list = _raise_2013

        exchange = SimpleNamespace()
        exchange._api = SimpleNamespace()
        exchange._api.fetch_open_orders = boom
        exchange._api.privateGetOrder = _raise_2013

        mgr = SimpleNamespace(
            config={"binana": {"environment": "testnet"}},
            order_lists=order_lists,
            exchange=exchange,
            scope_id="test-scope",
        )
        with self.assertRaisesRegex(Exception, "REPLACEMENT_OPEN_ORDER_ABSENCE_UNVERIFIED"):
            verify_absent_replacement(mgr, dict(_GEN), "ETH/USDT")

    def test_list_with_non_dict_items_fail_closed(self):
        mgr = _make_manager([{"id": "1"}, "not-a-dict"])
        with self.assertRaisesRegex(Exception, "REPLACEMENT_OPEN_ORDER_ABSENCE_UNVERIFIED"):
            verify_absent_replacement(mgr, dict(_GEN), "ETH/USDT")


class TestM007UndocumentedErrorCode(unittest.TestCase):
    """M007: explicit_absence must not match undocumented -2018."""

    def test_2018_not_matched_single_order(self):
        exc = RuntimeError('binance {"code":-2018,"msg":"Order list does not exist."}')
        self.assertFalse(explicit_absence(exc))

    def test_2018_not_matched_order_list(self):
        exc = RuntimeError('binance {"code":-2018,"msg":"Order list does not exist."}')
        self.assertFalse(explicit_absence(exc, order_list=True))

    def test_2013_still_matched_single_order(self):
        exc = RuntimeError('binance {"code":-2013,"msg":"Order does not exist."}')
        self.assertTrue(explicit_absence(exc))

    def test_2013_still_matched_order_list(self):
        exc = RuntimeError('binance {"code":-2013,"msg":"Order does not exist."}')
        self.assertTrue(explicit_absence(exc, order_list=True))


class TestM014ExactCodeComparison(unittest.TestCase):
    """M014: order_history_state must use exact integer code comparison."""

    def test_2013_json_code_matches(self):
        self.assertEqual(
            order_history_state('{"code":-2013,"msg":"Order does not exist."}', testnet=True),
            "ORDER_HISTORY_UNAVAILABLE",
        )

    def test_20130_does_not_match(self):
        self.assertIsNone(
            order_history_state('{"code":-20130,"msg":"..."}', testnet=True)
        )

    def test_free_text_does_not_match(self):
        self.assertIsNone(
            order_history_state("Order -2013 does not exist", testnet=True)
        )

    def test_string_code_does_not_match(self):
        self.assertIsNone(
            order_history_state('{"code":"-2013","msg":"..."}', testnet=True)
        )

    def test_none_does_not_match(self):
        self.assertIsNone(order_history_state(None, testnet=True))

    def test_not_testnet_returns_none(self):
        self.assertIsNone(
            order_history_state('{"code":-2013,"msg":"Order does not exist."}', testnet=False)
        )


if __name__ == "__main__":
    unittest.main()
