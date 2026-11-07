"""NSE equity market calendar with explicit, bounded coverage.

Unknown dates fail closed. The normal-session model intentionally does not
model special/Muhurat sessions.
"""
from __future__ import annotations

from datetime import date, datetime, time as dtime
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

IST = ZoneInfo("Asia/Kolkata")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CALENDAR_PATH = PROJECT_ROOT / "config" / "nse_equity_calendar.yaml"


@lru_cache(maxsize=8)
def _load_calendar(path_text: str) -> dict[str, Any]:
    path = Path(path_text)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"Could not read NSE equity calendar {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("NSE equity calendar must be a YAML mapping")
    try:
        valid_from = date.fromisoformat(str(raw["valid_from"]))
        valid_through = date.fromisoformat(str(raw["valid_through"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("NSE equity calendar requires ISO valid_from and valid_through dates") from exc
    if valid_through < valid_from:
        raise ValueError("NSE equity calendar valid_through must not precede valid_from")
    holidays_raw = raw.get("holidays", [])
    special_raw = raw.get("special_sessions", [])
    if not isinstance(holidays_raw, list) or not isinstance(special_raw, list):
        raise ValueError("NSE equity calendar holidays and special_sessions must be lists")

    holidays: dict[date, str] = {}
    for item in holidays_raw:
        if not isinstance(item, dict) or "date" not in item:
            raise ValueError("Each NSE holiday must contain a date and reason")
        try:
            day = date.fromisoformat(str(item["date"]))
        except ValueError as exc:
            raise ValueError(f"Invalid NSE holiday date: {item.get('date')}") from exc
        if day in holidays:
            raise ValueError(f"Duplicate NSE holiday date: {day.isoformat()}")
        holidays[day] = str(item.get("reason") or "NSE equity trading holiday")

    special_sessions: dict[date, str] = {}
    for item in special_raw:
        if not isinstance(item, dict) or "date" not in item:
            raise ValueError("Each special session must contain a date and reason")
        try:
            day = date.fromisoformat(str(item["date"]))
        except ValueError as exc:
            raise ValueError(f"Invalid NSE special-session date: {item.get('date')}") from exc
        special_sessions[day] = str(item.get("reason") or "Special session not modelled")

    return {
        "valid_from": valid_from,
        "valid_through": valid_through,
        "holidays": holidays,
        "special_sessions": special_sessions,
        "source_url": str(raw.get("source_url") or ""),
    }


def market_day_status(day: date, calendar_path: Path = DEFAULT_CALENDAR_PATH) -> dict[str, Any]:
    """Describe whether the normal NSE equity session is modelled as open."""
    if isinstance(day, datetime):
        day = day.astimezone(IST).date() if day.tzinfo else day.date()
    if not isinstance(day, date):
        raise TypeError("day must be a datetime.date")
    calendar = _load_calendar(str(Path(calendar_path).resolve()))
    if not calendar["valid_from"] <= day <= calendar["valid_through"]:
        return {
            "is_open": False,
            "status": "CALENDAR_OUT_OF_COVERAGE",
            "reason": (
                f"NSE calendar coverage is {calendar['valid_from']} through "
                f"{calendar['valid_through']}; {day} is outside the verified range."
            ),
        }
    if day in calendar["special_sessions"]:
        return {
            "is_open": False,
            "status": "SPECIAL_SESSION_NOT_SUPPORTED",
            "reason": calendar["special_sessions"][day],
        }
    if day in calendar["holidays"]:
        return {
            "is_open": False,
            "status": "EXCHANGE_HOLIDAY",
            "reason": calendar["holidays"][day],
        }
    if day.weekday() >= 5:
        return {
            "is_open": False,
            "status": "WEEKEND",
            "reason": "NSE normal equity market is closed on weekends.",
        }
    return {"is_open": True, "status": "OPEN", "reason": ""}


def calendar_maintenance_status(
    day: date | datetime,
    *,
    warning_days: int = 30,
    calendar_path: Path = DEFAULT_CALENDAR_PATH,
) -> dict[str, Any]:
    """Warn before the verified exchange calendar expires; never invent future holidays."""
    if warning_days < 0:
        raise ValueError("warning_days must be non-negative")
    current = day.astimezone(IST).date() if isinstance(day, datetime) and day.tzinfo else (
        day.date() if isinstance(day, datetime) else day
    )
    calendar = _load_calendar(str(Path(calendar_path).resolve()))
    valid_through = calendar["valid_through"]
    days_remaining = (valid_through - current).days
    next_year = current.year + 1
    next_year_covered = calendar["valid_from"].year <= next_year <= calendar["valid_through"].year
    expired = current > valid_through
    warning = expired or days_remaining < warning_days
    if expired:
        status = "CALENDAR_COVERAGE_EXPIRED"
        message = (
            f"Verified NSE calendar coverage ended on {valid_through}; "
            "normal-session decisions outside coverage fail closed."
        )
    elif warning:
        status = "CALENDAR_UPDATE_REQUIRED"
        message = (
            f"Verified NSE calendar coverage ends in {days_remaining} calendar days "
            f"({valid_through}). Add the next year's official NSE equity holiday list when published."
        )
    else:
        status = "CURRENT"
        message = ""
    return {
        "status": status,
        "warning": warning,
        "as_of": current.isoformat(),
        "valid_through": valid_through.isoformat(),
        "days_remaining": days_remaining,
        "warning_days": warning_days,
        "next_calendar_year": next_year,
        "next_year_covered": next_year_covered,
        "message": message,
    }


def is_trading_day(day: date, calendar_path: Path = DEFAULT_CALENDAR_PATH) -> bool:
    return bool(market_day_status(day, calendar_path)["is_open"])


def is_regular_market_session(
    now: datetime,
    *,
    calendar_path: Path = DEFAULT_CALENDAR_PATH,
    market_open: dtime = dtime(9, 15),
    market_close: dtime = dtime(15, 30),
) -> bool:
    current = now.astimezone(IST) if now.tzinfo else now.replace(tzinfo=IST)
    return (
        is_trading_day(current.date(), calendar_path)
        and market_open <= current.time().replace(tzinfo=None) <= market_close
    )


def market_session_message(
    now: datetime | None = None,
    *,
    calendar_path: Path = DEFAULT_CALENDAR_PATH,
) -> str | None:
    """Return a human-readable close reason or None for a normal cash session."""
    current = now or datetime.now(IST)
    current = current.astimezone(IST) if current.tzinfo else current.replace(tzinfo=IST)
    day_status = market_day_status(current.date(), calendar_path)
    if not day_status["is_open"]:
        return f"{day_status['status']}: {day_status['reason']} No intraday scan was run."
    if current.time() < dtime(9, 15) or current.time() > dtime(15, 30):
        return "Outside normal NSE cash-market hours (09:15–15:30 IST). Intraday signals are not evaluated."
    return None
