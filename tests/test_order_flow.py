from datetime import datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.order_flow import (
    OrderFlowCaptureSettings,
    OrderFlowError,
    KiteTickRecorder,
    analyze_order_flow,
    normalise_kite_tick,
    read_order_flow_events,
)

IST = ZoneInfo("Asia/Kolkata")


def stamp(value):
    return datetime.fromisoformat(value).replace(tzinfo=IST)


def kite_tick(
    last_price=100.0,
    last_quantity=10,
    last_trade_time="2026-11-11T09:20:01",
    bid=99.95,
    ask=100.0,
):
    return {
        "instrument_token": 12345,
        "exchange_timestamp": stamp("2026-11-11T09:20:01"),
        "last_trade_time": stamp(last_trade_time) if last_trade_time else None,
        "last_price": last_price,
        "last_quantity": last_quantity,
        "volume_traded": 10000,
        "depth": {
            "buy": [
                {"price": bid, "quantity": 100, "orders": 3},
                {"price": bid - 0.05, "quantity": 50, "orders": 2},
            ],
            "sell": [
                {"price": ask, "quantity": 50, "orders": 2},
                {"price": ask + 0.05, "quantity": 25, "orders": 1},
            ],
        },
    }


def test_kite_tick_normalisation_keeps_top_of_book_and_depth():
    result = normalise_kite_tick(
        "sbin",
        kite_tick(),
        received_at=stamp("2026-11-11T09:20:02"),
    )

    assert result["symbol"] == "SBIN"
    assert result["instrument_token"] == 12345
    assert result["best_bid"] == pytest.approx(99.95)
    assert result["best_ask"] == pytest.approx(100.0)
    assert result["buy_depth_quantity"] == pytest.approx(150)
    assert result["sell_depth_quantity"] == pytest.approx(75)
    assert len(result["depth_buy"]) == 2
    assert result["source"] == "KITE_WEBSOCKET_FULL_SNAPSHOT"


def test_kite_tick_without_valid_last_price_is_ignored():
    assert normalise_kite_tick("SBIN", {"last_price": None}) is None
    assert normalise_kite_tick("SBIN", {"last_price": float("nan")}) is None


def test_quote_touch_classifies_estimated_buy_and_sell_volume_and_deduplicates():
    events = [
        {
            "symbol": "SBIN",
            "timestamp": "2026-11-11T09:20:01+05:30",
            "last_trade_time": "2026-11-11T09:20:01+05:30",
            "last_price": 100.00,
            "last_quantity": 10,
            "best_bid": 99.95,
            "best_ask": 100.00,
            "buy_depth_quantity": 150,
            "sell_depth_quantity": 75,
        },
        {
            "symbol": "SBIN",
            "timestamp": "2026-11-11T09:20:02+05:30",
            "last_trade_time": "2026-11-11T09:20:01+05:30",
            "last_price": 100.00,
            "last_quantity": 10,
            "best_bid": 99.95,
            "best_ask": 100.00,
            "buy_depth_quantity": 150,
            "sell_depth_quantity": 75,
        },
        {
            "symbol": "SBIN",
            "timestamp": "2026-11-11T09:20:03+05:30",
            "last_trade_time": "2026-11-11T09:20:03+05:30",
            "last_price": 99.95,
            "last_quantity": 7,
            "best_bid": 99.95,
            "best_ask": 100.00,
            "buy_depth_quantity": 150,
            "sell_depth_quantity": 75,
        },
    ]

    result = analyze_order_flow(events)

    assert result["data_quality"] == "ESTIMATED_KITE_WEBSOCKET_SNAPSHOT"
    assert result["unique_trade_updates"] == 2
    assert result["duplicate_trade_snapshots_skipped"] == 1
    assert result["classified_buy_volume_estimate"] == pytest.approx(10)
    assert result["classified_sell_volume_estimate"] == pytest.approx(7)
    assert result["estimated_delta"] == pytest.approx(3)
    assert result["estimated_delta_pct_of_classified_volume"] == pytest.approx(100 * 3 / 17)
    assert result["mean_displayed_depth_imbalance"] == pytest.approx(1 / 3)


def test_tick_rule_handles_inside_spread_uptick_downtick_and_zero_tick_carry():
    events = [
        {
            "timestamp": "2026-11-11T09:20:01+05:30",
            "last_trade_time": "2026-11-11T09:20:01+05:30",
            "last_price": 100.00,
            "last_quantity": 5,
            "best_bid": 99.95,
            "best_ask": 100.05,
        },
        {
            "timestamp": "2026-11-11T09:20:02+05:30",
            "last_trade_time": "2026-11-11T09:20:02+05:30",
            "last_price": 100.05,
            "last_quantity": 7,
            "best_bid": 100.00,
            "best_ask": 100.10,
        },
        {
            "timestamp": "2026-11-11T09:20:03+05:30",
            "last_trade_time": "2026-11-11T09:20:03+05:30",
            "last_price": 100.05,
            "last_quantity": 3,
            "best_bid": 100.00,
            "best_ask": 100.10,
        },
        {
            "timestamp": "2026-11-11T09:20:04+05:30",
            "last_trade_time": "2026-11-11T09:20:04+05:30",
            "last_price": 99.95,
            "last_quantity": 4,
            "best_bid": 99.90,
            "best_ask": 100.00,
        },
    ]

    result = analyze_order_flow(events)

    assert result["unclassified_volume"] == pytest.approx(5)
    assert result["classified_buy_volume_estimate"] == pytest.approx(10)
    assert result["classified_sell_volume_estimate"] == pytest.approx(4)
    assert result["classification_counts"]["UPTICK_ESTIMATE"] == 1
    assert result["classification_counts"]["ZERO_TICK_CARRY_ESTIMATE"] == 1
    assert result["classification_counts"]["DOWNTICK_ESTIMATE"] == 1


