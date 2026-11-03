"""Build a human-readable checklist and structured manifest of daily artifacts."""
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
    """Write the checklist and return upload-ready artifact metadata.

    The local SQLite database remains explicitly excluded from automatic upload.
    """
    current = now or datetime.now(IST)
    current = current.replace(tzinfo=IST) if current.tzinfo is None else current.astimezone(IST)
    day = current.date().isoformat()
    date_stamp = current.strftime("%Y%m%d")
    prefix = f"paper_daily_{day}"
    report_result = report_result or {}

    # label, path, required, purpose, remote category, upload automatically
    artifacts: list[tuple[str, Path, bool, str, str, bool]] = [
        ("Daily Markdown report", Path(report_result.get("markdown_path", reports_dir / f"{prefix}.md")), True,
         "Readable summary of champion, challenger, and watch-shadow performance", "reports", True),
        ("Daily JSON report", Path(report_result.get("json_path", reports_dir / f"{prefix}.json")), True,
         "Structured results, risk overruns, events, and separate account summaries", "reports", True),
        ("Combined daily trade CSV", Path(report_result.get("csv_path", reports_dir / f"{prefix}_trades.csv")), True,
         "Champion, challenger, and watch-shadow trades/candidate decisions", "reports", True),
        ("Daily report state JSON", daily_state_file, True,
         "Final account snapshots and report-generation status", "diagnostics", True),
        ("Scan-run ledger CSV", scan_ledger, True,
         "Every completed/attempted scan and its status", "diagnostics", True),
        ("Signal journal CSV", paper_journal, True,
         "Candidate actions, directions, confidence, and rejection context", "diagnostics", True),
        ("Scheduler state JSON", scheduler_state_file, True,
         "Last attempted 5-minute candle boundary", "diagnostics", True),
        ("Service log", service_log, True,
         "Startup/shutdown, scan diagnostics, WebSocket and risk warnings", "logs", True),
    ]

    # Add every scan JSON generated for this IST date. It is optional because a
    # failed/zero-output scan may legitimately have no report file.
    for scan_path in sorted(Path(reports_dir).glob(f"phase6_scan_{date_stamp}_*.json")):
        artifacts.append((
            "Intraday scan JSON report", scan_path, False,
            "Full per-scan universe selection, candidate decisions and diagnostics",
            "scans", True,
        ))

    selected = set(dict.fromkeys(s.upper() for s in symbols))
    candle_candidates: dict[str, tuple[Path, bool]] = {}
    for symbol in selected:
        candle_candidates[f"{symbol}.csv"] = (
            Path(candle_archive_dir) / f"{symbol}.csv", True
        )
    # Universe top-N can change during the session. Include other candle files
    # touched on this date even if that symbol is no longer selected at 15:20.
    for candle_path in sorted(Path(candle_archive_dir).glob("*.csv")):
        try:
            modified_today = datetime.fromtimestamp(candle_path.stat().st_mtime, IST).date() == current.date()
        except OSError:
            modified_today = False
        if modified_today:
            candle_candidates.setdefault(candle_path.name, (candle_path, False))
    for candle_path, required in candle_candidates.values():
        symbol = candle_path.stem.upper()
        artifacts.append((
            f"{symbol} Kite candle archive", candle_path, required,
            f"Archived five-minute candles used by the research run for {symbol}",
            "candles", True,
        ))

    artifacts.append((
        "SQLite paper account database (excluded from automatic upload)",
        db_path,
        False,
        "Contains the full persistent account ledger; intentionally kept local by default",
        "diagnostics",
        False,
    ))

    def display_path(path: Path) -> str:
        try:
            return path.resolve().relative_to(root.resolve()).as_posix()
        except (ValueError, OSError):
            return str(path)

    lines = [
        f"TradingGPT post-run upload checklist — {day}",
        f"Created: {current.isoformat()}",
        f"Checklist trigger: {reason}",
        "",
        "When configured and all REQUIRED files are present, READY artifacts are uploaded to the PRIVATE GitHub research repository.",
        "The upload creates/updates daily/" + day + "/ in that repository.",
        "Do not upload API credentials, access tokens, .env files, or browser/profile data.",
        "The SQLite paper-account database is intentionally excluded from automatic upload.",
        "",
    ]
    ready = 0
    missing_required = 0
    for label, path, required, purpose, category, upload in artifacts:
        exists = path.is_file()
        if exists:
            ready += 1
        elif required:
            missing_required += 1
        status = "READY" if exists else (
            "MISSING — check local run/configuration" if required else "OPTIONAL — not present"
        )
        priority = "REQUIRED" if required else "OPTIONAL"
        upload_note = "Included in automatic upload" if upload else "Kept local"
        lines.append(f"[{status}] [{priority}] {label}")
        lines.append(f"  Path: {display_path(path)}")
        lines.append(f"  Destination: {category}/" + path.name)
        lines.append(f"  Upload: {upload_note}")
        lines.append(f"  Why: {purpose}")

    checklist_path = Path(reports_dir) / f"paper_upload_checklist_{day}.txt"
    lines.extend([
        "",
        f"Checklist file: {display_path(checklist_path)}",
        f"Files ready: {ready}/{len(artifacts)}",
        f"Required files missing: {missing_required}",
        "",
        "Research-only note: all fills/P&L are hypothetical. No real Kite orders were submitted.",
        "When automatic upload is enabled, this checklist is included in the private GitHub daily package.",
    ])
    body = "\n".join(lines) + "\n"
    checklist_path.parent.mkdir(parents=True, exist_ok=True)
    checklist_path.write_text(body, encoding="utf-8")

    artifact_records = []
    for label, path, required, purpose, category, upload in artifacts:
        try:
            relative_path = path.resolve().relative_to(root.resolve()).as_posix()
        except (ValueError, OSError):
            relative_path = None
        artifact_records.append({
            "label": label,
            "path": str(path),
            "relative_path": relative_path,
            "required": required,
            "exists": path.is_file(),
            "purpose": purpose,
            "category": category,
            "upload": upload,
        })
    artifact_records.append({
        "label": "Daily upload checklist",
        "path": str(checklist_path),
        "relative_path": display_path(checklist_path),
        "required": True,
        "exists": True,
        "purpose": "Human-readable inventory of the exact daily research package",
        "category": "reports",
        "upload": True,
    })

    return {
        "checklist_path": str(checklist_path),
        "ready_files": ready,
        "total_files": len(artifacts),
        "missing_required_files": missing_required,
        "text": body,
        "artifacts": artifact_records,
    }
