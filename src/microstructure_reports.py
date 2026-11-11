"""Generate end-of-day Volume Profile and Order Flow research summaries."""
from __future__ import annotations

import csv
import gzip
import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.candle_archive import append_completed_candles
from src.data_fetcher import KiteMarketData
from src.market_calendar import market_day_status
from src.order_flow import OrderFlowError, analyze_order_flow, read_order_flow_events
from src.microstructure_validation import evaluate_order_flow_predictive_accuracy
from src.volume_profile import VolumeProfileError, build_volume_profile

IST = ZoneInfo("Asia/Kolkata")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False)
    path.write_text(rendered + "\n", encoding="utf-8")


def _session_bar_coverage(rows: list[dict[str, Any]], report_date: str) -> dict[str, Any]:
    """Check the exact 75-bar regular NSE 5-minute grid, not just row count."""
    session_day = date.fromisoformat(report_date)
    expected = {
        datetime.combine(session_day, time(9, 15), tzinfo=IST) + timedelta(minutes=5 * index)
        for index in range(75)
    }
    actual: set[datetime] = set()
    for row in rows:
        raw = str(row.get("date") or row.get("timestamp") or "")
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        stamp = stamp.replace(tzinfo=IST) if stamp.tzinfo is None else stamp.astimezone(IST)
        actual.add(stamp)
    missing = sorted(expected - actual)
    present = sorted(expected & actual)
    return {
        "candle_rows": len(rows),
        "first_bar_start_ist": present[0].strftime("%H:%M") if present else None,
        "latest_bar_start_ist": present[-1].strftime("%H:%M") if present else None,
        "expected_final_bar_start_ist": "15:25",
        "missing_bar_count": len(missing),
        "missing_bar_starts_ist": [stamp.strftime("%H:%M") for stamp in missing[:10]],
        "full_session_coverage": not missing,
    }


def _load_tick_sizes(cache_path: Path) -> tuple[dict[str, float], str | None]:
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, "Kite instrument cache is missing; Volume Profile reports cannot be calculated."
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}, (
            "Kite instrument cache could not be read; Volume Profile reports cannot be calculated."
        )

    rows = payload.get("instruments") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return {}, "Kite instrument cache has an unexpected format."

    tick_sizes: dict[str, float] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("tradingsymbol") or "").strip().upper()
        try:
            tick_size = float(row.get("tick_size"))
        except (TypeError, ValueError):
            continue
        if symbol and tick_size > 0:
            tick_sizes[symbol] = tick_size
    return tick_sizes, None


def _refresh_eod_candles(
    *,
    root: Path,
    candle_archive_dir: Path,
    report_date: str,
    current: datetime,
) -> list[str]:
    """Refresh incomplete archived symbols after the cash close; never blocks reporting."""
    if (
        current.date().isoformat() != report_date
        or current.time().replace(tzinfo=None) < time(15, 30)
        or not market_day_status(current.date()).get("is_open")
    ):
        return []

    candidates: list[Path] = []
    for path in sorted(candle_archive_dir.glob("*.csv")):
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            session_rows = [
                row for row in rows
                if str(row.get("date") or row.get("timestamp") or "").startswith(report_date)
            ]
            if len(session_rows) < 50:
                continue
            coverage = _session_bar_coverage(session_rows, report_date)
            if not coverage["full_session_coverage"]:
                candidates.append(path)
        except (OSError, csv.Error, UnicodeDecodeError, ValueError):
            # The profile builder reports the precise archive error separately.
            continue

    if not candidates:
        return []

    warnings: list[str] = []
    try:
        provider = KiteMarketData.from_cached_session()
        instruments = provider.instruments(now=current)
        instrument_map = {
            str(row.get("tradingsymbol") or "").strip().upper(): row
            for row in instruments
            if row.get("segment") == "NSE"
            and row.get("instrument_type") == "EQ"
            and row.get("instrument_token")
        }
    except Exception as exc:
        return [
            "End-of-day candle refresh unavailable; existing archives were retained: "
            f"{type(exc).__name__}: {exc}"
        ]

    start = datetime.combine(current.date(), time(9, 15), tzinfo=IST)
    refreshed = 0
    for path in candidates:
        symbol = path.stem.upper()
        instrument = instrument_map.get(symbol)
        if instrument is None:
            warnings.append(f"{symbol}: no NSE EQ instrument token for end-of-day refresh.")
            continue
        try:
            candles = provider.historical_candles(
                int(instrument["instrument_token"]), start, current, "5minute"
            )
            result = append_completed_candles(
                symbol,
                candles,
                candle_archive_dir,
                now=current,
                provider_mode="kite",
            )
            refreshed += 1
            if result.rows_observed == 0:
                warnings.append(f"{symbol}: Kite returned no additional completed session bars.")
        except Exception as exc:
            warnings.append(f"{symbol}: end-of-day candle refresh failed: {type(exc).__name__}: {exc}")
    warnings.insert(
        0,
        f"End-of-day candle refresh attempted for {len(candidates)} symbol(s); "
        f"updated {refreshed}.",
    )
    return warnings


