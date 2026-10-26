from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from src import advisory_engine
from src.advisory_engine import _kite_quote_verification

IST = ZoneInfo("Asia/Kolkata")


def test_kite_quote_verification_accepts_recent_positive_quote():
    now = datetime(2026, 10, 22, 10, 0, tzinfo=IST)
    verified, warning = _kite_quote_verification(
        {"last_price": 1500.0, "timestamp": datetime(2026, 10, 22, 9, 59, tzinfo=IST)},
        now,
    )
    assert verified is True
    assert warning == ""


def test_kite_quote_verification_fails_closed_for_stale_or_missing_values():
    now = datetime(2026, 10, 22, 10, 0, tzinfo=IST)
    stale, stale_warning = _kite_quote_verification(
        {"last_price": 1500.0, "timestamp": datetime(2026, 10, 22, 9, 30, tzinfo=IST)},
        now,
    )
    missing, missing_warning = _kite_quote_verification(None, now)
    invalid, invalid_warning = _kite_quote_verification(
        {"last_price": 0, "timestamp": now}, now,
    )
    assert stale is False and "stale" in stale_warning
    assert missing is False and "missing" in missing_warning
    assert invalid is False and "last price" in invalid_warning


def test_kite_provider_uses_authenticated_read_only_client_and_marks_quote_status(monkeypatch, tmp_path):
    now = datetime(2026, 10, 22, 10, 0, tzinfo=IST)
    captured = {}

    class FakeKite:
        def instruments(self, now=None):
            return [{
                "tradingsymbol": "INFY",
                "instrument_token": 408065,
                "segment": "NSE",
                "instrument_type": "EQ",
            }]

        def fetch_nifty50_15m_candles(self, instrument_rows, now=None):
            return []

        def fetch_symbol_bundle(self, instrument, now=None):
            captured["instrument"] = instrument
            return {
                "quote": {"last_price": 1500.0, "timestamp": now},
                "candles_5m": [],
                "candles_15m": [],
                "provider": "fake",
            }

    fake = FakeKite()
    monkeypatch.setattr(
        "src.data_fetcher.KiteMarketData.from_cached_session",
        lambda: fake,
    )
    monkeypatch.setattr(advisory_engine, "_fetch_nifty_15m", lambda _now: [])
    monkeypatch.setattr(
        advisory_engine, "fetch_news",
        lambda *args, **kwargs: {"items": [], "errors": []},
    )

    report = advisory_engine.run_scan(
        symbols=["INFY"], now=now, max_symbols=1, output_dir=tmp_path,
        data_provider="kite",
    )

    assert captured["instrument"]["tradingsymbol"] == "INFY"
    assert report["data_provider_mode"] == "kite"
    assert "Kite Connect" in report["stock_data_provider"]
    assert "Kite Connect" in report["index_regime_provider"]
    assert report["fetch_errors"] == []
    assert report["candidates"][0]["data_verified_live"] is True
    assert report["candidates"][0]["action"] == "NO_TRADE"


def test_kite_provider_skips_symbols_not_in_nse_equity_dump(monkeypatch, tmp_path):
    now = datetime(2026, 10, 22, 10, 0, tzinfo=IST)

    class FakeKite:
        def instruments(self, now=None):
            return [{
                "tradingsymbol": "INFY",
                "instrument_token": 408065,
                "segment": "NSE",
                "instrument_type": "EQ",
            }]

        def fetch_nifty50_15m_candles(self, instrument_rows, now=None):
            return []

        def fetch_symbol_bundle(self, instrument, now=None):
            raise AssertionError("missing symbols must not trigger a data fetch")

    monkeypatch.setattr(
        "src.data_fetcher.KiteMarketData.from_cached_session",
        lambda: FakeKite(),
    )
    monkeypatch.setattr(advisory_engine, "_fetch_nifty_15m", lambda _now: [])
    monkeypatch.setattr(
        advisory_engine, "fetch_news",
        lambda *args, **kwargs: {"items": [], "errors": []},
    )

    report = advisory_engine.run_scan(
        symbols=["NOTAREALSYMBOL"], now=now, max_symbols=1, output_dir=tmp_path,
        data_provider="kite",
    )

    assert report["symbols_evaluated"] == 0
    assert any("not found as an NSE equity" in error for error in report["fetch_errors"])
    assert report["candidates"] == []

def test_live_quote_verification_uses_observation_time_not_scan_start(monkeypatch, tmp_path):
    # The quote arrives 90 seconds after scan start. It is fresh at retrieval
    # time, even though it appears to be in the future relative to scan start.
    scan_start = datetime(2026, 10, 26, 12, 28, 0, tzinfo=IST)
    quote_check = datetime(2026, 10, 26, 12, 30, 0, tzinfo=IST)
    times = [scan_start, quote_check]
    RealDatetime = datetime

    class ControlledDatetime(RealDatetime):
        @classmethod
        def now(cls, tz=None):
            value = times.pop(0)
            return cls.fromtimestamp(value.timestamp(), tz) if tz else cls.fromtimestamp(value.timestamp())

    monkeypatch.setattr(advisory_engine, "datetime", ControlledDatetime)

    class FakeKite:
        def instruments(self, now=None):
            return [{
                "tradingsymbol": "INFY",
                "instrument_token": 408065,
                "segment": "NSE",
                "instrument_type": "EQ",
            }]

        def fetch_nifty50_15m_candles(self, instrument_rows, now=None):
            return []

        def fetch_symbol_bundle(self, instrument, now=None):
            return {
                "quote": {
                    "last_price": 1500.0,
                    "timestamp": now + timedelta(seconds=90),
                },
                "candles_5m": [],
                "candles_15m": [],
                "provider": "fake",
            }

    fake = FakeKite()
    monkeypatch.setattr(
        "src.data_fetcher.KiteMarketData.from_cached_session",
        lambda: fake,
    )
    monkeypatch.setattr(
        advisory_engine, "fetch_news",
        lambda *args, **kwargs: {"items": [], "errors": []},
    )

    report = advisory_engine.run_scan(
        symbols=["INFY"],
        now=None,
        max_symbols=1,
        output_dir=tmp_path,
        data_provider="kite",
    )

    assert report["fetch_errors"] == []
    assert report["candidates"][0]["data_verified_live"] is True
    assert not any(
        "timestamp is in the future" in reason
        for reason in report["candidates"][0]["reasons"]
    )

