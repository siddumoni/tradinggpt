"""Net-of-cost trade statistics and conservative promotion gate (research only)."""
from __future__ import annotations
import argparse
import json
import math
from pathlib import Path
from typing import Any
import numpy as np
import pandas as pd
import yaml

IST = "Asia/Kolkata"
REQUIRED = {"symbol", "side", "entry_time", "exit_time", "entry", "exit", "quantity", "gross_pnl", "transaction_charges"}

def _timestamp(value: Any, label: str) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    if pd.isna(stamp):
        raise ValueError(f"{label} contains a missing timestamp")
    return stamp.tz_localize(IST) if stamp.tzinfo is None else stamp.tz_convert(IST)

def load_trades(path: Path) -> pd.DataFrame:
    try:
        frame = pd.read_csv(path)
    except (OSError, pd.errors.ParserError) as exc:
        raise ValueError(f"Cannot read trade details {path}: {exc}") from exc
    missing = REQUIRED.difference(frame.columns)
    if missing:
        raise ValueError(f"Trade details missing required columns: {sorted(missing)}")
    if "accepted" in frame:
        accepted = frame["accepted"].astype(str).str.strip().str.casefold().isin({"true", "1", "yes"})
        frame = frame.loc[accepted].copy()
    return frame.reset_index(drop=True)

def _prepare_trades(trades: pd.DataFrame, *, slippage_bps: float) -> pd.DataFrame:
    missing = REQUIRED.difference(trades.columns)
    if missing:
        raise ValueError(f"Trade details missing required columns: {sorted(missing)}")
    if not math.isfinite(slippage_bps) or slippage_bps < 8 or slippage_bps > 100:
        raise ValueError("promotion analysis slippage must be between 8 and 100 bps per side")
    frame = trades.copy().reset_index(drop=True)
    frame["side"] = frame["side"].astype(str).str.upper()
    if not frame["side"].isin(["LONG", "SHORT"]).all():
        raise ValueError("side must contain only LONG or SHORT")
    for col in ("entry", "exit", "quantity", "gross_pnl", "transaction_charges"):
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
        if frame[col].isna().any() or not np.isfinite(frame[col].to_numpy(dtype=float)).all():
            raise ValueError(f"{col} contains missing or non-finite values")
    if (frame[["entry", "exit", "quantity"]] <= 0).any().any() or (frame["transaction_charges"] < 0).any():
        raise ValueError("prices and quantity must be positive; charges cannot be negative")
    q = frame["quantity"].to_numpy(dtype=float)
    if not np.equal(q, np.floor(q)).all():
        raise ValueError("quantity must contain whole shares")
    calculated = np.where(frame.side.eq("LONG"), (frame.exit-frame.entry)*frame.quantity, (frame.entry-frame.exit)*frame.quantity)
    if not np.allclose(calculated, frame.gross_pnl, rtol=1e-6, atol=0.02):
        raise ValueError("gross_pnl does not reconcile to side, entry, exit and quantity")
    frame["entry_ts"] = [_timestamp(v, "entry_time") for v in frame.entry_time]
    frame["exit_ts"] = [_timestamp(v, "exit_time") for v in frame.exit_time]
    if (frame.exit_ts < frame.entry_ts).any():
        raise ValueError("exit_time cannot precede entry_time")
    frame["session"] = frame.entry_ts.map(lambda x: x.strftime("%Y-%m-%d"))
    frame["entry_hour"] = frame.entry_ts.map(lambda x: x.hour)
    frame["net_pnl_8bps"] = frame.gross_pnl - frame.transaction_charges - ((frame.entry+frame.exit)*frame.quantity*slippage_bps/10000.0)
    return frame.sort_values(["exit_ts", "entry_ts"], kind="stable").reset_index(drop=True)

