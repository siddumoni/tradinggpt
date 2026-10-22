"""Reprice a fixed set of historical trades under alternative slippage assumptions.

Research only. This tool deliberately does not rerun signals, resize positions, or
re-apply the minimum net reward/risk acceptance gate. Every input trade remains in
the result under every requested slippage value, isolating cost sensitivity from
trade-selection sensitivity. It never submits orders.
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path
from typing import Iterable

import pandas as pd

from src.cost_calculator import ChargeRates, estimate_intraday_round_trip_cost
from src.risk_engine import load_phase2_settings

REQUIRED_COLUMNS = {
    "symbol", "side", "entry_time", "exit_time", "entry", "exit",
    "quantity", "gross_pnl",
}


def parse_slippage_values(value: str) -> tuple[float, ...]:
    """Parse comma-separated per-side bps and reject ambiguous settings."""
    try:
        values = tuple(float(part.strip()) for part in value.split(",") if part.strip())
    except (TypeError, ValueError) as exc:
        raise ValueError("slippage values must be comma-separated numbers") from exc
    if not values:
        raise ValueError("provide at least one slippage value")
    if len(set(values)) != len(values):
        raise ValueError("slippage values must not contain duplicates")
    if any(not math.isfinite(v) or v < 0 or v > 100 for v in values):
        raise ValueError("slippage values must be finite and between 0 and 100 bps per side")
    return values


def reprice_trade_set(
    trades: pd.DataFrame,
    *,
    slippage_values: Iterable[float],
    charge_rates: ChargeRates,
    source_label: str = "trade_log",
) -> pd.DataFrame:
    """Return one detail row per original trade and slippage value.

    Entry/exit prices, side, quantity, timestamps, and trade membership stay fixed.
    The source gross P&L is checked against those fixed fields before recomputing
    configurable transaction charges and the per-side slippage estimate.
    """
    missing = REQUIRED_COLUMNS.difference(trades.columns)
    if missing:
        raise ValueError(f"Trade log missing required columns: {sorted(missing)}")

    values = tuple(float(v) for v in slippage_values)
    if not values or any(not math.isfinite(v) or v < 0 or v > 100 for v in values):
        raise ValueError("slippage values must be finite and between 0 and 100 bps per side")

    frame = trades.copy().reset_index(drop=True)
    if frame.empty:
        return pd.DataFrame(columns=[
            "source_file", "trade_id", "symbol", "side", "entry_time", "exit_time",
            "entry", "exit", "quantity", "gross_pnl", "slippage_bps_per_side",
            "brokerage", "stt", "exchange_charges", "sebi_charges", "gst",
            "stamp_duty", "ipft", "transaction_charges", "slippage_cost",
            "total_estimated_costs", "net_pnl", "exit_reason",
        ])

    frame["side"] = frame["side"].astype(str).str.upper()
    if not frame["side"].isin(["LONG", "SHORT"]).all():
        bad = sorted(frame.loc[~frame["side"].isin(["LONG", "SHORT"]), "side"].unique())
        raise ValueError(f"Trade log has unsupported side value(s): {bad}")

    for column in ("entry", "exit", "quantity", "gross_pnl"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
        if frame[column].isna().any() or not frame[column].map(math.isfinite).all():
            raise ValueError(f"Trade log column {column!r} contains missing or non-finite values")
    if (frame[["entry", "exit"]] <= 0).any().any():
        raise ValueError("Trade log entry and exit prices must be positive")
    quantity_values = frame["quantity"].to_numpy(dtype=float)
    if (quantity_values < 1).any() or not all(q.is_integer() for q in quantity_values):
        raise ValueError("Trade log quantity must contain positive integers")

    for column in ("entry_time", "exit_time"):
        parsed = pd.to_datetime(frame[column], errors="coerce", utc=True)
        if parsed.isna().any():
            raise ValueError(f"Trade log column {column!r} contains invalid timestamps")
        frame[f"_{column}_sort"] = parsed

    output: list[dict] = []
    # Realized P&L is ordered by exit time; ties preserve the input file order.
    frame = frame.sort_values(["_exit_time_sort", "_entry_time_sort"], kind="stable")
    for source_row, (_, row) in enumerate(frame.iterrows(), start=1):
        side = row["side"]
        entry = float(row["entry"])
        exit_price = float(row["exit"])
        quantity = int(row["quantity"])
        gross_pnl = (exit_price - entry) * quantity if side == "LONG" else (entry - exit_price) * quantity
        reported_gross = float(row["gross_pnl"])
        if not math.isclose(gross_pnl, reported_gross, rel_tol=1e-6, abs_tol=0.02):
            raise ValueError(
                f"Trade {source_row} gross_pnl does not reconcile: "
                f"reported={reported_gross:.6f}, recalculated={gross_pnl:.6f}"
            )

        charges = estimate_intraday_round_trip_cost(
            side=side,
            entry_price=entry,
            exit_price=exit_price,
            quantity=quantity,
            rates=charge_rates,
        )
        for bps in values:
            slippage_cost = (entry + exit_price) * quantity * bps / 10_000.0
            transaction_charges = charges.total
            total_costs = transaction_charges + slippage_cost
            item = {
                "source_file": source_label,
                "trade_id": int(row.name) + 1,
                "symbol": str(row["symbol"]).upper(),
                "side": side,
                "entry_time": row["entry_time"],
                "exit_time": row["exit_time"],
                "entry": entry,
                "exit": exit_price,
                "quantity": quantity,
                "gross_pnl": gross_pnl,
                "slippage_bps_per_side": bps,
                "brokerage": charges.brokerage,
                "stt": charges.stt,
                "exchange_charges": charges.exchange_charges,
                "sebi_charges": charges.sebi_charges,
                "gst": charges.gst,
                "stamp_duty": charges.stamp_duty,
                "ipft": charges.ipft,
                "transaction_charges": transaction_charges,
                "slippage_cost": slippage_cost,
                "total_estimated_costs": total_costs,
                "net_pnl": gross_pnl - total_costs,
            }
            if "exit_reason" in frame.columns:
                item["exit_reason"] = row["exit_reason"]
            output.append(item)
    return pd.DataFrame(output)


def summarize_repricing(
    detail: pd.DataFrame, *, slippage_values: Iterable[float], initial_capital: float
) -> pd.DataFrame:
    """Aggregate repriced trades by source file and cost assumption."""
    if not math.isfinite(initial_capital) or initial_capital <= 0:
        raise ValueError("initial_capital must be finite and positive")
    result: list[dict] = []
    sources = detail["source_file"].drop_duplicates().tolist() if not detail.empty else []
    for source in sources:
        source_frame = detail.loc[detail["source_file"] == source]
        for bps in slippage_values:
            group = source_frame.loc[source_frame["slippage_bps_per_side"] == float(bps)].copy()
            group = group.sort_values(["exit_time", "entry_time"], kind="stable")
            net = group["net_pnl"].astype(float)
            wins = net[net > 0]
            losses = net[net < 0]
            equity = float(initial_capital)
            peak = equity
            max_drawdown = 0.0
            for pnl in net:
                equity += float(pnl)
                peak = max(peak, equity)
                max_drawdown = max(max_drawdown, peak - equity)
            loss_amount = abs(float(losses.sum()))
            profit_factor = float(wins.sum()) / loss_amount if loss_amount > 0 else (
                math.inf if len(wins) else None
            )
            result.append({
                "source_file": source,
                "slippage_bps_per_side": float(bps),
                "frozen_trade_count": int(len(group)),
                "gross_pnl": float(group["gross_pnl"].sum()) if len(group) else 0.0,
                "transaction_charges": float(group["transaction_charges"].sum()) if len(group) else 0.0,
                "slippage_cost": float(group["slippage_cost"].sum()) if len(group) else 0.0,
                "total_estimated_costs": float(group["total_estimated_costs"].sum()) if len(group) else 0.0,
                "net_pnl": float(net.sum()) if len(group) else 0.0,
                "expectancy_per_trade": float(net.mean()) if len(group) else None,
                "win_rate_pct": float((net > 0).mean() * 100.0) if len(group) else None,
                "profit_factor": profit_factor,
                "max_drawdown": max_drawdown,
                "ending_equity": equity,
                "warning": (
                    "Frozen trade set; no signal rerun, position resizing, or minimum-net-RR re-filter. "
                    "Research diagnostic only, not an out-of-sample result."
                ),
            })
    return pd.DataFrame(result)


def _resolve_trade_files(inputs: list[str]) -> list[Path]:
    files: list[Path] = []
    for raw_path in inputs:
        path = Path(raw_path)
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            files.extend(sorted(p for p in path.rglob("*_trades.csv") if p.is_file()))
        else:
            raise FileNotFoundError(f"Trade log path not found: {path}")
    # Stable de-duplication protects against supplying both a folder and a file in it.
    unique = list(dict.fromkeys(p.resolve() for p in files))
    if not unique:
        raise FileNotFoundError("No trade logs found; directories must contain *_trades.csv files")
    return unique


def run_repricing(
    trade_files: list[Path],
    *,
    slippage_values: tuple[float, ...],
    output_path: Path,
    initial_capital: float,
    charge_rates: ChargeRates,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Reprice each input file independently and save summary and trade-level CSVs."""
    detail_frames: list[pd.DataFrame] = []
    for path in trade_files:
        frame = pd.read_csv(path)
        detail_frames.append(reprice_trade_set(
            frame,
            slippage_values=slippage_values,
            charge_rates=charge_rates,
            source_label=str(path.resolve()),
        ))
    detail = pd.concat(detail_frames, ignore_index=True) if detail_frames else pd.DataFrame()
    summary = summarize_repricing(
        detail, slippage_values=slippage_values, initial_capital=initial_capital
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    summary.to_csv(output_path, index=False)
    detail_path = output_path.with_name(f"{output_path.stem}_trade_details.csv")
    detail.to_csv(detail_path, index=False)
    print(f"Frozen-trade-set repricing summary: {output_path}")
    print(f"Trade-level details: {detail_path}")
    print("Each source file is an independent frozen trade set; do not sum different files as one portfolio.")
    print("No signals were regenerated and no minimum-net-R:R acceptance gate was re-applied.")
    return summary, detail


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reprice saved trade logs at alternative slippage without changing trade membership."
    )
    parser.add_argument(
        "--trades", nargs="+", required=True,
        help="Trade CSV paths, or directories containing *_trades.csv. Each file is repriced independently.",
    )
    parser.add_argument("--slippage-bps", default="2,5,8", help="Comma-separated per-side bps (e.g. 2,5,8)")
    parser.add_argument(
        "--output", default="reports/backtests/frozen_trade_slippage_repricing.csv",
        help="Summary CSV path; a sibling *_trade_details.csv is also written.",
    )
    parser.add_argument("--initial-capital", type=float, default=None, help="Starting equity used for drawdown; defaults to config/settings.yaml")
    args = parser.parse_args()
    try:
        values = parse_slippage_values(args.slippage_bps)
        files = _resolve_trade_files(args.trades)
    except (ValueError, FileNotFoundError) as exc:
        parser.error(str(exc))

    configured_risk, charge_rates = load_phase2_settings()
    initial_capital = configured_risk.capital_inr if args.initial_capital is None else args.initial_capital
    if not math.isfinite(initial_capital) or initial_capital <= 0:
        parser.error("--initial-capital must be finite and positive")
    run_repricing(
        files,
        slippage_values=values,
        output_path=Path(args.output),
        initial_capital=initial_capital,
        charge_rates=charge_rates,
    )


if __name__ == "__main__":
    main()
