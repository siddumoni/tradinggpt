from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from src.kite_five_year_backtest import _normalise_candles, fetch_five_year_candles, iter_kite_chunks

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
