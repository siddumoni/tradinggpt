"""Schedule read-only Kite paper scans after each completed 5-minute candle.

Run this on the Windows machine that has the project's .venv and cached Kite session.
It never places, modifies, or cancels orders.
"""
from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
MARKET_OPEN = dtime(9, 15)
LAST_SCAN_BOUNDARY = dtime(15, 15)
BAR_SECONDS = 5 * 60
DEFAULT_SYMBOLS = "RELIANCE,HDFCBANK,INFY"
LOGGER = logging.getLogger("paper_scan_scheduler")


def _to_ist(value: datetime) -> datetime:
    return value.replace(tzinfo=IST) if value.tzinfo is None else value.astimezone(IST)


def latest_completed_boundary(
    now: datetime,
    *,
    grace_seconds: int = 10,
    max_lateness_seconds: int = 120,
) -> datetime | None:
    """Return the newest eligible 5-minute close boundary, or None.

    The first candle starts at 09:15 and completes at 09:20. A short grace period
    allows Kite to publish the completed candle. Boundaries older than the lateness
    window are skipped rather than scanned with stale quotes.
    """
    if grace_seconds < 0:
        raise ValueError("grace_seconds must be non-negative")
    if max_lateness_seconds < grace_seconds:
        raise ValueError("max_lateness_seconds must be >= grace_seconds")

    current = _to_ist(now)
    if current.weekday() >= 5:
        return None

    session_open = datetime.combine(current.date(), MARKET_OPEN, tzinfo=IST)
    first_boundary = session_open + timedelta(seconds=BAR_SECONDS)
    last_boundary = datetime.combine(current.date(), LAST_SCAN_BOUNDARY, tzinfo=IST)
    effective = current - timedelta(seconds=grace_seconds)
    if effective < first_boundary:
        return None

    elapsed = int((effective - session_open).total_seconds())
    boundary_number = elapsed // BAR_SECONDS
    boundary = session_open + timedelta(seconds=boundary_number * BAR_SECONDS)
    if boundary < first_boundary or boundary > last_boundary:
        return None
    lateness = (current - boundary).total_seconds()
    if lateness > max_lateness_seconds:
        return None
    return boundary


def read_last_attempted_boundary(state_path: Path) -> str | None:
    """Read the persisted candle key; unreadable state is logged and treated as empty."""
    if not state_path.exists():
        return None
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        value = payload.get("last_attempted_candle") if isinstance(payload, dict) else None
        return value if isinstance(value, str) and value else None
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.warning("Could not read scheduler state %s: %s", state_path, exc)
        return None


