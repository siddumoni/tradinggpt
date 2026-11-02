import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.paper_broker import PaperBroker

IST = ZoneInfo("Asia/Kolkata")


def _stamp():
    return datetime.now(IST).isoformat()


def _candidate(symbol, *, confidence=70, rr=2.0, side="LONG", signal_time=None):
    if side == "LONG":
        action, stop, target_1, target_2 = "CONDITIONAL_BUY", 98.0, 104.0, 108.0
    else:
        action, stop, target_1, target_2 = "CONDITIONAL_SHORT", 102.0, 96.0, 94.0
    return {
        "symbol": symbol,
        "action": action,
        "side": side,
        "strategy": "vwap_trend",
        "confidence": confidence,
        "market_regime": "BULLISH",
        "signal_time": signal_time or _stamp(),
        "reference_entry": 100.0,
        "stop_loss": stop,
        "target_1": target_1,
        "target_2": target_2,
        "net_rr_t1": rr,
        "data_verified_live": True,
        "margin_verified": True,
        "company_specific_risk": "LOW",
        "market_context_risk": "LOW",
        "reasons": ["test candidate"],
    }


def _quote(**changes):
    quote = {
        "last_price": 100.0,
        "bid": 99.99,
        "ask": 100.01,
        "timestamp": _stamp(),
        "verified_live": True,
    }
    quote.update(changes)
    return quote


def _report(tmp_path, candidates, snapshots, universe=None):
    path = Path(tmp_path) / "scan.json"
    path.write_text(json.dumps({
        "status": "RESEARCH_SCAN_ONLY",
        "as_of": _stamp(),
        "provider": "Kite",
        "data_provider_mode": "kite",
        "advisory_only": True,
        "orders_placed_or_modified": False,
        "candidates": candidates,
        "market_snapshots": snapshots,
        "universe_selection": universe or {},
        "symbols_requested": (universe or {}).get("selected_symbols", []),
    }), encoding="utf-8")
    return path


def test_ranked_candidate_is_processed_before_lower_quality_candidate(tmp_path):
    broker = PaperBroker(tmp_path / "paper.sqlite3")
    lower = _candidate("LOWER", confidence=60, rr=1.6, signal_time="2026-11-02T10:00:00+05:30")
    stronger = _candidate("STRONGER", confidence=90, rr=2.7, side="SHORT", signal_time="2026-11-02T10:05:00+05:30")

    result = broker.process_scan_report(_report(
        tmp_path,
        [lower, stronger],  # Deliberately opposite to the expected allocation order.
        {"LOWER": _quote(), "STRONGER": _quote()},
    ))

    assert result["opened"] == 2
    assert result["allocation_method"] == "quality_first_greedy_with_shared_cash_and_risk_caps"
    assert [item["symbol"] for item in result["allocation_order"]] == ["STRONGER", "LOWER"]
    assert result["allocation_order"][0]["allocation_score"] > result["allocation_order"][1]["allocation_score"]
    state = broker.snapshot()
    ranked_events = [event for event in reversed(state["events"]) if event["event_type"] == "ALLOCATION_RANKED"]
    assert [event["symbol"] for event in ranked_events] == ["STRONGER", "LOWER"]


def test_shared_portfolio_risk_cap_reduces_later_position_risk(tmp_path):
    broker = PaperBroker(
        tmp_path / "paper.sqlite3",
        initial_capital=100_000,
        risk_per_trade=500,
        max_open_positions=3,
        max_portfolio_risk_inr=650,
    )
    first = _candidate("FIRST", confidence=95, rr=2.8, signal_time="2026-11-02T10:00:00+05:30")
    second = _candidate("SECOND", confidence=80, rr=2.2, signal_time="2026-11-02T10:05:00+05:30", side="SHORT")

    result = broker.process_scan_report(_report(
        tmp_path,
        [first, second],
        {"FIRST": _quote(), "SECOND": _quote()},
    ))

    state = broker.snapshot()
    account = state["account"]
    assert result["opened"] == 2
    assert account["starting_capital"] == 100_000
    assert account["open_planned_risk_inr"] <= 650
    assert account["max_portfolio_risk_inr"] == 650
    assert account["available_portfolio_risk_inr"] == pytest.approx(
        650 - account["open_planned_risk_inr"]
    )
    assert len(state["open_positions"]) == 2
    risks = [position["planned_net_risk_inr"] for position in state["open_positions"]]
    budgets = [position["risk_budget_inr"] for position in state["open_positions"]]
    assert all(0 < value <= 500 for value in risks)
    assert all(0 < risk <= budget <= 500 for risk, budget in zip(risks, budgets))
    assert budgets[1] < budgets[0]  # Later entries only receive the remaining portfolio-risk budget.
    assert sum(risks) <= 650


def test_default_portfolio_risk_cap_tracks_open_slots_and_per_trade_risk(tmp_path):
    broker = PaperBroker(
        tmp_path / "paper.sqlite3",
        risk_per_trade=500,
        max_open_positions=3,
    )
    assert broker.max_portfolio_risk_inr == pytest.approx(1_500)


@pytest.mark.parametrize(
    "quote_changes,reason",
    [
        ({"bid": None}, "Executable bid/ask"),
        ({"ask": None}, "Executable bid/ask"),
        ({"bid": 100.02, "ask": 99.99}, "crossed"),
    ],
)
def test_paper_entry_requires_a_valid_non_crossed_executable_quote(tmp_path, quote_changes, reason):
    broker = PaperBroker(tmp_path / "paper.sqlite3")
    candidate = _candidate("BADQUOTE")
    result = broker.process_scan_report(_report(
        tmp_path,
        [candidate],
        {"BADQUOTE": _quote(**quote_changes)},
    ))
    assert result["opened"] == 0
    assert result["rejected"] == 1
    assert any(reason.lower() in event["message"].lower() for event in broker.snapshot()["events"])


def test_paper_broker_tracks_current_top_n_and_keeps_open_position_symbols(tmp_path):
    broker = PaperBroker(tmp_path / "paper.sqlite3")
    first = _candidate("FIRST", confidence=90, rr=2.5, signal_time="2026-11-02T10:00:00+05:30")
    first_universe = {"mode": "nse_equity", "selected_symbols": ["FIRST", "SECOND"]}
    result = broker.process_scan_report(_report(
        tmp_path, [first], {"FIRST": _quote()}, universe=first_universe
    ))
    assert result["opened"] == 1
    assert set(broker.monitored_symbols()) == {"FIRST", "SECOND"}

    next_universe = {"mode": "nse_equity", "selected_symbols": ["THIRD", "FOURTH"]}
    broker.process_scan_report(_report(tmp_path, [], {}, universe=next_universe))
    assert set(broker.monitored_symbols()) == {"FIRST", "THIRD", "FOURTH"}
