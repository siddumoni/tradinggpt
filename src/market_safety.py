"""Fail-closed market microstructure checks for advisory and paper scans."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time as dtime
from math import isfinite
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import yaml

IST = ZoneInfo("Asia/Kolkata")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SAFETY_PATH = PROJECT_ROOT / "config" / "market_safety.yaml"


@dataclass(frozen=True)
class MarketSafetySettings:
    circuit_near_band_buffer_pct: float = 0.5
    relative_volume_lookback_sessions: int = 20
    relative_volume_minimum_valid_sessions: int = 10
    relative_volume_minimum_ratio: float = 1.0
    relative_volume_lookback_calendar_days: int = 45


def load_market_safety_settings(path: Path = DEFAULT_SAFETY_PATH) -> MarketSafetySettings:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise ValueError(f"Could not read market-safety settings {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("Market-safety settings must be a YAML mapping")
    circuit = raw.get("circuit_limits", {})
    relative = raw.get("relative_volume", {})
    if not isinstance(circuit, dict) or not isinstance(relative, dict):
        raise ValueError("circuit_limits and relative_volume settings must be mappings")
    try:
        buffer_pct = float(circuit.get("near_band_buffer_pct", 0.5))
        lookback = int(relative.get("lookback_sessions", 20))
        minimum_sessions = int(relative.get("minimum_valid_sessions", 10))
        minimum_ratio = float(relative.get("minimum_ratio", 1.0))
        calendar_days = int(relative.get("lookback_calendar_days", 45))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Market-safety thresholds must be numeric") from exc
    if not isfinite(buffer_pct) or buffer_pct < 0:
        raise ValueError("circuit near-band buffer must be finite and non-negative")
    if lookback < 1 or minimum_sessions < 1 or minimum_sessions > lookback:
        raise ValueError("relative-volume session counts are invalid")
    if not isfinite(minimum_ratio) or minimum_ratio <= 0:
        raise ValueError("relative-volume minimum ratio must be finite and positive")
    if calendar_days < lookback:
        raise ValueError("relative-volume calendar lookback must be at least the session lookback")
    return MarketSafetySettings(
        circuit_near_band_buffer_pct=buffer_pct,
        relative_volume_lookback_sessions=lookback,
        relative_volume_minimum_valid_sessions=minimum_sessions,
        relative_volume_minimum_ratio=minimum_ratio,
        relative_volume_lookback_calendar_days=calendar_days,
    )


def inspect_circuit_limits(quote: Any, *, buffer_pct: float = 0.5) -> dict[str, Any]:
    """Require valid exchange band data and reject prices within buffer of either band."""
    if not isfinite(float(buffer_pct)) or buffer_pct < 0:
        raise ValueError("buffer_pct must be finite and non-negative")
    if not isinstance(quote, dict):
        return {"eligible": False, "status": "QUOTE_MISSING", "reason": "Quote is missing."}
    try:
        price = float(quote.get("last_price"))
        lower = float(quote.get("lower_circuit_limit"))
        upper = float(quote.get("upper_circuit_limit"))
    except (TypeError, ValueError, OverflowError):
        return {
            "eligible": False,
            "status": "CIRCUIT_LIMITS_MISSING",
            "reason": "Last price and both exchange circuit limits are required.",
        }
    if not all(isfinite(value) and value > 0 for value in (price, lower, upper)) or lower >= upper:
        return {
            "eligible": False,
            "status": "CIRCUIT_LIMITS_INVALID",
            "reason": "Last price and circuit limits must be finite, positive, and lower must be below upper.",
        }
    if price < lower or price > upper:
        return {
            "eligible": False,
            "status": "PRICE_OUTSIDE_CIRCUIT_BAND",
            "price": price,
            "lower_circuit_limit": lower,
            "upper_circuit_limit": upper,
            "reason": "Last price is outside the supplied exchange circuit band.",
        }
    lower_distance = (price - lower) / price * 100.0
    upper_distance = (upper - price) / price * 100.0
    if upper_distance <= buffer_pct:
        return {
            "eligible": False,
            "status": "NEAR_UPPER_CIRCUIT",
            "price": price,
            "lower_circuit_limit": lower,
            "upper_circuit_limit": upper,
            "distance_to_upper_pct": round(upper_distance, 4),
            "distance_to_lower_pct": round(lower_distance, 4),
            "reason": f"Price is within {buffer_pct:.2f}% of the upper circuit limit.",
        }
    if lower_distance <= buffer_pct:
        return {
            "eligible": False,
            "status": "NEAR_LOWER_CIRCUIT",
            "price": price,
            "lower_circuit_limit": lower,
            "upper_circuit_limit": upper,
            "distance_to_upper_pct": round(upper_distance, 4),
            "distance_to_lower_pct": round(lower_distance, 4),
            "reason": f"Price is within {buffer_pct:.2f}% of the lower circuit limit.",
        }
    return {
        "eligible": True,
        "status": "CLEAR",
        "price": price,
        "lower_circuit_limit": lower,
        "upper_circuit_limit": upper,
        "distance_to_upper_pct": round(upper_distance, 4),
        "distance_to_lower_pct": round(lower_distance, 4),
        "reason": "",
    }


def time_of_day_relative_volume(
    current_candles: pd.DataFrame,
    historical_candles: pd.DataFrame,
    *,
    lookback_sessions: int = 20,
    minimum_valid_sessions: int = 10,
    minimum_ratio: float = 1.0,
) -> dict[str, Any]:
    """Compare cumulative volume to same-clock-time prefixes of prior full sessions.

    Only prior dates are considered. Current and historical timestamp prefixes
    must match exactly; gaps are never filled or interpolated.
    """
    if lookback_sessions < 1 or minimum_valid_sessions < 1 or minimum_valid_sessions > lookback_sessions:
        raise ValueError("relative-volume session counts are invalid")
    if not isfinite(float(minimum_ratio)) or minimum_ratio <= 0:
        raise ValueError("minimum_ratio must be finite and positive")

    def result(status: str, reason: str, **values: Any) -> dict[str, Any]:
        return {"eligible": False, "status": status, "reason": reason, **values}

    if current_candles is None or current_candles.empty:
        return result("CURRENT_SESSION_MISSING", "Current-session 5-minute candles are unavailable.")
    if historical_candles is None or historical_candles.empty:
        return result("HISTORICAL_SESSIONS_MISSING", "Prior-session 5-minute history is unavailable.")

    current = current_candles.copy().sort_index()
    if not isinstance(current.index, pd.DatetimeIndex) or current.index.tz is None:
        return result("CURRENT_TIMESTAMPS_INVALID", "Current-session timestamps must be timezone-aware.")
    current.index = current.index.tz_convert(IST)
    session_day = current.index[-1].date()
    session = current.loc[[stamp.date() == session_day for stamp in current.index]]
    last_stamp = session.index[-1]
    start = datetime.combine(session_day, dtime(9, 15), tzinfo=IST)
    expected = pd.date_range(start=start, end=last_stamp, freq="5min")
    if last_stamp < start or not session.index.equals(expected):
        return result(
            "INCOMPLETE_CURRENT_SESSION",
            "Current-session 5-minute bars do not form a complete 09:15-to-latest-bar sequence.",
            last_completed_bar=last_stamp.isoformat(),
        )
    if "volume" not in session.columns:
        return result("CURRENT_VOLUME_MISSING", "Current-session volume column is missing.")
    current_volume = float(pd.to_numeric(session["volume"], errors="coerce").sum())
    if not isfinite(current_volume) or current_volume < 0:
        return result("CURRENT_VOLUME_INVALID", "Current-session cumulative volume is invalid.")

    history = historical_candles.copy().sort_index()
    if not isinstance(history.index, pd.DatetimeIndex) or history.index.tz is None:
        return result("HISTORICAL_TIMESTAMPS_INVALID", "Historical timestamps must be timezone-aware.")
    history.index = history.index.tz_convert(IST)
    if "volume" not in history.columns:
        return result("HISTORICAL_VOLUME_MISSING", "Historical volume column is missing.")

    valid_sessions: list[tuple[date, float]] = []
    history_days = sorted({stamp.date() for stamp in history.index if stamp.date() < session_day}, reverse=True)
    for day in history_days:
        rows = history.loc[[stamp.date() == day for stamp in history.index]]
        prefix = rows.loc[rows.index.time <= last_stamp.time()]
        if not prefix.index.equals(pd.date_range(
            start=datetime.combine(day, dtime(9, 15), tzinfo=IST),
            end=datetime.combine(day, last_stamp.time(), tzinfo=IST),
            freq="5min",
        )):
            continue
        volume = float(pd.to_numeric(prefix["volume"], errors="coerce").sum())
        if isfinite(volume) and volume > 0:
            valid_sessions.append((day, volume))
        if len(valid_sessions) >= lookback_sessions:
            break

    if len(valid_sessions) < minimum_valid_sessions:
        return result(
            "INSUFFICIENT_HISTORY",
            f"Only {len(valid_sessions)} complete comparable prior sessions are available; {minimum_valid_sessions} are required.",
            sessions_used=len(valid_sessions),
            minimum_valid_sessions=minimum_valid_sessions,
            current_cumulative_volume=current_volume,
            last_completed_bar=last_stamp.isoformat(),
        )
    volumes = [item[1] for item in valid_sessions]
    baseline = float(pd.Series(volumes, dtype="float64").median())
    if not isfinite(baseline) or baseline <= 0:
        return result("HISTORICAL_BASELINE_INVALID", "Historical median cumulative volume is invalid.")
    ratio = current_volume / baseline
    eligible = ratio >= minimum_ratio
    return {
        "eligible": eligible,
        "status": "PASSED" if eligible else "BELOW_MINIMUM",
        "reason": "" if eligible else f"Relative volume {ratio:.2f}x is below the required {minimum_ratio:.2f}x.",
        "ratio": round(ratio, 4),
        "current_cumulative_volume": round(current_volume, 2),
        "historical_median_cumulative_volume": round(baseline, 2),
        "sessions_used": len(valid_sessions),
        "minimum_valid_sessions": minimum_valid_sessions,
        "minimum_ratio": minimum_ratio,
        "lookback_sessions": lookback_sessions,
        "last_completed_bar": last_stamp.isoformat(),
        "baseline_method": "median of prior complete same-clock-time cumulative-volume prefixes",
    }
