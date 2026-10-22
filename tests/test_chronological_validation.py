import pandas as pd
import pytest

from src.chronological_validation import make_json_safe, split_by_session


def frame_with_sessions(count: int) -> pd.DataFrame:
    dates = pd.bdate_range("2026-08-03", periods=count)
    stamps = pd.DatetimeIndex([
        pd.Timestamp(f"{day.date()} 09:15", tz="Asia/Kolkata")
        for day in dates
    ])
    return pd.DataFrame({"close": [100.0] * len(stamps)}, index=stamps)


def test_chronological_split_requires_minimum_sessions_in_both_partitions():
    frame = frame_with_sessions(15)
    with pytest.raises(ValueError, match="at least 10 complete sessions"):
        split_by_session(frame, 0.70)


def test_chronological_split_keeps_sessions_in_time_order():
    frame = frame_with_sessions(34)
    test_start, test_end, train_sessions, test_sessions = split_by_session(frame, 0.70)

    assert len(train_sessions) >= 10
    assert len(test_sessions) >= 10
    assert max(train_sessions) < min(test_sessions)
    assert test_start.date() == min(test_sessions)
    assert test_end.date() == max(test_sessions) + pd.Timedelta(days=1)


def test_non_finite_json_values_are_serialized_as_null():
    assert make_json_safe(float("inf")) is None
    assert make_json_safe({"metrics": [float("nan"), 2.0]}) == {"metrics": [None, 2.0]}
