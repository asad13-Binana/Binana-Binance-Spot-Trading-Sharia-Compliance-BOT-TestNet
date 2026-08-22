from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from services.market_context.analytics import SpotMicrostructureAnalytics
from services.market_context.evidence import capture_signal_evidence
from services.market_context.execution_observer import install_signal_observer
from services.market_context.service import MarketContextService
from services.market_context.spot_stream import (
    LIVE_ENDPOINT,
    PUBLIC_MARKET_ENDPOINT,
    TESTNET_ENDPOINT,
    BoundedBackoff,
    SpotMarketStream,
    validated_endpoint,
)
from tests._harness import sign_signal

ROOT = Path(__file__).resolve().parents[1]
SYMBOL = "ETHUSDT"


def _agg(identifier, *, maker, price="100", quantity="1", event_ms=1_000_000):
    return {
        "e": "aggTrade",
        "s": SYMBOL,
        "a": identifier,
        "p": price,
        "q": quantity,
        "T": event_ms,
        "m": maker,
    }


def _book(update_id, *, bid="99", bid_qty="3", ask="101", ask_qty="1"):
    return {
        "u": update_id,
        "s": SYMBOL,
        "b": bid,
        "B": bid_qty,
        "a": ask,
        "A": ask_qty,
    }


def _analytics():
    value = SpotMicrostructureAnalytics(max_age_ms=15_000)
    value.set_symbols({SYMBOL})
    return value


def test_aggtrade_aggressor_side_windows_and_cvd_are_decimal_exact():
    analytics = _analytics()
    assert analytics.ingest_agg_trade(
        _agg(1, maker=False, quantity="2"), received_mono=100, received_wall=1000
    )
    assert analytics.ingest_agg_trade(
        _agg(2, maker=True), received_mono=100.1, received_wall=1000
    )
    flow = analytics.snapshot(now_mono=100.2, now_wall=1000)["symbols"][SYMBOL][
        "spot_aggressive_flow"
    ]
    # m=false => buyer taker => aggressive BUY; m=true => aggressive SELL.
    assert flow["aggressive_buy_quote_60s"] == "200"
    assert flow["aggressive_sell_quote_60s"] == "100"
    assert flow["taker_buy_ratio_60s"] == "0.6666666666666666666666666667"
    assert flow["cvd_quote_60s"] == "100"
    assert flow["cvd_quote_session"] == "100"
    assert flow["trade_count_10s"] == 2


def test_trade_flow_intensity_and_acceleration_compare_nonoverlapping_windows():
    analytics = _analytics()
    assert analytics.ingest_agg_trade(
        _agg(1, maker=False, event_ms=980_000),
        received_mono=80,
        received_wall=980,
    )
    assert analytics.ingest_agg_trade(
        _agg(2, maker=False, event_ms=1_000_000),
        received_mono=100,
        received_wall=1000,
    )
    flow = analytics.snapshot(now_mono=100, now_wall=1000)["symbols"][SYMBOL][
        "spot_aggressive_flow"
    ]
    assert flow["trade_intensity_per_second_10s"] == "0.1"
    assert flow["trade_intensity_per_second_30s"] == "0.06666666666666666666666666667"
    assert flow["trade_flow_acceleration_ratio_10s_vs_prior_20s"] == "2"
    assert flow["trade_flow_acceleration_delta_tps"] == "0.05"


def test_bookticker_uses_local_monotonic_freshness_and_has_no_fake_event_time():
    analytics = _analytics()
    assert analytics.ingest_book_ticker(_book(5), received_mono=200, received_wall=2000)
    liquidity = analytics.snapshot(now_mono=200.25, now_wall=2000.25)["symbols"][
        SYMBOL
    ]["top_of_book_liquidity"]
    assert liquidity["book_ticker_age_ms"] == 250
    assert liquidity["last_book_ticker_received_at"].startswith("1970-")
    assert liquidity["exchange_event_time"] is None
    assert liquidity["spread"] == "2"
    assert liquidity["spread_bps"] == "200"
    assert liquidity["top_of_book_quantity_pressure"] == "0.5"


