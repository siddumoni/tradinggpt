from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from src.candle_archive import append_completed_candles

IST = ZoneInfo("Asia/Kolkata")


def sample_candles():
    index = pd.date_range("2026-10-22 09:15:00", periods=8, freq="5min", tz=IST)
    frame = pd.DataFrame({
        "open": [100.0] * len(index),
        "high": [101.0] * len(index),
        "low": [99.0] * len(index),
        "close": [100.5] * len(index),
        "volume": [1000] * len(index),
    }, index=index)
    frame.index.name = "timestamp"
    return frame.reset_index().rename(columns={"timestamp": "date"}).to_dict("records")


def test_archive_only_completed_bars_and_preserve_provider_mode(tmp_path):
    now = datetime(2026, 10, 22, 9, 32, tzinfo=IST)
    result = append_completed_candles(
        "INFY", sample_candles(), tmp_path, now=now, provider_mode="kite",
    )
    saved = pd.read_csv(tmp_path / "INFY.csv")

    assert result.rows_observed == 3
    assert result.total_rows == 3
    assert len(saved) == 3
    assert set(saved["provider_mode"]) == {"kite"}
    assert saved["date"].tolist() == [
        "2026-10-22T09:15:00+0530",
        "2026-10-22T09:20:00+0530",
        "2026-10-22T09:25:00+0530",
    ]


def test_archive_upserts_repeated_timestamps_without_duplicates(tmp_path):
    now = datetime(2026, 10, 22, 9, 42, tzinfo=IST)
    candles = sample_candles()
    first = append_completed_candles("INFY", candles, tmp_path, now=now, provider_mode="yahoo")
    changed = [dict(c) for c in candles]
    changed[0]["close"] = 100.25
    second = append_completed_candles("INFY", changed, tmp_path, now=now, provider_mode="yahoo")
    saved = pd.read_csv(tmp_path / "INFY.csv")

    assert first.total_rows == 5
    assert second.total_rows == 5
    assert saved["date"].is_unique
    assert saved.loc[0, "close"] == pytest.approx(100.25)


def test_archive_rejects_mixing_yahoo_and_kite_for_same_symbol(tmp_path):
    now = datetime(2026, 10, 22, 9, 42, tzinfo=IST)
    append_completed_candles("INFY", sample_candles(), tmp_path, now=now, provider_mode="yahoo")

    with pytest.raises(ValueError, match="Provider mismatch"):
        append_completed_candles("INFY", sample_candles(), tmp_path, now=now, provider_mode="kite")


def test_archive_rejects_legacy_archive_without_provenance(tmp_path):
    now = datetime(2026, 10, 22, 9, 42, tzinfo=IST)
    (tmp_path / "INFY.csv").write_text(
        "date,open,high,low,close,volume\n2026-10-22T09:15:00+0530,100,101,99,100,1000\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="no provider_mode metadata"):
        append_completed_candles("INFY", sample_candles(), tmp_path, now=now, provider_mode="kite")


def test_archive_rejects_bad_symbol_and_unknown_provider(tmp_path):
    now = datetime(2026, 10, 22, 9, 42, tzinfo=IST)
    with pytest.raises(ValueError, match="symbol"):
        append_completed_candles("../INFY", sample_candles(), tmp_path, now=now, provider_mode="kite")
    with pytest.raises(ValueError, match="provider_mode"):
        append_completed_candles("INFY", sample_candles(), tmp_path, now=now, provider_mode="unknown")

def test_archive_keeps_final_modeled_1510_candle_and_excludes_later_bars(tmp_path):
    index = pd.date_range("2026-10-22 15:05:00", periods=3, freq="5min", tz=IST)
    frame = pd.DataFrame({
        "open": [100.0, 100.5, 101.0],
        "high": [101.0, 101.5, 102.0],
        "low": [99.0, 100.0, 100.5],
        "close": [100.5, 101.0, 101.5],
        "volume": [1000, 1100, 1200],
    }, index=index)
    frame.index.name = "timestamp"
    result = append_completed_candles(
        "INFY", frame.reset_index().rename(columns={"timestamp": "date"}).to_dict("records"),
        tmp_path, now=datetime(2026, 10, 22, 15, 21, tzinfo=IST), provider_mode="kite",
    )
    saved = pd.read_csv(tmp_path / "INFY.csv")

    assert result.rows_observed == 2
    assert saved["date"].tolist() == [
        "2026-10-22T15:05:00+0530",
        "2026-10-22T15:10:00+0530",
    ]
