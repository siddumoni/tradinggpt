import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.watch_shadow_portfolio import WatchShadowPortfolio

IST = ZoneInfo("Asia/Kolkata")


def stamp(value=None):
    value = value or datetime.now(IST)
    return value.isoformat()


def candidate(symbol="HDFCBANK", **changes):
    item = {
        "symbol": symbol,
        "action": "WATCH_CANDIDATE",
        "side": "LONG",
        "strategy": "vwap_trend",
        "confidence": 70,
        "market_regime": "BULLISH",
        "signal_time": "2026-10-27T10:00:00+05:30",
        "reference_entry": 100.0,
        "stop_loss": 99.0,
        "target_1": 101.1,
        "target_2": 102.0,
        "quantity": 0,
        "data_verified_live": True,
        "margin_verified": True,
        "news_status": "CHECKED_NO_MATCH",
        "company_specific_risk": "LOW",
        "reasons": ["Rejected: Target 1 net R:R is below 1.50."],
    }
    item.update(changes)
    return item


def quote(**changes):
    item = {
        "last_price": 100.0,
        "bid": 99.99,
        "ask": 100.01,
        "timestamp": stamp(),
        "verified_live": True,
    }
    item.update(changes)
    return item


def report_file(tmp_path, candidates, snapshots=None, *, provider="kite", orders=False):
    path = tmp_path / "scan.json"
    path.write_text(json.dumps({
        "status": "RESEARCH_SCAN_ONLY",
        "as_of": "2026-10-27T10:05:00+05:30",
        "provider": "Kite",
        "data_provider_mode": provider,
        "advisory_only": True,
        "orders_placed_or_modified": orders,
        "candidates": candidates,
        "market_snapshots": snapshots or {},
    }), encoding="utf-8")
    return path


def test_rr_only_watch_candidate_opens_in_separate_shadow_book(tmp_path):
    shadow = WatchShadowPortfolio(tmp_path / "paper.sqlite3")
    result = shadow.process_scan_report(report_file(
        tmp_path, [candidate()], {"HDFCBANK": quote()}
    ))

    assert result == {"seen": 1, "opened": 1, "excluded": 0, "duplicates": 0}
    state = shadow.snapshot()
    assert state["account"]["open_positions"] == 1
    assert state["account"]["closed_trades"] == 0
    trade = state["open_positions"][0]
    assert trade["symbol"] == "HDFCBANK"
    assert trade["quantity"] > 0
    assert trade["source_rejection_reason"].startswith("Rejected:")
    assert trade["entry_fill_price"] > trade["entry_reference_price"]
    assert state["orders_sent_to_broker"] is False


def test_watch_shadow_closes_with_costs_and_net_pnl(tmp_path):
    shadow = WatchShadowPortfolio(tmp_path / "paper.sqlite3")
    shadow.process_scan_report(report_file(tmp_path, [candidate()], {"HDFCBANK": quote()}))
    closed = shadow.update_market_price(
        "HDFCBANK", 102.2, bid=102.18, ask=102.2, market_time=datetime.now(IST)
    )
    assert closed == 1
    state = shadow.snapshot()
    trade = state["closed_trades"][0]
    assert trade["status"] == "CLOSED"
    assert trade["exit_reason"] == "TARGET_2"
    assert trade["transaction_charges"] > 0
    assert trade["slippage_cost"] > 0
    assert trade["net_pnl"] == pytest.approx(trade["gross_pnl"] - trade["total_estimated_costs"])
    assert state["account"]["open_positions"] == 0


def test_watch_shadow_deduplicates_same_signal(tmp_path):
    shadow = WatchShadowPortfolio(tmp_path / "paper.sqlite3")
    report = report_file(tmp_path, [candidate()], {"HDFCBANK": quote()})
    assert shadow.process_scan_report(report)["opened"] == 1
    second = shadow.process_scan_report(report)
    assert second["duplicates"] == 1
    assert second["opened"] == 0
    assert shadow.snapshot()["account"]["candidate_count"] == 1


def test_watch_candidate_with_other_rejection_is_excluded(tmp_path):
    shadow = WatchShadowPortfolio(tmp_path / "paper.sqlite3")
    item = candidate(reasons=["15-minute stock trend is mixed; setup suppressed."])
    result = shadow.process_scan_report(report_file(tmp_path, [item], {"HDFCBANK": quote()}))
    assert result["opened"] == 0
    state = shadow.snapshot()
    assert state["account"]["excluded_candidates"] == 1
    assert state["recent_signals"][0]["status"] == "EXCLUDED"
    assert "minimum net-R:R-only" in state["recent_signals"][0]["exclusion_reason"]


def test_watch_shadow_fails_closed_for_stale_quote_or_missing_depth(tmp_path):
    shadow = WatchShadowPortfolio(tmp_path / "paper.sqlite3", max_quote_age_seconds=60)
    stale = quote(timestamp=stamp(datetime.now(IST) - timedelta(minutes=5)))
    r1 = shadow.process_scan_report(report_file(tmp_path, [candidate()], {"HDFCBANK": stale}))
    assert r1["opened"] == 0
    assert "stale or future-dated" in shadow.snapshot()["recent_signals"][0]["exclusion_reason"]

    # A distinct signal is needed because first-seen decisions are immutable/idempotent.
    item = candidate(signal_time="2026-10-27T10:05:00+05:30")
    r2 = shadow.process_scan_report(report_file(tmp_path, [item], {"HDFCBANK": quote(bid=None, ask=None)}))
    assert r2["opened"] == 0
    assert "bid/ask" in shadow.snapshot()["recent_signals"][0]["exclusion_reason"]


def test_watch_shadow_rejects_unsafe_report_and_non_kite_data(tmp_path):
    shadow = WatchShadowPortfolio(tmp_path / "paper.sqlite3")
    with pytest.raises(ValueError, match="only accepts Kite"):
        shadow.process_scan_report(report_file(tmp_path, [], provider="yahoo"))
    with pytest.raises(ValueError, match="no-order assertion"):
        shadow.process_scan_report(report_file(tmp_path, [], orders=True))


def test_high_company_risk_is_not_shadowed(tmp_path):
    shadow = WatchShadowPortfolio(tmp_path / "paper.sqlite3")
    item = candidate(company_specific_risk="HIGH")
    result = shadow.process_scan_report(report_file(tmp_path, [item], {"HDFCBANK": quote()}))
    assert result["opened"] == 0
    assert "High company-specific news risk" in shadow.snapshot()["recent_signals"][0]["exclusion_reason"]