def write_microstructure_reports(
    *,
    root: Path,
    report_date: str,
    candle_archive_dir: Path,
    staging_dir: Path,
    now: datetime,
) -> list[dict[str, Any]]:
    """Write date-scoped JSON summaries; data gaps are explicit and non-fatal."""
    root = Path(root).resolve()
    candle_archive_dir = Path(candle_archive_dir)
    staging_dir = Path(staging_dir)
    current = now.replace(tzinfo=IST) if now.tzinfo is None else now.astimezone(IST)
    refresh_warnings = _refresh_eod_candles(
        root=root,
        candle_archive_dir=candle_archive_dir,
        report_date=report_date,
        current=current,
    )
    tick_sizes, cache_warning = _load_tick_sizes(root / ".local" / "nse_instruments.json")

    volume_symbols: dict[str, Any] = {}
    volume_coverage: dict[str, Any] = {}
    volume_warnings: list[str] = list(refresh_warnings)
    if cache_warning:
        volume_warnings.append(cache_warning)

    for candle_path in sorted(candle_archive_dir.glob("*.csv")):
        symbol = candle_path.stem.upper()
        tick_size = tick_sizes.get(symbol)
        if tick_size is None:
            volume_warnings.append(
                f"{symbol}: no valid tick size in the cached NSE instrument master."
            )
            continue
        try:
            with candle_path.open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            session_rows = [
                row
                for row in rows
                if str(row.get("date") or row.get("timestamp") or "").startswith(report_date)
            ]
            if not session_rows:
                continue
            coverage = _session_bar_coverage(session_rows, report_date)
            full_session_coverage = coverage["full_session_coverage"]
            volume_coverage[symbol] = coverage
            if not full_session_coverage:
                volume_warnings.append(
                    f"{symbol}: archive contains {len(session_rows)} session candle(s), "
                    f"latest bar starts at {coverage['latest_bar_start_ist'] or 'UNKNOWN'} IST, "
                    f"and {coverage['missing_bar_count']} of the expected 75 session bars are missing."
                )
            volume_symbols[symbol] = build_volume_profile(
                session_rows,
                tick_size=tick_size,
                as_of=current,
                bar_minutes=5,
                symbol=symbol,
            )
        except (
            OSError,
            csv.Error,
            UnicodeDecodeError,
            VolumeProfileError,
            ValueError,
        ) as exc:
            volume_warnings.append(f"{symbol}: {type(exc).__name__}: {exc}")

    volume_payload = {
        "report_type": "VOLUME_PROFILE",
        "report_date": report_date,
        "generated_at": current.isoformat(),
        "research_only": True,
        "status": (
            "NO_DATA"
            if not volume_symbols
            else "READY"
            if all(item["full_session_coverage"] for item in volume_coverage.values())
            else "PARTIAL"
        ),
        "coverage": {
            "symbols": volume_coverage,
            "expected_regular_session_bars": 75,
            "expected_final_bar_start_ist": "15:25",
            "market_close_ist": "15:30",
        },
        "method": "UNIFORM_OHLC_RANGE_ESTIMATE",
        "symbols_processed": len(volume_symbols),
        "symbols": volume_symbols,
        "warnings": volume_warnings,
        "limitations": [
            "Volume is estimated by distributing each completed 5-minute candle uniformly "
            "across its low-to-high tick levels.",
            "POC, value area, HVN and LVN are estimates, not exact exchange trade volume at price.",
            "Only current-session candles available in the local Kite archive are included.",
        ],
    }
    volume_path = staging_dir / "volume_profile" / "volume_profile_summary.json"
    _write_json(volume_path, volume_payload)

    order_flow_symbols: dict[str, Any] = {}
    order_flow_warnings: list[str] = []
    order_flow_dir = root / ".local" / "order_flow" / report_date
    if order_flow_dir.is_dir():
        for event_path in sorted(order_flow_dir.glob("*.jsonl.gz")):
            symbol = event_path.name.removesuffix(".jsonl.gz").upper()
            try:
                events = read_order_flow_events(event_path)
                order_flow_symbols[symbol] = analyze_order_flow(events, depth_levels=5)
            except (
                OSError,
                EOFError,
                gzip.BadGzipFile,
                OrderFlowError,
                ValueError,
            ) as exc:
                order_flow_warnings.append(f"{symbol}: {type(exc).__name__}: {exc}")
    else:
        order_flow_warnings.append("No Order Flow capture directory exists for this session.")

    order_flow_payload = {
        "report_type": "ORDER_FLOW",
        "report_date": report_date,
        "generated_at": current.isoformat(),
        "research_only": True,
        "status": "READY" if order_flow_symbols else "NO_DATA",
        "symbols_processed": len(order_flow_symbols),
        "symbols": order_flow_symbols,
        "warnings": order_flow_warnings,
        "limitations": [
            "Kite WebSocket snapshots are not a guaranteed record of every exchange execution "
            "or order-book event.",
            (
                "Buy/sell volume and delta are estimates unless an explicit aggressor-side field is supplied."
            ),
            "Displayed depth is resting quantity and can be cancelled; it is not executed volume.",
            "Snapshot-based top-of-book OFI can miss intermediate book changes.",
        ],
    }
    order_flow_path = staging_dir / "order_flow" / "order_flow_summary.json"
    _write_json(order_flow_path, order_flow_payload)

    validation_by_symbol: dict[str, Any] = {}
    validation_warnings: list[str] = []
    total_predictions = 0
    total_correct = 0
    evaluated_dates: set[str] = set()
    order_flow_root = root / ".local" / "order_flow"
    if order_flow_root.is_dir():
        for session_dir in sorted(path for path in order_flow_root.iterdir() if path.is_dir()):
            for event_path in sorted(session_dir.glob("*.jsonl.gz")):
                symbol = event_path.name.removesuffix(".jsonl.gz").upper()
                candle_path = candle_archive_dir / f"{symbol}.csv"
                if not candle_path.is_file():
                    validation_warnings.append(
                        f"{session_dir.name}/{symbol}: candle archive is missing."
                    )
                    continue
                try:
                    with candle_path.open("r", encoding="utf-8-sig", newline="") as handle:
                        candle_rows = list(csv.DictReader(handle))
                    events = read_order_flow_events(event_path)
                    result = evaluate_order_flow_predictive_accuracy(events, candle_rows)
                    previous = validation_by_symbol.get(symbol)
                    if previous is None:
                        validation_by_symbol[symbol] = result
                    else:
                        # Aggregate the disjoint session files without pooling raw trade prints.
                        previous["predictions"] += result["predictions"]
                        previous["correct_predictions"] += result["correct_predictions"]
                        previous["sessions_evaluated"] += result["sessions_evaluated"]
                        previous["input_snapshot_count"] += result["input_snapshot_count"]
                        previous["out_of_session_snapshot_count"] += result["out_of_session_snapshot_count"]
                        previous["session_dates"] = sorted(
                            set(previous.get("session_dates", [])) | set(result.get("session_dates", []))
                        )
                        count = previous["predictions"]
                        previous["accuracy_pct"] = (
                            previous["correct_predictions"] / count * 100.0 if count else None
                        )
                        previous["status"] = (
                            "READY_FOR_REVIEW"
                            if count >= 100 and previous["sessions_evaluated"] >= 20
                            else "INSUFFICIENT_PROSPECTIVE_EVIDENCE"
                            if count
                            else "NOT_TESTED"
                        )
                except (OSError, EOFError, gzip.BadGzipFile, csv.Error, OrderFlowError, ValueError) as exc:
                    validation_warnings.append(
                        f"{session_dir.name}/{symbol}: {type(exc).__name__}: {exc}"
                    )

    for result in validation_by_symbol.values():
        total_predictions += int(result.get("predictions") or 0)
        total_correct += int(result.get("correct_predictions") or 0)
        evaluated_dates.update(result.get("session_dates") or [])
    validation_accuracy = (
        total_correct / total_predictions * 100.0 if total_predictions else None
    )
    validation_sessions = len(evaluated_dates)
    validation_status = (
        "NOT_TESTED"
        if not total_predictions
        else "READY_FOR_REVIEW"
        if total_predictions >= 100 and validation_sessions >= 20
        else "INSUFFICIENT_PROSPECTIVE_EVIDENCE"
    )
    validation_payload = {
        "report_type": "MICROSTRUCTURE_PREDICTIVE_VALIDATION",
        "report_date": report_date,
        "generated_at": current.isoformat(),
        "research_only": True,
        "status": validation_status,
        "order_flow": {
            "method": "ORDER_FLOW_DELTA_SIGN_VS_NEXT_5M_CLOSE",
            "predictions": total_predictions,
            "correct_predictions": total_correct,
            "accuracy_pct": validation_accuracy,
            "sessions_evaluated": validation_sessions,
            "session_dates": sorted(evaluated_dates),
            "minimum_predictions_required": 100,
            "minimum_independent_sessions_required": 20,
            "symbols": validation_by_symbol,
        },
        "volume_profile": {
            "status": "NOT_TESTED",
            "reason": (
                "The current POC/value-area report is calculated from the completed full session. "
                "Using it to predict movements earlier in that same session would introduce look-ahead "
                "bias. Time-sliced, point-in-time profiles are required first."
            ),
        },
        "warnings": validation_warnings,
        "limitations": [
            "This is a preliminary directional diagnostic, not a net-P&L or executable-fill backtest.",
            "Kite snapshot aggressor-side classification is estimated and may omit exchange events.",
            "Accuracy remains insufficient evidence until at least 100 predictions across 20 independent sessions.",
            "No output from this report is connected to Champion entries, exits, ranking or sizing.",
        ],
    }
    validation_path = staging_dir / "microstructure_validation" / "microstructure_validation_summary.json"
    _write_json(validation_path, validation_payload)

    return [
        {
            "label": "Daily Volume Profile summary",
            "path": str(volume_path),
            "required": False,
            "exists": volume_path.is_file(),
            "purpose": (
                "Current-session estimated POC, VAH, VAL, HVN and LVN for symbols "
                "with valid candle and tick-size data"
            ),
            "category": "volume_profile",
            "upload": volume_path.is_file(),
        },
        {
            "label": "Daily Order Flow summary",
            "path": str(order_flow_path),
            "required": False,
            "exists": order_flow_path.is_file(),
            "purpose": (
                "Estimated buy/sell volume, delta, displayed-depth imbalance and "
                "snapshot OFI for captured symbols"
            ),
            "category": "order_flow",
            "upload": order_flow_path.is_file(),
        },
        {
            "label": "Microstructure predictive validation summary",
            "path": str(validation_path),
            "required": False,
            "exists": validation_path.is_file(),
            "purpose": (
                "Research-only next-candle directional accuracy diagnostics with explicit "
                "sample-size and look-ahead-bias gates"
            ),
            "category": "microstructure_validation",
            "upload": validation_path.is_file(),
        },
    ]
