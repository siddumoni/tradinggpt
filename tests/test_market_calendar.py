from datetime import date, datetime
from zoneinfo import ZoneInfo

from src.market_calendar import is_regular_market_session, market_day_status, market_session_message

IST = ZoneInfo("Asia/Kolkata")


def test_known_2026_equity_holiday_is_closed():
    day = date(2026, 11, 10)
    status = market_day_status(day)
    assert status["is_open"] is False
    assert status["status"] == "EXCHANGE_HOLIDAY"
    assert "Diwali-Balipratipada" in status["reason"]
    assert is_regular_market_session(datetime(2026, 11, 10, 10, 0, tzinfo=IST)) is False
    assert "EXCHANGE_HOLIDAY" in market_session_message(datetime(2026, 11, 10, 10, 0, tzinfo=IST))


def test_weekend_remains_closed():
    status = market_day_status(date(2026, 11, 7))
    assert status["is_open"] is False
    assert status["status"] == "WEEKEND"


def test_special_muhurat_session_is_not_treated_as_normal_cash_session():
    status = market_day_status(date(2026, 11, 8))
    assert status["is_open"] is False
    assert status["status"] == "SPECIAL_SESSION_NOT_SUPPORTED"


def test_calendar_fails_closed_outside_verified_coverage():
    status = market_day_status(date(2027, 1, 4))
    assert status["is_open"] is False
    assert status["status"] == "CALENDAR_OUT_OF_COVERAGE"
