from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from src.paper_upload_checklist import write_upload_checklist

IST = ZoneInfo("Asia/Kolkata")


def test_upload_checklist_marks_required_artifacts_ready_and_names_paths(tmp_path):
    reports = tmp_path / "reports"
    logs = tmp_path / "logs"
    archive = tmp_path / "data" / "paper_candles_kite"
    reports.mkdir(parents=True)
    logs.mkdir(parents=True)
    archive.mkdir(parents=True)
    report_paths = {
        "markdown_path": str(reports / "paper_daily_2026-10-28.md"),
        "json_path": str(reports / "paper_daily_2026-10-28.json"),
        "csv_path": str(reports / "paper_daily_2026-10-28_trades.csv"),
    }
    required_paths = [
        *map(Path, report_paths.values()),
        logs / "paper_daily_report_state.json",
        logs / "paper_scan_runs.csv",
        logs / "paper_signals.csv",
        logs / "paper_scan_scheduler_state.json",
        logs / "paper_trading_service.log",
        archive / "RELIANCE.csv",
    ]
    for path in required_paths:
        path.write_text("test", encoding="utf-8")

    result = write_upload_checklist(
        root=tmp_path,
        reports_dir=reports,
        report_result=report_paths,
        daily_state_file=logs / "paper_daily_report_state.json",
        scan_ledger=logs / "paper_scan_runs.csv",
        paper_journal=logs / "paper_signals.csv",
        scheduler_state_file=logs / "paper_scan_scheduler_state.json",
        service_log=logs / "paper_trading_service.log",
        candle_archive_dir=archive,
        db_path=tmp_path / "data" / "paper_trading.sqlite3",
        symbols=["RELIANCE"],
        now=datetime(2026, 10, 28, 15, 25, tzinfo=IST),
        reason="market close daily summary",
    )
    assert result["missing_required_files"] == 0
    assert result["checklist_path"] == str(reports / "paper_upload_checklist_2026-10-28.txt")
    body = Path(result["checklist_path"]).read_text(encoding="utf-8")
    assert "[READY] [REQUIRED] Daily Markdown report" in body
    assert "logs/paper_scan_runs.csv" in body
    assert "data/paper_candles_kite/RELIANCE.csv" in body
    assert "SQLite paper account database (optional but useful)" in body