def test_stale_status_is_explicit_and_does_not_discard_last_known_values():
    analytics = _analytics()
    analytics.ingest_book_ticker(_book(5), received_mono=100, received_wall=1000)
    record = analytics.snapshot(now_mono=116, now_wall=1016)["symbols"][SYMBOL]
    assert record["status"] == "stale"
    assert record["top_of_book_liquidity"]["best_bid"] == "99"
    assert record["top_of_book_liquidity"]["book_ticker_age_ms"] == 16_000


def test_malformed_nonfinite_crossed_duplicate_and_out_of_order_events_are_rejected():
    analytics = _analytics()
    assert analytics.ingest_agg_trade(
        _agg(10, maker=False), received_mono=100, received_wall=1000
    )
    assert not analytics.ingest_agg_trade(
        _agg(10, maker=False), received_mono=101, received_wall=1001
    )
    assert not analytics.ingest_agg_trade(
        _agg(9, maker=False), received_mono=101, received_wall=1001
    )
    assert not analytics.ingest_agg_trade(
        _agg(11, maker=False, price="NaN"), received_mono=101, received_wall=1001
    )
    assert analytics.ingest_book_ticker(_book(5), received_mono=100, received_wall=1000)
    assert not analytics.ingest_book_ticker(
        _book(6, bid="102", ask="101"), received_mono=101, received_wall=1001
    )
    snapshot = analytics.snapshot(now_mono=101, now_wall=1001)
    stats = snapshot["statistics"]
    assert stats["duplicate_messages"] == 1
    assert stats["out_of_order_messages"] == 1
    assert stats["malformed_messages"] >= 2
    assert snapshot["symbols"][SYMBOL]["top_of_book_liquidity"]["best_bid"] == "99"


def test_memory_is_bounded_to_one_second_buckets():
    analytics = _analytics()
    for identifier in range(1, 2001):
        assert analytics.ingest_agg_trade(
            _agg(
                identifier, maker=bool(identifier % 2), event_ms=1_000_000 + identifier
            ),
            received_mono=100 + identifier / 10_000,
            received_wall=1000 + identifier / 1000,
        )
    assert (
        len(analytics._states[SYMBOL].buckets) == 1
    )  # bounded by time, not event count


class _FakeSocket:
    def __init__(self):
        self.sent = []
        self.closed = False

    def send(self, value):
        self.sent.append(json.loads(value))

    def close(self):
        self.closed = True


def test_one_socket_dynamic_subscribe_unsubscribe_and_acknowledgements():
    clock = iter(range(1, 100)).__next__
    analytics = SpotMicrostructureAnalytics(max_age_ms=15_000)
    stream = SpotMarketStream(
        analytics,
        endpoint=TESTNET_ENDPOINT,
        rotation_seconds=3600,
        command_interval_seconds=0.2,
        monotonic=clock,
    )
    stream.update_symbols({"ETHUSDT", "SOLUSDT"})
    socket = _FakeSocket()
    stream._on_open(socket)
    try:
        assert len(socket.sent) == 1
        first = socket.sent[0]
        assert first["method"] == "SUBSCRIBE"
        assert set(first["params"]) == {
            "ethusdt@aggTrade",
            "ethusdt@bookTicker",
            "solusdt@aggTrade",
            "solusdt@bookTicker",
        }
        stream._on_message(socket, json.dumps({"result": None, "id": first["id"]}))
        assert stream.status()["subscription_ready"] is True
        stream.update_symbols({"SOLUSDT", "ADAUSDT"})
        assert [row["method"] for row in socket.sent[1:]] == [
            "UNSUBSCRIBE",
            "SUBSCRIBE",
        ]
        assert analytics.snapshot(now_mono=20, now_wall=20)["symbol_count"] == 2
    finally:
        stream.stop()