def session_block_bootstrap(values: pd.Series, sessions: pd.Series, *, resamples: int = 10_000, seed: int = 42) -> tuple[float, float]:
    """Percentile CI for trade expectancy, resampling whole entry sessions."""
    if resamples < 1000:
        raise ValueError("at least 1,000 bootstrap resamples are required")
    grouped = [values[sessions.eq(key)].to_numpy(dtype=float) for key in sorted(sessions.unique())]
    grouped = [x for x in grouped if len(x)]
    if not grouped:
        raise ValueError("no sessions available for bootstrap")
    rng = np.random.default_rng(seed)
    draws = np.empty(resamples, dtype=float)
    count = len(grouped)
    for i in range(resamples):
        chosen = rng.integers(0, count, size=count)
        sample = np.concatenate([grouped[j] for j in chosen])
        draws[i] = float(sample.mean())
    low, high = np.quantile(draws, [0.025, 0.975])
    return float(low), float(high)

def _profit_concentration(frame: pd.DataFrame, key: str) -> tuple[float, str | None]:
    positive = frame.loc[frame.net_pnl_8bps > 0].groupby(key).net_pnl_8bps.sum()
    total = float(positive.sum())
    if total <= 0 or positive.empty:
        return 0.0, None
    shares = positive / total
    leader = str(shares.idxmax())
    return float(shares.max()), leader

def _max_losing_streak(values: pd.Series) -> int:
    best = current = 0
    for value in values:
        current = current + 1 if value < 0 else 0
        best = max(best, current)
    return best

def calculate_statistics(trades: pd.DataFrame, *, initial_capital: float = 20_000.0, slippage_bps: float = 8.0, bootstrap_resamples: int = 10_000, seed: int = 42) -> dict[str, Any]:
    if not math.isfinite(initial_capital) or initial_capital <= 0:
        raise ValueError("initial_capital must be finite and positive")
    frame = _prepare_trades(trades, slippage_bps=slippage_bps)
    if frame.empty:
        raise ValueError("No accepted, completed trades available for statistical analysis")
    net = frame.net_pnl_8bps
    wins = net[net > 0]
    losses = net[net < 0]
    gross_wins = float(wins.sum())
    gross_losses = abs(float(losses.sum()))
    pf = gross_wins/gross_losses if gross_losses else (math.inf if gross_wins else None)
    ci_low, ci_high = session_block_bootstrap(net, frame.session, resamples=bootstrap_resamples, seed=seed)
    equity = initial_capital + net.cumsum()
    peaks = equity.cummax().clip(lower=initial_capital)
    drawdown = peaks-equity
    max_dd = float(drawdown.max()) if len(drawdown) else 0.0
    max_dd_pct = float((drawdown/peaks.replace(0, np.nan)).max()*100) if len(drawdown) else 0.0
    symbol_conc, symbol_leader = _profit_concentration(frame, "symbol")
    day_conc, day_leader = _profit_concentration(frame, "session")
    hours = frame.groupby("entry_hour").net_pnl_8bps.agg(["count", "mean", "sum"]).reset_index()
    hour_buckets = [{"hour_ist": int(r.entry_hour), "trades": int(r["count"]), "expectancy_inr": float(r["mean"]), "net_pnl_inr": float(r["sum"])} for _, r in hours.iterrows()]
    mae_available = "mae_r" in frame and pd.to_numeric(frame["mae_r"], errors="coerce").notna().any()
    mfe_available = "mfe_r" in frame and pd.to_numeric(frame["mfe_r"], errors="coerce").notna().any()
    mae = pd.to_numeric(frame["mae_r"], errors="coerce") if mae_available else pd.Series(dtype=float)
    mfe = pd.to_numeric(frame["mfe_r"], errors="coerce") if mfe_available else pd.Series(dtype=float)
    return {
        "analysis_status": "COMPLETED",
        "sample": {"trades": int(len(frame)), "sessions": int(frame.session.nunique()), "first_session": str(frame.session.min()), "last_session": str(frame.session.max())},
        "slippage_bps_per_side": float(slippage_bps),
        "net_pnl_8bps_inr": float(net.sum()),
        "expectancy_inr_per_trade": float(net.mean()),
        "expectancy_ci_95_inr": {"lower": ci_low, "upper": ci_high, "method": "session-block percentile bootstrap", "resamples": int(bootstrap_resamples), "seed": int(seed)},
        "win_rate_pct": float((net > 0).mean()*100),
        "profit_factor": None if pf is None or not math.isfinite(pf) else float(pf),
        "profit_factor_status": "infinite_no_losses" if pf is not None and math.isinf(pf) else "not_available" if pf is None else "finite",
        "max_realized_drawdown_inr": max_dd,
        "max_realized_drawdown_pct": max_dd_pct,
        "max_losing_streak": _max_losing_streak(net),
        "mae_r_mean": float(mae.mean()) if mae_available else None,
        "mfe_r_mean": float(mfe.mean()) if mfe_available else None,
        "mae_mfe_status": "available" if mae_available and mfe_available else "not_available_in_source" if not mae_available and not mfe_available else "partially_available",
        "profit_concentration": {"max_symbol_share": symbol_conc, "top_symbol": symbol_leader, "max_session_share": day_conc, "top_session": day_leader},
        "hour_buckets": hour_buckets,
        "limitations": [
            "Research statistics only; not evidence of live execution quality or future returns.",
            "Drawdown is realized trade-close equity drawdown and excludes intratrade mark-to-market.",
            "Session bootstrap preserves within-session trade clusters but does not model serial dependence across adjacent sessions.",
            "8 bps per-side slippage is repriced on fixed accepted trades; position sizes and acceptance are not re-optimized at that cost level.",
            "Profit concentration uses positive net P&L contribution and reports symbol and entry-session leaders separately.",
        ],
    }

