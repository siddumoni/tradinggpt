"""Shared timestamp rules for publication-aware research simulations."""
from __future__ import annotations

from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

IST = ZoneInfo("Asia/Kolkata")


def to_ist(value: Any, *, label: str = "timestamp") -> pd.Timestamp:
    """Parse a timestamp and normalize it to Asia/Kolkata."""
    try:
        stamp = pd.Timestamp(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"Invalid {label}: {value!r}") from exc
    if pd.isna(stamp):
        raise ValueError(f"Missing {label}")
    return stamp.tz_localize(IST) if stamp.tzinfo is None else stamp.tz_convert(IST)


def next_five_minute_bar_after(signal_time: Any, available_at: Any) -> pd.Timestamp:
    """First candle start after both signal-bar completion and signal availability.

    Candle timestamps denote their start. Publication at exactly a candle start is
    treated conservatively: that candle is already underway and cannot be filled.
    """
    signal = to_ist(signal_time, label="signal timestamp")
    available = to_ist(available_at, label="signal availability timestamp")
    signal_bar_end = signal + pd.Timedelta(minutes=5)
    first_after_publication = available.floor("5min") + pd.Timedelta(minutes=5)
    return max(signal_bar_end, first_after_publication)
