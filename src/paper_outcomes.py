"""Evaluate saved paper-signal observations against subsequent 5-minute OHLCV bars.

This is an offline research evaluator, not order execution. It assumes entry at the
next candle open, rejects gap-invalidated levels, applies stop-first handling when
stop and target are both touched, and refuses to score outcomes across data gaps.
Completed candidate trades can be sent through src.portfolio_replay for shared-capital
allocation; they are not actual or guaranteed fills.
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import replace
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from src.cost_calculator import ChargeRates, estimate_intraday_round_trip_cost
from src.risk_engine import RiskSettings, assess_trade, load_phase2_settings

IST = ZoneInfo("Asia/Kolkata")
BAR_MINUTES = 5
SESSION_START = dtime(9, 15)
LAST_BAR_START = dtime(15, 10)
OHLCV = ("open", "high", "low", "close", "volume")
REQUIRED_JOURNAL_COLUMNS = (
    "signal_id", "symbol", "side", "strategy", "signal_time", "reference_entry",
    "stop_loss", "target_1", "target_2", "data_verified_live", "paper_status",
)


def to_ist_timestamp(value: Any, *, label: str) -> pd.Timestamp:
    try:
        timestamp = pd.Timestamp(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"Invalid {label} timestamp {value!r}") from exc
    if pd.isna(timestamp):
        raise ValueError(f"Missing {label} timestamp")
    return timestamp.tz_localize(IST) if timestamp.tzinfo is None else timestamp.tz_convert(IST)


def parse_market_time(value: str, *, label: str) -> dtime:
    try:
        parsed = dtime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must use HH:MM or HH:MM:SS") from exc
    return parsed.replace(tzinfo=None)


def strict_csv_bool(value: Any) -> bool:
    """Only an explicit true value is trusted; missing/unknown booleans fail closed."""
    return str(value).strip().casefold() == "true"


def load_journal(path: Path) -> pd.DataFrame:
    """Read a paper-signal journal and validate the minimum required schema."""
    try:
        frame = pd.read_csv(path, dtype={"signal_id": str, "symbol": str, "side": str})
    except (OSError, pd.errors.ParserError) as exc:
        raise ValueError(f"Could not read paper journal {path}: {exc}") from exc
    missing = set(REQUIRED_JOURNAL_COLUMNS).difference(frame.columns)
    if missing:
        raise ValueError(f"Paper journal is missing required columns: {sorted(missing)}")
    if frame["signal_id"].isna().any() or frame["symbol"].isna().any():
        raise ValueError("Paper journal signal_id and symbol cannot be blank")
    return frame


def load_candles(path: Path) -> pd.DataFrame:
    """Read and strictly validate a single symbol's candle-start OHLCV CSV."""
    try:
        raw = pd.read_csv(path)
    except (OSError, pd.errors.ParserError) as exc:
        raise ValueError(f"Could not read candle file {path}: {exc}") from exc
    if raw.empty:
        raise ValueError(f"Candle file is empty: {path}")
    raw.columns = [str(col).strip().lower() for col in raw.columns]
    timestamp_col = next((col for col in ("date", "timestamp", "datetime", "time") if col in raw.columns), None)
    if timestamp_col is None:
        raise ValueError(f"Candle file {path} must include a date/timestamp/datetime/time column")
    missing = set(OHLCV).difference(raw.columns)
    if missing:
        raise ValueError(f"Candle file {path} missing OHLCV columns: {sorted(missing)}")

    timestamps = [to_ist_timestamp(value, label=f"{path} candle") for value in raw[timestamp_col]]
    frame = raw.loc[:, list(OHLCV)].copy()
    if "provider_mode" in raw.columns:
        frame["provider_mode"] = raw["provider_mode"].astype(str).to_numpy()
    frame.index = pd.DatetimeIndex(timestamps, name="timestamp")
    if frame.index.has_duplicates:
        duplicates = frame.index[frame.index.duplicated(keep=False)].astype(str).tolist()
        raise ValueError(f"Candle file {path} contains duplicate timestamps: {duplicates[:5]}")
    frame = frame.sort_index()
    for column in OHLCV:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    values = frame.loc[:, list(OHLCV)]
    if values.isna().any().any() or not all(math.isfinite(float(v)) for v in values.to_numpy().flatten()):
        raise ValueError(f"Candle file {path} contains missing or non-finite OHLCV values")
    prices = frame[["open", "high", "low", "close"]]
    if (prices <= 0).any().any():
        raise ValueError(f"Candle file {path} contains non-positive prices")
    valid = (
        (frame["high"] >= frame[["open", "close", "low"]].max(axis=1))
        & (frame["low"] <= frame[["open", "close", "high"]].min(axis=1))
    )
    if not valid.all():
        raise ValueError(f"Candle file {path} contains inconsistent OHLC high/low values")
    if (frame["volume"] < 0).any():
        raise ValueError(f"Candle file {path} contains negative volume")

    # Expect five-minute candle-start timestamps on a regular five-minute grid.
    aligned = [
        (stamp.hour * 60 + stamp.minute - (9 * 60 + 15)) % BAR_MINUTES == 0
        and stamp.second == 0 and stamp.microsecond == 0
        for stamp in frame.index
    ]
    if any(not is_aligned for is_aligned in aligned):
        raise ValueError(f"Candle file {path} contains timestamps not aligned to 5-minute candle starts")
    in_session = [
        SESSION_START <= stamp.time().replace(tzinfo=None) <= LAST_BAR_START
        for stamp in frame.index
    ]
    frame = frame.loc[in_session]
    if frame.empty:
        raise ValueError(f"Candle file {path} has no regular-session 5-minute bars")
    return frame


