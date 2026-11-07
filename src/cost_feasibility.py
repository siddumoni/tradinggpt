"""Cost-feasibility audit for prospective scanner signal journals and candidate CSVs."""
from __future__ import annotations

import argparse
import math
from pathlib import Path
from typing import Any

import pandas as pd

from src.risk_engine import calculate_net_reward_risk, load_phase2_settings

MINIMUM_SLIPPAGE_BPS = 5.0
PROMOTION_SLIPPAGE_BPS = 8.0
MINIMUM_NET_RR = 1.5


def _number(value: Any, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} must be a finite number") from exc
    if not pd.notna(number) or number == float("inf") or number == float("-inf"):
        raise ValueError(f"{field} must be a finite number")
    return number


def _journal_value(row: dict[str, Any], journal_name: str, candidate_name: str) -> Any:
    if journal_name in row and pd.notna(row[journal_name]):
        return row[journal_name]
    return row.get(candidate_name)


def audit_candidates(
    frame: pd.DataFrame,
    *,
    slippage_floor_bps: float = MINIMUM_SLIPPAGE_BPS,
    profile: str = "research",
) -> pd.DataFrame:
    """Calculate net T2 R:R at the 5 bps floor and 8 bps stress case.

    Accepts either normalized candidate columns (date/entry/stop/target_2) or
    the append-only paper signal journal (signal_time/reference_entry/stop_loss).
    Zero-quantity journal observations are retained as unscorable, not assigned
    an invented one-share quantity.
    """
    if not math.isfinite(float(slippage_floor_bps)) or not MINIMUM_SLIPPAGE_BPS <= slippage_floor_bps <= 100:
        raise ValueError("slippage floor must be between 5 and 100 bps per side")
    if frame.empty:
        return pd.DataFrame(columns=[
            "date", "symbol", "strategy", "strategy_version", "side", "status", "quantity", "stop_distance_pct",
            "round_trip_cost_pct", "net_rr_t2", "net_rr_t2_8bps",
            "passes_1_5_net_rr", "passes_1_5_net_rr_8bps",
        ])
    configured, rates = load_phase2_settings(profile=profile)
    del configured  # Account capital is not needed to audit the fixed candidate levels.
    rows: list[dict[str, Any]] = []
    for raw in frame.to_dict("records"):
        entry = _number(_journal_value(raw, "reference_entry", "entry"), "entry")
        stop = _number(_journal_value(raw, "stop_loss", "stop"), "stop")
        target_2 = _number(raw.get("target_2"), "target_2")
        target_1_value = _journal_value(raw, "target_1", "target_2")
        target_1 = _number(target_1_value, "target_1")
        side = str(raw.get("side", "")).strip().upper()
        symbol = str(raw.get("symbol", "")).strip().upper()
        strategy = str(raw.get("strategy") or "UNSPECIFIED").strip()
        strategy_version = str(raw.get("strategy_version") or "UNVERSIONED_SIGNAL_JOURNAL").strip()
        stamp_value = raw.get("signal_time")
        if stamp_value is None or pd.isna(stamp_value) or not str(stamp_value).strip():
            stamp_value = raw.get("date")
        if stamp_value is None or pd.isna(stamp_value) or not str(stamp_value).strip():
            raise ValueError("candidate date or signal_time is required")
        stamp = pd.Timestamp(stamp_value)
        if pd.isna(stamp):
            raise ValueError("candidate date or signal_time is invalid")
        date = stamp.date().isoformat()
        if not symbol:
            raise ValueError("candidate symbol is required")
        if side not in {"LONG", "SHORT"}:
            raise ValueError("candidate side must be LONG or SHORT")
        if min(entry, stop, target_1, target_2) <= 0:
            raise ValueError("candidate prices must be positive")
        if side == "LONG" and not stop < entry < target_1 <= target_2:
            raise ValueError("invalid LONG stop/entry/target ordering")
        if side == "SHORT" and not target_2 <= target_1 < entry < stop:
            raise ValueError("invalid SHORT stop/entry/target ordering")
        quantity_value = raw.get("quantity")
        try:
            quantity_float = float(quantity_value)
        except (TypeError, ValueError, OverflowError):
            quantity_float = 0.0
        if not pd.notna(quantity_float) or quantity_float <= 0 or not quantity_float.is_integer():
            rows.append({
                "date": date, "symbol": symbol, "strategy": strategy,
                "strategy_version": strategy_version, "side": side,
                "status": "UNSCORABLE_NONPOSITIVE_OR_INVALID_QUANTITY",
                "quantity": quantity_value, "stop_distance_pct": abs(entry - stop) / entry * 100,
                "round_trip_cost_pct": None, "round_trip_cost_pct_8bps": None,
                "net_rr_t2": None, "net_rr_t2_8bps": None,
                "slippage_bps_per_side": None, "slippage_bps_per_side_8bps": None,
                "passes_1_5_net_rr": None, "passes_1_5_net_rr_8bps": None,
            })
            continue
        quantity = int(quantity_float)
        spread_bps = MINIMUM_SLIPPAGE_BPS
        bid, ask = raw.get("bid"), raw.get("ask")
        if pd.notna(bid) and pd.notna(ask):
            bid_value, ask_value = _number(bid, "bid"), _number(ask, "ask")
            if bid_value > 0 and ask_value >= bid_value:
                mid = (bid_value + ask_value) / 2
                spread_bps = max(MINIMUM_SLIPPAGE_BPS, (ask_value - bid_value) / (2 * mid) * 10_000)
        slip_5 = max(float(slippage_floor_bps), spread_bps)
        slip_8 = max(PROMOTION_SLIPPAGE_BPS, spread_bps, float(slippage_floor_bps))
        metrics_5 = calculate_net_reward_risk(
            side=side, entry_price=entry, stop_loss=stop,
            target_1=target_1, target_2=target_2, quantity=quantity,
            slippage_bps_per_side=slip_5, charge_rates=rates,
        )
        metrics_8 = calculate_net_reward_risk(
            side=side, entry_price=entry, stop_loss=stop,
            target_1=target_1, target_2=target_2, quantity=quantity,
            slippage_bps_per_side=slip_8, charge_rates=rates,
        )
        rows.append({
            "date": date, "symbol": symbol, "strategy": strategy,
            "strategy_version": strategy_version, "side": side, "status": "AUDITABLE",
            "quantity": quantity,
            "stop_distance_pct": abs(entry - stop) / entry * 100,
            "round_trip_cost_pct": (metrics_5["net_risk_inr"] - metrics_5["gross_risk_inr"]) / (entry * quantity) * 100,
            "round_trip_cost_pct_8bps": (metrics_8["net_risk_inr"] - metrics_8["gross_risk_inr"]) / (entry * quantity) * 100,
            "net_rr_t2": metrics_5["net_rr_t2"],
            "net_rr_t2_8bps": metrics_8["net_rr_t2"],
            "slippage_bps_per_side": slip_5,
            "slippage_bps_per_side_8bps": slip_8,
            "passes_1_5_net_rr": metrics_5["net_rr_t2"] >= MINIMUM_NET_RR,
            "passes_1_5_net_rr_8bps": metrics_8["net_rr_t2"] >= MINIMUM_NET_RR,
        })
    return pd.DataFrame(rows)


