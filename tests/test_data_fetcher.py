from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.data_fetcher import (
    DataClientConfig, DataFetchError, KiteMarketData, MarketClosedError,
    is_regular_market_hours, is_weekend, market_session_message,
)

IST = ZoneInfo("Asia/Kolkata")


class FakeKite:
    def __init__(self):
        self.instrument_calls = 0
        self.ltp_calls = []
        self.quote_calls = []
        self.historical_calls = []

    def instruments(self, exchange):
        self.instrument_calls += 1
        assert exchange == "NSE"
        return [{"tradingsymbol": "RELIANCE", "instrument_token": 1, "segment": "NSE", "instrument_type": "EQ"}]

    def ltp(self, *keys):
        self.ltp_calls.append(keys)
        return {key: {"last_price": 100.0} for key in keys}

    def quote(self, *keys):
        self.quote_calls.append(keys)
        return {key: {"last_price": 100.0, "depth": {"buy": [], "sell": []}} for key in keys}

    def historical_data(self, token, start, end, interval):
        self.historical_calls.append((token, start, end, interval))
        return [{"date": start, "open": 99, "high": 101, "low": 98, "close": 100, "volume": 1000}]


def test_weekend_and_market_hours():
    assert is_weekend(date(2026, 10, 17))  # Saturday
    assert not is_weekend(date(2026, 10, 19))  # Monday
    assert is_regular_market_hours(datetime(2026, 10, 19, 9, 15, tzinfo=IST))
    assert not is_regular_market_hours(datetime(2026, 10, 19, 8, 59, tzinfo=IST))
    assert market_session_message(datetime(2026, 10, 17, 10, 0, tzinfo=IST))
    assert market_session_message(datetime(2026, 10, 19, 8, 0, tzinfo=IST))
    assert market_session_message(datetime(2026, 10, 19, 10, 0, tzinfo=IST)) is None


def test_instrument_cache_is_reused_for_same_day(tmp_path: Path):
    kite = FakeKite()
    client = KiteMarketData(kite, DataClientConfig(retries=1, retry_delay_seconds=0))
    cache = tmp_path / "instruments.json"
    now = datetime(2026, 10, 19, 10, 0, tzinfo=IST)
    assert len(client.instruments(cache, now)) == 1
    assert len(client.instruments(cache, now)) == 1
    assert kite.instrument_calls == 1


def test_instrument_cache_refreshes_on_new_day(tmp_path: Path):
    kite = FakeKite()
    client = KiteMarketData(kite, DataClientConfig(retries=1, retry_delay_seconds=0))
    cache = tmp_path / "instruments.json"
    client.instruments(cache, datetime(2026, 10, 19, 10, 0, tzinfo=IST))
    client.instruments(cache, datetime(2026, 10, 20, 10, 0, tzinfo=IST))
    assert kite.instrument_calls == 2


def test_resolve_watchlist_reports_missing_symbols(tmp_path: Path):
    kite = FakeKite()
    client = KiteMarketData(kite, DataClientConfig(retries=1, retry_delay_seconds=0))
    watchlist = tmp_path / "watchlist.yaml"
    watchlist.write_text("nifty50:\n  - RELIANCE\n  - MISSING\nadditional_liquid_fo: []\n", encoding="utf-8")
    rows, missing = client.resolve_watchlist(watchlist, FakeKite().instruments("NSE"))
    assert [row["tradingsymbol"] for row in rows] == ["RELIANCE"]
    assert missing == ["MISSING"]


def test_ltp_and_quote_batching():
    kite = FakeKite()
    client = KiteMarketData(kite, DataClientConfig(
        retries=1, retry_delay_seconds=0, quote_batch_size=2,
        quote_min_interval_seconds=0,
    ))
    symbols = ["RELIANCE", "TCS", "INFY"]
    assert len(client.ltp(symbols)) == 3
    assert len(kite.ltp_calls) == 2
    assert len(client.quotes(symbols)) == 3
    assert len(kite.quote_calls) == 2