def test_stream_dispatches_raw_and_combined_payloads_and_handles_shutdown():
    now = [1000.0]
    analytics = _analytics()
    stream = SpotMarketStream(
        analytics,
        endpoint=TESTNET_ENDPOINT,
        rotation_seconds=3600,
        monotonic=lambda: now[0],
        wall_clock=lambda: now[0],
    )
    socket = _FakeSocket()
    stream._ws = socket
    stream._connected = True
    stream._on_message(socket, json.dumps(_agg(1, maker=False)))
    stream._on_message(
        socket, json.dumps({"stream": "ethusdt@bookTicker", "data": _book(2)})
    )
    assert (
        analytics.snapshot(now_mono=1000, now_wall=1000)["symbols"][SYMBOL]["status"]
        == "fresh"
    )
    stream._on_message(socket, json.dumps({"e": "serverShutdown"}))
    assert socket.closed is True
    assert stream.status()["last_error"] == "server_shutdown"


def test_reconnect_backoff_is_exponential_jittered_and_bounded():
    backoff = BoundedBackoff(
        initial=1, maximum=8, jitter_ratio=0.2, random_value=lambda: 1.0
    )
    assert [backoff.next_delay() for _ in range(5)] == [1.2, 2.4, 4.8, 8.0, 8.0]
    backoff.reset()
    assert backoff.next_delay() == 1.2


def test_package_endpoint_binding_cannot_cross_testnet_and_live():
    assert validated_endpoint("", testnet=True) == PUBLIC_MARKET_ENDPOINT
    assert validated_endpoint("", testnet=False) == LIVE_ENDPOINT
    with pytest.raises(ValueError):
        validated_endpoint(LIVE_ENDPOINT, testnet=True)
    with pytest.raises(ValueError):
        validated_endpoint(TESTNET_ENDPOINT, testnet=True)
    with pytest.raises(ValueError):
        validated_endpoint("wss://evil.example/ws", testnet=False)


