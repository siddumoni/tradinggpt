import csv
import json
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src.paper_daily_report import generate_daily_report
from src.watch_shadow_portfolio import WatchShadowPortfolio
from src.candidate_outcome_tracker import CandidateOutcomeTracker

IST = ZoneInfo("Asia/Kolkata")


def test_daily_report_writes_json_csv_markdown_and_separate_tracks(tmp_path):
    out = generate_daily_report(
        tmp_path / "paper.sqlite3",
        tmp_path / "reports",
        datetime(2026, 10, 26, 16, 0, tzinfo=IST),
    )
    assert out["champion_closed_trades"] == 0 and out["shadow_closed_trades"] == 0
    assert out["watch_shadow_closed_trades"] == 0
    base = tmp_path / "reports" / "paper_daily_2026-10-26"
    assert base.with_suffix(".json").is_file()
    assert base.with_suffix(".md").is_file()
    assert (tmp_path / "reports" / "paper_daily_2026-10-26_trades.csv").is_file()
    payload = json.loads(base.with_suffix(".json").read_text(encoding="utf-8"))
    assert payload["orders_sent_to_broker"] is False
    assert {"champion", "challengers", "watch_shadow", "candidate_outcomes"} <= payload.keys()
    assert {"champion", "challengers", "watch_shadow", "candidate_outcomes"} <= payload["summary"].keys()
    with (tmp_path / "reports" / "paper_daily_2026-10-26_trades.csv").open(
        encoding="utf-8", newline=""
    ) as handle:
        reader = csv.DictReader(handle)
        assert "source_rejection_reason" in reader.fieldnames
        assert "risk_budget_inr" in reader.fieldnames
        assert "stop_loss" in reader.fieldnames
        assert "target_1" in reader.fieldnames
        assert "target_2" in reader.fieldnames
        assert "stop_distance_pct_from_entry_fill" in reader.fieldnames
    markdown = base.with_suffix(".md").read_text(encoding="utf-8")
    assert "## Champion account" in markdown
    assert "## Challenger strategy lab" in markdown
    assert "## Watch-candidate shadow book" in markdown


