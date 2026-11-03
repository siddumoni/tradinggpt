import csv
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from src.paper_upload_checklist import write_upload_checklist

IST = ZoneInfo("Asia/Kolkata")


def test_upload_checklist_builds_date_scoped_artifact_exports(tmp_path):
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
    for path in map(Path, report_paths.values()):
        path.write_text("test daily report", encoding="utf-8")
    (logs / "paper_daily_report_state.json").write_text('{"report_date": "2026-10-28"}', encoding="utf-8")
    (logs / "paper_scan_scheduler_state.json").write_text("{}", encoding="utf-8")

    (logs / "paper_scan_runs.csv").write_text(
        "scan_id,scan_as_of,recorded_at,candidate_count\n"
        "old,2026-10-27T15:15:00+05:30,2026-10-27T15:15:05+05:30,2\n"
        "today,2026-10-28T15:15:00+05:30,2026-10-28T15:15:05+05:30,3\n",
        encoding="utf-8",
    )
    (logs / "paper_signals.csv").write_text(
        "signal_id,scan_as_of,recorded_at,symbol\n"
        "old,2026-10-27T15:15:00+05:30,2026-10-27T15:15:05+05:30,INFY\n"
        "today,2026-10-28T15:15:00+05:30,2026-10-28T15:15:05+05:30,RELIANCE\n",
        encoding="utf-8",
    )
    (logs / "paper_trading_service.log").write_text(
        "2026-10-27 15:20:00,000 INFO old session\n"
        "2026-10-28 15:20:00,000 INFO today finalization\n"
        "Traceback (most recent call last):\n"
        "  RuntimeError: research diagnostic\n"
        "2026-10-29 09:00:00,000 INFO next session\n",
        encoding="utf-8",
    )
    (archive / "RELIANCE.csv").write_text(
        "date,open,high,low,close,volume,provider_mode\n"
        "2026-10-27T15:10:00+0530,100,101,99,100,1000,kite\n"
        "2026-10-28T09:15:00+0530,101,102,100,101,1100,kite\n"
        "2026-10-28T09:20:00+0530,101,103,100,102,1200,kite\n",
        encoding="utf-8",
    )

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
    assert "Cumulative local CSV archives are sliced to this date before upload." in body
    assert "reports/github_upload_staging/2026-10-28/candles/RELIANCE.csv" in body
    assert "SQLite paper account database (excluded from automatic upload)" in body
    assert "Destination: reports/paper_daily_2026-10-28.md" in body
    assert "When automatic upload is enabled, this checklist is included" in body

    by_label = {item["label"]: item for item in result["artifacts"]}
    assert by_label["Daily Markdown report"]["category"] == "reports"
    assert by_label["Daily Markdown report"]["upload"] is True
    assert by_label["SQLite paper account database (excluded from automatic upload)"]["upload"] is False
    assert by_label["Daily upload checklist"]["exists"] is True

    candle_export = Path(by_label["RELIANCE daily Kite candle extract"]["path"])
    with candle_export.open(encoding="utf-8", newline="") as handle:
        candle_rows = list(csv.DictReader(handle))
    assert len(candle_rows) == 2
    assert all(row["date"].startswith("2026-10-28") for row in candle_rows)

    ledger_export = Path(by_label["Daily scan-run ledger CSV"]["path"])
    with ledger_export.open(encoding="utf-8", newline="") as handle:
        ledger_rows = list(csv.DictReader(handle))
    assert len(ledger_rows) == 1
    assert ledger_rows[0]["scan_id"] == "today"

    journal_export = Path(by_label["Daily signal journal CSV"]["path"])
    with journal_export.open(encoding="utf-8", newline="") as handle:
        journal_rows = list(csv.DictReader(handle))
    assert len(journal_rows) == 1
    assert journal_rows[0]["signal_id"] == "today"

    daily_log = Path(by_label["Daily service log"]["path"]).read_text(encoding="utf-8")
    assert "today finalization" in daily_log
    assert "RuntimeError: research diagnostic" in daily_log
    assert "old session" not in daily_log
    assert "next session" not in daily_log


def test_checklist_includes_intraday_scan_json_files_as_optional_uploads(tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir()
    scan = reports / "phase6_scan_20261028_151500.json"
    scan.write_text('{"research_only": true}', encoding="utf-8")
    result = write_upload_checklist(
        root=tmp_path,
        reports_dir=reports,
        report_result=None,
        daily_state_file=tmp_path / "logs" / "daily.json",
        scan_ledger=tmp_path / "logs" / "scan_runs.csv",
        paper_journal=tmp_path / "logs" / "signals.csv",
        scheduler_state_file=tmp_path / "logs" / "scheduler.json",
        service_log=tmp_path / "logs" / "service.log",
        candle_archive_dir=tmp_path / "candles",
        db_path=tmp_path / "data" / "paper.sqlite3",
        symbols=[],
        now=datetime(2026, 10, 28, 15, 25, tzinfo=IST),
    )
    scan_item = next(
        item for item in result["artifacts"]
        if item["label"] == "Intraday scan JSON report"
    )
    assert scan_item["path"] == str(scan)
    assert scan_item["category"] == "scans"
    assert scan_item["required"] is False
    assert scan_item["upload"] is True



def test_checklist_rebuilds_staging_and_rejects_stale_daily_state(tmp_path):
    reports = tmp_path / "reports"
    logs = tmp_path / "logs"
    reports.mkdir()
    logs.mkdir()
    staging = reports / "github_upload_staging" / "2026-10-28"
    staging.mkdir(parents=True)
    stale = staging / "logs" / "paper_trading_service.log"
    stale.parent.mkdir()
    stale.write_text("stale previous finalization", encoding="utf-8")
    state = logs / "paper_daily_report_state.json"
    state.write_text('{"report_date": "2026-10-27"}', encoding="utf-8")

    result = write_upload_checklist(
        root=tmp_path,
        reports_dir=reports,
        report_result=None,
        daily_state_file=state,
        scan_ledger=logs / "paper_scan_runs.csv",
        paper_journal=logs / "paper_signals.csv",
        scheduler_state_file=logs / "scheduler.json",
        service_log=logs / "missing_service.log",
        candle_archive_dir=tmp_path / "candles",
        db_path=tmp_path / "data" / "paper.sqlite3",
        symbols=[],
        now=datetime(2026, 10, 28, 15, 25, tzinfo=IST),
    )

    state_record = next(
        item for item in result["artifacts"] if item["label"] == "Daily report state JSON"
    )
    service_record = next(
        item for item in result["artifacts"] if item["label"] == "Daily service log"
    )
    assert state_record["exists"] is False
    assert state_record["upload"] is False
    assert service_record["exists"] is False
    assert not stale.exists()
    assert result["missing_required_files"] >= 2