def summarize_gate_rates(audit: pd.DataFrame) -> pd.DataFrame:
    """Summarize 1.5 net T2 R:R pass rates by signal date and symbol."""
    if audit.empty:
        return pd.DataFrame(columns=[
            "date", "symbol", "strategy", "strategy_version", "candidate_count", "unscorable_count",
            "passes_1_5_net_rr", "pass_rate_5bps",
            "passes_1_5_net_rr_8bps", "pass_rate_8bps",
        ])
    rows = []
    for (date, symbol, strategy, strategy_version), group in audit.groupby(
        ["date", "symbol", "strategy", "strategy_version"], dropna=False, sort=True
    ):
        valid = group.loc[group["status"] == "AUDITABLE"]
        count = len(valid)
        pass_5 = int(valid["passes_1_5_net_rr"].sum()) if count else 0
        pass_8 = int(valid["passes_1_5_net_rr_8bps"].sum()) if count else 0
        rows.append({
            "date": str(date), "symbol": str(symbol), "strategy": str(strategy),
            "strategy_version": str(strategy_version), "candidate_count": count,
            "unscorable_count": int(len(group) - count),
            "passes_1_5_net_rr": pass_5,
            "pass_rate_5bps": pass_5 / count if count else None,
            "passes_1_5_net_rr_8bps": pass_8,
            "pass_rate_8bps": pass_8 / count if count else None,
        })
    return pd.DataFrame(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Append-only paper signal journal or normalized candidate CSV.")
    parser.add_argument("--output", type=Path, help="Write candidate-level cost audit CSV.")
    parser.add_argument("--summary-output", type=Path, help="Write date/symbol gate pass-rate CSV.")
    parser.add_argument("--profile", choices=("research", "paper", "live-advisory"), default="research")
    parser.add_argument("--slippage-floor-bps", type=float, default=MINIMUM_SLIPPAGE_BPS)
    args = parser.parse_args()
    try:
        source = pd.read_csv(args.input)
        audit = audit_candidates(source, slippage_floor_bps=args.slippage_floor_bps, profile=args.profile)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    summary = summarize_gate_rates(audit)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        audit.to_csv(args.output, index=False)
    if args.summary_output:
        args.summary_output.parent.mkdir(parents=True, exist_ok=True)
        summary.to_csv(args.summary_output, index=False)
    if audit.empty:
        print("No candidate rows found.")
        return 0
    print(summary.to_string(index=False))
    auditable = audit.loc[audit["status"] == "AUDITABLE"]
    unscorable = len(audit) - len(auditable)
    if auditable.empty:
        print(f"No auditable candidates; {unscorable} rows have missing/invalid quantity.")
        return 0
    print(
        f"Auditable candidates: {len(auditable)}; unscorable: {unscorable}; "
        f"1.5 net T2 R:R pass rate: {auditable['passes_1_5_net_rr'].mean():.1%} "
        f"at {args.slippage_floor_bps:g}+ bps/side; "
        f"{auditable['passes_1_5_net_rr_8bps'].mean():.1%} at 8+ bps/side."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