def resolve_candle_file(symbol: str, candles_dir: Path) -> Path | None:
    """Resolve a unique candle file; ambiguous caches fail instead of guessing."""
    exact = candles_dir / f"{symbol.upper()}.csv"
    if exact.is_file():
        return exact
    matches = sorted(
        path for path in candles_dir.glob(f"{symbol.upper()}_*.csv")
        if path.is_file()
    )
    if len(matches) > 1:
        raise ValueError(
            f"Multiple candle files found for {symbol}: {[str(p) for p in matches]}. "
            f"Copy the chosen data source to {exact.name} or use a directory with one cache per symbol."
        )
    return matches[0] if matches else None


def _float_value(row: pd.Series, name: str) -> float:
    try:
        value = float(row[name])
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"signal {row.get('signal_id', '?')}: {name} must be numeric") from exc
    if not math.isfinite(value):
        raise ValueError(f"signal {row.get('signal_id', '?')}: {name} must be finite")
    return value


def _base_result(row: pd.Series, *, candle_path: Path | None) -> dict[str, Any]:
    result = row.to_dict()
    result.update({
        "candle_file": str(candle_path) if candle_path else "",
        "candle_provider_mode": "UNVERIFIED_EXTERNAL_FILE",
        "outcome_status": "NOT_EVALUATED",
        "outcome_note": "",
        "entry_time": "",
        "exit_time": "",
        "fill_entry": None,
        "fill_exit": None,
        "evaluated_quantity": 0,
        "gross_pnl": None,
        "transaction_charges": None,
        "slippage_cost": None,
        "total_estimated_costs": None,
        "net_pnl": None,
        "exit_reason": "",
        "risk_filter_reason": "",
        "source_live_data_verified": strict_csv_bool(row.get("data_verified_live")),
        "research_only": True,
    })
    return result


