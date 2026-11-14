"""Replay archived Kite scanner snapshots through the shared virtual paper account.

This is an offline, research-only replay. It uses the recorded point-in-time
scanner candidates and quotes, then applies completed 5-minute OHLC bars to
open paper positions. It never places broker orders.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.paper_broker import PAPER_BASELINE_VERSION, PaperBroker, to_ist

IST = ZoneInfo("Asia/Kolkata")
BAR_MINUTES = 5
REPLAY_ENGINE_VERSION = 1
SESSION_START = time(9, 15)
LAST_BAR_START = time(15, 10)
DIRECTIONAL_ACTIONS = {"CONDITIONAL_BUY", "CONDITIONAL_SHORT"}
TRADE_COLUMNS = [
    "id",
    "signal_id",
    "strategy",
    "strategy_version",
    "symbol",
    "side",
    "signal_time",
    "entry_time",
    "entry_reference_price",
    "entry_fill_price",
    "stop_loss",
    "target_1",
    "target_2",
    "quantity",
    "exit_time",
    "exit_reference_price",
    "exit_fill_price",
    "exit_reason",
    "gross_pnl",
    "transaction_charges",
    "slippage_cost",
    "total_estimated_costs",
    "net_pnl",
    "risk_budget_inr",
]


@dataclass
class ReplayClock:
    """Mutable-by-assignment event clock injected into the virtual broker."""

    current: datetime


@dataclass(frozen=True)
class Candle:
    symbol: str
    start: datetime
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class ScanReport:
    path: Path
    as_of: datetime
    conditional_count: int
    conditional_symbols: frozenset[str]


@dataclass(frozen=True)
class DayInputs:
    day: date
    scan_reports: tuple[ScanReport, ...]
    candles: tuple[Candle, ...]
    hashes: tuple[tuple[str, str], ...]
    candle_counts: dict[str, int]
    conditional_candidates: int
    conditional_symbols: tuple[str, ...]
    strategy_version: str


def _positive_number(value: Any, *, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} must be a finite positive number") from exc
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{label} must be a finite positive number")
    return number


def _json_file(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read JSON input {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return value


def _expected_starts(day: date) -> tuple[datetime, ...]:
    current = datetime.combine(day, SESSION_START, tzinfo=IST)
    last = datetime.combine(day, LAST_BAR_START, tzinfo=IST)
    result = []
    while current <= last:
        result.append(current)
        current += timedelta(minutes=BAR_MINUTES)
    return tuple(result)


def _verify_input(
    path: Path,
    archive_root: Path,
    manifest_entries: dict[str, dict[str, Any]],
) -> tuple[str, str]:
    relative = path.resolve().relative_to(archive_root.resolve()).as_posix()
    entry = manifest_entries.get(relative)
    if entry is None:
        raise ValueError(f"Archive manifest does not list input file {relative}")
    raw = path.read_bytes()
    expected_size = entry.get("size_bytes")
    if expected_size is not None and int(expected_size) != len(raw):
        raise ValueError(f"Archive size checksum mismatch for {relative}")
    digest = hashlib.sha256(raw).hexdigest()
    expected_digest = str(entry.get("sha256") or "").lower()
    if not expected_digest or digest != expected_digest:
        raise ValueError(f"Archive SHA-256 mismatch for {relative}")
    return relative, digest


def _prepare_day(day_dir: Path, archive_root: Path) -> DayInputs:
    manifest_path = day_dir / "manifest.json"
    manifest = _json_file(manifest_path)
    day = date.fromisoformat(day_dir.name)
    if manifest.get("report_date") != day.isoformat():
        raise ValueError(f"Manifest date does not match folder {day_dir.name}")
    if manifest.get("research_only") is not True:
        raise ValueError(f"Refusing non-research archive day {day_dir.name}")
    if manifest.get("orders_sent_to_broker") is not False:
        raise ValueError(f"Archive day {day_dir.name} does not assert no broker orders")

    listed = manifest.get("files")
    if not isinstance(listed, list):
        raise ValueError(f"Archive manifest has no file list for {day_dir.name}")
    manifest_entries = {
        str(item.get("path")): item
        for item in listed
        if isinstance(item, dict) and item.get("path")
    }
    hashes: list[tuple[str, str]] = [
        (
            manifest_path.resolve().relative_to(archive_root.resolve()).as_posix(),
            hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        )
    ]
    daily_report_path = day_dir / "reports" / f"paper_daily_{day.isoformat()}.md"
    relative, digest = _verify_input(daily_report_path, archive_root, manifest_entries)
    hashes.append((relative, digest))
    daily_report = daily_report_path.read_text(encoding="utf-8-sig")
    version_matches = re.findall(
        r"^## Champion account\s+—\s+([A-Za-z0-9_]+)\s*$",
        daily_report,
        flags=re.MULTILINE,
    )
    if len(version_matches) != 1:
        raise ValueError(
            f"Daily report for {day} must explicitly pin the Champion strategy version; "
            "select a date range with versioned reports."
        )
    strategy_version = version_matches[0]
    if strategy_version != PAPER_BASELINE_VERSION:
        raise ValueError(
            f"Archive day {day} uses {strategy_version}, but this code replays "
            f"{PAPER_BASELINE_VERSION}; select a matching date range."
        )
    scan_paths = sorted((day_dir / "scans").glob("phase6_scan_*.json"))
    if not scan_paths:
        raise ValueError(f"No archived phase6 scan reports in {day_dir / 'scans'}")

    reports: list[ScanReport] = []
    required_symbols: set[str] = set()
    conditional_candidates = 0
    scan_times: set[datetime] = set()
    for path in scan_paths:
        relative, digest = _verify_input(path, archive_root, manifest_entries)
        hashes.append((relative, digest))
        report = _json_file(path)
        if report.get("status") != "RESEARCH_SCAN_ONLY":
            raise ValueError(f"Unexpected scan status in {relative}")
        if report.get("orders_placed_or_modified") is not False:
            raise ValueError(f"Scan report does not assert no orders: {relative}")
        if str(report.get("data_provider_mode", "")).lower() != "kite":
            raise ValueError(f"Scan report is not from the Kite archive: {relative}")
        as_of = to_ist(report.get("as_of"))
        if as_of.date() != day:
            raise ValueError(f"Scan timestamp is outside archive day: {relative}")
        if as_of in scan_times:
            raise ValueError(f"Duplicate scan timestamp {as_of.isoformat()} in {day_dir.name}")
        scan_times.add(as_of)
        candidates = report.get("candidates")
        if not isinstance(candidates, list):
            raise ValueError(f"Scan report has no candidate list: {relative}")
        symbols: set[str] = set()
        count = 0
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            if str(candidate.get("action") or "").upper() in DIRECTIONAL_ACTIONS:
                symbol = str(candidate.get("symbol") or "").strip().upper()
                if not symbol:
                    raise ValueError(f"Directional candidate has no symbol in {relative}")
                symbols.add(symbol)
                required_symbols.add(symbol)
                count += 1
        conditional_candidates += count
        reports.append(ScanReport(path, as_of, count, frozenset(symbols)))

    reports.sort(key=lambda item: (item.as_of, item.path.name))
    expected = set(_expected_starts(day))
    candles: list[Candle] = []
    candle_counts: dict[str, int] = {}
    for symbol in sorted(required_symbols):
        path = day_dir / "candles" / f"{symbol}.csv"
        if not path.is_file():
            raise ValueError(f"Missing Kite candle archive for directional candidate {symbol} on {day}")
        relative, digest = _verify_input(path, archive_root, manifest_entries)
        hashes.append((relative, digest))
        seen: dict[datetime, Candle] = {}
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                required_columns = {"date", "open", "high", "low", "close", "volume", "provider_mode"}
                if not reader.fieldnames or not required_columns.issubset(reader.fieldnames):
                    raise ValueError(f"Candle columns are incomplete in {relative}")
                for row in reader:
                    if str(row.get("provider_mode") or "").strip().lower() != "kite":
                        raise ValueError(f"Non-Kite candle in {relative}")
                    stamp = to_ist(row.get("date"))
                    if stamp.date() != day:
                        raise ValueError(f"Candle date is outside {day} in {relative}")
                    open_price = _positive_number(row.get("open"), label=f"{relative} open")
                    high = _positive_number(row.get("high"), label=f"{relative} high")
                    low = _positive_number(row.get("low"), label=f"{relative} low")
                    close = _positive_number(row.get("close"), label=f"{relative} close")
                    volume = float(row.get("volume", "nan"))
                    if not math.isfinite(volume) or volume < 0:
                        raise ValueError(f"Invalid volume in {relative}")
                    if high < max(open_price, low, close) or low > min(open_price, high, close):
                        raise ValueError(f"Invalid OHLC in {relative} at {stamp.isoformat()}")
                    if stamp not in expected:
                        # Bars outside the 15:15 strategy square-off window do not participate.
                        continue
                    if stamp in seen:
                        raise ValueError(f"Duplicate candle timestamp in {relative}: {stamp}")
                    seen[stamp] = Candle(symbol, stamp, open_price, high, low, close)
        except OSError as exc:
            raise ValueError(f"Could not read candle file {path}: {exc}") from exc
        missing = sorted(expected - set(seen))
        if missing:
            preview = ", ".join(value.strftime("%H:%M") for value in missing[:8])
            raise ValueError(
                f"Incomplete 09:15–15:15 strategy simulation window for {symbol} on {day}; "
                f"expected 72 five-minute bars starting 09:15–15:10 IST, missing {len(missing)} "
                f"beginning at {preview}"
            )
        candle_counts[symbol] = len(seen)
        candles.extend(seen.values())

    candles.sort(key=lambda item: (item.start + timedelta(minutes=BAR_MINUTES), item.symbol))
    return DayInputs(
        day=day,
        scan_reports=tuple(reports),
        candles=tuple(candles),
        hashes=tuple(sorted(hashes)),
        candle_counts=candle_counts,
        conditional_candidates=conditional_candidates,
        conditional_symbols=tuple(sorted(required_symbols)),
        strategy_version=strategy_version,
    )


def _load_days(
    archive_root: Path,
    *,
    from_date: date | None = None,
    to_date: date | None = None,
) -> list[DayInputs]:
    if from_date and to_date and from_date > to_date:
        raise ValueError("from_date must be earlier than or equal to to_date")
    daily_root = archive_root / "daily"
    if not daily_root.is_dir():
        raise ValueError(f"Expected a daily/ directory below {archive_root}")
    day_dirs = sorted(path for path in daily_root.iterdir() if path.is_dir())
    prepared = []
    for day_dir in day_dirs:
        if not (day_dir / "manifest.json").is_file():
            continue
        try:
            day = date.fromisoformat(day_dir.name)
        except ValueError as exc:
            raise ValueError(f"Invalid daily archive folder {day_dir.name}") from exc
        if from_date and day < from_date:
            continue
        if to_date and day > to_date:
            continue
        prepared.append(_prepare_day(day_dir, archive_root))
    if not prepared:
        raise ValueError(f"No manifested research days found below {daily_root}")
    return prepared


def _apply_quote_marks(
    broker: PaperBroker,
    snapshots: Any,
    as_of: datetime,
    *,
    max_age_seconds: float,
) -> list[str]:
    if not isinstance(snapshots, dict):
        return []
    open_positions = broker.snapshot().get("open_positions", [])
    updated: list[str] = []
    for position in open_positions:
        symbol = str(position.get("symbol") or "").upper()
        quote = snapshots.get(symbol)
        if not isinstance(quote, dict) or quote.get("verified_live") is not True:
            continue
        quote_time_value = quote.get("timestamp") or quote.get("last_trade_time")
        try:
            quote_time = to_ist(quote_time_value)
            age = (as_of - quote_time).total_seconds()
            last = _positive_number(quote.get("last_price"), label=f"{symbol} last price")
            bid = _positive_number(quote.get("bid"), label=f"{symbol} bid")
            ask = _positive_number(quote.get("ask"), label=f"{symbol} ask")
        except (TypeError, ValueError, OverflowError):
            continue
        if age < -2 or age > max_age_seconds or bid > ask:
            continue
        broker.update_market_price(
            symbol,
            last,
            bid=bid,
            ask=ask,
            market_time=quote_time,
            received_at=as_of,
        )
        updated.append(symbol)
    return updated


def _mark_candle_close(broker: PaperBroker, candle: Candle, end: datetime) -> None:
    if not any(
        str(position.get("symbol") or "").upper() == candle.symbol
        for position in broker.snapshot().get("open_positions", [])
    ):
        return
    # A candle close is an OHLC-only mark, not a recorded bid/ask. Using close
    # for both sides keeps the assumption explicit; statutory costs and configured
    # slippage are still included by PaperBroker's unrealized-P&L calculation.
    broker.update_market_price(
        candle.symbol,
        candle.close,
        bid=candle.close,
        ask=candle.close,
        market_time=end,
        received_at=end,
    )


def _all_closed_trades(broker: PaperBroker) -> list[dict[str, Any]]:
    with broker._connect() as db:
        rows = db.execute("SELECT * FROM trades WHERE status='CLOSED' ORDER BY exit_time,id")
        return [dict(row) for row in rows]


def _performance_summary(
    trades: list[dict[str, Any]],
    initial_capital: float,
    daily: list[dict[str, Any]],
) -> dict[str, Any]:
    nets = [float(row.get("net_pnl") or 0.0) for row in trades]
    positive = sum(value for value in nets if value > 0)
    negative = abs(sum(value for value in nets if value < 0))
    return {
        "closed_trades": len(trades),
        "wins": sum(value > 0 for value in nets),
        "win_rate_pct": 100.0 * sum(value > 0 for value in nets) / len(nets) if nets else 0.0,
        "gross_pnl": round(sum(float(row.get("gross_pnl") or 0.0) for row in trades), 2),
        "estimated_costs": round(sum(float(row.get("total_estimated_costs") or 0.0) for row in trades), 2),
        "net_pnl": round(sum(nets), 2),
        "profit_factor": round(positive / negative, 6) if negative else (None if not positive else "infinite"),
        "initial_capital": round(initial_capital, 2),
        "ending_realized_equity": round(initial_capital + sum(nets), 2),
        "daily_equity": daily,
        "maximum_close_to_close_drawdown": max(
            (float(item["drawdown_from_peak"]) for item in daily),
            default=0.0,
        ),
    }


def run_archive_replay(
    archive_root: Path,
    output_dir: Path,
    *,
    initial_capital: float = 100_000.0,
    risk_per_trade: float = 500.0,
    max_daily_loss: float = 2_000.0,
    max_open_positions: int = 3,
    max_portfolio_risk: float | None = None,
    slippage_bps: float = 5.0,
    minimum_net_rr: float = 1.5,
    quote_mark_max_age_seconds: float = 360.0,
    from_date: date | None = None,
    to_date: date | None = None,
) -> dict[str, Any]:
    """Replay manifested daily scanner archives through PaperBroker chronologically."""
    archive_root = Path(archive_root).resolve()
    output_dir = Path(output_dir).resolve()
    if archive_root == output_dir or archive_root in output_dir.parents:
        raise ValueError("Replay output must be outside the source archive")
    days = _load_days(archive_root, from_date=from_date, to_date=to_date)
    input_hashes = sorted({item for day in days for item in day.hashes})
    input_digest = hashlib.sha256(
        "".join(f"{path}:{digest}\n" for path, digest in input_hashes).encode("utf-8")
    ).hexdigest()
    effective_portfolio_risk = (
        max_portfolio_risk
        if max_portfolio_risk is not None
        else risk_per_trade * max_open_positions
    )
    run_settings = {
        "initial_capital": initial_capital,
        "risk_per_trade": risk_per_trade,
        "max_daily_loss": max_daily_loss,
        "max_open_positions": max_open_positions,
        "max_portfolio_risk": effective_portfolio_risk,
        "slippage_bps": slippage_bps,
        "minimum_net_rr": minimum_net_rr,
        "quote_mark_max_age_seconds": quote_mark_max_age_seconds,
    }
    run_identity = {
        "input_sha256": input_digest,
        "replay_engine_version": REPLAY_ENGINE_VERSION,
        "strategy_version": PAPER_BASELINE_VERSION,
        "selected_dates": [item.day.isoformat() for item in days],
        "requested_date_range": {
            "from": from_date.isoformat() if from_date else None,
            "to": to_date.isoformat() if to_date else None,
        },
        "account_settings": run_settings,
    }
    run_digest = hashlib.sha256(
        json.dumps(
            run_identity,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    output_dir.mkdir(parents=True, exist_ok=True)
    clock = ReplayClock(datetime.combine(days[0].day, SESSION_START, tzinfo=IST))
    scan_results: list[dict[str, Any]] = []
    daily: list[dict[str, Any]] = []
    processed_days = 0
    unresolved_positions: list[dict[str, Any]] = []
    status = "COMPLETE"

    with tempfile.TemporaryDirectory(prefix="paper-archive-replay-") as temp_dir:
        broker = PaperBroker(
            Path(temp_dir) / "paper_replay.sqlite3",
            initial_capital=initial_capital,
            risk_per_trade=risk_per_trade,
            max_daily_loss=max_daily_loss,
            max_open_positions=max_open_positions,
            max_portfolio_risk_inr=max_portfolio_risk,
            slippage_bps=slippage_bps,
            minimum_net_rr=minimum_net_rr,
            clock=lambda: clock.current,
        )
        for day_data in days:
            if broker.snapshot().get("open_positions"):
                status = "INCOMPLETE_OPEN_POSITIONS"
                unresolved_positions = broker.snapshot()["open_positions"]
                break
            before = float(broker.snapshot()["account"]["realized_pnl"])
            events: list[tuple[datetime, int, str, Any]] = []
            for candle in day_data.candles:
                end = candle.start + timedelta(minutes=BAR_MINUTES)
                events.append((end, 0, f"{candle.symbol}:{candle.start.isoformat()}", candle))
            for scan in day_data.scan_reports:
                events.append((scan.as_of, 1, scan.path.name, scan))
            events.sort(key=lambda item: (item[0], item[1], item[2]))

            for at, kind, _key, payload in events:
                clock.current = at
                if kind == 0:
                    candle = payload
                    broker.process_candle(
                        candle.symbol,
                        candle.start,
                        open_price=candle.open,
                        high=candle.high,
                        low=candle.low,
                        close=candle.close,
                        bar_minutes=BAR_MINUTES,
                    )
                    _mark_candle_close(broker, candle, at)
                    continue

                scan = payload
                report = _json_file(scan.path)
                marks_before = _apply_quote_marks(
                    broker,
                    report.get("market_snapshots"),
                    scan.as_of,
                    max_age_seconds=quote_mark_max_age_seconds,
                )
                result = broker.process_scan_report(scan.path)
                scan_results.append({
                    "date": day_data.day.isoformat(),
                    "scan_file": scan.path.name,
                    "as_of": scan.as_of.isoformat(),
                    "conditional_candidates": scan.conditional_count,
                    "opened": result["opened"],
                    "rejected": result["rejected"],
                    "neutral": result["neutral"],
                    "allocation_order": result["allocation_order"],
                    "quote_marks_updated": marks_before,
                })

            clock.current = datetime.combine(
                day_data.day, time(15, 15), tzinfo=IST
            )
            snapshot = broker.snapshot()
            unresolved_positions = snapshot.get("open_positions", [])
            realized = float(snapshot["account"]["realized_pnl"])
            daily_net = realized - before
            equity = initial_capital + realized
            prior_peak = max(
                [initial_capital]
                + [float(item["ending_realized_equity"]) for item in daily]
            )
            daily.append({
                "date": day_data.day.isoformat(),
                "scan_reports": len(day_data.scan_reports),
                "conditional_candidates": day_data.conditional_candidates,
                "conditional_symbols": list(day_data.conditional_symbols),
                "complete_candle_counts": day_data.candle_counts,
                "closed_trades_today": sum(
                    1 for row in _all_closed_trades(broker)
                    if str(row.get("exit_time") or "").startswith(day_data.day.isoformat())
                ),
                "net_pnl": round(daily_net, 2),
                "ending_realized_equity": round(equity, 2),
                "drawdown_from_peak": round(max(0.0, prior_peak - equity), 2),
                "open_positions_at_close": len(unresolved_positions),
            })
            processed_days += 1
            if unresolved_positions:
                status = "INCOMPLETE_OPEN_POSITIONS"
                break

        clock.current = datetime.combine(days[min(processed_days, len(days)) - 1].day, time(15, 15), tzinfo=IST)
        snapshot = broker.snapshot()
        trades = _all_closed_trades(broker)
        performance = _performance_summary(trades, initial_capital, daily)
        summary = {
            "report_type": "PAPER_ARCHIVE_REPLAY",
            "run_id": run_digest[:16],
            "status": status,
            "research_only": True,
            "orders_sent_to_broker": False,
            "input_archive": str(archive_root),
            "input_sha256": input_digest,
            "replay_engine_version": REPLAY_ENGINE_VERSION,
            "days_available": len(days),
            "days_processed": processed_days,
            "strategy_version": PAPER_BASELINE_VERSION,
            "requested_date_range": {
                "from": from_date.isoformat() if from_date else None,
                "to": to_date.isoformat() if to_date else None,
            },
            "simulation_window": {
                "first_bar_start_ist": SESSION_START.isoformat(),
                "last_bar_start_ist": LAST_BAR_START.isoformat(),
                "bar_minutes": BAR_MINUTES,
                "required_bars_per_symbol": len(_expected_starts(days[0].day)),
                "square_off_time_ist": "15:15",
                "nse_regular_close_ist": "15:30",
            },
            "scan_reports_processed": len(scan_results),
            "candidate_rows_processed": sum(item.conditional_count for day in days[:processed_days] for item in day.scan_reports),
            "candidate_symbols_checked": sorted({symbol for day in days[:processed_days] for symbol in day.conditional_symbols}),
            "account_settings": {
                "initial_capital": initial_capital,
                "risk_per_trade": risk_per_trade,
                "max_daily_loss": max_daily_loss,
                "max_open_positions": max_open_positions,
                "max_portfolio_risk": effective_portfolio_risk,
                "slippage_bps_per_side": slippage_bps,
                "minimum_net_rr": minimum_net_rr,
                "quote_mark_max_age_seconds": quote_mark_max_age_seconds,
            },
            **performance,
            "unresolved_positions": unresolved_positions,
            "scan_results": scan_results,
        }
        report_path = output_dir / f"paper_archive_replay_{run_digest[:16]}.json"
        trades_path = output_dir / f"paper_archive_replay_{run_digest[:16]}_trades.csv"
        summary["summary_path"] = str(report_path)
        summary["trades_path"] = str(trades_path)
        report_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        with trades_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=TRADE_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(trades)
        return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Replay manifested Kite scan archives through the research-only shared paper account."
    )
    parser.add_argument("--archive-root", type=Path, required=True, help="Local clone of tradinggpt-research-data.")
    parser.add_argument("--output-dir", type=Path, default=Path("reports/paper_archive_replay"))
    parser.add_argument("--initial-capital", type=float, default=100_000.0)
    parser.add_argument("--risk-per-trade", type=float, default=500.0)
    parser.add_argument("--max-daily-loss", type=float, default=2_000.0)
    parser.add_argument("--max-open-positions", type=int, default=3)
    parser.add_argument("--max-portfolio-risk", type=float, default=None)
    parser.add_argument("--slippage-bps", type=float, default=5.0)
    parser.add_argument("--minimum-net-rr", type=float, default=1.5)
    parser.add_argument("--quote-mark-max-age-seconds", type=float, default=360.0)
    parser.add_argument("--from-date", type=date.fromisoformat)
    parser.add_argument("--to-date", type=date.fromisoformat)
    args = parser.parse_args()
    summary = run_archive_replay(
        args.archive_root,
        args.output_dir,
        initial_capital=args.initial_capital,
        risk_per_trade=args.risk_per_trade,
        max_daily_loss=args.max_daily_loss,
        max_open_positions=args.max_open_positions,
        max_portfolio_risk=args.max_portfolio_risk,
        slippage_bps=args.slippage_bps,
        minimum_net_rr=args.minimum_net_rr,
        quote_mark_max_age_seconds=args.quote_mark_max_age_seconds,
        from_date=args.from_date,
        to_date=args.to_date,
    )
    print(json.dumps({
        "status": summary["status"],
        "run_id": summary["run_id"],
        "closed_trades": summary["closed_trades"],
        "net_pnl": summary["net_pnl"],
        "summary_path": summary["summary_path"],
        "trades_path": summary["trades_path"],
    }, indent=2))


if __name__ == "__main__":
    main()
