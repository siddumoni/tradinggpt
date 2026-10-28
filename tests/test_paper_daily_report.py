import csv
import json
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from src.paper_daily_report import generate_daily_report
from src.watch_shadow_portfolio import WatchShadowPortfolio

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
    assert {"champion", "challengers", "watch_shadow"} <= payload.keys()
    assert {"champion", "challengers", "watch_shadow"} <= payload["summary"].keys()
    with (tmp_path / "reports" / "paper_daily_2026-10-26_trades.csv").open(
        encoding="utf-8", newline=""
    ) as handle:
        reader = csv.DictReader(handle)
        assert "source_rejection_reason" in reader.fieldnames
        assert "risk_budget_inr" in reader.fieldnames
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
