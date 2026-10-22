"""Journal prospective advisory signals for later paper review; never execute orders.

This module imports saved scanner JSON snapshots into an append-only CSV. Signal
identities are derived from symbol, side, strategy, and signal candle so rerunning
the journal does not duplicate the same candle's setup. Signals using unverified
data or unverified margin remain explicitly watch-only.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
JOURNAL_FIELDS = [
    "signal_id", "recorded_at", "scan_as_of", "report_path", "provider",
    "report_status", "symbol", "side", "strategy", "action", "confidence",
    "market_regime", "signal_time", "reference_entry", "stop_loss", "target_1",
    "target_2", "quantity", "estimated_net_risk_inr", "estimated_net_reward_t1_inr",
    "net_rr_t1", "data_verified_live", "margin_verified", "news_status", "news_risk",
    "paper_status", "status_note",
]
DIRECTIONAL_SIDES = {"LONG", "SHORT"}
PAPER_ACTIONS = {"CONDITIONAL_BUY", "CONDITIONAL_SHORT", "WATCH_CANDIDATE"}


def _finite_positive(value: Any, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"candidate {field} must be a finite positive number") from exc
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"candidate {field} must be a finite positive number")
    return number


def _iso_timestamp(value: Any, field: str) -> str:
    try:
        timestamp = datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"scanner report has invalid {field} timestamp: {value!r}") from exc
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=IST)
    return timestamp.astimezone(IST).isoformat()


def _signal_id(symbol: str, side: str, strategy: str, signal_time: str) -> str:
    identity = "|".join((symbol.upper(), side.upper(), strategy.strip().lower(), signal_time))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def load_scan_report(path: Path) -> dict[str, Any]:
    """Load a saved scanner JSON report and validate its outer shape."""
    try:
        report = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read scanner JSON report {path}: {exc}") from exc
    if not isinstance(report, dict):
        raise ValueError("scanner report must be a JSON object")
    if not isinstance(report.get("status"), str) or not report["status"]:
        raise ValueError("scanner report must contain a non-empty status")
    if not isinstance(report.get("as_of"), str):
        raise ValueError("scanner report must contain an as_of timestamp")
    report["_validated_as_of"] = _iso_timestamp(report["as_of"], "as_of")
    candidates = report.get("candidates", [])
    if not isinstance(candidates, list) or any(not isinstance(c, dict) for c in candidates):
        raise ValueError("scanner report candidates must be a list of objects")
    return report


def _candidate_event(
    report: dict[str, Any],
    candidate: dict[str, Any],
    *,
    scan_path: Path,
    recorded_at: str,
) -> dict[str, Any] | None:
    """Create a journal row for a directional setup; ignore neutral/no-trade rows."""
    side = str(candidate.get("side") or "").upper()
    if side not in DIRECTIONAL_SIDES:
        return None

    action = str(candidate.get("action") or "UNKNOWN").upper()
    if action not in PAPER_ACTIONS:
        raise ValueError(f"candidate {candidate.get('symbol', '?')} has unsupported action {action!r}")
    symbol = str(candidate.get("symbol") or "").strip().upper()
    strategy = str(candidate.get("strategy") or "").strip()
    if not symbol or not strategy:
        raise ValueError("directional candidate must include symbol and strategy")
    signal_time = _iso_timestamp(candidate.get("signal_time"), "signal_time")
    reference_entry = _finite_positive(candidate.get("reference_entry"), "reference_entry")
    stop_loss = _finite_positive(candidate.get("stop_loss"), "stop_loss")
    target_1 = _finite_positive(candidate.get("target_1"), "target_1")
    target_2 = _finite_positive(candidate.get("target_2"), "target_2")
    quantity_raw = candidate.get("quantity", 0)
    try:
        quantity_float = float(quantity_raw)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("candidate quantity must be a non-negative integer") from exc
    if not math.isfinite(quantity_float) or quantity_float < 0 or not quantity_float.is_integer():
        raise ValueError("candidate quantity must be a non-negative integer")

    if side == "LONG" and not stop_loss < reference_entry < target_1 <= target_2:
        raise ValueError("LONG candidate levels must satisfy stop < entry < target_1 <= target_2")
    if side == "SHORT" and not target_2 <= target_1 < reference_entry < stop_loss:
        raise ValueError("SHORT candidate levels must satisfy target_2 <= target_1 < entry < stop")

    # Fail closed: only JSON boolean true is accepted as verification. A string such as "False" must not become truthy.
    data_verified = candidate.get("data_verified_live") is True
    margin_verified = candidate.get("margin_verified") is True
    if not data_verified:
        paper_status = "WATCH_ONLY_UNVERIFIED_DATA"
        status_note = "Data source is not verified as live exchange data; no execution or performance claim."
    elif not margin_verified:
        paper_status = "WATCH_ONLY_UNVERIFIED_MARGIN"
        status_note = "Margin verification is missing; remain watch-only. No orders are sent by this tool."
    elif action == "WATCH_CANDIDATE":
        paper_status = "WATCH_ONLY_NOT_APPROVED"
        status_note = "Scanner gates did not conditionally approve the candidate; observe only."
    else:
        paper_status = "PAPER_ONLY_NOT_EXECUTED"
        status_note = "Eligible for prospective paper review only; no actual or simulated fill is asserted."

    signal_time_for_id = signal_time
    return {
        "signal_id": _signal_id(symbol, side, strategy, signal_time_for_id),
        "recorded_at": recorded_at,
        "scan_as_of": report["_validated_as_of"],
        "report_path": str(scan_path.resolve()),
        "provider": str(report.get("provider", "unknown")),
        "report_status": report["status"],
        "symbol": symbol,
        "side": side,
        "strategy": strategy,
        "action": action,
        "confidence": candidate.get("confidence"),
        "market_regime": candidate.get("market_regime"),
        "signal_time": signal_time,
        "reference_entry": reference_entry,
        "stop_loss": stop_loss,
        "target_1": target_1,
        "target_2": target_2,
        "quantity": int(quantity_float),
        "estimated_net_risk_inr": candidate.get("estimated_net_risk_inr"),
        "estimated_net_reward_t1_inr": candidate.get("estimated_net_reward_t1_inr"),
        "net_rr_t1": candidate.get("net_rr_t1"),
        "data_verified_live": data_verified,
        "margin_verified": margin_verified,
        "news_status": str(candidate.get("news_status", "UNKNOWN")),
        "news_risk": str(candidate.get("news_risk", "UNKNOWN")),
        "paper_status": paper_status,
        "status_note": status_note,
    }


def append_scan_report(
    scan_path: Path,
    journal_path: Path,
    *,
    recorded_at: datetime | None = None,
) -> dict[str, int | str]:
    """Idempotently append eligible directional setups from one scanner report."""
    scan_path = Path(scan_path)
    journal_path = Path(journal_path)
    report = load_scan_report(scan_path)
    stamp = (recorded_at or datetime.now(IST))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=IST)
    recorded_at_text = stamp.astimezone(IST).isoformat()

    events: list[dict[str, Any]] = []
    skipped_non_directional = 0
    for candidate in report.get("candidates", []):
        row = _candidate_event(
            report, candidate, scan_path=scan_path, recorded_at=recorded_at_text
        )
        if row is None:
            skipped_non_directional += 1
        else:
            events.append(row)

    journal_path.parent.mkdir(parents=True, exist_ok=True)
    existing_ids: set[str] = set()
    has_existing_file = journal_path.exists() and journal_path.stat().st_size > 0
    if has_existing_file:
        try:
            with journal_path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                if reader.fieldnames != JOURNAL_FIELDS:
                    raise ValueError(
                        f"Existing journal has an unexpected schema: {journal_path}; "
                        "do not append incompatible formats."
                    )
                existing_ids = {
                    str(row.get("signal_id", "")) for row in reader if row.get("signal_id")
                }
        except OSError as exc:
            raise ValueError(f"Could not read existing journal {journal_path}: {exc}") from exc

    new_rows: list[dict[str, Any]] = []
    seen_ids = set(existing_ids)
    for event in events:
        signal_id = event["signal_id"]
        if signal_id in seen_ids:
            continue
        new_rows.append(event)
        seen_ids.add(signal_id)
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    with journal_path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=JOURNAL_FIELDS, extrasaction="ignore")
        if not has_existing_file:
            writer.writeheader()
        writer.writerows(new_rows)

    return {
        "report_path": str(scan_path),
        "journal_path": str(journal_path),
        "directional_candidates": len(events),
        "new_events": len(new_rows),
        "duplicates_skipped": len(events) - len(new_rows),
        "non_directional_skipped": skipped_non_directional,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Append directional candidates from a saved advisory scan to a paper-only journal."
    )
    parser.add_argument("--scan", required=True, type=Path, help="Saved phase6_scan_*.json report")
    parser.add_argument("--journal", type=Path, default=Path("logs/paper_signals.csv"))
    args = parser.parse_args()
    try:
        result = append_scan_report(args.scan, args.journal)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2))
    print("Journal only: no orders, fills, or realised P&L are created by this command.")


if __name__ == "__main__":
    main()