def write_last_attempted_boundary(state_path: Path, boundary: datetime) -> None:
    """Atomically persist the boundary before launching the scanner to avoid duplicates."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = state_path.with_suffix(state_path.suffix + ".tmp")
    payload = {
        "last_attempted_candle": _to_ist(boundary).isoformat(),
        "updated_at": datetime.now(IST).isoformat(),
        "note": "Persisted before scan launch to avoid duplicate attempts after restart.",
    }
    temporary_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temporary_path.replace(state_path)


def process_due_boundary(
    boundary: datetime | None,
    state_path: Path,
    scan_runner: Callable[[datetime], int],
) -> bool:
    """Run at most one scanner attempt for a completed candle boundary.

    Returns True if a scan attempt was launched. State is written before launching;
    failed scans are logged and retried on the next completed candle, not in a tight
    loop for the same candle.
    """
    if boundary is None:
        return False
    candle_key = _to_ist(boundary).isoformat()
    if read_last_attempted_boundary(state_path) == candle_key:
        return False

    write_last_attempted_boundary(state_path, boundary)
    LOGGER.info("Starting paper scan for completed candle boundary %s", candle_key)
    try:
        exit_code = scan_runner(boundary)
    except Exception:
        LOGGER.exception("Paper scanner launch failed for %s; next candle will be retried", candle_key)
        return True
    if exit_code == 0:
        LOGGER.info("Paper scan finished successfully for %s", candle_key)
    else:
        LOGGER.error("Paper scan exited with code %s for %s; next candle will be retried", exit_code, candle_key)
    return True


def _resolve_path(root: Path, path: Path) -> Path:
    return path if path.is_absolute() else root / path


def run_scanner_process(
    boundary: datetime,
    *,
    repo_root: Path,
    symbols: str,
    paper_journal: Path,
    scan_ledger: Path,
    candle_archive_dir: Path,
    timeout_seconds: int,
    paper_broker: Any | None = None,
    after_report: Callable[[Path], None] | None = None,
    account_capital: float = 100_000.0,
    risk_per_trade: float = 500.0,
) -> int:
    """Run the scanner and process its saved snapshot through the virtual account."""
    command = [
        sys.executable,
        "-m",
        "src.advisory_engine",
        "--provider", "kite",
        "--symbols", symbols,
        "--paper-journal", str(paper_journal),
        "--scan-ledger", str(scan_ledger),
        "--candle-archive-dir", str(candle_archive_dir),
        "--account-capital", str(account_capital),
        "--risk-per-trade", str(risk_per_trade),
        "--minimum-net-rr", "1.5",
        "--verbose",
    ]
    LOGGER.info("Command for %s: %s", _to_ist(boundary).isoformat(), subprocess.list2cmdline(command))
    try:
        completed = subprocess.run(
            command,
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        LOGGER.error("Scanner timed out after %s seconds at %s", timeout_seconds, _to_ist(boundary).isoformat())
        stdout = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        if stdout.strip():
            LOGGER.info("Scanner partial stdout:\n%s", stdout.rstrip())
        if stderr.strip():
            LOGGER.error("Scanner partial stderr:\n%s", stderr.rstrip())
        return 124
    except OSError:
        LOGGER.exception("Could not start the scanner process")
        return 127

    if completed.stdout.strip():
        LOGGER.info("Scanner output:\n%s", completed.stdout.rstrip())
    if completed.stderr.strip():
        if completed.returncode == 0:
            LOGGER.warning("Scanner stderr:\n%s", completed.stderr.rstrip())
        else:
            LOGGER.error("Scanner stderr:\n%s", completed.stderr.rstrip())
    if completed.returncode == 0:
        report_path = _extract_report_path(completed.stdout, repo_root)
        if report_path is None:
            LOGGER.error("Scanner exited successfully but did not print an existing report path; paper broker skipped.")
        else:
            if paper_broker is not None:
                try:
                    result = paper_broker.process_scan_report(report_path)
                    LOGGER.info(
                        "Virtual broker processed scan: conditional entries opened=%s; candidate rejections=%s; "
                        "WATCH_CANDIDATE=%s; NO_TRADE=%s; other actions=%s (candidates=%s). "
                        "WATCH_CANDIDATE is a directional research signal, not a conditional entry.",
                        result.get("opened"), result.get("rejected"), result.get("watch_candidates", 0),
                        result.get("no_trade", 0), result.get("other_actions", 0), result.get("candidate_count"),
                    )
                except Exception:
                    LOGGER.exception("Virtual broker could not process the scan report; no real orders were sent.")
            if after_report is not None:
                try:
                    after_report(report_path)
                except Exception:
                    LOGGER.exception("Post-scan shadow research processing failed.")
    return int(completed.returncode)


def _extract_report_path(stdout: str, repo_root: Path) -> Path | None:
    for line in (stdout or "").splitlines():
        if line.strip().startswith("Report:"):
            raw = line.split(":", 1)[1].strip()
            if not raw: return None
            path = Path(raw)
            if not path.is_absolute(): path = Path(repo_root) / path
            if path.is_file(): return path
            fallback = Path(raw)
            return fallback if fallback.is_file() else None
    return None


def _configure_logging(log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=[file_handler, console_handler], force=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run read-only Kite paper scans after each completed 5-minute candle."
    )
    parser.add_argument("--symbols", default=DEFAULT_SYMBOLS, help="Comma-separated NSE symbols")
    parser.add_argument("--poll-seconds", type=float, default=5.0, help="Scheduler polling interval")
    parser.add_argument("--grace-seconds", type=int, default=10, help="Wait after candle close before scanning")
    parser.add_argument("--max-lateness-seconds", type=int, default=120,
                        help="Skip a candle if the scheduler notices it later than this")
    parser.add_argument("--scan-timeout-seconds", type=int, default=240, help="Timeout for one scanner run")
    parser.add_argument("--paper-journal", type=Path, default=Path("logs/paper_signals.csv"))
    parser.add_argument("--scan-ledger", type=Path, default=Path("logs/paper_scan_runs.csv"))
    parser.add_argument("--candle-archive-dir", type=Path, default=Path("data/paper_candles_kite"))
    parser.add_argument("--state-file", type=Path, default=Path("logs/paper_scan_scheduler_state.json"))
    parser.add_argument("--log-file", type=Path, default=Path("logs/paper_scan_scheduler.log"))
    args = parser.parse_args(argv)

    if not args.symbols.strip() or not all(s.strip() for s in args.symbols.split(",")):
        parser.error("--symbols must contain non-empty comma-separated symbols")
    if args.poll_seconds <= 0:
        parser.error("--poll-seconds must be positive")
    if args.grace_seconds < 0:
        parser.error("--grace-seconds must be non-negative")
    if args.max_lateness_seconds < args.grace_seconds:
        parser.error("--max-lateness-seconds must be >= --grace-seconds")
    if args.scan_timeout_seconds <= 0:
        parser.error("--scan-timeout-seconds must be positive")

    repo_root = Path(__file__).resolve().parents[1]
    paper_journal = _resolve_path(repo_root, args.paper_journal)
    scan_ledger = _resolve_path(repo_root, args.scan_ledger)
    candle_archive_dir = _resolve_path(repo_root, args.candle_archive_dir)
    state_file = _resolve_path(repo_root, args.state_file)
    log_file = _resolve_path(repo_root, args.log_file)
    _configure_logging(log_file)
    LOGGER.info(
        "Paper scan scheduler started; root=%s symbols=%s poll=%ss grace=%ss. "
        "Scans run on weekdays after completed 5-minute candle boundaries from 09:20 through 15:15 IST.",
        repo_root, args.symbols, args.poll_seconds, args.grace_seconds,
    )
    LOGGER.info("Journal=%s ledger=%s candle_archive=%s state=%s", paper_journal, scan_ledger, candle_archive_dir, state_file)

    def scan(boundary: datetime) -> int:
        return run_scanner_process(
            boundary,
            repo_root=repo_root,
            symbols=args.symbols,
            paper_journal=paper_journal,
            scan_ledger=scan_ledger,
            candle_archive_dir=candle_archive_dir,
            timeout_seconds=args.scan_timeout_seconds,
        )

    try:
        while True:
            now = datetime.now(IST)
            try:
                boundary = latest_completed_boundary(
                    now,
                    grace_seconds=args.grace_seconds,
                    max_lateness_seconds=args.max_lateness_seconds,
                )
                process_due_boundary(boundary, state_file, scan)
            except Exception:
                LOGGER.exception("Unexpected scheduler cycle failure; continuing")
            time.sleep(args.poll_seconds)
    except KeyboardInterrupt:
        LOGGER.info("Scheduler stopped by keyboard interrupt")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
