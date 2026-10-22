"""Replay saved per-symbol trades against one shared cash and risk budget.

The inputs must be one selected baseline trade log per intended candidate stream.
This is a portfolio allocation replay of already-generated trades, not a true
portfolio backtest: the strategy signals are not regenerated when capital is busy.
No order is submitted, modified, or cancelled.
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from src.cost_calculator import ChargeRates, estimate_intraday_round_trip_cost
from src.risk_engine import load_phase2_settings

IST = ZoneInfo("Asia/Kolkata")
REQUIRED_COLUMNS = {
    "symbol", "side", "entry_time", "exit_time", "entry", "exit",
    "stop_loss", "quantity", "gross_pnl",
}


def _parse_ist(value: Any, *, column: str, source: str) -> pd.Timestamp:
    try:
        timestamp = pd.Timestamp(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{source}: invalid {column} timestamp {value!r}") from exc
    if pd.isna(timestamp):
        raise ValueError(f"{source}: missing {column} timestamp")
    if timestamp.tzinfo is None:
        return timestamp.tz_localize(IST)
    return timestamp.tz_convert(IST)


def _resolve_trade_files(inputs: list[str | Path]) -> list[Path]:
    files: list[Path] = []
    for value in inputs:
        path = Path(value)
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            files.extend(sorted(p for p in path.rglob("*_trades.csv") if p.is_file()))
        else:
            raise FileNotFoundError(f"Trade log path not found: {path}")
    # Keep argument order for deterministic same-time allocation priority.
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in files:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(resolved)
    if not unique:
        raise FileNotFoundError("No trade logs found; directories must contain *_trades.csv files")
    return unique


def load_candidate_trades(trade_files: list[Path]) -> list[dict]:
    """Load and validate saved trades; preserve input file/row order as tie-break."""
    candidates: list[dict] = []
    for file_order, path in enumerate(trade_files):
        frame = pd.read_csv(path)
        missing = REQUIRED_COLUMNS.difference(frame.columns)
        if missing:
            raise ValueError(f"{path}: trade log missing required columns {sorted(missing)}")
        for row_index, row in frame.iterrows():
            source = str(path)
            side = str(row["side"]).upper()
            if side not in {"LONG", "SHORT"}:
                raise ValueError(f"{source}, row {row_index + 2}: side must be LONG or SHORT")
            try:
                entry = float(row["entry"])
                exit_price = float(row["exit"])
                stop = float(row["stop_loss"])
                quantity_float = float(row["quantity"])
                reported_gross = float(row["gross_pnl"])
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(f"{source}, row {row_index + 2}: invalid numeric trade field") from exc
            if not all(math.isfinite(v) for v in (entry, exit_price, stop, quantity_float, reported_gross)):
                raise ValueError(f"{source}, row {row_index + 2}: trade values must be finite")
            if entry <= 0 or exit_price <= 0 or stop <= 0:
                raise ValueError(f"{source}, row {row_index + 2}: prices must be positive")
            if quantity_float < 1 or not quantity_float.is_integer():
                raise ValueError(f"{source}, row {row_index + 2}: quantity must be a positive integer")
            if side == "LONG" and not stop < entry:
                raise ValueError(f"{source}, row {row_index + 2}: LONG stop_loss must be below entry")
            if side == "SHORT" and not stop > entry:
                raise ValueError(f"{source}, row {row_index + 2}: SHORT stop_loss must be above entry")

            entry_ts = _parse_ist(row["entry_time"], column="entry_time", source=source)
            exit_ts = _parse_ist(row["exit_time"], column="exit_time", source=source)
            # Same-bar stop-outs can share the entry and exit candle label under
            # start-time timestamps. An exit before entry is not chronologically valid.
            if exit_ts < entry_ts:
                raise ValueError(
                    f"{source}, row {row_index + 2}: exit_time precedes entry_time "
                    "(check source timestamps and candle-start semantics)"
                )
            quantity = int(quantity_float)
            gross = (exit_price - entry) * quantity if side == "LONG" else (entry - exit_price) * quantity
            if not math.isclose(gross, reported_gross, rel_tol=1e-6, abs_tol=0.02):
                raise ValueError(
                    f"{source}, row {row_index + 2}: gross P&L mismatch; "
                    f"reported={reported_gross:.6f}, recalculated={gross:.6f}"
                )
            candidates.append({
                "source_file": source,
                "source_row": int(row_index) + 2,
                "file_order": file_order,
                "row_order": int(row_index),
                "symbol": str(row["symbol"]).upper(),
                "side": side,
                "entry_time": str(row["entry_time"]),
                "exit_time": str(row["exit_time"]),
                "_entry_ts": entry_ts,
                "_exit_ts": exit_ts,
                "entry": entry,
                "exit": exit_price,
                "stop_loss": stop,
                "original_quantity": quantity,
                "source_gross_pnl": reported_gross,
                "exit_reason": str(row.get("exit_reason", "")),
            })
    return candidates


def _trade_costs(side: str, entry: float, exit_price: float, quantity: int,
                 slippage_bps_per_side: float, rates: ChargeRates) -> dict[str, float]:
    charges = estimate_intraday_round_trip_cost(
        side=side, entry_price=entry, exit_price=exit_price,
        quantity=quantity, rates=rates,
    )
    slippage = (entry + exit_price) * quantity * slippage_bps_per_side / 10_000.0
    return {
        "brokerage": charges.brokerage,
        "stt": charges.stt,
        "exchange_charges": charges.exchange_charges,
        "sebi_charges": charges.sebi_charges,
        "gst": charges.gst,
        "stamp_duty": charges.stamp_duty,
        "ipft": charges.ipft,
        "transaction_charges": charges.total,
        "slippage_cost": slippage,
        "total_estimated_costs": charges.total + slippage,
    }


def simulate_portfolio(
    trade_files: list[Path],
    *,
    initial_capital: float,
    risk_per_trade: float,
    slippage_bps_per_side: float,
    max_concurrent_positions: int,
    charge_rates: ChargeRates,
) -> tuple[dict, pd.DataFrame]:
    """Allocate one shared cash pool to saved candidate trades in entry-time order.

    Each candidate is capped by its source quantity, the per-trade risk budget,
    and available unreserved capital. Round-trip costs/slippage at the stop are
    included when checking risk and reserving capital. Active positions reserve
    entry notional plus a stop-cost buffer. At exit, actual saved exit-price costs
    are realised and update equity. When candidates share an entry timestamp,
    the order of --trades inputs then row order is the deterministic tie-break.
    """
    for name, value in (
        ("initial_capital", initial_capital),
        ("risk_per_trade", risk_per_trade),
    ):
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be finite and positive")
    if not math.isfinite(slippage_bps_per_side) or not 0 <= slippage_bps_per_side <= 100:
        raise ValueError("slippage_bps_per_side must be finite and between 0 and 100")
    if isinstance(max_concurrent_positions, bool) or max_concurrent_positions < 1:
        raise ValueError("max_concurrent_positions must be a positive integer")

    candidates = load_candidate_trades(trade_files)
    candidates.sort(key=lambda c: (c["_entry_ts"], c["file_order"], c["row_order"]))

    equity = float(initial_capital)
    active: list[dict] = []
    output: list[dict] = []
    closed: list[dict] = []
    max_active_observed = 0

    def close_due_positions(at_time: pd.Timestamp | None = None) -> None:
        nonlocal equity, active
        due = [
            position for position in active
            if at_time is None or position["_exit_ts"] <= at_time
        ]
        due.sort(key=lambda p: (p["_exit_ts"], p["_entry_ts"], p["_file_order"], p["_row_order"]))
        for position in due:
            equity += position["net_pnl"]
            closed.append({
                "exit_ts": position["_exit_ts"],
                "entry_ts": position["_entry_ts"],
                "file_order": position["_file_order"],
                "row_order": position["_row_order"],
                "net_pnl": position["net_pnl"],
            })
            active.remove(position)

    for candidate in candidates:
        entry_ts = candidate["_entry_ts"]
        close_due_positions(entry_ts)

        base = {
            key: value for key, value in candidate.items()
            if not key.startswith("_") and key not in {"file_order", "row_order", "source_gross_pnl"}
        }
        base.update({
            "accepted": False,
            "rejection_reason": "",
            "available_capital_at_entry": round(equity - sum(p["_reserved_capital"] for p in active), 2),
            "original_quantity": candidate["original_quantity"],
            "quantity": 0,
            "reserved_capital": 0.0,
            "gross_pnl": None,
            "brokerage": None,
            "stt": None,
            "exchange_charges": None,
            "sebi_charges": None,
            "gst": None,
            "stamp_duty": None,
            "ipft": None,
            "transaction_charges": None,
            "slippage_cost": None,
            "total_estimated_costs": None,
            "net_pnl": None,
        })

        if any(p["symbol"] == candidate["symbol"] for p in active):
            base["rejection_reason"] = "same_symbol_position_already_open"
            output.append(base)
            continue
        if len(active) >= max_concurrent_positions:
            base["rejection_reason"] = "max_concurrent_positions_reached"
            output.append(base)
            continue

        available = equity - sum(p["_reserved_capital"] for p in active)
        entry = candidate["entry"]
        stop = candidate["stop_loss"]
        exit_price = candidate["exit"]
        side = candidate["side"]
        risk_per_share = abs(entry - stop)
        cash_quantity = math.floor(max(0.0, available) / entry)
        risk_quantity = math.floor(risk_per_trade / risk_per_share)
        quantity = min(candidate["original_quantity"], cash_quantity, risk_quantity)

        accepted_costs: dict[str, float] | None = None
        reserve_costs: dict[str, float] | None = None
        reserve = 0.0
        while quantity > 0:
            stop_costs = _trade_costs(side, entry, stop, quantity, slippage_bps_per_side, charge_rates)
            estimated_stop_loss = risk_per_share * quantity + stop_costs["total_estimated_costs"]
            reserve = entry * quantity + stop_costs["total_estimated_costs"]
            # Risk must be within budget and the full notional plus cost buffer
            # must fit available capital while other positions remain open.
            if estimated_stop_loss <= risk_per_trade and reserve <= available:
                reserve_costs = stop_costs
                accepted_costs = _trade_costs(
                    side, entry, exit_price, quantity, slippage_bps_per_side, charge_rates
                )
                break
            quantity -= 1

        if quantity < 1 or accepted_costs is None or reserve_costs is None:
            base["rejection_reason"] = (
                "insufficient_available_capital"
                if cash_quantity < 1 or available <= 0
                else "no_share_fits_risk_or_reserved_capital"
            )
            output.append(base)
            continue

        gross = (
            (exit_price - entry) * quantity
            if side == "LONG" else (entry - exit_price) * quantity
        )
        net = gross - accepted_costs["total_estimated_costs"]
        base.update({
            "accepted": True,
            "rejection_reason": "",
            "quantity": quantity,
            "reserved_capital": reserve,
            "gross_pnl": gross,
            **accepted_costs,
            "net_pnl": net,
            "quantity_reduction": candidate["original_quantity"] - quantity,
            "risk_budget_at_stop": (
                risk_per_share * quantity + reserve_costs["total_estimated_costs"]
            ),
        })
        base["_entry_ts"] = entry_ts
        base["_exit_ts"] = candidate["_exit_ts"]
        base["_file_order"] = candidate["file_order"]
        base["_row_order"] = candidate["row_order"]
        base["_reserved_capital"] = reserve
        active.append(base)
        max_active_observed = max(max_active_observed, len(active))
        output.append(base)

    close_due_positions(None)

    details = pd.DataFrame(output)
    accepted = details.loc[details["accepted"]] if not details.empty else pd.DataFrame()
    accepted_net = accepted["net_pnl"].astype(float) if not accepted.empty else pd.Series(dtype=float)
    total_positive = float(accepted_net[accepted_net > 0].sum())
    total_negative = abs(float(accepted_net[accepted_net < 0].sum()))
    if total_negative > 0:
        profit_factor: float | None = total_positive / total_negative
    elif total_positive > 0:
        profit_factor = math.inf
    else:
        profit_factor = None

    peak = float(initial_capital)
    running_equity = float(initial_capital)
    max_drawdown = 0.0
    for trade in sorted(closed, key=lambda c: (c["exit_ts"], c["entry_ts"], c["file_order"], c["row_order"])):
        running_equity += float(trade["net_pnl"])
        peak = max(peak, running_equity)
        max_drawdown = max(max_drawdown, peak - running_equity)

    summary = {
        "input_files": len(trade_files),
        "candidate_trades": int(len(candidates)),
        "accepted_trades": int(len(accepted)),
        "rejected_trades": int(len(details) - len(accepted)),
        "same_symbol_conflicts": int(
            (details.get("rejection_reason", pd.Series(dtype=str)) == "same_symbol_position_already_open").sum()
        ) if not details.empty else 0,
        "capital_or_risk_rejections": int(
            details.get("rejection_reason", pd.Series(dtype=str)).isin([
                "insufficient_available_capital", "no_share_fits_risk_or_reserved_capital"
            ]).sum()
        ) if not details.empty else 0,
        "max_concurrent_positions_configured": int(max_concurrent_positions),
        "max_concurrent_positions_observed": int(max_active_observed),
        "initial_capital": float(initial_capital),
        "risk_per_trade": float(risk_per_trade),
        "slippage_bps_per_side": float(slippage_bps_per_side),
        "gross_pnl": float(accepted["gross_pnl"].sum()) if not accepted.empty else 0.0,
        "transaction_charges": float(accepted["transaction_charges"].sum()) if not accepted.empty else 0.0,
        "slippage_cost": float(accepted["slippage_cost"].sum()) if not accepted.empty else 0.0,
        "total_estimated_costs": float(accepted["total_estimated_costs"].sum()) if not accepted.empty else 0.0,
        "net_pnl": float(accepted_net.sum()) if len(accepted_net) else 0.0,
        "expectancy_per_accepted_trade": float(accepted_net.mean()) if len(accepted_net) else None,
        "win_rate_pct": float((accepted_net > 0).mean() * 100.0) if len(accepted_net) else None,
        "profit_factor": profit_factor,
        "max_realized_drawdown": float(max_drawdown),
        "ending_equity": float(initial_capital + accepted_net.sum()) if len(accepted_net) else float(initial_capital),
        "allocation_tie_break": "entry timestamp, then input file order, then row order",
        "warning": (
            "Portfolio replay of already-generated per-symbol trades, not a full portfolio backtest. "
            "Signals are not regenerated when capital is busy; realized drawdown excludes intratrade mark-to-market. "
            "Use one selected baseline log per intended candidate stream, not overlapping slippage/filter variants."
        ),
    }
    # Remove private timestamps / allocation keys from output CSV.
    for column in ("_entry_ts", "_exit_ts", "_file_order", "_row_order", "_reserved_capital"):
        if column in details.columns:
            details = details.drop(columns=column)
    return summary, details


def run_portfolio_replay(
    trade_files: list[Path],
    *,
    output_path: Path,
    initial_capital: float,
    risk_per_trade: float,
    slippage_bps_per_side: float,
    max_concurrent_positions: int,
    charge_rates: ChargeRates,
) -> tuple[dict, pd.DataFrame]:
    summary, details = simulate_portfolio(
        trade_files,
        initial_capital=initial_capital,
        risk_per_trade=risk_per_trade,
        slippage_bps_per_side=slippage_bps_per_side,
        max_concurrent_positions=max_concurrent_positions,
        charge_rates=charge_rates,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path = output_path.with_suffix(".json")
    details_path = output_path.with_name(f"{output_path.stem}_trade_details.csv")
    # JSON has no standard Infinity value. Preserve an explicit status alongside
    # null so a no-loss sample is not confused with a zero profit factor.
    safe_summary = dict(summary)
    if isinstance(safe_summary.get("profit_factor"), float) and not math.isfinite(safe_summary["profit_factor"]):
        safe_summary["profit_factor"] = None
        safe_summary["profit_factor_status"] = "infinite_no_losing_trades"
    else:
        safe_summary["profit_factor_status"] = "finite_or_not_available"
    summary_path.write_text(
        __import__("json").dumps(safe_summary, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    details.to_csv(details_path, index=False)
    print(f"Portfolio replay summary: {summary_path}")
    print(f"Trade-level allocations: {details_path}")
    print("Research replay only. No orders submitted. See docs/PORTFOLIO_REPLAY.md for limitations.")
    return summary, details


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Replay saved per-symbol backtest trades using one shared cash and risk budget."
    )
    parser.add_argument(
        "--trades", nargs="+", required=True,
        help="Trade CSV paths or directories containing *_trades.csv. One selected baseline per stream.",
    )
    parser.add_argument("--initial-capital", type=float, default=None)
    parser.add_argument("--risk-per-trade", type=float, default=None)
    parser.add_argument("--slippage-bps", type=float, default=None)
    parser.add_argument("--max-concurrent-positions", type=int, default=3)
    parser.add_argument("--output", default="reports/backtests/portfolio_replay.csv",
                        help="Output stem; writes .json summary and *_trade_details.csv")
    args = parser.parse_args()
    try:
        files = _resolve_trade_files(args.trades)
    except FileNotFoundError as exc:
        parser.error(str(exc))

    configured_risk, charge_rates = load_phase2_settings()
    initial_capital = configured_risk.capital_inr if args.initial_capital is None else args.initial_capital
    risk_per_trade = configured_risk.risk_per_trade_inr if args.risk_per_trade is None else args.risk_per_trade
    slippage = configured_risk.slippage_bps_per_side if args.slippage_bps is None else args.slippage_bps
    if not math.isfinite(initial_capital) or initial_capital <= 0:
        parser.error("--initial-capital must be finite and positive")
    if not math.isfinite(risk_per_trade) or risk_per_trade <= 0:
        parser.error("--risk-per-trade must be finite and positive")
    if not math.isfinite(slippage) or not 0 <= slippage <= 100:
        parser.error("--slippage-bps must be between 0 and 100")
    if args.max_concurrent_positions < 1:
        parser.error("--max-concurrent-positions must be a positive integer")
    run_portfolio_replay(
        files,
        output_path=Path(args.output),
        initial_capital=initial_capital,
        risk_per_trade=risk_per_trade,
        slippage_bps_per_side=slippage,
        max_concurrent_positions=args.max_concurrent_positions,
        charge_rates=charge_rates,
    )


if __name__ == "__main__":
    main()