def test_trade_updates_without_trade_timestamp_are_not_counted_as_executed_volume():
    events = [
        {
            "timestamp": "2026-11-11T09:20:01+05:30",
            "last_price": 100.0,
            "last_quantity": 50,
            "best_bid": 99.95,
            "best_ask": 100.0,
        },
    ]

    result = analyze_order_flow(events)

    assert result["unique_trade_updates"] == 0
    assert result["trade_updates_missing_last_trade_time"] == 1
    assert result["classified_buy_volume_estimate"] == 0
    assert result["unclassified_volume"] == 0


def test_an_explicit_aggressor_side_is_supported_but_still_snapshot_labeled():
    events = [
        {
            "symbol": "RELIANCE",
            "timestamp": "2026-11-11T09:20:01+05:30",
            "last_trade_time": "2026-11-11T09:20:01+05:30",
            "last_price": 100,
            "last_quantity": 25,
            "aggressor_side": "BUY",
        },
    ]

    result = analyze_order_flow(events)

    assert result["classified_buy_volume_estimate"] == pytest.approx(25)
    assert result["classification_counts"]["PROVIDED_AGGRESSOR_SIDE"] == 1
    assert result["data_quality"] == "ESTIMATED_KITE_WEBSOCKET_SNAPSHOT"


def test_recorder_writes_jsonl_and_drains_queue_on_shutdown(tmp_path):
    settings = OrderFlowCaptureSettings(
        enabled=True,
        directory=tmp_path,
        queue_size=10,
        batch_size=2,
        flush_interval_seconds=0.1,
    )
    recorder = KiteTickRecorder(settings)
    recorder.start()

    assert recorder.submit(
        kite_tick(),
        symbol="SBIN",
        received_at=stamp("2026-11-11T09:20:02"),
    )
    stats = recorder.stop(timeout_seconds=2)

    target = tmp_path / "2026-11-11" / "SBIN.jsonl.gz"
    rows = read_order_flow_events(target)

    assert stats["accepted"] == 1
    assert stats["written"] == 1
    assert stats["pending"] == 0
    assert stats["writer_alive"] is False
    assert rows[0]["symbol"] == "SBIN"
    assert rows[0]["last_price"] == pytest.approx(100.0)



def test_mixed_symbol_snapshots_are_rejected_to_prevent_cross_symbol_tick_rules():
    events = [
        {
            "symbol": "SBIN",
            "timestamp": "2026-11-11T09:20:00+05:30",
            "last_trade_time": "2026-11-11T09:20:00+05:30",
            "last_price": 100.0,
            "last_quantity": 10,
        },
        {
            "symbol": "INFY",
            "timestamp": "2026-11-11T09:20:01+05:30",
            "last_trade_time": "2026-11-11T09:20:01+05:30",
            "last_price": 1500.0,
            "last_quantity": 10,
        },
    ]

    with pytest.raises(OrderFlowError, match="one symbol at a time"):
        analyze_order_flow(events)



def test_jsonl_reader_reports_malformed_line(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_text('{"ok": true}\nnot-json\n', encoding="utf-8")

    with pytest.raises(OrderFlowError, match="line 2"):
        read_order_flow_events(path)


@pytest.mark.parametrize("depth_levels", [0, -1])
def test_depth_level_count_must_be_positive(depth_levels):
    with pytest.raises(OrderFlowError, match="depth_levels"):
        analyze_order_flow([{"timestamp": "2026-11-11T09:20:00", "last_price": 100}], depth_levels=depth_levels)



def test_depth_level_limit_uses_only_the_requested_number_of_levels():
    events = [
        {
            "timestamp": "2026-11-11T09:20:00+05:30",
            "last_price": 100.0,
            "depth_buy": [
                {"price": 100.00, "quantity": 100},
                {"price": 99.95, "quantity": 50},
                {"price": 99.90, "quantity": 25},
            ],
            "depth_sell": [
                {"price": 100.05, "quantity": 50},
                {"price": 100.10, "quantity": 25},
                {"price": 100.15, "quantity": 15},
            ],
            # Precomputed totals cover all five levels and must not override
            # an explicit, narrower depth_levels argument.
            "buy_depth_quantity": 175,
            "sell_depth_quantity": 90,
        }
    ]

    result = analyze_order_flow(events, depth_levels=1)

    assert result["latest_displayed_bid_quantity"] == pytest.approx(100)
    assert result["latest_displayed_ask_quantity"] == pytest.approx(50)
    assert result["latest_displayed_depth_imbalance"] == pytest.approx(1 / 3)


def test_empty_order_flow_input_fails_closed():
    with pytest.raises(OrderFlowError, match="At least one"):
        analyze_order_flow([])
