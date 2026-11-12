"""Research-only diagnostics for opening-range breakout quality."""
import pandas as pd

from src.strategies.opening_range import opening_range_quality_diagnostics, opening_range_signal


def make_session():
    index = pd.date_range("2026-11-12 09:15", periods=5, freq="5min", tz="Asia/Kolkata")
    return pd.DataFrame(
        {
            "open": [100.0, 100.0, 100.0, 101.0, 102.0],
            "high": [101.0, 102.0, 103.0, 104.0, 105.0],
            "low": [99.0, 99.0, 99.0, 100.0, 101.0],
            "close": [100.0, 101.0, 102.0, 103.0, 104.5],
            "volume": [1000, 1000, 1000, 2000, 3000],
        },
        index=index,
    )


def test_strong_breakout_quality_is_pass():
    frame = make_session()
    result = opening_range_quality_diagnostics(frame, 103.0, 99.0, frame.iloc[-1], "LONG")
    assert result["status"] == "PASS"
    assert result["flags"] == []
    assert result["research_only_non_blocking"] is True


def test_overextended_breakout_is_flagged_but_signal_is_not_blocked():
    frame = make_session()
    frame.iloc[-1, frame.columns.get_loc("open")] = 104.0
    frame.iloc[-1, frame.columns.get_loc("high")] = 110.0
    frame.iloc[-1, frame.columns.get_loc("low")] = 103.5
    frame.iloc[-1, frame.columns.get_loc("close")] = 109.5
    result = opening_range_quality_diagnostics(frame, 103.0, 99.0, frame.iloc[-1], "LONG")
    assert result["status"] == "REVIEW"
    assert "OVEREXTENDED_FROM_OPENING_RANGE" in result["flags"]
    signal = opening_range_signal("TEST", frame, market_regime="BULLISH")
    assert signal is not None
    assert signal.trigger_price == 103.0
    assert "research-only quality=REVIEW" in signal.reason


def test_failed_prior_breakout_is_flagged_for_research():
    index = pd.date_range("2026-11-12 09:15", periods=6, freq="5min", tz="Asia/Kolkata")
    frame = pd.DataFrame(
        {
            "open": [100.0, 100.0, 100.0, 103.0, 101.0, 103.0],
            "high": [101.0, 102.0, 103.0, 105.0, 104.0, 105.0],
            "low": [99.0, 99.0, 99.0, 102.0, 100.0, 102.0],
            "close": [100.0, 101.0, 102.0, 104.0, 102.0, 104.5],
            "volume": [1000, 1000, 1000, 2500, 2200, 3000],
        },
        index=index,
    )
    result = opening_range_quality_diagnostics(frame, 103.0, 99.0, frame.iloc[-1], "LONG")
    assert result["prior_failed_breakout"] is True
    assert "PRIOR_BREAKOUT_FAILED_RETRY" in result["flags"]
    signal = opening_range_signal("TEST", frame, market_regime="BULLISH")
    assert signal is not None
    assert signal.trigger_price == 103.0