def test_daily_report_includes_watch_shadow_trade_and_risk_overrun(tmp_path):
    db_path = tmp_path / "paper.sqlite3"
    WatchShadowPortfolio(db_path)
    now = datetime.now(IST)
    timestamp = now.replace(hour=12, minute=0, second=0, microsecond=0).isoformat()
    exit_time = now.replace(hour=12, minute=10, second=0, microsecond=0).isoformat()
    with sqlite3.connect(db_path) as db:
        db.execute(
            """INSERT INTO watch_shadow_trades(
                 signal_id,symbol,side,strategy,signal_time,scan_as_of,status,
                 risk_budget_inr,source_rejection_reason,exit_time,exit_reason,
                 gross_pnl,transaction_charges,slippage_cost,total_estimated_costs,
                 net_pnl,created_at,updated_at)
               VALUES(?,?,?,?,? ,?,'CLOSED',?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "test-watch-shadow-loss", "RELIANCE", "SHORT", "vwap_ema_momentum",
                timestamp, timestamp, 500.0, "Rejected by minimum after-cost R:R",
                exit_time, "STOP_LOSS", -344.4, 82.56, 98.66, 181.22, -525.62,
                timestamp, exit_time,
            ),
        )
    out = generate_daily_report(db_path, tmp_path / "reports", now)
    payload = json.loads((tmp_path / "reports" / f"paper_daily_{now.date().isoformat()}.json").read_text())
    assert out["watch_shadow_closed_trades"] == 1
    assert out["watch_shadow_net_pnl"] == -525.62
    assert payload["summary"]["watch_shadow"]["closed_trades"] == 1
    assert payload["summary"]["watch_shadow"]["net_pnl"] == -525.62
    assert payload["summary"]["risk_overrun_count"] == 1
    assert payload["risk_overruns"][0]["track"] == "WATCH_SHADOW"
    with (tmp_path / "reports" / f"paper_daily_{now.date().isoformat()}_trades.csv").open(
        encoding="utf-8", newline=""
    ) as handle:
        rows = list(csv.DictReader(handle))
    assert any(row["track"] == "WATCH_SHADOW" and row["symbol"] == "RELIANCE" for row in rows)
    markdown = (tmp_path / "reports" / f"paper_daily_{now.date().isoformat()}.md").read_text()
    assert "exceeded planned risk" in markdown



def test_daily_report_includes_independent_candidate_outcomes_without_portfolio_claim(tmp_path):
    db_path = tmp_path / "paper.sqlite3"
    CandidateOutcomeTracker(db_path)
    now = datetime(2026, 11, 3, 16, 0, tzinfo=IST)
    stamp = now.replace(hour=10, minute=0, second=0, microsecond=0).isoformat()
    with sqlite3.connect(db_path) as db:
        db.execute(
            """INSERT INTO candidate_outcomes(
                 candidate_id,symbol,side,strategy,signal_time,scan_as_of,status,
                 risk_budget_inr,source_rejection_reason,exclusion_reason,created_at,updated_at)
               VALUES(?,?,?,?,?,?,'INCOMPLETE_DATA_GAP',?,?,?,?,?)""",
            (
                "independent-outcome-gap", "SYRMA", "SHORT", "vwap_ema_momentum",
                stamp, stamp, 500.0, "Rejected: Target 1 net R:R is below 1.50.",
                "Missing candle after 12:20; outcome unscored.", stamp, stamp,
            ),
        )

    out = generate_daily_report(db_path, tmp_path / "reports", now)
    report_path = tmp_path / "reports" / f"paper_daily_{now.date().isoformat()}"
    payload = json.loads(report_path.with_suffix(".json").read_text(encoding="utf-8"))
    assert out["candidate_outcomes_eligible_today"] == 1
    assert out["candidate_outcomes_closed_today"] == 0
    assert out["candidate_outcomes_incomplete_today"] == 1
    assert payload["summary"]["candidate_outcomes"]["incomplete_data_gaps"] == 1
    assert payload["summary"]["candidate_outcomes"]["invalidated_legacy_method"] == 0
    assert out["candidate_outcomes_invalidated_legacy_today"] == 0
    assert payload["candidate_outcomes"]["aggregate_pnl_is_not_portfolio_pnl"] is True
    with (tmp_path / "reports" / f"paper_daily_{now.date().isoformat()}_trades.csv").open(
        encoding="utf-8", newline=""
    ) as handle:
        rows = list(csv.DictReader(handle))
    row = next(row for row in rows if row["track"] == "CANDIDATE_OUTCOME")
    assert row["symbol"] == "SYRMA"
    assert row["status"] == "INCOMPLETE_DATA_GAP"
    assert row["net_pnl"] == ""
    markdown = report_path.with_suffix(".md").read_text(encoding="utf-8")
    assert "## Independent candidate outcome tracker" in markdown
    assert "do not sum their P&L as a portfolio result" in markdown


def test_daily_report_separates_new_champion_from_archived_foundation_candidate(tmp_path):
    from src.paper_broker import PAPER_BASELINE_VERSION, PaperBroker

    db_path = tmp_path / "paper.sqlite3"
    PaperBroker(db_path)
    now = datetime(2026, 11, 6, 16, 0, tzinfo=IST)
    entry_time = datetime(2026, 11, 6, 10, 0, tzinfo=IST).isoformat()
    with sqlite3.connect(db_path) as db:
        for signal_id, symbol, version in (
            ("active-v2", "TCS", PAPER_BASELINE_VERSION),
            ("archived-foundation", "INFY", "nse_champion_v2_foundation_candidate"),
        ):
            db.execute(
                """INSERT INTO trades(
                    signal_id,strategy,strategy_version,symbol,side,status,signal_time,entry_time,
                    signal_reference_entry,entry_reference_price,entry_fill_price,stop_loss,target_1,target_2,
                    quantity,risk_budget_inr,position_value_inr,planned_net_risk_inr,market_regime,
                    entry_reason,created_at
                ) VALUES(?,?,?,?,?,'OPEN',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    signal_id, "vwap_trend", version, symbol, "LONG",
                    entry_time, entry_time, 100.0, 100.0, 100.05,
                    98.0, 104.0, 108.0, 10, 500.0, 1000.5, 200.0,
                    "BULLISH", "version-isolation test", entry_time,
                ),
            )

    output_dir = tmp_path / "reports"
    generate_daily_report(db_path, output_dir, now)
    report_base = output_dir / "paper_daily_2026-11-06"
    payload = json.loads(report_base.with_suffix(".json").read_text(encoding="utf-8"))

    assert payload["champion"]["strategy_version"] == "nse_champion_v2"
    assert payload["champion_archive"]["performance_must_not_be_pooled_with_nse_champion_v2"] is True
    assert payload["champion"]["closed_trades_today"] == 0
    assert payload["champion_archive"]["closed_trades_today"] == 0
    with (output_dir / "paper_daily_2026-11-06_trades.csv").open(
        encoding="utf-8", newline=""
    ) as handle:
        rows = list(csv.DictReader(handle))
    tracks = {row["symbol"]: row["track"] for row in rows}
    assert tracks["TCS"] == "CHAMPION"
    assert tracks["INFY"] == "CHAMPION_ARCHIVE"
    tcs = next(row for row in rows if row["symbol"] == "TCS")
    assert float(tcs["stop_loss"]) == 98.0
    assert float(tcs["target_1"]) == 104.0
    assert float(tcs["target_2"]) == 108.0
    assert float(tcs["stop_distance_pct_from_entry_fill"]) == pytest.approx(2.049, abs=0.001)
