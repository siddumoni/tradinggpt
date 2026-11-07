"""Create distinct daily reports for champion, challenger, and watch-shadow paper books."""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from src.paper_broker import DEFAULT_DB, PAPER_BASELINE_VERSION, PaperBroker
from src.watch_shadow_portfolio import WatchShadowPortfolio
from src.candidate_outcome_tracker import CandidateOutcomeTracker

IST = ZoneInfo("Asia/Kolkata")


def generate_daily_report(
    db_path: Path = DEFAULT_DB,
    output_dir: Path = Path("reports"),
    now: datetime | None = None,
) -> dict:
    """Write the daily Markdown/JSON/CSV report without mixing the three books."""
    current = now or datetime.now(IST)
    current = current.replace(tzinfo=IST) if current.tzinfo is None else current.astimezone(IST)
    day = current.date().isoformat()
    db_path = Path(db_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    broker = PaperBroker(db_path)
    state = broker.snapshot()
    champion_account = state["account"]
    watch_book = WatchShadowPortfolio(
        db_path,
        initial_capital=float(champion_account["starting_capital"]),
        risk_per_trade=float(champion_account.get("risk_per_trade", 500.0)),
        max_daily_loss=float(champion_account.get("max_daily_loss", 2000.0)),
        max_open_positions=int(champion_account.get("max_open_positions", 3)),
    ).snapshot()
    candidate_outcomes = CandidateOutcomeTracker(db_path).snapshot(day)

    with sqlite3.connect(db_path) as db:
        db.row_factory = sqlite3.Row
        trades = [dict(r) for r in db.execute(
            "SELECT * FROM trades WHERE substr(entry_time,1,10)=? OR substr(exit_time,1,10)=? "
            "ORDER BY COALESCE(exit_time,entry_time)", (day, day)
        )]
        shadows = [dict(r) for r in db.execute(
            "SELECT * FROM shadow_signals WHERE substr(signal_time,1,10)=? OR substr(exit_time,1,10)=? "
            "ORDER BY COALESCE(exit_time,signal_time)", (day, day)
        )]
        watch_trades = [dict(r) for r in db.execute(
            "SELECT * FROM watch_shadow_trades WHERE substr(signal_time,1,10)=? OR substr(exit_time,1,10)=? "
            "ORDER BY COALESCE(exit_time,signal_time,created_at)", (day, day)
        )]
        events = [dict(r) for r in db.execute(
            "SELECT event_time,event_type,severity,symbol,message FROM events "
            "WHERE substr(event_time,1,10)=? ORDER BY id", (day,)
        )]

    champion_closed = [
        row for row in trades
        if row["status"] == "CLOSED"
        and row.get("strategy_version") == PAPER_BASELINE_VERSION
        and str(row.get("exit_time") or "").startswith(day)
    ]
    champion_archive_closed = [
        row for row in trades
        if row["status"] == "CLOSED"
        and row.get("strategy_version") != PAPER_BASELINE_VERSION
        and str(row.get("exit_time") or "").startswith(day)
    ]
    challenger_closed = [
        row for row in shadows
        if row["status"] == "CLOSED" and str(row.get("exit_time") or "").startswith(day)
    ]
    watch_closed = [
        row for row in watch_trades
        if row["status"] == "CLOSED" and str(row.get("exit_time") or "").startswith(day)
    ]
    watch_excluded = [row for row in watch_trades if row["status"] == "EXCLUDED"]
    watch_open = [row for row in watch_trades if row["status"] == "OPEN"]

    def pnl(items: list[dict]) -> float:
        return round(sum(float(row.get("net_pnl") or 0) for row in items), 2)

    challenger_strategies = []
    for version in sorted({row.get("strategy_version") for row in shadows if row.get("strategy_version")}):
        items = [row for row in challenger_closed if row.get("strategy_version") == version]
        challenger_strategies.append({
            "strategy_version": version,
            "strategy_name": next(
                (row.get("strategy_name") for row in shadows if row.get("strategy_version") == version),
                version,
            ),
            "closed_trades": len(items),
            "net_pnl": pnl(items),
        })

    champion = {
        "strategy_version": PAPER_BASELINE_VERSION,
        "starting_capital": champion_account["starting_capital"],
        "closed_trades_today": len(champion_closed),
        "net_pnl_today": pnl(champion_closed),
        "all_time_realized_net_pnl": champion_account["realized_pnl"],
        "open_positions": champion_account["open_positions"],
        "closed_trades": champion_closed,
    }
    champion_archive = {
        "closed_trades_today": len(champion_archive_closed),
        "net_pnl_today": pnl(champion_archive_closed),
        "closed_trades": champion_archive_closed,
        "research_only": True,
        "performance_must_not_be_pooled_with_nse_champion_v2": True,
    }
    challengers = {
        "closed_trades_today": len(challenger_closed),
        "net_pnl_today": pnl(challenger_closed),
        "strategy_summary": challenger_strategies,
        "closed_trades": challenger_closed,
        "signals_today": shadows,
    }
    watch_shadow = {
        "account": watch_book["account"],
        "closed_trades_today": len(watch_closed),
        "net_pnl_today": pnl(watch_closed),
        "excluded_candidates_today": len(watch_excluded),
        "candidate_observations_today": len(watch_trades),
        "open_positions_now": len(watch_book.get("open_positions", [])),
        "open_positions": watch_book.get("open_positions", []),
        "closed_trades": watch_closed,
        "observations_today": watch_trades,
        "research_only": True,
        "orders_sent_to_broker": False,
    }

    risk_overruns = []
    for track, rows in (
        ("CHAMPION", champion_closed),
        ("CHAMPION_ARCHIVE", champion_archive_closed),
        ("CHALLENGER", challenger_closed),
        ("WATCH_SHADOW", watch_closed),
        ("CANDIDATE_OUTCOME", [row for row in candidate_outcomes["observations"] if row.get("status") == "CLOSED"]),
    ):
        for row in rows:
            budget = row.get("risk_budget_inr")
            net = row.get("net_pnl")
            if budget is not None and net is not None and float(net) < -abs(float(budget)):
                risk_overruns.append({
                    "track": track,
                    "symbol": row.get("symbol"),
                    "side": row.get("side"),
                    "exit_time": row.get("exit_time"),
                    "net_pnl": round(float(net), 2),
                    "planned_risk_inr": round(abs(float(budget)), 2),
                    "excess_loss_inr": round(abs(float(net)) - abs(float(budget)), 2),
                    "exit_reason": row.get("exit_reason"),
                })

    prefix = f"paper_daily_{day}"
    json_path = output_dir / f"{prefix}.json"
    markdown_path = output_dir / f"{prefix}.md"
    csv_path = output_dir / f"{prefix}_trades.csv"

    payload = {
        "report_date": day,
        "generated_at": current.isoformat(),
        "research_only": True,
        "orders_sent_to_broker": False,
        "summary": {
            "champion": {
                "closed_trades": champion["closed_trades_today"],
                "net_pnl": champion["net_pnl_today"],
                "open_positions": champion["open_positions"],
            },
            "champion_archive": {
                "closed_trades": champion_archive["closed_trades_today"],
                "net_pnl": champion_archive["net_pnl_today"],
            },
            "challengers": {
                "closed_trades": challengers["closed_trades_today"],
                "net_pnl": challengers["net_pnl_today"],
            },
            "watch_shadow": {
                "closed_trades": watch_shadow["closed_trades_today"],
                "net_pnl": watch_shadow["net_pnl_today"],
                "open_positions": watch_shadow["open_positions_now"],
                "excluded_candidates": watch_shadow["excluded_candidates_today"],
                "candidate_observations": watch_shadow["candidate_observations_today"],
            },
            "candidate_outcomes": {
                "eligible_observations": candidate_outcomes["eligible_observations"],
                "closed_outcomes": candidate_outcomes["closed_outcomes"],
                "wins": candidate_outcomes["wins"],
                "losses": candidate_outcomes["losses"],
                "incomplete_data_gaps": candidate_outcomes["incomplete_data_gaps"],
                "pending_entries": candidate_outcomes["pending_entries"],
                "excluded": candidate_outcomes["excluded"],
                "invalidated_legacy_method": candidate_outcomes["invalidated_legacy_method"],
            },
            "risk_overrun_count": len(risk_overruns),
        },
        "champion": champion,
        "champion_archive": champion_archive,
        "challengers": challengers,
        "watch_shadow": watch_shadow,
        "candidate_outcomes": candidate_outcomes,
        "risk_overruns": risk_overruns,
        "events": events,
        "warning": (
            "Fills, transaction costs and P&L are hypothetical estimates. nse_champion_v2, archived champion versions, "
            "challengers, watch-shadow and candidate-outcome results are separate; candidate outcomes overlap and are not portfolio P&L."
        ),
    }
    json_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str, allow_nan=False),
        encoding="utf-8",
    )

    columns = [
        "track", "strategy", "strategy_version", "symbol", "side", "status", "signal_time",
        "scan_as_of", "signal_available_at", "method_version", "entry_time", "entry_reference_price",
        "entry_fill_price", "quantity", "exit_time", "exit_fill_price",
        "exit_reason", "gross_pnl", "transaction_charges", "slippage_cost",
        "total_estimated_costs", "net_pnl", "risk_budget_inr", "reason",
        "source_rejection_reason", "exclusion_reason", "assessed_net_rr_t1",
        "estimated_net_risk_inr", "estimated_net_reward_t1_inr",
    ]
    rows = []
    for row in trades:
        rows.append({
            "track": (
                "CHAMPION"
                if row.get("strategy_version") == PAPER_BASELINE_VERSION
                else "CHAMPION_ARCHIVE"
            ), "strategy": row.get("strategy"),
            "strategy_version": row.get("strategy_version"), "symbol": row.get("symbol"),
            "side": row.get("side"), "status": row.get("status"), "signal_time": row.get("signal_time"),
            "entry_time": row.get("entry_time"), "entry_fill_price": row.get("entry_fill_price"),
            "quantity": row.get("quantity"), "exit_time": row.get("exit_time"),
            "exit_fill_price": row.get("exit_fill_price"), "exit_reason": row.get("exit_reason"),
            "gross_pnl": row.get("gross_pnl"), "transaction_charges": row.get("transaction_charges"),
            "slippage_cost": row.get("slippage_cost"), "total_estimated_costs": row.get("total_estimated_costs"),
            "net_pnl": row.get("net_pnl"), "risk_budget_inr": row.get("risk_budget_inr"),
            "reason": row.get("entry_reason"),
        })
    for row in shadows:
        rows.append({
            "track": "CHALLENGER", "strategy": row.get("strategy_name"),
            "strategy_version": row.get("strategy_version"), "symbol": row.get("symbol"),
            "side": row.get("side"), "status": row.get("status"), "signal_time": row.get("signal_time"),
            "entry_time": row.get("entry_time"), "entry_fill_price": row.get("entry_fill_price"),
            "quantity": row.get("quantity"), "exit_time": row.get("exit_time"),
            "exit_fill_price": row.get("exit_fill_price"), "exit_reason": row.get("exit_reason"),
            "gross_pnl": row.get("gross_pnl"), "transaction_charges": row.get("transaction_charges"),
            "slippage_cost": row.get("slippage_cost"), "total_estimated_costs": row.get("total_estimated_costs"),
            "net_pnl": row.get("net_pnl"), "reason": row.get("reason"),
        })
    for row in watch_trades:
        rows.append({
            "track": "WATCH_SHADOW", "strategy": row.get("strategy"),
            "strategy_version": "", "symbol": row.get("symbol"), "side": row.get("side"),
            "status": row.get("status"), "signal_time": row.get("signal_time"),
            "scan_as_of": row.get("scan_as_of"), "signal_available_at": row.get("signal_available_at"),
            "method_version": row.get("method_version"), "entry_time": row.get("entry_time"),
            "entry_reference_price": row.get("entry_reference_price"), "entry_fill_price": row.get("entry_fill_price"),
            "quantity": row.get("quantity"), "exit_time": row.get("exit_time"),
            "exit_fill_price": row.get("exit_fill_price"), "exit_reason": row.get("exit_reason"),
            "gross_pnl": row.get("gross_pnl"), "transaction_charges": row.get("transaction_charges"),
            "slippage_cost": row.get("slippage_cost"), "total_estimated_costs": row.get("total_estimated_costs"),
            "net_pnl": row.get("net_pnl"), "risk_budget_inr": row.get("risk_budget_inr"),
            "reason": row.get("source_rejection_reason"),
            "source_rejection_reason": row.get("source_rejection_reason"),
            "exclusion_reason": row.get("exclusion_reason"),
            "assessed_net_rr_t1": row.get("assessed_net_rr_t1"),
            "estimated_net_risk_inr": row.get("estimated_net_risk_inr"),
            "estimated_net_reward_t1_inr": row.get("estimated_net_reward_t1_inr"),
        })
    for row in candidate_outcomes["observations"]:
        rows.append({
            "track": "CANDIDATE_OUTCOME", "strategy": row.get("strategy"),
            "strategy_version": "", "symbol": row.get("symbol"), "side": row.get("side"),
            "status": row.get("status"), "signal_time": row.get("signal_time"),
            "scan_as_of": row.get("scan_as_of"), "signal_available_at": row.get("signal_available_at"),
            "method_version": row.get("method_version"), "entry_time": row.get("entry_time"),
            "entry_reference_price": row.get("entry_reference_price"), "entry_fill_price": row.get("entry_fill_price"),
            "quantity": row.get("quantity"), "exit_time": row.get("exit_time"),
            "exit_fill_price": row.get("exit_fill_price"), "exit_reason": row.get("exit_reason"),
            "gross_pnl": row.get("gross_pnl"), "transaction_charges": row.get("transaction_charges"),
            "slippage_cost": row.get("slippage_cost"), "total_estimated_costs": row.get("total_estimated_costs"),
            "net_pnl": row.get("net_pnl"), "risk_budget_inr": row.get("risk_budget_inr"),
            "reason": row.get("source_rejection_reason"),
            "source_rejection_reason": row.get("source_rejection_reason"),
            "exclusion_reason": row.get("exclusion_reason"),
            "assessed_net_rr_t1": row.get("assessed_net_rr_t1"),
            "estimated_net_risk_inr": row.get("estimated_net_risk_inr"),
            "estimated_net_reward_t1_inr": row.get("estimated_net_reward_t1_inr"),
        })
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)

    challenger_lines = "\n".join(
        f"| {row.get('strategy_name') or row.get('strategy_version')} | {row.get('symbol')} | "
        f"{row.get('side')} | {row.get('status')} | {row.get('net_pnl') if row.get('net_pnl') is not None else '—'} |"
        for row in shadows
    ) or "| No challenger observations | — | — | — | — |"
    watch_lines = "\n".join(
        f"| {row.get('symbol')} | {row.get('side')} | {row.get('status')} | "
        f"{row.get('net_pnl') if row.get('net_pnl') is not None else '—'} | "
        f"{row.get('exclusion_reason') or row.get('exit_reason') or row.get('source_rejection_reason') or '—'} |"
        for row in watch_trades
    ) or "| No watch-candidate observations | — | — | — | — |"
    outcome_rows = [row for row in candidate_outcomes["observations"]
                    if row.get("status") not in {"EXCLUDED", "INVALIDATED_LEGACY_METHOD"}]
    outcome_lines = "\n".join(
        f"| {row.get('symbol')} | {row.get('strategy')} | {row.get('side')} | {row.get('status')} | "
        f"{row.get('net_pnl') if row.get('net_pnl') is not None else '—'} |"
        for row in outcome_rows[-40:]
    ) or "| No eligible candidate outcomes yet | — | — | — | — |"
    risk_lines = "\n".join(
        f"- **{item['track']} {item['symbol']}**: net loss INR {abs(item['net_pnl']):,.2f} "
        f"exceeded planned risk INR {item['planned_risk_inr']:,.2f} by INR {item['excess_loss_inr']:,.2f} "
        f"({item['exit_reason']})."
        for item in risk_overruns
    ) or "- No closed trade exceeded its recorded planned per-trade risk today."

    md = f"""# TradingGPT paper-trading daily report — {day}

Generated: {current.isoformat()}

> **RESEARCH ONLY.** No real Kite orders were submitted. Fills, charges and slippage are hypothetical estimates. Each research book is separate; candidate outcomes are overlapping single-candidate experiments, not a portfolio.

## Champion account
- Virtual starting capital: INR {champion['starting_capital']:,.2f}
- Champion trades closed today: {champion['closed_trades_today']}
- Champion net P&L today: INR {champion['net_pnl_today']:,.2f}
- All-time champion realised net P&L: INR {champion['all_time_realized_net_pnl']:,.2f}
- Current open champion positions: {champion['open_positions']}

## Challenger strategy lab
- Challenger trades closed today: {challengers['closed_trades_today']}
- Challenger net P&L today: INR {challengers['net_pnl_today']:,.2f}

| Strategy | Symbol | Side | Status | Net P&L |
|---|---|---|---|---:|
{challenger_lines}

## Watch-candidate shadow book
- Virtual starting capital: INR {watch_shadow['account']['starting_capital']:,.2f}
- Watch-shadow trades closed today: {watch_shadow['closed_trades_today']}
- Watch-shadow net P&L today: INR {watch_shadow['net_pnl_today']:,.2f}
- All-time watch-shadow realised net P&L: INR {watch_shadow['account']['realized_pnl']:,.2f}
- Current open watch positions: {watch_shadow['open_positions_now']}
- Watch candidates excluded today: {watch_shadow['excluded_candidates_today']}
- Watch-candidate observations today: {watch_shadow['candidate_observations_today']}

| Symbol | Side | Status | Net P&L | Reason / exit |
|---|---|---|---:|---|
{watch_lines}

## Independent candidate outcome tracker
- Eligible near-miss observations: {candidate_outcomes['eligible_observations']}
- Closed hypothetical outcomes: {candidate_outcomes['closed_outcomes']}
- Wins / losses: {candidate_outcomes['wins']} / {candidate_outcomes['losses']}
- Incomplete candle outcomes: {candidate_outcomes['incomplete_data_gaps']}
- Pending entries / open outcomes: {candidate_outcomes['pending_entries']} / {candidate_outcomes['open_outcomes']}
- Excluded observations: {candidate_outcomes['excluded']}
- Invalidated legacy observations: {candidate_outcomes['invalidated_legacy_method']}

Each eligible candidate now enters at the first five-minute candle open strictly after both the signal candle has completed and the scanner publication/quote-validation timestamp. Older results from the prior signal-candle-only timing method are retained for audit but excluded from performance statistics. Estimated costs/slippage are applied once. There is no three-position or shared-cash limit in this ledger. Signals can overlap in time and symbol; **do not sum their P&L as a portfolio result**.

| Symbol | Strategy | Side | Outcome | Net P&L |
|---|---|---|---|---:|
{outcome_lines}

## Planned-risk overruns
{risk_lines}

## Output files
- Markdown report: {markdown_path}
- JSON report: {json_path}
- Combined Champion/Challenger/Watch-Shadow/Candidate-Outcome research extract: {csv_path}

Positive paper results are not a guarantee of future profitability. Watch-shadow and challenger results do not alter champion performance.
"""
    markdown_path.write_text(md, encoding="utf-8")

    return {
        "report_date": day,
        "json_path": str(json_path),
        "markdown_path": str(markdown_path),
        "csv_path": str(csv_path),
        "champion_closed_trades": len(champion_closed),
        "champion_net_pnl": pnl(champion_closed),
        "shadow_closed_trades": len(challenger_closed),
        "shadow_net_pnl": pnl(challenger_closed),
        "watch_shadow_closed_trades": len(watch_closed),
        "watch_shadow_net_pnl": pnl(watch_closed),
        "watch_shadow_open_positions": len(watch_book.get("open_positions", [])),
        "watch_shadow_excluded_candidates": len(watch_excluded),
        "watch_shadow_candidate_count": len(watch_trades),
        "candidate_outcomes_eligible_today": candidate_outcomes["eligible_observations"],
        "candidate_outcomes_closed_today": candidate_outcomes["closed_outcomes"],
        "candidate_outcomes_incomplete_today": candidate_outcomes["incomplete_data_gaps"],
        "candidate_outcomes_invalidated_legacy_today": candidate_outcomes["invalidated_legacy_method"],
        "risk_overrun_count": len(risk_overruns),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Generate separate research-only daily paper-trading reports.")
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output-dir", type=Path, default=Path("reports"))
    args = parser.parse_args(argv)
    print(json.dumps(generate_daily_report(args.db_path, args.output_dir), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
