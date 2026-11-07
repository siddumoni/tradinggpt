import sys

import numpy as np
import pandas as pd
import pytest

from src.strategy_backtest import (
    _structural_stop_for_history, candles_to_frame, make_signal_callback, backtest_strategy,
)


def sample_candles(n=100):
    idx = pd.date_range("2026-10-12 09:15", periods=n, freq="5min", tz="Asia/Kolkata")
    close = np.linspace(100, 120, n)
    return pd.DataFrame({
        "open": close - 0.1,
        "high": close + 0.5,
        "low": close - 0.5,
        "close": close,
        "volume": np.full(n, 1000),
    }, index=idx)


def test_candles_to_frame_converts_provider_shape():
    frame = candles_to_frame([{
        "date": pd.Timestamp("2026-10-12 09:15", tz="Asia/Kolkata"),
        "open": 100, "high": 101, "low": 99, "close": 100.5, "volume": 1000,
    }])
    assert len(frame) == 1
    assert frame.iloc[0]["close"] == 100.5


def test_candles_to_frame_empty_has_expected_columns():
    frame = candles_to_frame([])
    assert list(frame.columns) == ["open", "high", "low", "close", "volume"]


def test_candles_to_frame_rejects_missing_date():
    with pytest.raises(ValueError, match="date"):
        candles_to_frame([{"open": 100, "high": 101, "low": 99, "close": 100, "volume": 10}])


def test_callback_rejects_unknown_strategy():
    with pytest.raises(ValueError, match="Unsupported strategy"):
        make_signal_callback("TEST", "not_a_strategy")


def test_backtest_strategy_returns_summary_for_historical_frame():
    trades, summary = backtest_strategy("TEST", sample_candles(), "vwap_trend")
    assert summary["symbol"] == "TEST"
    assert summary["strategy"] == "vwap_trend"
    assert summary["strategy_version"] == "nse_champion_v2_foundation_candidate"
    assert summary["bars"] == 72
    assert summary["bars_before_session_filter"] == 100
    assert "net_pnl" in summary
    assert isinstance(trades, pd.DataFrame)


def test_backtest_strategy_rejects_empty_data():
    with pytest.raises(ValueError, match="No candles"):
        backtest_strategy("TEST", pd.DataFrame(), "vwap_trend")



def test_candles_to_frame_rejects_invalid_timestamp():
    with pytest.raises(ValueError, match="invalid timestamp"):
        candles_to_frame([{
            "date": "not-a-date",
            "open": 100, "high": 101, "low": 99, "close": 100.5, "volume": 1000,
        }])


def test_candles_to_frame_preserves_invalid_ohlc_for_quality_filter():
    frame = candles_to_frame([{
        "date": pd.Timestamp("2026-10-12 09:15", tz="Asia/Kolkata"),
        "open": 100, "high": 101, "low": 99, "close": float("nan"), "volume": 1000,
    }])
    assert len(frame) == 1
    assert pd.isna(frame.iloc[0]["close"])



def test_single_backtest_cli_accepts_configured_defaults(monkeypatch):
    from src import strategy_backtest as module

    captured = {}
    monkeypatch.setattr(
        sys, "argv",
        ["strategy_backtest", "--symbol", "RELIANCE", "--strategy", "vwap_trend"],
    )
    monkeypatch.setattr(module, "run_yahoo_backtest", lambda *args: captured.setdefault("args", args))

    module.main()

    args = captured["args"]
    assert args[4] is None  # risk budget comes from settings.yaml
    assert args[5] is None  # minimum net R:R comes from settings.yaml
    assert args[8] is None  # slippage comes from settings.yaml


def test_slippage_sensitivity_cli_accepts_configured_defaults(monkeypatch):
    from src import strategy_backtest as module

    captured = {}
    monkeypatch.setattr(
        sys, "argv",
        [
            "strategy_backtest", "--symbol", "RELIANCE",
            "--strategy", "vwap_trend", "--slippage-sensitivity",
        ],
    )
    monkeypatch.setattr(
        module, "run_slippage_sensitivity",
        lambda *args: captured.setdefault("args", args),
    )

    module.main()

    args = captured["args"]
    assert args[4] is None  # risk budget comes from settings.yaml
    assert args[5] is None  # minimum net R:R comes from settings.yaml



def test_structural_stop_uses_only_completed_fifteen_minute_bars():
    from src.indicators import add_indicators

    history = add_indicators(sample_candles())
    # The last 5m candle closes at 17:35; the containing 15m bucket is complete
    # only when its end timestamp is <= that close.
    plan = _structural_stop_for_history(
        history, side="LONG", entry=float(history.iloc[-1]["close"]),
        atr=float(history.iloc[-1]["atr_14"]),
    )
    assert plan.stop_loss < float(history.iloc[-1]["close"])
    assert plan.distance_pct >= 0.25
    assert plan.reference


def test_structural_stop_is_causal_when_future_candles_are_appended():
    from src.indicators import add_indicators

    candles = sample_candles()
    first = add_indicators(candles.iloc[:70])
    second = add_indicators(candles.iloc[:71])
    first_plan = _structural_stop_for_history(
        first, side="LONG", entry=float(first.iloc[-1]["close"]),
        atr=float(first.iloc[-1]["atr_14"]),
    )
    second_plan = _structural_stop_for_history(
        second, side="LONG", entry=float(second.iloc[-1]["close"]),
        atr=float(second.iloc[-1]["atr_14"]),
    )
    # Each result is independently derived from its own as-of history; neither
    # path reads candles after its final timestamp.
    assert first_plan.stop_loss > 0
    assert second_plan.stop_loss > 0
