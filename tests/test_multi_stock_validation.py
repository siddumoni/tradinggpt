import numpy as np
import pandas as pd

from src.multi_stock_validation import filter_complete_sessions


def make_session(day="2026-10-12"):
    index = pd.date_range(
        f"{day} 09:15", f"{day} 15:10", freq="5min", tz="Asia/Kolkata"
    )
    return pd.DataFrame(
        {
            "open": np.full(len(index), 100.0),
            "high": np.full(len(index), 101.0),
            "low": np.full(len(index), 99.0),
            "close": np.full(len(index), 100.5),
            "volume": np.full(len(index), 1000),
        },
        index=index,
    )


def test_multi_stock_filter_keeps_only_complete_valid_session():
    complete = make_session("2026-10-12")
    incomplete = make_session("2026-10-13").drop(
        pd.Timestamp("2026-10-13 11:45", tz="Asia/Kolkata")
    )
    result, diagnostics = filter_complete_sessions(pd.concat([complete, incomplete]))

    assert len(result) == 72
    assert set(result.index.date) == {pd.Timestamp("2026-10-12").date()}
    assert diagnostics["sessions_kept"] == 1
    assert diagnostics["sessions_excluded"] == 1
    assert "11:45" in diagnostics["excluded_session_dates"]


def test_multi_stock_filter_excludes_non_finite_price_session():
    frame = make_session()
    frame.loc[pd.Timestamp("2026-10-12 10:00", tz="Asia/Kolkata"), "close"] = np.inf

    result, diagnostics = filter_complete_sessions(frame)

    assert result.empty
    assert diagnostics["sessions_kept"] == 0
    assert diagnostics["sessions_excluded"] == 1
    assert "invalid OHLCV rows" in diagnostics["excluded_session_dates"]


def test_multi_stock_filter_excludes_negative_volume_session():
    frame = make_session()
    frame.loc[pd.Timestamp("2026-10-12 10:00", tz="Asia/Kolkata"), "volume"] = -1

    result, diagnostics = filter_complete_sessions(frame)

    assert result.empty
    assert diagnostics["sessions_kept"] == 0
    assert diagnostics["sessions_excluded"] == 1
    assert "invalid OHLCV rows" in diagnostics["excluded_session_dates"]
