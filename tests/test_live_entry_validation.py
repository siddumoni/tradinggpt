from datetime import datetime, timedelta
from dataclasses import replace
from zoneinfo import ZoneInfo

import pytest

from src.cost_calculator import ChargeRates
from src.live_entry_validation import inspect_executable_quote, revalidate_candidate_at_quote
from src.market_scanner import ScanCandidate
from src.risk_engine import RiskSettings

IST = ZoneInfo("Asia/Kolkata")


def candidate(side="LONG"):
    if side == "LONG":
        reference, stop, target_1, target_2 = 100.0, 99.0, 103.0, 104.0
        action = "CONDITIONAL_BUY"
    else:
        reference, stop, target_1, target_2 = 100.0, 101.0, 97.0, 96.0
        action = "CONDITIONAL_SHORT"
    return ScanCandidate(
        symbol="TEST",
        action=action,
        side=side,
        strategy="test_strategy",
        confidence=70,
        market_regime="BULLISH",
        signal_time="2026-11-02T11:00:00+05:30",
        reference_entry=reference,
        stop_loss=stop,
        target_1=target_1,
        target_2=target_2,
        quantity=50,
        estimated_net_risk_inr=50.0,
        estimated_net_reward_t1_inr=100.0,
        net_rr_t1=2.0,
        margin_verified=True,
        data_verified_live=True,
        news_status="CHECKED_NO_MATCH",
        news_risk="LOW",
        reasons=["deterministic test candidate"],
        market_context_status="NO_MATCH",
        market_context_risk="LOW",
        company_headline_match_status="NO_MATCH_IN_SUPPLIED_FEEDS",
        company_specific_risk="LOW",
    )


def quote(bid, ask, timestamp):
    return {
        "last_price": (bid + ask) / 2,
        "timestamp": timestamp,
        "depth": {"buy": [{"price": bid}], "sell": [{"price": ask}]},
    }


def settings():
    return RiskSettings(
        capital_inr=20_000,
        risk_per_trade_inr=200,
        minimum_net_risk_reward=1.5,
        slippage_bps_per_side=5,
    )


def validate(item, q, now, margin=50.0, verified=True):
    return revalidate_candidate_at_quote(
        item,
        q,
        margin_per_share=margin,
        settings=settings(),
        charge_rates=ChargeRates(),
        quote_verified=verified,
        now=now,
    )


def test_live_long_uses_best_ask_and_returns_full_cost_breakdown():
    now = datetime(2026, 11, 2, 11, 5, tzinfo=IST)
    result = validate(candidate("LONG"), quote(100.0, 100.1, now), now)

    assert result.action == "CONDITIONAL_BUY"
    assert result.entry_validation["status"] == "REVALIDATED"
    assert result.entry_validation["entry_price_source"] == "best_ask"
    assert result.entry_validation["executable_entry_price"] == pytest.approx(100.1)
    assert result.entry_validation["price_drift_pct"] == pytest.approx(0.1)
    assert result.quantity > 0
    assert result.entry_validation["risk_breakdown"]["costs_at_stop"]["total"] > 0
    assert result.entry_validation["risk_breakdown"]["net_rr_t1"] >= 1.5


def test_live_quote_that_erodes_net_rr_demotes_candidate_to_watch():
    now = datetime(2026, 11, 2, 11, 5, tzinfo=IST)
    result = validate(candidate("LONG"), quote(101.7, 101.8, now), now)

    assert result.action == "WATCH_CANDIDATE"
    assert result.entry_validation["status"] == "REJECTED_NET_RR"
    assert result.quantity == 0
    assert result.entry_validation["price_drift_pct"] == pytest.approx(1.8)
    assert result.entry_validation["risk_breakdown"]["costs_at_t1"] is not None
    assert result.entry_validation["risk_breakdown"]["net_rr_t1"] < 1.5


def test_live_quote_beyond_target_invalidates_original_setup():
    now = datetime(2026, 11, 2, 11, 5, tzinfo=IST)
    result = validate(candidate("LONG"), quote(103.4, 103.5, now), now)

    assert result.action == "WATCH_CANDIDATE"
    assert result.entry_validation["status"] == "REJECTED_INVALIDATED_SETUP"
    assert result.quantity == 0