def evaluate_signal(
    row: pd.Series,
    candles: pd.DataFrame | None,
    *,
    candle_path: Path | None,
    settings: RiskSettings,
    charge_rates: ChargeRates,
    slippage_bps_per_side: float,
    entry_cutoff: dtime,
) -> dict[str, Any]:
    """Score one signal only when an unambiguous next-bar entry and exit are known."""
    result = _base_result(row, candle_path=candle_path)
    signal_id = str(row["signal_id"])
    symbol = str(row["symbol"]).strip().upper()
    side = str(row["side"]).strip().upper()
    result["symbol"] = symbol
    if candles is None:
        result.update(outcome_status="MISSING_CANDLE_FILE", outcome_note=f"No unique candle file resolved for {symbol}.")
        return result
    if side not in ("LONG", "SHORT"):
        result.update(outcome_status="INVALID_SIDE", outcome_note=f"Unsupported side: {side}")
        return result

    if "provider_mode" in candles.columns:
        modes = sorted({
            str(value).strip().lower()
            for value in candles["provider_mode"].dropna().unique()
            if str(value).strip()
        })
        if len(modes) > 1:
            result.update(
                outcome_status="CANDLE_PROVIDER_MIXED",
                outcome_note=f"Selected candle file mixes provider modes: {modes}; outcomes are not scored.",
                candle_provider_mode=",".join(modes),
            )
            return result
        mode = modes[0] if modes else "unknown"
        result["candle_provider_mode"] = mode
        journal_provider = str(row.get("provider", "")).strip().lower()
        expected_mode = (
            "kite" if "kite connect" in journal_provider
            else "yahoo" if "yahoo" in journal_provider
            else None
        )
        if expected_mode and mode != expected_mode:
            result.update(
                outcome_status="CANDLE_PROVIDER_MISMATCH",
                outcome_note=f"Signal source expects {expected_mode!r}, but candle archive is {mode!r}; outcomes are not scored.",
            )
            return result

    try:
        signal_ts = to_ist_timestamp(row["signal_time"], label=f"signal {signal_id}")
        reference_entry = _float_value(row, "reference_entry")
        stop = _float_value(row, "stop_loss")
        target_1 = _float_value(row, "target_1")
        target_2 = _float_value(row, "target_2")
    except ValueError as exc:
        result.update(outcome_status="INVALID_SIGNAL_ROW", outcome_note=str(exc))
        return result

    signal_idx = candles.index.get_indexer([signal_ts])[0]
    if signal_idx < 0:
        result.update(
            outcome_status="SIGNAL_CANDLE_NOT_FOUND",
            outcome_note="Exact signal-candle timestamp is absent from the selected OHLCV cache; check source, timezone and retention.",
        )
        return result
    observed_close = float(candles.iloc[signal_idx]["close"])
    tolerance = max(0.02, reference_entry * 5.0 / 10_000.0)
    if abs(observed_close - reference_entry) > tolerance:
        result.update(
            outcome_status="REFERENCE_CLOSE_MISMATCH",
            outcome_note=f"Signal reference close {reference_entry:.4f} differs from cache close {observed_close:.4f} by more than tolerance {tolerance:.4f}.",
        )
        return result

    entry_ts = signal_ts + pd.Timedelta(minutes=BAR_MINUTES)
    result["entry_time"] = entry_ts.isoformat()
    if entry_ts.date() != signal_ts.date():
        result.update(outcome_status="NO_SAME_SESSION_NEXT_BAR", outcome_note="No next-bar entry across a session boundary.")
        return result
    if entry_ts.time().replace(tzinfo=None) >= entry_cutoff:
        result.update(outcome_status="ENTRY_AFTER_CUTOFF", outcome_note=f"Next-bar entry is at or after {entry_cutoff.strftime('%H:%M')} IST.")
        return result
    entry_idx = candles.index.get_indexer([entry_ts])[0]
    if entry_idx < 0:
        latest = candles.index[-1]
        status = "PENDING_NEXT_BAR" if entry_ts > latest else "MISSING_NEXT_BAR"
        note = (
            "Next candle has not appeared in the available cache yet."
            if status == "PENDING_NEXT_BAR"
            else "The expected next candle is missing from the selected cache."
        )
        result.update(outcome_status=status, outcome_note=note)
        result["entry_time"] = ""
        return result

    entry_price = float(candles.iloc[entry_idx]["open"])
    try:
        if side == "LONG":
            valid_levels = stop < entry_price < target_1 <= target_2
        else:
            valid_levels = target_2 <= target_1 < entry_price < stop
        if not valid_levels:
            result.update(
                outcome_status="ENTRY_OPEN_INVALIDATED_LEVELS",
                outcome_note="Next-bar open gapped through a stop/target level; no hypothetical entry is assumed.",
                fill_entry=entry_price,
            )
            return result
        assessment = assess_trade(
            side=side,
            entry_price=entry_price,
            stop_loss=stop,
            target_1=target_1,
            target_2=target_2,
            settings=settings,
            estimated_margin_per_share=entry_price,  # Cash-only notional limit; no MIS leverage assumed.
            charge_rates=charge_rates,
        )
    except ValueError as exc:
        result.update(outcome_status="INVALID_PRICE_LEVELS", outcome_note=str(exc), fill_entry=entry_price)
        return result

    result.update(
        fill_entry=entry_price,
        evaluated_quantity=assessment.quantity,
        risk_filter_reason=assessment.reason,
        net_rr_t1=assessment.net_rr_t1,
        net_rr_t2=assessment.net_rr_t2,
    )
    if not assessment.accepted or assessment.quantity < 1:
        result.update(
            outcome_status="ENTRY_REJECTED_BY_RISK_FILTER",
            outcome_note="Next-bar price failed the configured cash-only risk/after-cost R:R checks; no hypothetical position opened.",
        )
        return result

    session_rows = candles.loc[candles.index.date == signal_ts.date()]
    last_seen: pd.Timestamp | None = None
    exit_price: float | None = None
    exit_ts: pd.Timestamp | None = None
    exit_reason = ""
    status = ""
    note = ""
    quantity = int(assessment.quantity)

    for stamp, bar in session_rows.loc[entry_ts:].iterrows():
        if last_seen is not None and stamp != last_seen + pd.Timedelta(minutes=BAR_MINUTES):
            status = "INCOMPLETE_DATA_GAP"
            note = f"Missing expected 5-minute candle after {last_seen.isoformat()}; outcome is not scored across the gap."
            break
        open_price = float(bar["open"])
        high = float(bar["high"])
        low = float(bar["low"])
        # Conservative OHLC assumption: when both stop and target occur in one bar,
        # assume the stop happened first. Stop gaps fill at the worse bar open.
        stop_hit = low <= stop if side == "LONG" else high >= stop
        target_hit = high >= target_2 if side == "LONG" else low <= target_2
        if stop_hit:
            exit_price = min(stop, open_price) if side == "LONG" else max(stop, open_price)
            exit_reason = "STOP_LOSS"
            exit_ts = stamp + pd.Timedelta(minutes=BAR_MINUTES)
            status = "COMPLETED_RESEARCH_ONLY"
            note = "Stop-first intrabar policy applied; exit timestamp is the bar-end boundary for conservative capital release."
            break
        if target_hit:
            # No favorable price improvement is credited on a gap through target.
            exit_price = target_2
            exit_reason = "TARGET_2"
            exit_ts = stamp + pd.Timedelta(minutes=BAR_MINUTES)
            status = "COMPLETED_RESEARCH_ONLY"
            note = "Full quantity exits at target 2; target 1 is only the entry viability gate."
            break
        if stamp.time().replace(tzinfo=None) == LAST_BAR_START:
            exit_price = float(bar["close"])
            exit_reason = "SESSION_SQUARE_OFF"
            exit_ts = stamp + pd.Timedelta(minutes=BAR_MINUTES)
            status = "COMPLETED_RESEARCH_ONLY"
            note = "Position closed at the final regular-session candle close."
            break
        last_seen = stamp

    if not status:
        last_session_stamp = session_rows.index[-1] if not session_rows.empty else None
        if last_session_stamp is None:
            status, note = "INCOMPLETE_SESSION_DATA", "No session bars were available after the hypothetical entry."
        elif last_session_stamp.time().replace(tzinfo=None) < LAST_BAR_START:
            if last_session_stamp == candles.index[-1] and signal_ts.date() == candles.index[-1].date():
                status, note = "PENDING_MORE_CANDLES", "The available cache ends before the session square-off; rerun after complete bars are available."
            else:
                status, note = "INCOMPLETE_SESSION_DATA", "The signal session ends in the cache before the final regular-session bar."
        else:
            status, note = "INCOMPLETE_SESSION_DATA", "No defensible exit was found in the available session bars."
    result["outcome_status"] = status
    result["outcome_note"] = note
    if status != "COMPLETED_RESEARCH_ONLY" or exit_price is None or exit_ts is None:
        return result

    gross = (exit_price - entry_price) * quantity if side == "LONG" else (entry_price - exit_price) * quantity
    costs = estimate_intraday_round_trip_cost(
        side=side, entry_price=entry_price, exit_price=exit_price,
        quantity=quantity, rates=charge_rates,
    )
    slippage_cost = (entry_price + exit_price) * quantity * slippage_bps_per_side / 10_000.0
    total_costs = costs.total + slippage_cost
    net = gross - total_costs
    result.update({
        "exit_time": exit_ts.isoformat(),
        "fill_exit": exit_price,
        "evaluated_quantity": quantity,
        "gross_pnl": gross,
        "brokerage": costs.brokerage,
        "stt": costs.stt,
        "exchange_charges": costs.exchange_charges,
        "sebi_charges": costs.sebi_charges,
        "gst": costs.gst,
        "stamp_duty": costs.stamp_duty,
        "ipft": costs.ipft,
        "transaction_charges": costs.total,
        "slippage_cost": slippage_cost,
        "total_estimated_costs": total_costs,
        "net_pnl": net,
        "exit_reason": exit_reason,
    })
    return result


