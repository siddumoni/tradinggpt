"""Build a human-readable checklist and date-scoped artifact package for GitHub."""
from __future__ import annotations

import csv
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
LOG_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2} ")


def _write_daily_csv_export(
    source: Path,
    destination: Path,
    *,
    day: str,
    timestamp_fields: tuple[str, ...],
    include_header_only: bool = True,
) -> tuple[int, bool]:
    """Write only rows whose ISO timestamp belongs to day; return rows and existence."""
    source = Path(source)
    destination = Path(destination)
    if not source.is_file():
        return 0, False
    try:
        with source.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            fields = reader.fieldnames
            if not fields or not any(field in fields for field in timestamp_fields):
                return 0, False
            rows = []
            for row in reader:
                stamps = [str(row.get(field) or "") for field in timestamp_fields if field in fields]
                if any(stamp.startswith(day) for stamp in stamps):
                    rows.append(row)
    except (OSError, csv.Error, UnicodeDecodeError):
        return 0, False

    if not rows and not include_header_only:
        return 0, False
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return len(rows), True


def _write_daily_log_export(source: Path, destination: Path, *, day: str) -> tuple[int, bool]:
    """Keep timestamped log entries for this date and their following traceback lines."""
    source = Path(source)
    destination = Path(destination)
    if not source.is_file():
        return 0, False
    try:
        lines = source.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    except OSError:
        return 0, False

    selected: list[str] = []
    current_block = False
    for line in lines:
        if LOG_TIMESTAMP.match(line):
            current_block = line.startswith(day + " ")
        if current_block:
            selected.append(line)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("".join(selected), encoding="utf-8")
    return len(selected), True


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
    """Write the checklist and return structured date-scoped upload artifacts.

    Local scan journals and candle archives remain cumulative. This function
    exports only the report-date rows into a small staging tree for GitHub.
    """
    current = now or datetime.now(IST)
    current = current.replace(tzinfo=IST) if current.tzinfo is None else current.astimezone(IST)
    day = current.date().isoformat()
    date_stamp = current.strftime("%Y%m%d")
    prefix = f"paper_daily_{day}"
    report_result = report_result or {}
    staging = Path(reports_dir) / "github_upload_staging" / day

    # label, upload path, required, purpose, category, upload automatically, source path
    artifacts: list[tuple[str, Path, bool, str, str, bool, Path | None]] = [
        ("Daily Markdown report", Path(report_result.get("markdown_path", reports_dir / f"{prefix}.md")), True,
         "Readable summary of champion, challenger, and watch-shadow performance", "reports", True, None),
        ("Daily JSON report", Path(report_result.get("json_path", reports_dir / f"{prefix}.json")), True,
         "Structured results, risk overruns, events, and separate account summaries", "reports", True, None),
        ("Combined daily trade CSV", Path(report_result.get("csv_path", reports_dir / f"{prefix}_trades.csv")), True,
         "Champion, challenger, and watch-shadow trades/candidate decisions", "reports", True, None),
        ("Daily report state JSON", daily_state_file, True,
         "Final account snapshots and report-generation status", "diagnostics", True, None),
        ("Scheduler state JSON", scheduler_state_file, True,
         "Last attempted 5-minute candle boundary", "diagnostics", True, None),
    ]

    # Cumulative scan ledger and signal journal are sliced by scan_as_of so
    # each GitHub date folder contains only that date's rows.
    for label, source, filename, purpose in (
        ("Daily scan-run ledger CSV", Path(scan_ledger), "paper_scan_runs.csv",
         "Only scan-run ledger rows whose scan_as_of date matches this report date"),
        ("Daily signal journal CSV", Path(paper_journal), "paper_signals.csv",
         "Only directional journal rows whose scan_as_of date matches this report date"),
    ):
        staged_path = staging / "diagnostics" / filename
        row_count, exists = _write_daily_csv_export(
            source, staged_path, day=day, timestamp_fields=("scan_as_of", "recorded_at")
        )
        artifacts.append((
            label, staged_path, True,
            f"{purpose}; rows={row_count}; source={source}",
            "diagnostics", True, source,
        ))
        if not exists:
            # The required flag will make finalization fail closed if the
            # cumulative local source cannot be read or has an unknown schema.
            artifacts[-1] = (
                label, staged_path, True,
                f"{purpose}; date extract could not be created; source={source}",
                "diagnostics", True, source,
            )

    # Keep only this date's service-log entries (and associated traceback lines).
    staged_log = staging / "logs" / "paper_trading_service.log"
    log_lines, log_exists = _write_daily_log_export(Path(service_log), staged_log, day=day)
    artifacts.append((
        "Daily service log", staged_log, True,
        f"Only log entries timestamped on {day}; selected lines={log_lines}; source={service_log}",
        "logs", True, Path(service_log),
    ))
    if not log_exists:
        artifacts[-1] = (
            "Daily service log", staged_log, True,
            f"Daily service log extract could not be created; source={service_log}",
            "logs", True, Path(service_log),
        )

    # Include every scan JSON produced on this IST date.
    for scan_path in sorted(Path(reports_dir).glob(f"phase6_scan_{date_stamp}_*.json")):
        artifacts.append((
            "Intraday scan JSON report", scan_path, False,
            "Full per-scan universe selection, candidate decisions and diagnostics",
            "scans", True, None,
        ))

    # Local candle files contain all archived sessions. Export only rows from
    # this date to avoid recommitting the entire cumulative history every day.
    selected = set(dict.fromkeys(s.upper() for s in symbols))
    candle_sources: dict[str, Path] = {}
    for symbol in selected:
        candle_sources[f"{symbol}.csv"] = Path(candle_archive_dir) / f"{symbol}.csv"
    for candle_path in sorted(Path(candle_archive_dir).glob("*.csv")):
        try:
            modified_today = datetime.fromtimestamp(candle_path.stat().st_mtime, IST).date() == current.date()
        except OSError:
            modified_today = False
        if modified_today:
            candle_sources.setdefault(candle_path.name, candle_path)

    for filename, source in sorted(candle_sources.items()):
        symbol = Path(filename).stem.upper()
        staged_path = staging / "candles" / filename
        row_count, exists = _write_daily_csv_export(
            source, staged_path, day=day, timestamp_fields=("date",),
            include_header_only=False,
        )
        artifacts.append((
            f"{symbol} daily Kite candle extract",
            staged_path,
            False,
            f"{row_count} five-minute candle row(s) for {day}; local cumulative source={source}",
            "candles",
            exists,
            source,
        ))

    artifacts.append((
        "SQLite paper account database (excluded from automatic upload)",
        Path(db_path),
        False,
        "Contains the full persistent account ledger; intentionally kept local by default",
        "diagnostics",
        False,
        None,
    ))

    def display_path(path: Path) -> str:
        try:
            return path.resolve().relative_to(Path(root).resolve()).as_posix()
        except (ValueError, OSError):
            return str(path)

    lines = [
        f"TradingGPT post-run upload checklist — {day}",
        f"Created: {current.isoformat()}",
        f"Checklist trigger: {reason}",
        "",
        "When configured and all REQUIRED files are present, READY artifacts are uploaded to the PRIVATE GitHub research repository.",
        "The upload creates/updates daily/" + day + "/ in that repository.",
        "Cumulative local CSV archives are sliced to this date before upload.",
        "Do not upload API credentials, access tokens, .env files, or browser/profile data.",
        "The SQLite paper-account database is intentionally excluded from automatic upload.",
        "",
    ]
    ready = 0
    missing_required = 0
    for label, path, required, purpose, category, upload, source in artifacts:
        exists = path.is_file()
        if exists:
            ready += 1
        elif required:
            missing_required += 1
        status = "READY" if exists else (
            "MISSING — check local run/configuration" if required else "OPTIONAL — not present"
        )
        priority = "REQUIRED" if required else "OPTIONAL"
        upload_note = "Included in automatic upload" if upload else "Kept local / no rows for this date"
        lines.append(f"[{status}] [{priority}] {label}")
        lines.append(f"  Path: {display_path(path)}")
        if source:
            lines.append(f"  Local cumulative/source path: {display_path(source)}")
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
    for label, path, required, purpose, category, upload, source in artifacts:
        try:
            relative_path = path.resolve().relative_to(Path(root).resolve()).as_posix()
        except (ValueError, OSError):
            relative_path = None
        artifact_records.append({
            "label": label,
            "path": str(path),
            "relative_path": relative_path,
            "source_path": str(source) if source else None,
            "required": required,
            "exists": path.is_file(),
            "purpose": purpose,
            "category": category,
            "upload": upload and path.is_file(),
        })
    artifact_records.append({
        "label": "Daily upload checklist",
        "path": str(checklist_path),
        "relative_path": display_path(checklist_path),
        "source_path": None,
        "required": True,
        "exists": True,
        "purpose": "Human-readable inventory of the exact date-scoped research package",
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
