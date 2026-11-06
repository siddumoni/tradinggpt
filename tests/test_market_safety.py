from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from src.market_safety import inspect_circuit_limits, load_market_safety_settings, time_of_day_relative_volume

IST = ZoneInfo("Asia/Kolkata")


def test_circuit_limit_check_requires_both_exchange_bands():
    result = inspect_circuit_limits({"last_price": 100.0})
    assert result["eligible"] is False
    assert result["status"] == "CIRCUIT_LIMITS_MISSING"


def test_circuit_limit_check_blocks_near_upper_and_lower_bands():
    upper = inspect_circuit_limits({
        "last_price": 99.8, "lower_circuit_limit": 80.0, "upper_circuit_limit": 100.0,
    }, buffer_pct=0.5)
    lower = inspect_circuit_limits({
        "last_price": 80.2, "lower_circuit_limit": 80.0, "upper_circuit_limit": 100.0,
    }, buffer_pct=0.5)
    assert upper["eligible"] is False and upper["status"] == "NEAR_UPPER_CIRCUIT"
    assert lower["eligible"] is False and lower["status"] == "NEAR_LOWER_CIRCUIT"


def test_circuit_limit_check_passes_well_inside_valid_band():
    result = inspect_circuit_limits({
        "last_price": 100.0, "lower_circuit_limit": 80.0, "upper_circuit_limit": 120.0,
    }, buffer_pct=0.5)
    assert result["eligible"] is True
    assert result["status"] == "CLEAR"


def _bars(day: str, last_time: str, volume: float) -> pd.DataFrame:
    stamps = pd.date_range(
        start=f"{day} 09:15", end=f"{day} {last_time}", freq="5min", tz=IST
    )
    return pd.DataFrame({"volume": [volume] * len(stamps)}, index=stamps)


def test_relative_volume_uses_prior_sessions_at_same_clock_time():
    current = _bars("2026-10-26", "10:55", 1000)
    histories = pd.concat([
        _bars(f"2026-10-{day:02d}", "10:55", 500)
        for day in (5, 6, 7, 8, 9, 12, 13, 14, 15, 16)
    ])
    result = time_of_day_relative_volume(current, histories, lookback_sessions=20, minimum_valid_sessions=10)
    assert result["eligible"] is True
    assert result["ratio"] == 2.0
    assert result["sessions_used"] == 10


def test_relative_volume_fails_closed_with_gaps_or_fewer_sessions():
    current = _bars("2026-10-26", "10:55", 1000)
    incomplete = _bars("2026-10-26", "10:55", 1000).drop(
        pd.Timestamp("2026-10-26 09:35", tz=IST)
    )
    prior = pd.concat([
        _bars(f"2026-10-{day:02d}", "10:55", 500)
        for day in (16, 19)
    ])
    assert time_of_day_relative_volume(incomplete, prior)["status"] == "INCOMPLETE_CURRENT_SESSION"
    assert time_of_day_relative_volume(current, prior)["status"] == "INSUFFICIENT_HISTORY"


def test_relative_volume_ignores_same_day_future_bars():
    current = _bars("2026-10-26", "10:55", 1000)
    prior = _bars("2026-10-23", "10:55", 500)
    future = _bars("2026-10-26", "15:25", 999999)
    result = time_of_day_relative_volume(
        current, pd.concat([prior, future]), lookback_sessions=1, minimum_valid_sessions=1
    )
    assert result["eligible"] is True
    assert result["ratio"] == 2.0


def test_market_safety_config_is_valid():
    settings = load_market_safety_settings()
    assert settings.circuit_near_band_buffer_pct == 0.5
    assert settings.relative_volume_minimum_valid_sessions == 10
