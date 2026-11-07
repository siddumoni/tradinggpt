"""Previous-session OHLC levels and classic pivot points."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from math import isfinite

import pandas as pd

from src.market_calendar import IST, market_day_status


def _previous_normal_session(day: date) -> date:
    """Resolve the previous normal NSE session from the verified calendar."""
    candidate = day - timedelta(days=1)
    for _ in range(15):
        status = market_day_status(candidate)
        if status["status"] == "CALENDAR_OUT_OF_COVERAGE":
            raise ValueError(
                f"Cannot verify previous NSE session for {day}: calendar coverage stops before {candidate}"
            )
        if status["is_open"] is True:
            return candidate
        candidate -= timedelta(days=1)
    raise ValueError(f"Could not resolve the previous normal NSE session before {day}")


def previous_session_levels(
    daily_candles: pd.DataFrame,
    *,
    session_date: date | datetime | None = None,
) -> dict[str, float | str]:
    """Return the exact previous normal session OHLC and classic pivots.

    With session_date, the latest prior daily row must match the previous
    normal NSE session from the verified calendar. The current/forming daily
    candle is excluded and missing prior-session data fails closed. Without
    session_date, the legacy penultimate-row contract is retained.
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
    if df.index.tz is None:
        df.index = df.index.tz_localize(IST)
    else:
        df.index = df.index.tz_convert(IST)
    if session_date is None:
        if len(df) < 2:
            raise ValueError("At least two daily candles are required")
        previous = df.iloc[-2]
        previous_timestamp = df.index[-2]
    else:
        cutoff = (
            session_date.astimezone(IST).date()
            if isinstance(session_date, datetime) and session_date.tzinfo
            else session_date.date() if isinstance(session_date, datetime)
            else session_date
        )
        expected_date = _previous_normal_session(cutoff)
        previous_session_rows = df.loc[[stamp.date() == expected_date for stamp in df.index]]
        if previous_session_rows.empty:
            raise ValueError(
                f"Daily candle for previous NSE session {expected_date} is missing; "
                "no older session is substituted."
            )
        previous = previous_session_rows.iloc[-1]
        previous_timestamp = previous_session_rows.index[-1]
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