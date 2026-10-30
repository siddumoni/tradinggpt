from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src import advisory_engine, market_scanner
from src.strategies.models import StrategySignal
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




def test_nse_equity_universe_uses_bulk_quote_screen_and_selected_instruments(monkeypatch, tmp_path):
    now = datetime(2026, 10, 30, 11, 0, tzinfo=IST)
    captured = {}

    class FakeUniverseKite:
        def instruments(self, now=None):
            return [
                {"tradingsymbol": "INFY", "instrument_token": 408065,
                 "exchange": "NSE", "segment": "NSE", "instrument_type": "EQ"},
                {"tradingsymbol": "NIFTY 50", "instrument_token": 256265,
                 "exchange": "NSE", "segment": "INDICES", "instrument_type": "EQ"},
            ]

        def screen_liquid_nse_equities(self, instrument_rows, **kwargs):
            captured["screen_rows"] = instrument_rows
            captured["screen_kwargs"] = kwargs
            quote = {
                "last_price": 1500.0,
                "timestamp": now,
                "volume": 100000,
                "average_price": 1499.0,
                "depth": {"buy": [{"price": 1499.5}], "sell": [{"price": 1500.5}]},
            }
            return [instrument_rows[0]], {"INFY": quote}, {
                "mode": "NSE_EQ_LIQUID_TOP",
                "instruments_total": 1,
                "selected_for_technical_analysis": 1,
                "selected_symbols": ["INFY"],
                "filters": {"min_current_day_volume": kwargs["min_current_day_volume"]},
            }

        def fetch_nifty50_15m_candles(self, instrument_rows, now=None):
            return []

        def fetch_symbol_bundle(self, instrument, now=None, *, quote_data=None, include_daily_candles=True):
            captured["bundle_args"] = {
                "symbol": instrument["tradingsymbol"],
                "quote_data": quote_data,
                "include_daily_candles": include_daily_candles,
            }
            return {
                "quote": quote_data,
                "candles_5m": [],
                "candles_15m": [],
                "provider": "fake Kite",
            }

    fake = FakeUniverseKite()
    monkeypatch.setattr(
        "src.data_fetcher.KiteMarketData.from_cached_session",
        lambda: fake,
    )
    monkeypatch.setattr(
        advisory_engine, "fetch_news",
        lambda *args, **kwargs: {"items": [], "errors": []},
    )

    report = advisory_engine.run_scan(
        symbols=[],
        now=now,
        output_dir=tmp_path,
        data_provider="kite",
        universe_mode="nse_equity",
        universe_max_symbols=25,
        universe_min_current_day_volume=12345,
    )

    assert report["symbols_requested"] == ["INFY"]
    assert report["symbols_evaluated"] == 1
    assert report["universe_selection"]["mode"] == "NSE_EQ_LIQUID_TOP"
    assert report["universe_selection"]["filters"]["min_current_day_volume"] == 12345
    assert captured["screen_kwargs"]["max_symbols"] == 25
    assert captured["bundle_args"]["symbol"] == "INFY"
    assert captured["bundle_args"]["quote_data"]["last_price"] == 1500.0
    assert captured["bundle_args"]["include_daily_candles"] is False
    assert report["candidates"][0]["data_verified_live"] is True
    assert report["advisory_only"] is True
    assert report["orders_placed_or_modified"] is False


def test_nse_equity_universe_requires_kite_provider():
    with pytest.raises(ValueError, match="requires --provider kite"):
        advisory_engine.run_scan(
            symbols=[],
            now=datetime(2026, 10, 30, 11, 0, tzinfo=IST),
            data_provider="yahoo",
            universe_mode="nse_equity",
        )