def test_historical_candles_validates_interval_and_timezone():
    client = KiteMarketData(FakeKite(), DataClientConfig(retries=1, retry_delay_seconds=0))
    start = datetime(2026, 10, 19, 9, 15, tzinfo=IST)
    end = start + timedelta(minutes=15)
    assert client.historical_candles(1, start, end, "5minute")
    with pytest.raises(ValueError):
        client.historical_candles(1, start.replace(tzinfo=None), end, "5minute")
    with pytest.raises(ValueError):
        client.historical_candles(1, start, end, "1minute")


def test_retry_then_success(monkeypatch):
    client = KiteMarketData(FakeKite(), DataClientConfig(retries=2, retry_delay_seconds=0))
    attempts = {"n": 0}
    def flaky(*keys):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("temporary")
        return {key: {"last_price": 1} for key in keys}
    client.kite.ltp = flaky
    assert client.ltp(["RELIANCE"])
    assert attempts["n"] == 2


def test_retries_exhausted_raises_clear_error():
    kite = FakeKite()
    def fail(*keys):
        raise RuntimeError("down")
    kite.ltp = fail
    client = KiteMarketData(kite, DataClientConfig(retries=2, retry_delay_seconds=0))
    with pytest.raises(DataFetchError, match="LTP request failed after 2 attempts"):
        client.ltp(["RELIANCE"])



def test_kite_nifty_index_history_uses_index_instrument_token():
    kite = FakeKite()
    client = KiteMarketData(kite, DataClientConfig(retries=1, retry_delay_seconds=0))
    rows = [
        {"tradingsymbol": "RELIANCE", "instrument_token": 1, "segment": "NSE", "exchange": "NSE"},
        {"tradingsymbol": "NIFTY 50", "name": "NIFTY 50", "instrument_token": 256265,
         "segment": "INDICES", "exchange": "NSE"},
    ]
    now = datetime(2026, 10, 19, 10, 0, tzinfo=IST)
    candles = client.fetch_nifty50_15m_candles(rows, now=now)
    assert candles
    assert kite.historical_calls[-1][0] == 256265
    assert kite.historical_calls[-1][3] == "15minute"


def test_kite_nifty_index_history_fails_closed_when_index_missing():
    client = KiteMarketData(FakeKite(), DataClientConfig(retries=1, retry_delay_seconds=0))
    with pytest.raises(DataFetchError, match="NIFTY 50 index instrument was not found"):
        client.fetch_nifty50_15m_candles([
            {"tradingsymbol": "RELIANCE", "instrument_token": 1, "segment": "NSE", "exchange": "NSE"}
        ], now=datetime(2026, 10, 19, 10, 0, tzinfo=IST))


def test_kite_margin_estimator_uses_read_only_one_share_mis_calculator():
    kite = FakeKite()
    captured = {}
    def order_margins(params):
        captured["params"] = params
        return [{"total": 22.5}]
    kite.order_margins = order_margins
    client = KiteMarketData(kite, DataClientConfig(retries=1, retry_delay_seconds=0))
    value = client.estimate_mis_margin_per_share("INFY", "LONG", 1500.0)
    assert value == pytest.approx(22.5)
    assert captured["params"][0]["tradingsymbol"] == "INFY"
    assert captured["params"][0]["transaction_type"] == "BUY"
    assert captured["params"][0]["product"] == "MIS"
    assert captured["params"][0]["quantity"] == 1
    assert captured["params"][0]["order_type"] == "LIMIT"


def test_kite_margin_estimator_fails_closed_for_missing_margin():
    kite = FakeKite()
    kite.order_margins = lambda _params: [{"total": 0}]
    client = KiteMarketData(kite, DataClientConfig(retries=1, retry_delay_seconds=0))
    with pytest.raises(DataFetchError, match="positive, usable MIS margin"):
        client.estimate_mis_margin_per_share("INFY", "LONG", 1500.0)


def test_full_quote_batches_never_exceed_kite_limit():
    class BatchKite(FakeKite):
        def quote(self, *keys):
            self.quote_calls.append(keys)
            return {key: {"last_price": 100.0} for key in keys}

    kite = BatchKite()
    client = KiteMarketData(
        kite,
        DataClientConfig(
            retries=1, retry_delay_seconds=0, quote_batch_size=1000,
            quote_min_interval_seconds=0,
        ),
    )
    symbols = [f"STOCK{i:04d}" for i in range(501)]
    result = client.quotes(symbols)
    assert len(result) == 501
    assert [len(batch) for batch in kite.quote_calls] == [500, 1]


