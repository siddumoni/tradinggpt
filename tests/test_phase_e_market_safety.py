from __future__ import annotations

import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

import src.market_scanner as scanner
from src.market_safety import (
    inspect_directional_level_proximity,
    load_market_safety_settings,
    load_surveillance_snapshot,
    surveillance_status_for_symbol,
)
from src.strategies.levels import previous_session_levels
from src.strategies.models import StrategySignal

IST = ZoneInfo("Asia/Kolkata")


def test_previous_session_levels_excludes_current_day_even_if_present():
    index = pd.to_datetime(["2026-11-05", "2026-11-06", "2026-11-07"])
    frame = pd.DataFrame(
        {
            "high": [110.0, 120.0, 999.0],
            "low": [90.0, 100.0, 1.0],
            "close": [105.0, 110.0, 500.0],
        },
        index=index,
    )

    levels = previous_session_levels(frame, session_date=date(2026, 11, 7))

    assert levels["previous_session_date"] == "2026-11-06"
    assert levels["previous_high"] == 120.0
    assert levels["previous_low"] == 100.0


def test_previous_session_levels_accepts_only_completed_prior_rows():
    frame = pd.DataFrame(
        {"high": [110.0], "low": [90.0], "close": [105.0]},
        index=pd.to_datetime(["2026-11-06"]),
    )
    levels = previous_session_levels(frame, session_date=date(2026, 11, 7))
    assert levels["previous_close"] == 105.0


def test_previous_session_levels_rejects_no_prior_or_invalid_ohlc():
    frame = pd.DataFrame(
        {"high": [100.0], "low": [110.0], "close": [105.0]},
        index=pd.to_datetime(["2026-11-06"]),
    )
    with pytest.raises(ValueError, match="Invalid previous-session OHLC"):
        previous_session_levels(frame, session_date=date(2026, 11, 7))
    with pytest.raises(ValueError, match="previous NSE session 2026-11-05 is missing"):
        previous_session_levels(frame, session_date=date(2026, 11, 6))


def test_directional_level_filter_blocks_only_nearby_levels_ahead():
    long_block = inspect_directional_level_proximity(
        "LONG",
        entry_price=100.0,
        atr=2.0,
        previous_levels={"previous_high": 100.4, "previous_low": 99.0, "pivot": 98.0},
        buffer_atr=0.3,
    )
    short_block = inspect_directional_level_proximity(
        "SHORT",
        entry_price=100.0,
        atr=2.0,
        previous_levels={"previous_high": 101.0, "previous_low": 99.6, "pivot": 102.0},
        buffer_atr=0.3,
    )

    assert long_block["eligible"] is False
    assert long_block["nearest_level_name"] == "previous_high"
    assert long_block["distance_atr"] == pytest.approx(0.2)
    assert short_block["eligible"] is False
    assert short_block["nearest_level_name"] == "previous_low"
    assert short_block["distance_atr"] == pytest.approx(0.2)


def test_directional_level_filter_ignores_levels_behind_trade_and_allows_clearance():
    result = inspect_directional_level_proximity(
        "LONG",
        entry_price=100.0,
        atr=2.0,
        previous_levels={"previous_high": 101.0, "previous_low": 99.0, "pivot": 98.0},
        buffer_atr=0.3,
    )
    assert result["eligible"] is True
    assert result["status"] == "CLEAR"
    assert result["distance_atr"] == pytest.approx(0.5)


def test_directional_level_filter_validates_inputs():
    with pytest.raises(ValueError, match="LONG or SHORT"):
        inspect_directional_level_proximity(
            "BUY", entry_price=100, atr=1, previous_levels={"pivot": 100}
        )
    with pytest.raises(ValueError, match="positive"):
        inspect_directional_level_proximity(
            "LONG", entry_price=100, atr=0, previous_levels={"pivot": 101}
        )


def test_market_safety_settings_include_exact_point_three_atr_buffer():
    settings = load_market_safety_settings()
    assert settings.previous_day_level_buffer_atr == pytest.approx(0.3)


def _surveillance_payload(as_of: str = "2026-11-07") -> dict:
    return {
        "schema_version": 1,
        "source": "NSE India",
        "source_url": "https://www.nseindia.com/reports/asm",
        "as_of": as_of,
        "complete": True,
        "symbols": {
            "INFY": [{"measure": "ASM", "stage": "Stage I"}],
            "TCS": [{"measure": "GSM", "stage": "Stage II"}],
        },
    }


def test_surveillance_snapshot_distinguishes_restricted_and_unlisted_symbols(tmp_path):
    path = tmp_path / "nse_surveillance.json"
    path.write_text(json.dumps(_surveillance_payload()), encoding="utf-8")

    snapshot = load_surveillance_snapshot(path, as_of=date(2026, 11, 7))
    restricted = surveillance_status_for_symbol("INFY", snapshot)
    unlisted = surveillance_status_for_symbol("RELIANCE", snapshot)

    assert snapshot["status"] == "VALID"
    assert restricted["eligible"] is False
    assert restricted["status"] == "RESTRICTED"
    assert restricted["measures"] == ["ASM"]
    assert unlisted["eligible"] is True
    assert unlisted["status"] == "NO_RESTRICTION_LISTED_IN_COMPLETE_SNAPSHOT"


