from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src.volume_profile import (
    VolumeProfileError,
    build_trade_volume_profile,
    build_volume_profile,
)

IST = ZoneInfo("Asia/Kolkata")


def candle(stamp, open_price, high, low, close, volume):
    return {
        "date": datetime.fromisoformat(stamp).replace(tzinfo=IST),
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    }


def test_candle_profile_conserves_volume_across_uniform_price_levels():
    rows = [
        candle("2026-11-10T09:15:00", 100.00, 100.10, 100.00, 100.05, 90),
        candle("2026-11-10T09:20:00", 100.05, 100.10, 100.00, 100.10, 60),
    ]

    profile = build_volume_profile(rows, tick_size=0.05, symbol="TEST")

    assert profile["approximate"] is True
    assert profile["exact_volume_at_price"] is False
    assert profile["method"] == "UNIFORM_OHLC_RANGE_ESTIMATE"
    assert profile["total_volume"] == pytest.approx(150)
    assert [item["price"] for item in profile["bins"]] == [100.0, 100.05, 100.1]
    assert [item["volume"] for item in profile["bins"]] == pytest.approx([50, 50, 50])
    assert profile["symbol"] == "TEST"


def test_poc_and_value_area_are_deterministic_and_cover_requested_volume():
    rows = [
        candle("2026-11-10T09:15:00", 100.0, 100.0, 100.0, 100.0, 100),
        candle("2026-11-10T09:20:00", 100.05, 100.05, 100.05, 100.05, 80),
        candle("2026-11-10T09:25:00", 100.10, 100.10, 100.10, 100.10, 20),
    ]

    profile = build_volume_profile(rows, tick_size=0.05, value_area_pct=0.70)

    assert profile["poc"]["price"] == 100.0
    assert profile["value_area"]["low"] == 100.0
    assert profile["value_area"]["high"] == 100.05
    assert profile["value_area"]["coverage_pct"] == pytest.approx(90.0)


def test_as_of_excludes_candles_that_have_not_fully_closed():
    rows = [
        candle("2026-11-10T09:15:00", 100, 100, 100, 100, 10),
        candle("2026-11-10T09:20:00", 101, 101, 101, 101, 20),
    ]

    profile = build_volume_profile(
        rows,
        tick_size=0.05,
        as_of=datetime(2026, 11, 10, 9, 24, tzinfo=IST),
    )

    assert profile["source_rows"] == 1
    assert profile["bars_excluded_incomplete"] == 1
    assert profile["total_volume"] == pytest.approx(10)


def test_naive_as_of_and_candle_times_are_interpreted_as_ist():
    rows = [candle("2026-11-10T09:15:00", 100, 100, 100, 100, 10)]

    profile = build_volume_profile(
        rows,
        tick_size=0.05,
        as_of=datetime(2026, 11, 10, 9, 20),
    )

    assert profile["session_date"] == "2026-11-10"
    assert profile["total_volume"] == pytest.approx(10)


@pytest.mark.parametrize(
    "rows, message",
    [
        ([], "At least one candle"),
        (
            [
                candle("2026-11-10T09:15:00", 100, 100, 100, 100, 10),
                candle("2026-11-10T09:15:00", 100, 100, 100, 100, 10),
            ],
            "Duplicate candle timestamp",
        ),
        (
            [
                candle("2026-11-10T09:15:00", 100, 100, 100, 100, 10),
                candle("2026-11-11T09:15:00", 100, 100, 100, 100, 10),
            ],
            "one profile per regular trading session",
        ),
        ([candle("2026-11-10T09:15:00", 100, 99, 100, 100, 10)], "Invalid OHLC range"),
        ([candle("2026-11-10T09:15:00", 100, 100, 100, 100, -1)], "non-negative"),
    ],
)
def test_invalid_candle_inputs_fail_closed(rows, message):
    with pytest.raises(VolumeProfileError, match=message):
        build_volume_profile(rows, tick_size=0.05)


@pytest.mark.parametrize("tick_size", [0, -0.05, float("nan"), float("inf")])
def test_invalid_tick_sizes_are_rejected(tick_size):
    rows = [candle("2026-11-10T09:15:00", 100, 100, 100, 100, 10)]

    with pytest.raises(VolumeProfileError, match="tick_size"):
        build_volume_profile(rows, tick_size=tick_size)


def test_trade_print_profile_is_not_called_exact_without_completeness_attestation():
    trades = [
        {"timestamp": "2026-11-10T09:15:01+05:30", "price": 100.00, "quantity": 12},
        {"timestamp": "2026-11-10T09:15:02+05:30", "price": 100.05, "quantity": 8},
        {"timestamp": "2026-11-10T09:15:03+05:30", "price": 100.00, "quantity": 5},
    ]

    partial = build_trade_volume_profile(trades, tick_size=0.05)
    complete = build_trade_volume_profile(
        trades,
        tick_size=0.05,
        complete_trade_feed_verified=True,
    )

    assert partial["approximate"] is True
    assert partial["exact_volume_at_price"] is False
    assert complete["approximate"] is False
    assert complete["exact_volume_at_price"] is True
    assert complete["poc"]["price"] == 100.0
    assert complete["poc"]["volume"] == pytest.approx(17)


def test_trade_profile_rejects_prices_off_tick_grid():
    with pytest.raises(VolumeProfileError, match="not aligned"):
        build_trade_volume_profile(
            [{"price": 100.02, "quantity": 3}],
            tick_size=0.05,
        )