def evaluate_journal(
    journal_path: Path,
    candles_dir: Path,
    *,
    output_path: Path,
    initial_capital: float | None = None,
    risk_per_trade: float | None = None,
    minimum_net_risk_reward: float | None = None,
    slippage_bps_per_side: float | None = None,
    entry_cutoff_ist: str | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """Evaluate a journal and write outcomes plus completed trades for allocation replay."""
    journal = load_journal(journal_path)
    if not candles_dir.is_dir():
        raise FileNotFoundError(f"Candles directory not found: {candles_dir}")
    configured, charge_rates = load_phase2_settings()
    capital = configured.capital_inr if initial_capital is None else float(initial_capital)
    risk_budget = configured.risk_per_trade_inr if risk_per_trade is None else float(risk_per_trade)
    min_rr = configured.minimum_net_risk_reward if minimum_net_risk_reward is None else float(minimum_net_risk_reward)
    slippage = configured.slippage_bps_per_side if slippage_bps_per_side is None else float(slippage_bps_per_side)
    cutoff = parse_market_time(entry_cutoff_ist or configured.entry_cutoff_ist, label="entry cutoff")
    for label, value in (("initial capital", capital), ("risk per trade", risk_budget), ("minimum net R:R", min_rr)):
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{label} must be finite and positive")
    if not math.isfinite(slippage) or not 0 <= slippage <= 100:
        raise ValueError("slippage must be finite and between 0 and 100 bps per side")

    # Hypothetical sizing uses the project's cash-only cap. The source margin flag
    # remains in output; this does not claim that live MIS margin was verified.
    paper_settings = replace(
        configured,
        capital_inr=capital,
        risk_per_trade_inr=risk_budget,
        minimum_net_risk_reward=min_rr,
        require_margin_verification=False,
        skip_if_margin_unavailable=False,
        slippage_bps_per_side=slippage,
    )
    unique_symbols = sorted(set(journal["symbol"].astype(str).str.upper()))
    candle_frames: dict[str, tuple[pd.DataFrame | None, Path | None]] = {}
    for symbol in unique_symbols:
        path = resolve_candle_file(symbol, candles_dir)
        candle_frames[symbol] = (load_candles(path) if path else None, path)

    rows = []
    for _, row in journal.iterrows():
        symbol = str(row["symbol"]).strip().upper()
        candles, path = candle_frames[symbol]
        rows.append(evaluate_signal(
            row, candles, candle_path=path, settings=paper_settings,
            charge_rates=charge_rates, slippage_bps_per_side=slippage,
            entry_cutoff=cutoff,
        ))
    outcomes = pd.DataFrame(rows)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    outcomes.to_csv(output_path, index=False)
    candidates_path = output_path.with_name(f"{output_path.stem}_portfolio_candidates.csv")
    if not outcomes.empty:
        completed = outcomes.loc[outcomes["outcome_status"] == "COMPLETED_RESEARCH_ONLY"].copy()
    else:
        completed = pd.DataFrame()
    portfolio_columns = ["symbol", "side", "entry_time", "exit_time", "entry", "exit", "stop_loss", "quantity", "gross_pnl", "exit_reason", "signal_id", "strategy"]
    if not completed.empty:
        portfolio = completed[[
            "symbol", "side", "entry_time", "exit_time", "fill_entry", "fill_exit",
            "stop_loss", "evaluated_quantity", "gross_pnl", "exit_reason", "signal_id", "strategy",
        ]].rename(columns={
            "fill_entry": "entry",
            "fill_exit": "exit",
            "evaluated_quantity": "quantity",
        })
    else:
        portfolio = pd.DataFrame(columns=portfolio_columns)
    portfolio.to_csv(candidates_path, index=False)

    status_counts = {
        str(status): int(count)
        for status, count in outcomes["outcome_status"].value_counts(dropna=False).items()
    } if not outcomes.empty else {}
    summary = {
        "journal_path": str(journal_path),
        "candles_directory": str(candles_dir),
        "outcomes_path": str(output_path),
        "portfolio_candidates_path": str(candidates_path),
        "signals_read": int(len(journal)),
        "completed_research_only_outcomes": int(len(completed)),
        "signals_from_verified_live_sources": int(outcomes["source_live_data_verified"].sum()) if not outcomes.empty else 0,
        "signals_from_unverified_or_unknown_sources": int((~outcomes["source_live_data_verified"]).sum()) if not outcomes.empty else 0,
        "status_counts": status_counts,
        "starting_cash_for_individual_signal_sizing": capital,
        "risk_per_signal_for_individual_sizing": risk_budget,
        "minimum_net_reward_risk_gate": min_rr,
        "slippage_bps_per_side": slippage,
        "method": (
            "Next-bar-open hypothetical entry; cash-only sizing and after-cost net-R:R gate; "
            "stop-first on ambiguous bars; target 2 full exit; no favorable target-gap improvement; "
            "stop gaps fill at worse bar open; no scoring across missing bars."
        ),
        "warning": (
            "Research-only hypothetical outcomes. Signals were evaluated individually and may overlap. "
            "Run src.portfolio_replay on the generated candidate CSV to apply one shared capital budget; "
            "neither output represents actual fills or verifies a profitable edge."
        ),
    }
    summary_path = output_path.with_name(f"{output_path.stem}_summary.json")
    summary_path.write_text(json.dumps(summary, indent=2, allow_nan=False), encoding="utf-8")
    print(f"Paper outcome detail: {output_path}")
    print(f"Completed hypothetical trade candidates: {candidates_path}")
    print(f"Outcome status summary (counts only): {summary_path}")
    print("Research only: no orders, actual fills, or profit claims are generated.")
    return outcomes, portfolio, summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Evaluate saved paper-signal observations against later 5-minute candles; never submits orders."
    )
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--candles-dir", type=Path, required=True,
                        help="One 5-minute CSV per symbol: SYMBOL.csv or a unique SYMBOL_*.csv")
    parser.add_argument("--output", type=Path, default=Path("reports/paper_outcomes.csv"))
    parser.add_argument("--initial-capital", type=float, default=None)
    parser.add_argument("--risk-per-trade", type=float, default=None)
    parser.add_argument("--minimum-net-rr", type=float, default=None)
    parser.add_argument("--slippage-bps", type=float, default=None)
    parser.add_argument("--entry-cutoff", default=None)
    args = parser.parse_args()
    try:
        evaluate_journal(
            args.journal, args.candles_dir, output_path=args.output,
            initial_capital=args.initial_capital,
            risk_per_trade=args.risk_per_trade,
            minimum_net_risk_reward=args.minimum_net_rr,
            slippage_bps_per_side=args.slippage_bps,
            entry_cutoff_ist=args.entry_cutoff,
        )
    except (ValueError, OSError, FileNotFoundError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