def test_live_short_uses_best_bid_for_executable_entry():
    now = datetime(2026, 11, 2, 11, 5, tzinfo=IST)
    result = validate(candidate("SHORT"), quote(99.9, 100.0, now), now)

    assert result.action == "CONDITIONAL_SHORT"
    assert result.entry_validation["entry_price_source"] == "best_bid"
    assert result.entry_validation["executable_entry_price"] == pytest.approx(99.9)


def test_missing_depth_never_falls_back_to_last_traded_price():
    now = datetime(2026, 11, 2, 11, 5, tzinfo=IST)
    result = validate(candidate(), {"last_price": 100.0, "timestamp": now}, now)

    assert result.action == "WATCH_CANDIDATE"
    assert result.entry_validation["status"] == "REJECTED_EXECUTABLE_QUOTE"
    assert "cannot replace an executable quote" in result.entry_validation["reason"]


def test_stale_quote_fails_closed():
    now = datetime(2026, 11, 2, 11, 20, tzinfo=IST)
    q = quote(100.0, 100.1, now - timedelta(minutes=16))
    result = validate(candidate(), q, now)

    assert result.action == "WATCH_CANDIDATE"
    assert result.entry_validation["status"] == "REJECTED_EXECUTABLE_QUOTE"
    assert "stale" in result.entry_validation["reason"].lower()


def test_quote_inspection_rejects_crossed_market():
    now = datetime(2026, 11, 2, 11, 5, tzinfo=IST)
    result = inspect_executable_quote(quote(100.2, 100.1, now), "LONG", now=now)
    assert result["valid"] is False
    assert "crossed" in result["reason"].lower()


def test_orb_long_is_rejected_when_executable_quote_retraces_below_trigger():
    now = datetime(2026, 11, 2, 11, 5, tzinfo=IST)
    item = replace(
        candidate("LONG"),
        strategy="opening_range_breakout",
        entry_validation={"strategy_trigger": {"price": 100.5, "rule": "ABOVE"}},
    )
    result = validate(item, quote(100.0, 100.1, now), now)
    assert result.action == "WATCH_CANDIDATE"
    assert result.entry_validation["status"] == "REJECTED_BREAKOUT_RETRACEMENT"
    assert result.quantity == 0


def test_orb_short_is_rejected_when_executable_quote_retraces_above_trigger():
    now = datetime(2026, 11, 2, 11, 5, tzinfo=IST)
    item = replace(
        candidate("SHORT"),
        strategy="opening_range_breakdown",
        entry_validation={"strategy_trigger": {"price": 99.5, "rule": "BELOW"}},
    )
    result = validate(item, quote(99.9, 100.0, now), now)
    assert result.action == "WATCH_CANDIDATE"
    assert result.entry_validation["status"] == "REJECTED_BREAKOUT_RETRACEMENT"
    assert result.quantity == 0


def test_orb_entry_fails_closed_when_trigger_metadata_is_missing():
    now = datetime(2026, 11, 2, 11, 5, tzinfo=IST)
    item = replace(candidate("LONG"), strategy="opening_range_breakout")
    result = validate(item, quote(100.0, 100.1, now), now)
    assert result.action == "WATCH_CANDIDATE"
    assert result.entry_validation["status"] == "REJECTED_MISSING_BREAKOUT_LEVEL"
    assert result.quantity == 0



def test_live_entry_rejects_stop_tighter_than_configured_minimum():
    now = datetime(2026, 11, 2, 11, 5, tzinfo=IST)
    item = replace(candidate("LONG"), stop_loss=99.9)
    result = validate(item, quote(100.0, 100.1, now), now)
    assert result.action == "WATCH_CANDIDATE"
    assert result.entry_validation["status"] == "REJECTED_RISK_GATE"
    assert "Stop distance" in result.entry_validation["reason"]
    assert result.quantity == 0
