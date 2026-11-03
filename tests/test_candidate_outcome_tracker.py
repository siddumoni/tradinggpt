import json
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from src.candidate_outcome_tracker import CandidateOutcomeTracker

IST = ZoneInfo("Asia/Kolkata")


def candidate(symbol, strategy="vwap_ema_momentum", side="LONG", **changes):
    item = {
        "symbol": symbol,
        "action": "WATCH_CANDIDATE",
        "side": side,
        "strategy": strategy,
        "signal_time": "2026-11-03T09:30:00+05:30",
        "reference_entry": 100.0,
        "stop_loss": 99.5 if side == "LONG" else 100.5,
        "target_1": 101.5 if side == "LONG" else 98.5,
        "target_2": 103.0 if side == "LONG" else 97.0,
        "data_verified_live": True,
        "margin_verified": True,
        "news_status": "CHECKED_MATCHES",
        "news_risk": "LOW",
        "company_specific_risk": "LOW",
        "reasons": ["Rejected: Target 1 net R:R is below 1.50."],
    }
    item.update(changes)
    return item


def write_report(tmp_path: Path, candidates):
    path = tmp_path / "scan.json"
    path.write_text(json.dumps({
        "status": "RESEARCH_SCAN_ONLY",
        "as_of": "2026-11-03T09:31:00+05:30",
        "data_provider_mode": "kite",
        "orders_placed_or_modified": False,
        "candidates": candidates,
    }), encoding="utf-8")
    return path


def write_candles(path: Path, *, target=False, gap=False, last_time="10:00"):
    start = pd.Timestamp("2026-11-03T09:15:00+05:30")
    end = pd.Timestamp(f"2026-11-03T{last_time}:00+05:30")
    index = pd.date_range(start, end, freq="5min")
    if gap and len(index) > 5:
        index = index.delete(5)
    frame = pd.DataFrame({
        "open": [100.0] * len(index),
        "high": [100.5] * len(index),
        "low": [99.9] * len(index),
        "close": [100.0] * len(index),
        "volume": [1000] * len(index),
        "provider_mode": ["kite"] * len(index),
    }, index=index)
    if target:
        stamp = pd.Timestamp("2026-11-03T09:40:00+05:30")
        frame.loc[stamp, ["high", "close"]] = [103.5, 103.2]
    frame.index.name = "date"
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.reset_index().to_csv(path, index=False)


def read_rows(db_path):
    with sqlite3.connect(db_path) as db:
        return db.execute(
            "SELECT symbol,status,net_pnl,exit_reason FROM candidate_outcomes ORDER BY symbol"
        ).fetchall()


def test_independent_tracker_evaluates_more_than_three_candidates(tmp_path):
    db_path = tmp_path / "paper.sqlite3"
    archive = tmp_path / "candles"
    tracker = CandidateOutcomeTracker(db_path)
    names = ["INFY", "SYRMA", "CDSL", "GROWW"]
    report = write_report(tmp_path, [candidate(name) for name in names])

    first = tracker.process_scan_report(report)
    second = tracker.process_scan_report(report)
    assert first == {"seen": 4, "eligible": 4, "excluded": 0, "duplicates": 0}
    assert second == {"seen": 4, "eligible": 0, "excluded": 0, "duplicates": 4}

    for name in names:
        write_candles(archive / f"{name}.csv")
    result = tracker.process_archive(archive, now=datetime(2026, 11, 3, 10, 5, tzinfo=IST))

    assert result["open_or_pending"] == 4
    assert tracker.open_symbols() == sorted(names)
    state = tracker.snapshot("2026-11-03")
    assert state["eligible_observations"] == 4
    assert state["open_outcomes"] == 4
    assert state["independent_single_candidate_model"] is True
    assert state["aggregate_pnl_is_not_portfolio_pnl"] is True


def test_independent_tracker_closes_with_costs_at_target_two(tmp_path):
    db_path = tmp_path / "paper.sqlite3"
    archive = tmp_path / "candles"
    tracker = CandidateOutcomeTracker(db_path)
    tracker.process_scan_report(write_report(tmp_path, [candidate("INFY")]))
    write_candles(archive / "INFY.csv", target=True)

    tracker.process_archive(archive, now=datetime(2026, 11, 3, 10, 5, tzinfo=IST))
    row = read_rows(db_path)[0]
    assert row[1] == "CLOSED"
    assert row[2] is not None
    assert row[3] == "TARGET_2"
    with sqlite3.connect(db_path) as db:
        detail = db.execute(
            "SELECT transaction_charges,slippage_cost,total_estimated_costs FROM candidate_outcomes"
        ).fetchone()
    assert all(value > 0 for value in detail)
    assert tracker.snapshot("2026-11-03")["closed_outcomes"] == 1


def test_missing_intraday_candle_marks_candidate_unscored(tmp_path):
    db_path = tmp_path / "paper.sqlite3"
    archive = tmp_path / "candles"
    tracker = CandidateOutcomeTracker(db_path)
    tracker.process_scan_report(write_report(tmp_path, [candidate("INFY")]))
    write_candles(archive / "INFY.csv", gap=True)

    tracker.process_archive(archive, now=datetime(2026, 11, 3, 10, 5, tzinfo=IST))
    row = read_rows(db_path)[0]
    assert row[1] == "INCOMPLETE_DATA_GAP"
    assert row[2] is None
    assert tracker.snapshot("2026-11-03")["incomplete_data_gaps"] == 1


def test_session_finalization_needs_final_1510_bar_and_assigns_no_pnl(tmp_path):
    db_path = tmp_path / "paper.sqlite3"
    archive = tmp_path / "candles"
    tracker = CandidateOutcomeTracker(db_path)
    tracker.process_scan_report(write_report(tmp_path, [candidate("INFY")]))
    write_candles(archive / "INFY.csv", last_time="12:20")

    result = tracker.process_archive(
        archive, now=datetime(2026, 11, 3, 15, 20, tzinfo=IST), finalize_session=True
    )
    row = read_rows(db_path)[0]
    assert row[1] == "INCOMPLETE_DATA_GAP"
    assert row[2] is None
    assert result["open_or_pending"] == 0
    assert result["incomplete_finalized"] == 1


def test_only_rr_only_candidates_enter_the_independent_tracker(tmp_path):
    tracker = CandidateOutcomeTracker(tmp_path / "paper.sqlite3")
    report = write_report(tmp_path, [
        candidate("INFY"),
        candidate("TCS", reasons=["15-minute stock trend is mixed; setup suppressed."]),
        candidate("SBIN", margin_verified=False),
    ])
    counts = tracker.process_scan_report(report)
    assert counts == {"seen": 3, "eligible": 1, "excluded": 2, "duplicates": 0}
    state = tracker.snapshot("2026-11-03")
    assert state["eligible_observations"] == 1
    assert state["excluded"] == 2