def test_immutable_package_mode_selects_the_correct_default_stream(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("BINANCE_SPOT_MARKET_STREAM", raising=False)
    monkeypatch.setenv("SHARED_ROOT", str(tmp_path))
    service = MarketContextService()
    mode = (ROOT / "RELEASE_MODE").read_text(encoding="utf-8").strip()
    expected = PUBLIC_MARKET_ENDPOINT if mode == "testnet" else LIVE_ENDPOINT
    assert service.mode == mode
    assert service.stream.endpoint == expected


def test_signal_evidence_is_fresh_bounded_and_explicitly_non_authorizing(tmp_path):
    analytics = _analytics()
    analytics.ingest_agg_trade(
        _agg(1, maker=False), received_mono=100, received_wall=1000
    )
    analytics.ingest_book_ticker(_book(1), received_mono=100, received_wall=1000)
    snapshot = analytics.snapshot(now_mono=100, now_wall=1000)
    snapshot["universe_snapshot_hash"] = "a" * 64
    path = tmp_path / "current.json"
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    evidence = capture_signal_evidence(path, SYMBOL, now=1001)
    assert evidence["available"] is True and evidence["fresh"] is True
    assert evidence["advisory_only"] is True
    assert evidence["used_for_trade_decision"] is False
    assert evidence["signal_time_agg_trade_age_ms"] == 1000
    assert evidence["signal_time_book_ticker_age_ms"] == 1000
    assert evidence["record"]["top_of_book_liquidity"]["spread_bps"] == "200"


def test_signal_observer_records_authenticated_evidence_without_changing_result(
    tmp_path, monkeypatch
):
    now = time.time()
    analytics = _analytics()
    analytics.ingest_agg_trade(
        _agg(1, maker=False, event_ms=round(now * 1000)),
        received_mono=100,
        received_wall=now,
    )
    analytics.ingest_book_ticker(_book(1), received_mono=100, received_wall=now)
    snapshot = analytics.snapshot(now_mono=100, now_wall=now)
    context_path = tmp_path / "current.json"
    context_path.write_text(json.dumps(snapshot), encoding="utf-8")
    evidence_dir = tmp_path / "signal_evidence"
    monkeypatch.setenv("MARKET_CONTEXT_FILE", str(context_path))
    monkeypatch.setenv("MARKET_CONTEXT_SIGNAL_EVIDENCE_DIR", str(evidence_dir))
    monkeypatch.setenv("AUDIT_LOG", str(tmp_path / "audit.jsonl"))

    signal_path = tmp_path / "signal.json"
    signal_path.write_text(
        json.dumps(
            sign_signal(
                {
                    "signal_id": "signal-1",
                    "pair": "ETH/USDT",
                    "symbol": SYMBOL,
                }
            )
        ),
        encoding="utf-8",
    )

    class Manager:
        calls = 0

        def process_signal(self, path, *args, **kwargs):
            self.calls += 1
            return True, "protected-result"

    install_signal_observer(Manager)
    manager = Manager()
    result = manager.process_signal(signal_path, {"ETH/USDT"}, "hash")
    assert result == (True, "protected-result")
    assert manager.calls == 1
    saved = json.loads((evidence_dir / "signal-1.json").read_text(encoding="utf-8"))
    assert saved["market_context"]["fresh"] is True
    assert saved["market_context"]["used_for_trade_decision"] is False
    assert saved["signal_processing_outcome"] == "(True, 'protected-result')"


def test_signal_observer_failure_is_isolated_and_original_runs_once(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MARKET_CONTEXT_FILE", str(tmp_path / "missing.json"))
    monkeypatch.setenv("AUDIT_LOG", str(tmp_path / "audit.jsonl"))

    class Manager:
        calls = 0

        def process_signal(self, path, *args, **kwargs):
            self.calls += 1
            return False, "unchanged"

    install_signal_observer(Manager)
    manager = Manager()
    result = manager.process_signal(tmp_path / "missing-signal.json", set(), "hash")
    assert result == (False, "unchanged")
    assert manager.calls == 1


def test_non_core_launchers_leave_protected_sources_untouched():
    universe_launcher = (ROOT / "services/market_context/universe_main.py").read_text(
        encoding="utf-8"
    )
    execution_launcher = (
        ROOT / "services/execution_sidecar/guarded_main.py"
    ).read_text(encoding="utf-8")
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "start_background()" in universe_launcher
    assert "install_signal_observer(order_manager.OrderManager)" in execution_launcher
    assert "python -m services.market_context.universe_main" in compose


def test_missing_corrupt_stale_evidence_never_raises_or_claims_freshness(tmp_path):
    missing = capture_signal_evidence(tmp_path / "missing.json", SYMBOL, now=1000)
    assert missing["reason"] == "snapshot_missing" and missing["fresh"] is False
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{", encoding="utf-8")
    assert capture_signal_evidence(corrupt, SYMBOL)["reason"] == "snapshot_unreadable"
    stale = {
        "schema_version": 1,
        "generated_at": "1970-01-01T00:00:01Z",
        "advisory_only": True,
        "spot_only": True,
        "can_trade": False,
        "symbols": {SYMBOL: {"symbol": SYMBOL, "status": "fresh"}},
    }
    corrupt.write_text(json.dumps(stale), encoding="utf-8")
    assert capture_signal_evidence(corrupt, SYMBOL, now=1000)["fresh"] is False


def test_component_contains_no_futures_endpoint_credentials_or_order_methods():
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((ROOT / "services/market_context").glob("*.py"))
    ).lower()
    for forbidden in (
        "/fapi/",
        "futures",
        "fundingrate",
        "openinterest",
        "binance_api_key",
        "binance_api_secret",
        "create_order",
        "cancel_order",
        "leverage",
        "margin_type",
    ):
        assert forbidden not in source


def test_protected_strategy_is_not_imported_by_market_context():
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((ROOT / "services/market_context").glob("*.py"))
    )
    assert "IctSmcStrategy" not in source
    assert "legacy_core" not in source
