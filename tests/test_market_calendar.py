from datetime import date, datetime
from zoneinfo import ZoneInfo

from src.market_calendar import calendar_maintenance_status, is_regular_market_session, market_day_status, market_session_message

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



def test_calendar_maintenance_warning_starts_with_less_than_thirty_days():
    status = calendar_maintenance_status(date(2026, 12, 1))
    assert status["days_remaining"] == 30
    assert status["warning"] is False
    assert status["status"] == "CURRENT"


def test_calendar_maintenance_warns_at_twenty_nine_days_and_reports_missing_next_year():
    status = calendar_maintenance_status(date(2026, 12, 2))
    assert status["days_remaining"] == 29
    assert status["warning"] is True
    assert status["status"] == "CALENDAR_UPDATE_REQUIRED"
    assert status["next_calendar_year"] == 2027
    assert status["next_year_covered"] is False


def test_calendar_maintenance_reports_expired_coverage():
    status = calendar_maintenance_status(date(2027, 1, 1))
    assert status["status"] == "CALENDAR_COVERAGE_EXPIRED"
    assert status["warning"] is True
