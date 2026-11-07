"""Previous-session OHLC levels and classic pivot points."""
from __future__ import annotations

from datetime import date, datetime
from math import isfinite

import pandas as pd


def previous_session_levels(
    daily_candles: pd.DataFrame,
    *,
    session_date: date | datetime | None = None,
) -> dict[str, float | str]:
    """Return the last completed daily candle and classic support/resistance pivots.

    With session_date, only rows dated strictly before the requested session are
    eligible. Without it, the legacy contract uses the penultimate row.
    """
    required = {"high", "low", "close"}
    missing = required.difference(daily_candles.columns)
    if missing:
        raise ValueError(f"Missing required columns: {sorted(missing)}")
    if not isinstance(daily_candles.index, pd.DatetimeIndex):
        raise ValueError("Daily candles require a DatetimeIndex")
    if daily_candles.index.has_duplicates:
        raise ValueError("Daily candle timestamps must be unique")
    df = daily_candles.sort_index().copy()
    if session_date is None:
        if len(df) < 2:
            raise ValueError("At least two daily candles are required")
        previous = df.iloc[-2]
        previous_timestamp = df.index[-2]
    else:
        cutoff = session_date.date() if isinstance(session_date, datetime) else session_date
        df = df.loc[[stamp.date() < cutoff for stamp in df.index]]
        if df.empty:
            raise ValueError("No completed daily candle exists before the requested session date")
        previous = df.iloc[-1]
        previous_timestamp = df.index[-1]
    try:
        high, low, close = (float(previous[name]) for name in ("high", "low", "close"))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Previous-session high, low and close must be numeric") from exc
    if (
        not all(isfinite(value) and value > 0 for value in (high, low, close))
        or high < low
        or not low <= close <= high
    ):
        raise ValueError("Invalid previous-session OHLC values")
    pivot = (high + low + close) / 3.0
    return {
        "previous_session_date": previous_timestamp.date().isoformat(),
        "previous_high": high,
        "previous_low": low,
        "previous_close": close,
        "pivot": pivot,
        "r1": 2 * pivot - low,
        "s1": 2 * pivot - high,
        "r2": pivot + (high - low),
        "s2": pivot - (high - low),
    }
