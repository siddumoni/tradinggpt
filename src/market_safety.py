"""Fail-closed market microstructure checks for advisory and paper scans."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time as dtime
from math import isfinite
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
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
    previous_day_level_buffer_atr: float = 0.3


def load_market_safety_settings(path: Path = DEFAULT_SAFETY_PATH) -> MarketSafetySettings:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise ValueError(f"Could not read market-safety settings {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("Market-safety settings must be a YAML mapping")
    circuit = raw.get("circuit_limits", {})
    relative = raw.get("relative_volume", {})
    previous_levels = raw.get("previous_day_levels", {})
    if not isinstance(circuit, dict) or not isinstance(relative, dict) or not isinstance(previous_levels, dict):
        raise ValueError("circuit_limits, relative_volume and previous_day_levels settings must be mappings")
    try:
        buffer_pct = float(circuit.get("near_band_buffer_pct", 0.5))
        lookback = int(relative.get("lookback_sessions", 20))
        minimum_sessions = int(relative.get("minimum_valid_sessions", 10))
        minimum_ratio = float(relative.get("minimum_ratio", 1.0))
        calendar_days = int(relative.get("lookback_calendar_days", 45))
        level_buffer_atr = float(previous_levels.get("proximity_buffer_atr", 0.3))
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
    if not isfinite(level_buffer_atr) or level_buffer_atr <= 0:
        raise ValueError("previous-day level ATR buffer must be finite and positive")
    return MarketSafetySettings(
        circuit_near_band_buffer_pct=buffer_pct,
        relative_volume_lookback_sessions=lookback,
        relative_volume_minimum_valid_sessions=minimum_sessions,
        relative_volume_minimum_ratio=minimum_ratio,
        relative_volume_lookback_calendar_days=calendar_days,
        previous_day_level_buffer_atr=level_buffer_atr,
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


DEFAULT_SURVEILLANCE_SNAPSHOT = PROJECT_ROOT / ".local" / "nse_surveillance.json"
SURVEILLANCE_MAX_AGE_DAYS = 3
SUPPORTED_SURVEILLANCE_MEASURES = {
    "ASM", "GSM", "ESM", "TRADE_TO_TRADE", "PERIODIC_CALL_AUCTION",
}


def load_surveillance_snapshot(
    path: Path = DEFAULT_SURVEILLANCE_SNAPSHOT,
    *,
    as_of: date | datetime | None = None,
    max_age_days: int = SURVEILLANCE_MAX_AGE_DAYS,
) -> dict[str, Any]:
    """Load an optional, manually refreshed NSE surveillance snapshot.

    The file is intentionally not synthesized from Kite instrument rows: those
    rows do not expose ASM/GSM/ESM classifications. Missing or stale evidence
    remains unavailable and is never interpreted as a clean surveillance status.
    """
    current_day = (
        as_of.astimezone(IST).date() if isinstance(as_of, datetime) and as_of.tzinfo
        else as_of.date() if isinstance(as_of, datetime)
        else as_of if isinstance(as_of, date)
        else datetime.now(IST).date()
    )
    if max_age_days < 0:
        raise ValueError("max_age_days must be non-negative")
    if not path.exists():
        return {
            "status": "NOT_AVAILABLE",
            "eligible": None,
            "reason": f"NSE surveillance snapshot is not available at {path}.",
            "source": None,
            "as_of": None,
        }
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return {
            "status": "INVALID",
            "eligible": None,
            "reason": f"NSE surveillance snapshot could not be read: {type(exc).__name__}.",
            "source": None,
            "as_of": None,
        }
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        return {
            "status": "INVALID",
            "eligible": None,
            "reason": "NSE surveillance snapshot schema_version must be 1 and content must be a mapping.",
            "source": None,
            "as_of": None,
        }
    source_url = str(raw.get("source_url") or "")
    parsed_url = urlparse(source_url)
    if parsed_url.scheme != "https" or parsed_url.hostname not in {"nseindia.com", "www.nseindia.com"}:
        return {
            "status": "INVALID",
            "eligible": None,
            "reason": "Surveillance snapshot source_url must be an HTTPS NSE India URL.",
            "source": raw.get("source"),
            "as_of": raw.get("as_of"),
        }
    try:
        snapshot_date = date.fromisoformat(str(raw.get("as_of") or ""))
    except (TypeError, ValueError):
        return {
            "status": "INVALID",
            "eligible": None,
            "reason": "Surveillance snapshot as_of must be an ISO calendar date.",
            "source": raw.get("source"),
            "as_of": raw.get("as_of"),
        }
    if snapshot_date > current_day:
        status = "INVALID"
        reason = "Surveillance snapshot date is in the future."
    elif (current_day - snapshot_date).days > max_age_days:
        status = "STALE"
        reason = (
            f"Surveillance snapshot is {(current_day - snapshot_date).days} calendar days old; "
            f"maximum allowed age is {max_age_days}."
        )
    elif raw.get("complete") is not True:
        status = "INCOMPLETE"
        reason = "Surveillance snapshot is not explicitly marked complete."
    elif not isinstance(raw.get("symbols"), dict):
        status = "INVALID"
        reason = "Surveillance snapshot symbols must be a mapping."
    else:
        status = "VALID"
        reason = ""
    return {
        "status": status,
        "eligible": True if status == "VALID" else None,
        "reason": reason,
        "source": raw.get("source") or "NSE India",
        "source_url": source_url,
        "as_of": snapshot_date.isoformat(),
        "complete": raw.get("complete") is True,
        "symbols": raw.get("symbols") if isinstance(raw.get("symbols"), dict) else {},
        "snapshot_date": snapshot_date.isoformat(),
    }


def surveillance_status_for_symbol(
    symbol: str,
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Return an explicit surveillance state; unknown evidence is not a clean bill."""
    status = str(snapshot.get("status") or "NOT_AVAILABLE").upper()
    if status != "VALID":
        return {
            "eligible": None,
            "status": status,
            "symbol": str(symbol).strip().upper(),
            "as_of": snapshot.get("as_of"),
            "reason": str(snapshot.get("reason") or "Surveillance classification is unavailable."),
        }
    symbol_key = str(symbol).strip().upper()
    symbols = snapshot.get("symbols")
    if not isinstance(symbols, dict):
        return {
            "eligible": None,
            "status": "INVALID",
            "symbol": symbol_key,
            "as_of": snapshot.get("as_of"),
            "reason": "Validated surveillance snapshot has no usable symbol mapping.",
        }
    entries = symbols.get(symbol_key, [])
    if entries is None:
        entries = []
    if not isinstance(entries, list):
        return {
            "eligible": None,
            "status": "INVALID",
            "symbol": symbol_key,
            "as_of": snapshot.get("as_of"),
            "reason": f"Surveillance entries for {symbol_key} must be a list.",
        }
    normalized: list[dict[str, str]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            return {
                "eligible": None,
                "status": "INVALID",
                "symbol": symbol_key,
                "as_of": snapshot.get("as_of"),
                "reason": f"Surveillance entry for {symbol_key} is malformed.",
            }
        measure = str(entry.get("measure") or "").strip().upper().replace("-", "_").replace(" ", "_")
        stage = str(entry.get("stage") or "").strip()
        if measure not in SUPPORTED_SURVEILLANCE_MEASURES:
            return {
                "eligible": None,
                "status": "INVALID",
                "symbol": symbol_key,
                "as_of": snapshot.get("as_of"),
                "reason": f"Unsupported surveillance measure {measure!r} for {symbol_key}.",
            }
        normalized.append({"measure": measure, "stage": stage})
    if normalized:
        measures = sorted({entry["measure"] for entry in normalized})
        return {
            "eligible": False,
            "status": "RESTRICTED",
            "symbol": symbol_key,
            "as_of": snapshot.get("as_of"),
            "measures": measures,
            "entries": normalized,
            "reason": f"{symbol_key} is listed under NSE surveillance: {', '.join(measures)}.",
        }
    return {
        "eligible": True,
        "status": "NO_RESTRICTION_LISTED_IN_COMPLETE_SNAPSHOT",
        "symbol": symbol_key,
        "as_of": snapshot.get("as_of"),
        "measures": [],
        "reason": "No supported surveillance classification is listed in the complete NSE snapshot.",
    }


def inspect_directional_level_proximity(
    side: str,
    *,
    entry_price: float,
    atr: float,
    previous_levels: dict[str, Any],
    buffer_atr: float = 0.3,
) -> dict[str, Any]:
    """Block a setup when a previous-session reference is too close ahead.

    LONG checks only prior high and pivot levels above the entry. SHORT checks
    only prior low and pivot levels below the entry. Levels behind the trade
    are not resistance/support in the direction of travel and are ignored.
    """
    direction = str(side).strip().upper()
    if direction not in {"LONG", "SHORT"}:
        raise ValueError("side must be LONG or SHORT")
    numbers = (entry_price, atr, buffer_atr)
    if not all(isfinite(float(value)) for value in numbers):
        raise ValueError("entry_price, ATR and buffer_atr must be finite")
    if entry_price <= 0 or atr <= 0 or buffer_atr <= 0:
        raise ValueError("entry_price, ATR and buffer_atr must be positive")
    if not isinstance(previous_levels, dict):
        raise ValueError("previous_levels must be a mapping")
    keys = ("previous_high", "pivot") if direction == "LONG" else ("previous_low", "pivot")
    ahead: list[tuple[str, float, float]] = []
    available_levels = 0
    for key in keys:
        raw = previous_levels.get(key)
        if raw is None:
            continue
        available_levels += 1
        try:
            level = float(raw)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"Previous-session level {key} is invalid") from exc
        if not isfinite(level) or level <= 0:
            raise ValueError(f"Previous-session level {key} must be finite and positive")
        distance = (level - entry_price) / atr if direction == "LONG" else (entry_price - level) / atr
        if distance >= 0:
            ahead.append((key, level, distance))
    if available_levels == 0:
        raise ValueError("No relevant previous-session reference levels are available")
    if not ahead:
        return {
            "eligible": True,
            "status": "NO_PREVIOUS_LEVEL_AHEAD",
            "side": direction,
            "buffer_atr": buffer_atr,
            "nearest_level": None,
            "distance_atr": None,
            "reason": "",
        }
    key, level, distance = min(ahead, key=lambda item: item[2])
    blocked = distance <= buffer_atr
    return {
        "eligible": not blocked,
        "status": "BLOCKED_NEAR_PREVIOUS_LEVEL" if blocked else "CLEAR",
        "side": direction,
        "buffer_atr": buffer_atr,
        "nearest_level_name": key,
        "nearest_level": level,
        "distance_atr": round(distance, 6),
        "reason": (
            f"{direction} entry is {distance:.2f} ATR from previous-session {key} "
            f"(₹{level:.2f}); minimum clearance is {buffer_atr:.2f} ATR."
            if blocked else ""
        ),
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