def test_missing_stale_incomplete_and_non_nse_surveillance_are_not_clear(tmp_path):
    missing = load_surveillance_snapshot(tmp_path / "missing.json", as_of=date(2026, 11, 7))
    assert missing["status"] == "NOT_AVAILABLE"
    assert surveillance_status_for_symbol("INFY", missing)["eligible"] is None

    stale_path = tmp_path / "stale.json"
    stale_path.write_text(json.dumps(_surveillance_payload("2026-11-01")), encoding="utf-8")
    stale = load_surveillance_snapshot(stale_path, as_of=date(2026, 11, 7))
    assert stale["status"] == "STALE"
    assert surveillance_status_for_symbol("INFY", stale)["eligible"] is None

    incomplete_path = tmp_path / "incomplete.json"
    payload = _surveillance_payload()
    payload["complete"] = False
    incomplete_path.write_text(json.dumps(payload), encoding="utf-8")
    incomplete = load_surveillance_snapshot(incomplete_path, as_of=date(2026, 11, 7))
    assert incomplete["status"] == "INCOMPLETE"

    invalid_path = tmp_path / "invalid.json"
    payload = _surveillance_payload()
    payload["source_url"] = "https://example.com/not-nse"
    invalid_path.write_text(json.dumps(payload), encoding="utf-8")
    invalid = load_surveillance_snapshot(invalid_path, as_of=date(2026, 11, 7))
    assert invalid["status"] == "INVALID"


def test_live_scanner_blocks_long_setup_with_previous_high_inside_point_three_atr(monkeypatch):
    now = datetime(2026, 10, 16, 9, 40, tzinfo=IST)
    prior_intraday = [
        {
            "date": stamp,
            "open": 100.0 + i * 0.2 - 0.1,
            "high": 100.0 + i * 0.2 + 0.3,
            "low": 100.0 + i * 0.2 - 0.3,
            "close": 100.0 + i * 0.2,
            "volume": 1000 + i * 10,
        }
        for i, stamp in enumerate(
            pd.date_range("2026-10-15 09:15", periods=72, freq="5min", tz=IST)
        )
    ]
    current_5m = [
        {
            "date": stamp,
            "open": 100.0 + i * 0.2 - 0.1,
            "high": 100.0 + i * 0.2 + 0.3,
            "low": 100.0 + i * 0.2 - 0.3,
            "close": 100.0 + i * 0.2,
            "volume": 1200,
        }
        for i, stamp in enumerate(
            pd.date_range("2026-10-16 09:15", periods=6, freq="5min", tz=IST)
        )
    ]
    current_15m = [
        {
            "date": stamp,
            "open": 100.0 + i * 0.2 - 0.1,
            "high": 100.0 + i * 0.2 + 0.3,
            "low": 100.0 + i * 0.2 - 0.3,
            "close": 100.0 + i * 0.2,
            "volume": 3000,
        }
        for i, stamp in enumerate(
            pd.date_range("2026-10-16 09:15", periods=9, freq="5min", tz=IST)
        )
    ]
    daily = [
        {"date": "2026-10-15", "open": 100, "high": 100.9, "low": 99, "close": 100.5, "volume": 100000},
        {"date": "2026-10-16", "open": 100, "high": 110, "low": 100, "close": 109, "volume": 1000},
    ]
    monkeypatch.setattr(
        scanner,
        "opening_range_signal",
        lambda *_args, **_kwargs: StrategySignal(
            symbol="TEST", side="LONG", strategy="opening_range",
            trigger="breakout", invalidation="below opening range",
            confidence=80, reason="test signal",
        ),
    )
    monkeypatch.setattr(scanner, "vwap_trend_signal", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(scanner, "vwap_pullback_signal", lambda *_args, **_kwargs: None)

    result = scanner.evaluate_symbol_bundle(
        "TEST",
        {
            "provider": "test",
            "candles_5m": current_5m,
            "candles_15m": current_15m,
            "candles_5m_history": prior_intraday,
            "candles_daily_60d": daily,
        },
        now=now,
    )

    assert result.action == "NO_TRADE"
    assert result.quantity == 0
    assert result.entry_validation["status"] == "REJECTED_PREVIOUS_DAY_LEVEL"
    assert result.entry_validation["previous_day_levels"]["proximity"]["distance_atr"] <= 0.3


def test_previous_session_levels_never_substitutes_an_older_daily_candle():
    frame = pd.DataFrame(
        {
            "high": [110.0],
            "low": [90.0],
            "close": [105.0],
        },
        index=pd.to_datetime(["2026-11-05"]),
    )
    with pytest.raises(ValueError, match="previous NSE session 2026-11-06 is missing"):
        previous_session_levels(frame, session_date=date(2026, 11, 9))


def test_previous_session_levels_skips_weekend_and_exchange_holiday():
    frame = pd.DataFrame(
        {
            "high": [120.0, 999.0],
            "low": [100.0, 1.0],
            "close": [110.0, 500.0],
        },
        index=pd.to_datetime(["2026-11-09", "2026-11-10"]),
    )
    levels = previous_session_levels(frame, session_date=date(2026, 11, 11))
    assert levels["previous_session_date"] == "2026-11-09"
    assert levels["previous_high"] == 120.0