def evaluate_promotion(stats: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    thresholds = config.get("thresholds", {})
    required_trades = int(thresholds.get("minimum_trades", 100))
    required_sessions = int(thresholds.get("minimum_sessions", 20))
    minimum_pf = float(thresholds.get("minimum_profit_factor", 1.0))
    max_concentration = float(thresholds.get("maximum_symbol_or_session_profit_share", 0.30))
    checks = {
        "minimum_trades": stats["sample"]["trades"] >= required_trades,
        "minimum_sessions": stats["sample"]["sessions"] >= required_sessions,
        "positive_95pct_expectancy_lower_bound_at_8bps": stats["expectancy_ci_95_inr"]["lower"] > 0,
        "minimum_profit_factor_at_8bps": stats["profit_factor_status"] == "infinite_no_losses" or (stats["profit_factor"] is not None and stats["profit_factor"] >= minimum_pf),
        "maximum_symbol_or_session_profit_concentration": max(stats["profit_concentration"]["max_symbol_share"], stats["profit_concentration"]["max_session_share"]) <= max_concentration,
    }
    failures = [key for key, passed in checks.items() if not passed]
    return {"decision": "PROMOTE" if not failures else "INCONCLUSIVE", "promotion_scope": "next research-validation stage only; never live trading authorization", "checks": checks, "failed_checks": failures, "thresholds": thresholds}

def load_promotion_config(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"Cannot load promotion config {path}: {exc}") from exc
    if not isinstance(value, dict) or not isinstance(value.get("thresholds"), dict):
        raise ValueError("promotion config must contain a thresholds mapping")
    return value

def render_markdown(stats: dict[str, Any], promotion: dict[str, Any]) -> str:
    s=stats; sample=s["sample"]; ci=s["expectancy_ci_95_inr"]; c=s["profit_concentration"]
    pf=s["profit_factor"] if s["profit_factor"] is not None else "N/A (no losses or no profit)"
    lines=[
        "# TradingGPT performance validation",
        "",
        f"**Decision: {promotion['decision']}** — {promotion['promotion_scope']}.",
        "",
        "## Sample and net performance (8 bps per side)",
        "",
        f"- Completed accepted trades: {sample['trades']}",
        f"- Trading sessions: {sample['sessions']} ({sample['first_session']} to {sample['last_session']})",
        f"- Net P&L: ₹{s['net_pnl_8bps_inr']:.2f}",
        f"- Expectancy: ₹{s['expectancy_inr_per_trade']:.2f} per trade",
        f"- 95% session-block bootstrap CI: ₹{ci['lower']:.2f} to ₹{ci['upper']:.2f}",
        f"- Win rate: {s['win_rate_pct']:.1f}%",
        f"- Profit factor: {pf if isinstance(pf,str) else f'{pf:.2f}'}",
        f"- Maximum realized drawdown: ₹{s['max_realized_drawdown_inr']:.2f} ({s['max_realized_drawdown_pct']:.2f}%)",
        f"- Maximum losing streak: {s['max_losing_streak']}",
        f"- Mean MAE / MFE: {s['mae_r_mean'] if s['mae_r_mean'] is not None else 'not available'} R / {s['mfe_r_mean'] if s['mfe_r_mean'] is not None else 'not available'} R",
        "",
        "## Concentration",
        "",
        f"- Largest symbol share of positive net P&L: {c['max_symbol_share']:.1%} ({c['top_symbol'] or 'none'})",
        f"- Largest session share of positive net P&L: {c['max_session_share']:.1%} ({c['top_session'] or 'none'})",
        "",
        "## Promotion checks",
        "",
    ]
    lines += [f"- [{'x' if passed else ' '}] {name}" for name,passed in promotion["checks"].items()]
    lines += ["", "## Hour-of-entry buckets", "", "| IST hour | Trades | Expectancy | Net P&L |", "|---:|---:|---:|---:|"]
    lines += [f"| {x['hour_ist']:02d}:00 | {x['trades']} | ₹{x['expectancy_inr']:.2f} | ₹{x['net_pnl_inr']:.2f} |" for x in s["hour_buckets"]]
    lines += ["", "## Limitations", ""] + [f"- {x}" for x in s["limitations"]]
    lines += ["", "No actual fills or future returns are implied. This report is a historical research diagnostic.", ""]
    return "\n".join(lines)

def analyze_file(input_path: Path, *, output_stem: Path, promotion_config: Path, results_doc: Path, initial_capital: float=20_000.0, slippage_bps: float=8.0, bootstrap_resamples: int=10_000, seed: int=42) -> tuple[dict[str,Any],dict[str,Any]]:
    trades=load_trades(input_path)
    stats=calculate_statistics(trades,initial_capital=initial_capital,slippage_bps=slippage_bps,bootstrap_resamples=bootstrap_resamples,seed=seed)
    promotion=evaluate_promotion(stats,load_promotion_config(promotion_config))
    output_stem.parent.mkdir(parents=True,exist_ok=True)
    payload={"statistics":stats,"promotion":promotion,"source_file":input_path.name}
    output_stem.with_suffix(".json").write_text(json.dumps(payload,indent=2,allow_nan=False),encoding="utf-8")
    markdown=render_markdown(stats,promotion)
    output_stem.with_suffix(".md").write_text(markdown,encoding="utf-8")
    results_doc.parent.mkdir(parents=True,exist_ok=True)
    results_doc.write_text(markdown,encoding="utf-8")
    print(f"Performance JSON: {output_stem.with_suffix('.json')}")
    print(f"Performance Markdown: {output_stem.with_suffix('.md')}")
    print(f"Sanitized results document: {results_doc}")
    print(f"Promotion decision: {promotion['decision']}")
    return stats,promotion

def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trades",type=Path,required=True,help="One shared-capital portfolio *_trade_details.csv file.")
    parser.add_argument("--output",type=Path,default=Path("reports/performance/validation"))
    parser.add_argument("--results-doc",type=Path,default=Path("docs/RESULTS.md"))
    parser.add_argument("--promotion-config",type=Path,default=Path("config/promotion.yaml"))
    parser.add_argument("--initial-capital",type=float,default=20_000.0)
    parser.add_argument("--slippage-bps",type=float,default=8.0)
    parser.add_argument("--bootstrap-resamples",type=int,default=10_000)
    parser.add_argument("--seed",type=int,default=42)
    args=parser.parse_args()
    try:
        analyze_file(args.trades,output_stem=args.output,promotion_config=args.promotion_config,results_doc=args.results_doc,initial_capital=args.initial_capital,slippage_bps=args.slippage_bps,bootstrap_resamples=args.bootstrap_resamples,seed=args.seed)
    except (ValueError,OSError) as exc:
        parser.error(str(exc))
if __name__=="__main__": main()
