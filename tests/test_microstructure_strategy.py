from datetime import timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from src.microstructure_strategy import (
    MicrostructureSettings,
    generate_microstructure_signals,
)

IST = ZoneInfo("Asia/Kolkata")


def _candles():
    index = pd.date_range("2026-11-12 09:15", periods=40, freq="5min", tz=IST)
    rows = {
        "open": [100.0] * 39 + [99.96],
        "high": [100.10] * 39 + [100.05],
        "low": [99.90] * 39 + [99.90],
        "close": [100.0] * 39 + [100.04],
        "volume": [1000.0] * 39 + [1200.0],
    }
    return pd.DataFrame(rows, index=index)


def _flow_events(bar_start, count=20):
    events = []
    for second in range(1, count + 1):
        stamp = bar_start + timedelta(seconds=second)
        events.append({
            "symbol": "SBIN",
            "timestamp": stamp.isoformat(),
            "last_trade_time": stamp.isoformat(),
            "last_price": 100.05,
            "last_quantity": 10,
            "best_bid": 100.00,
            "best_ask": 100.05,
            "depth_buy": [{"price": 100.00, "quantity": 200}],
            "depth_sell": [{"price": 100.05, "quantity": 100}],
        })
    return events


def test_microstructure_profile_is_built_from_bars_before_signal_bar(monkeypatch):
    import src.microstructure_strategy as strategy

    frame = _candles()
    signal_stamp = frame.index[-1].to_pydatetime()
    captured = {}

    def fake_profile(rows, **kwargs):
        captured["last_profile_timestamp"] = rows[-1]["date"]
        captured["source_rows"] = len(rows)
        assert kwargs["as_of"] == signal_stamp
        return {
            "value_area": {"low": 99.95, "high": 100.15},
            "poc": {"price": 100.05},
            "method": "UNIFORM_OHLC_RANGE_ESTIMATE",
        }

    monkeypatch.setattr(strategy, "build_volume_profile", fake_profile)
    settings = MicrostructureSettings(
        minimum_profile_bars=12,
        minimum_bucket_snapshots=15,
        minimum_trade_updates=5,
        minimum_book_imbalance=0.03,
    )

    signals = generate_microstructure_signals(
        "SBIN",
        frame,
        _flow_events(signal_stamp),
        tick_size=0.05,
        settings=settings,
    )

    assert captured["source_rows"] == 39
    assert captured["last_profile_timestamp"] == frame.index[-2].isoformat()
    assert all(item["signal_time"] == signal_stamp.isoformat() for item in signals)
    assert any(item["strategy_version"] == "micro_vp_value_rejection_ofi_v1" for item in signals)
    assert all("approximate" in item["reason"].lower() for item in signals)


def test_microstructure_flow_must_match_the_completed_signal_bar():
    frame = _candles()
    signal_stamp = frame.index[-1].to_pydatetime()
    prior_bucket_events = _flow_events(signal_stamp - timedelta(minutes=5))
    settings = MicrostructureSettings(
        minimum_profile_bars=12,
        minimum_bucket_snapshots=15,
        minimum_trade_updates=5,
    )

    result = generate_microstructure_signals(
        "SBIN",
        frame,
        prior_bucket_events,
        tick_size=0.05,
        settings=settings,
    )

    assert result == []


def test_microstructure_rejects_sparse_or_missing_order_flow():
    frame = _candles()
    signal_stamp = frame.index[-1].to_pydatetime()
    settings = MicrostructureSettings(
        minimum_profile_bars=12,
        minimum_bucket_snapshots=15,
        minimum_trade_updates=5,
    )

    result = generate_microstructure_signals(
        "SBIN",
        frame,
        _flow_events(signal_stamp, count=4),
        tick_size=0.05,
        settings=settings,
    )

    assert result == []
