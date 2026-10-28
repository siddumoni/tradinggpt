"""Build a human-readable checklist of artifacts to upload after a paper-trading run."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def write_upload_checklist(
    *,
    root: Path,
    reports_dir: Path,
    report_result: dict | None,
    daily_state_file: Path,
    scan_ledger: Path,
    paper_journal: Path,
    scheduler_state_file: Path,
    service_log: Path,
    candle_archive_dir: Path,
    db_path: Path,
    symbols: list[str],
    now: datetime | None = None,
    reason: str = "run finished",
) -> dict:
    """Write and return upload instructions, marking every expected file present/missing."""
    current = now or datetime.now(IST)
    current = current.replace(tzinfo=IST) if current.tzinfo is None else current.astimezone(IST)
    day = current.date().isoformat()
    prefix = f"paper_daily_{day}"
    report_result = report_result or {}

    artifacts: list[tuple[str, Path, bool, str]] = []
    report_paths = (
        ("Daily Markdown report", Path(report_result.get("markdown_path", reports_dir / f"{prefix}.md")), True,
         "Readable summary of champion, challenger, and watch-shadow performance"),
        ("Daily JSON report", Path(report_result.get("json_path", reports_dir / f"{prefix}.json")), True,
         "Structured results, risk overruns, events, and separate account summaries"),
        ("Combined daily trade CSV", Path(report_result.get("csv_path", reports_dir / f"{prefix}_trades.csv")), True,
         "Champion, challenger, and watch-shadow trades/candidate decisions"),
    )
    artifacts.extend(report_paths)
    artifacts.extend([
        ("Daily report state JSON", daily_state_file, True, "Final account snapshots and report-generation status"),
        ("Scan-run ledger CSV", scan_ledger, True, "Every completed/attempted scan and its status"),
        ("Signal journal CSV", paper_journal, True, "Candidate actions, directions, confidence, and rejection context"),
        ("Scheduler state JSON", scheduler_state_file, True, "Last attempted 5-minute candle boundary"),
        ("Service log", service_log, True, "Startup/shutdown, scan diagnostics, WebSocket and risk warnings"),
    ])
    for symbol in dict.fromkeys(s.upper() for s in symbols):
        artifacts.append((
            f"{symbol} Kite candle archive", candle_archive_dir / f"{symbol}.csv", True,
            f"Archived five-minute candles used by the research run for {symbol}",
        ))
    artifacts.append((
        "SQLite paper account database (optional but useful)",
        db_path,
        False,
        "Full persistent champion/challenger/watch-shadow histories and event journal",
    ))

    def display_path(path: Path) -> str:
        try:
            return str(path.resolve().relative_to(root.resolve()))
        except (ValueError, OSError):
            return str(path)

    lines = [
        f"TradingGPT post-run upload checklist — {day}",
        f"Created: {current.isoformat()}",
        f"Run ended because: {reason}",
        "",
        "Upload the READY files to ChatGPT for the next full analysis.",
        "Do not upload API credentials, access tokens, .env files, or browser/profile data.",
        "",
    ]
    ready = 0
    missing_required = 0
    for label, path, required, purpose in artifacts:
        exists = path.is_file()
        if exists:
            ready += 1
        elif required:
            missing_required += 1
        status = "READY" if exists else ("MISSING — check local run/configuration" if required else "OPTIONAL — not present")
        priority = "REQUIRED" if required else "OPTIONAL"
        lines.append(f"[{status}] [{priority}] {label}")
        lines.append(f"  Path: {display_path(path)}")
        lines.append(f"  Why: {purpose}")
    checklist_path = Path(reports_dir) / f"paper_upload_checklist_{day}.txt"
    lines.extend([
        "",
        f"Checklist file: {display_path(checklist_path)}",
        f"Files ready: {ready}/{len(artifacts)}",
        f"Required files missing: {missing_required}",
        "",
        "Research-only note: all fills/P&L are hypothetical. No real Kite orders were submitted.",
    ])
    body = "\n".join(lines) + "\n"
    checklist_path.parent.mkdir(parents=True, exist_ok=True)
    checklist_path.write_text(body, encoding="utf-8")
    return {
        "checklist_path": str(checklist_path),
        "ready_files": ready,
        "total_files": len(artifacts),
        "missing_required_files": missing_required,
        "text": body,
    }