def test_screen_liquid_nse_equities_ranks_fresh_quotes_and_explains_exclusions():
    now = datetime(2026, 10, 30, 11, 0, tzinfo=IST)

    class ScreenKite(FakeKite):
        def quote(self, *keys):
            self.quote_calls.append(keys)
            prices = {
                "NSE:RELIANCE": (2500.0, 100000, 2500.0),
                "NSE:TCS": (4000.0, 100000, 4000.0),
                "NSE:CHEAP": (20.0, 1000000, 20.0),
                "NSE:LOWVOL": (100.0, 100, 100.0),
                "NSE:STALE": (100.0, 100000, 100.0),
            }
            values = {}
            for key in keys:
                if key not in prices:
                    continue
                price, volume, average = prices[key]
                timestamp = now - timedelta(minutes=30) if key == "NSE:STALE" else now - timedelta(minutes=1)
                values[key] = {
                    "last_price": price,
                    "volume": volume,
                    "average_price": average,
                    "timestamp": timestamp,
                }
            return values

    instruments = [
        {"tradingsymbol": symbol, "instrument_token": i + 1, "exchange": "NSE",
         "segment": "NSE", "instrument_type": "EQ"}
        for i, symbol in enumerate(("RELIANCE", "TCS", "CHEAP", "LOWVOL", "STALE", "MISSING"))
    ]
    # These must not enter the NSE cash-equity quote universe.
    instruments += [
        {"tradingsymbol": "NIFTY 50", "instrument_token": 999, "exchange": "NSE",
         "segment": "INDICES", "instrument_type": "EQ"},
        {"tradingsymbol": "GOLDBEES", "instrument_token": 998, "exchange": "NSE",
         "segment": "NSE", "instrument_type": "ETF"},
    ]
    client = KiteMarketData(
        ScreenKite(),
        DataClientConfig(retries=1, retry_delay_seconds=0, quote_min_interval_seconds=0),
    )
    selected, quotes, summary = client.screen_liquid_nse_equities(
        instruments, now=now, max_symbols=1,
        min_current_day_volume=1000,
        min_current_day_turnover_inr=1000000,
    )
    assert [row["tradingsymbol"] for row in selected] == ["TCS"]
    assert quotes["TCS"]["last_price"] == 4000.0
    assert summary["instruments_total"] == 6
    assert summary["eligible_before_top_n"] == 2
    assert summary["selected_for_technical_analysis"] == 1
    assert summary["selection_metric"] == "average_price × current-day volume"
    assert summary["exclusions"]["PRICE_OUTSIDE_RANGE"] == 1
    assert summary["exclusions"]["CURRENT_DAY_VOLUME_BELOW_MINIMUM"] == 1
    assert summary["exclusions"]["QUOTE_STALE"] == 1
    assert summary["exclusions"]["QUOTE_MISSING"] == 1


def test_fetch_symbol_bundle_reuses_bulk_quote_and_can_skip_daily_history():
    kite = FakeKite()
    client = KiteMarketData(
        kite,
        DataClientConfig(retries=1, retry_delay_seconds=0, historical_min_interval_seconds=0),
    )
    quote = {
        "last_price": 123.0,
        "timestamp": datetime(2026, 10, 19, 10, 0, tzinfo=IST),
    }
    instrument = {
        "tradingsymbol": "RELIANCE",
        "instrument_token": 1,
        "exchange": "NSE",
        "segment": "NSE",
        "instrument_type": "EQ",
    }
    bundle = client.fetch_symbol_bundle(
        instrument,
        now=datetime(2026, 10, 19, 10, 0, tzinfo=IST),
        quote_data=quote,
        include_daily_candles=False,
    )
    assert bundle["quote"] is quote
    assert kite.quote_calls == []
    assert [call[3] for call in kite.historical_calls] == ["5minute", "15minute"]
    assert bundle["candles_daily_60d"] == []
