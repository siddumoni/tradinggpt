"""Generate end-of-day Volume Profile and Order Flow research summaries."""
from __future__ import annotations

import csv
import gzip
import json
from datetime import datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.order_flow import OrderFlowError, analyze_order_flow, read_order_flow_events
from src.volume_profile import VolumeProfileError, build_volume_profile

IST = ZoneInfo("Asia/Kolkata")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False)
    path.write_text(rendered + "\n", encoding="utf-8")


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
    tick_sizes, cache_warning = _load_tick_sizes(root / ".local" / "nse_instruments.json")

    volume_symbols: dict[str, Any] = {}
    volume_coverage: dict[str, Any] = {}
    volume_warnings: list[str] = []
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
            latest_stamp = max(
                datetime.fromisoformat(
                    str(row.get("date") or row.get("timestamp")).replace("Z", "+00:00")
                )
                for row in session_rows
            )
            latest_ist = (
                latest_stamp.replace(tzinfo=IST)
                if latest_stamp.tzinfo is None
                else latest_stamp.astimezone(IST)
            )
            full_session_coverage = (
                latest_ist.time().replace(tzinfo=None) >= time(15, 25)
                and len(session_rows) >= 75
            )
            volume_coverage[symbol] = {
                "candle_rows": len(session_rows),
                "latest_bar_start_ist": latest_ist.strftime("%H:%M"),
                "expected_final_bar_start_ist": "15:25",
                "full_session_coverage": full_session_coverage,
            }
            if not full_session_coverage:
                volume_warnings.append(
                    f"{symbol}: archive contains {len(session_rows)} session candle(s), "
                    f"latest starts at {latest_ist.strftime('%H:%M')} IST; final 15:25 candle "
                    "and/or full 75-bar session coverage is missing."
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
    ]