def _run_directional_kite_scan(monkeypatch, tmp_path, margin_result):
    """Exercise the real scanner/risk path with deterministic data and a mocked read-only Kite adapter."""
    now = datetime(2026, 10, 26, 11, 0, tzinfo=IST)
    start = datetime(2026, 10, 26, 9, 15, tzinfo=IST)
    calls = {"margin": [], "news": 0}

    def make_candles(count, interval_minutes):
        rows = []
        for i in range(count):
            close = 10.0 + i * 0.2
            rows.append({
                "date": start + timedelta(minutes=interval_minutes * i),
                "open": close - 0.1,
                "high": close + 0.3,
                "low": close - 0.3,
                "close": close,
                "volume": 1000 + i * 10,
            })
        return rows

    candles_5m = make_candles(21, 5)   # completed through 10:55 IST
    candles_15m = make_candles(8, 15)  # current 11:00 bar is dropped by the scanner

    class FakeKite:
        def instruments(self, now=None):
            return [
                {
                    "tradingsymbol": "INFY",
                    "instrument_token": 408065,
                    "segment": "NSE",
                    "instrument_type": "EQ",
                    "exchange": "NSE",
                    "name": "Infosys",
                },
                {
                    "tradingsymbol": "NIFTY 50",
                    "instrument_token": 256265,
                    "segment": "INDICES",
                    "instrument_type": "EQ",
                    "exchange": "NSE",
                    "name": "NIFTY 50",
                },
            ]

        def fetch_nifty50_15m_candles(self, instrument_rows, now=None):
            return candles_15m

        def fetch_symbol_bundle(self, instrument, now=None):
            assert instrument["tradingsymbol"] == "INFY"
            return {
                "quote": {"last_price": 14.0, "timestamp": now},
                "candles_5m": candles_5m,
                "candles_15m": candles_15m,
                "provider": "test Kite",
            }

        def estimate_mis_margin_per_share(self, tradingsymbol, side, price):
            calls["margin"].append((tradingsymbol, side, price))
            if isinstance(margin_result, Exception):
                raise margin_result
            return margin_result

    def fake_fetch_news(*args, **kwargs):
        calls["news"] += 1
        if calls["news"] == 1:
            return {
                "items": [{
                    "title": "Indian stock market opens steady",
                    "summary": "NIFTY market context",
                }],
                "errors": [],
            }
        return {
            "items": [{
                "title": "Infosys shares trade steady",
                "summary": "Company update",
            }],
            "errors": [],
        }

    monkeypatch.setattr(
        "src.data_fetcher.KiteMarketData.from_cached_session",
        lambda: FakeKite(),
    )
    monkeypatch.setattr(advisory_engine, "fetch_news", fake_fetch_news)
    monkeypatch.setattr(market_scanner, "classify_market_regime", lambda frame: "BULLISH")
    monkeypatch.setattr(market_scanner, "_stock_15m_regime", lambda frame: "BULLISH")
    monkeypatch.setattr(
        market_scanner,
        "opening_range_signal",
        lambda symbol, candles, market_regime="UNKNOWN", diagnostics=None: StrategySignal(
            symbol=symbol,
            side="LONG",
            strategy="opening_range_breakout",
            trigger="test trigger",
            invalidation="test invalidation",
            confidence=70,
            reason="deterministic integration-test candidate",
        ),
    )
    monkeypatch.setattr(
        market_scanner, "vwap_trend_signal",
        lambda symbol, candles, market_regime="UNKNOWN", diagnostics=None: None,
    )
    monkeypatch.setattr(
        market_scanner, "vwap_pullback_signal",
        lambda symbol, candles, market_regime="UNKNOWN", diagnostics=None: None,
    )

    report = advisory_engine.run_scan(
        symbols=["INFY"],
        now=now,
        max_symbols=1,
        output_dir=tmp_path,
        data_provider="kite",
    )
    return report, calls


def test_directional_kite_candidate_passes_quote_margin_risk_and_news_gates(monkeypatch, tmp_path):
    report, calls = _run_directional_kite_scan(monkeypatch, tmp_path, margin_result=15.0)
    candidate = report["candidates"][0]

    assert report["status"] == "RESEARCH_SCAN_ONLY"
    assert report["fetch_errors"] == []
    assert candidate["data_verified_live"] is True
    assert candidate["margin_verified"] is True
    assert candidate["action"] == "CONDITIONAL_BUY"
    assert candidate["side"] == "LONG"
    assert candidate["reference_entry"] > 0
    assert candidate["stop_loss"] < candidate["reference_entry"] < candidate["target_1"] <= candidate["target_2"]
    assert candidate["quantity"] > 0
    assert candidate["estimated_net_risk_inr"] <= report["risk_settings"]["risk_per_trade_inr"]
    assert candidate["net_rr_t1"] >= report["risk_settings"]["minimum_net_risk_reward"]
    assert candidate["news_status"] not in ("NOT_CHECKED", "UNAVAILABLE")
    assert report["margin_checks"] == [{
        "symbol": "INFY",
        "side": "LONG",
        "verified": True,
        "margin_per_share_inr": 15.0,
    }]
    assert calls["margin"] == [("INFY", "LONG", candidate["reference_entry"])]
    assert report["news"]["company_news_checked"] == ["INFY"]
    assert report["advisory_only"] is True
    assert report["orders_placed_or_modified"] is False


@pytest.mark.parametrize("invalid_margin", [None, 0.0, -1.0, float("nan"), float("inf")])
def test_invalid_kite_margin_estimate_fails_closed_in_full_candidate_pipeline(
    monkeypatch, tmp_path, invalid_margin
):
    report, calls = _run_directional_kite_scan(monkeypatch, tmp_path, margin_result=invalid_margin)
    candidate = report["candidates"][0]

    assert len(calls["margin"]) == 1
    assert report["margin_checks"][0]["verified"] is False
    assert "finite positive number" in report["margin_checks"][0]["reason"]
    assert candidate["data_verified_live"] is True
    assert candidate["margin_verified"] is False
    assert candidate["action"] == "WATCH_CANDIDATE"
    assert candidate["quantity"] == 0
    assert any("Margin estimate unavailable" in reason for reason in candidate["reasons"])
    assert report["advisory_only"] is True
    assert report["orders_placed_or_modified"] is False
