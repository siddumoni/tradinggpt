from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from src.kite_five_year_backtest import (
    _format_duration,
    _normalise_candles,
    _progress_bar,
    fetch_five_year_candles,
    iter_kite_chunks,
)

IST = ZoneInfo("Asia/Kolkata")


def test_iter_kite_chunks_covers_range_without_gaps_or_overlap():
    start = datetime(2021, 1, 1, tzinfo=IST)
    end = datetime(2021, 5, 1, tzinfo=IST)
    chunks = list(iter_kite_chunks(start, end))
    assert chunks[0][0] == start
    assert chunks[-1][1] == end
    assert all((right[0] - left[1]).total_seconds() == 1 for left, right in zip(chunks, chunks[1:]))
    assert all((chunk_end - chunk_start).days < 60 for chunk_start, chunk_end in chunks)


def test_iter_kite_chunks_rejects_naive_or_invalid_range():
    with pytest.raises(ValueError, match="timezone-aware"):
        list(iter_kite_chunks(datetime(2021, 1, 1), datetime(2021, 1, 2)))
    with pytest.raises(ValueError, match="start must be <= end"):
        list(iter_kite_chunks(datetime(2021, 1, 2, tzinfo=IST), datetime(2021, 1, 1, tzinfo=IST)))


def candle(stamp, close=100.0):
    return {"date": pd.Timestamp(stamp, tz=IST), "open": close - 0.5, "high": close + 1,
            "low": close - 1, "close": close, "volume": 1000}


def test_normalise_candles_converts_and_sorts_timezone_aware_rows():
    frame = _normalise_candles([candle("2021-01-01 09:20"), candle("2021-01-01 09:15")])
    assert frame.index.is_monotonic_increasing
    assert str(frame.index.tz) == "Asia/Kolkata"
    assert list(frame.columns) == ["open", "high", "low", "close", "volume"]


def test_normalise_candles_drops_identical_boundary_duplicates():
    frame = _normalise_candles([candle("2021-01-01 09:15"), candle("2021-01-01 09:15")])
    assert len(frame) == 1


def test_normalise_candles_rejects_conflicting_duplicates():
    with pytest.raises(Exception, match="conflicting duplicate"):
        _normalise_candles([candle("2021-01-01 09:15", 100), candle("2021-01-01 09:15", 101)])


def test_fetch_five_year_candles_uses_kite_5minute_chunks():
    class FakeMarketData:
        def __init__(self):
            self.calls = []

        def historical_candles(self, token, start, end, interval):
            self.calls.append((token, start, end, interval))
            return [candle("2021-01-01 09:15")] if len(self.calls) == 1 else []

    client = FakeMarketData()
    frame = fetch_five_year_candles(client, 123, datetime(2021, 1, 1, tzinfo=IST),
                                    datetime(2021, 2, 1, tzinfo=IST))
    assert len(client.calls) == 1
    assert client.calls[0][0] == 123
    assert client.calls[0][3] == "5minute"
    assert len(frame) == 1



def test_progress_helpers_render_power_shell_friendly_status():
    assert _format_duration(3661.9) == "1:01:01"
    assert _progress_bar(0, 10, width=10) == "[----------]"
    assert _progress_bar(5, 10, width=10) == "[#####-----]"
    assert _progress_bar(10, 10, width=10) == "[##########]"


def test_fetch_five_year_candles_reports_every_chunk_and_running_row_count():
    class FakeMarketData:
        def __init__(self):
            self.calls = []

        def historical_candles(self, token, start, end, interval):
            self.calls.append((token, start, end, interval))
            if len(self.calls) == 1:
                return [candle("2021-01-01 09:15")]
            return []

    client = FakeMarketData()
    updates = []
    frame = fetch_five_year_candles(
        client,
        456,
        datetime(2021, 1, 1, tzinfo=IST),
        datetime(2021, 5, 1, tzinfo=IST),
        progress_callback=lambda *args: updates.append(args),
    )

    assert len(frame) == 1
    assert len(updates) == len(client.calls)
    assert len(updates) > 1
    assert updates[-1][0] == updates[-1][1]
    assert updates[-1][2] == 1
    assert all(update[3] >= 0 for update in updates)
    assert all(call[0] == 456 and call[3] == "5minute" for call in client.calls)
