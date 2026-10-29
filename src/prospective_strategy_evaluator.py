"""Prospective evaluation of frozen VWAP strategies on newly archived Kite candles.

This process only consumes newly appended candles from the Kite archive. On first
start it seeds its watermark at the archive head and never reconstructs signals
from old candles. All fills and P&L are hypothetical. It never places orders.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import math
import time
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from src.indicators import add_indicators
from src.paper_outcomes import (
    evaluate_signal,
    load_candles,
    parse_market_time,
    resolve_candle_file,
)
from src.risk_engine import load_phase2_settings
from src.strategy_backtest import (
    SUPPORTED_STRATEGIES,
    filter_incomplete_sessions,
    make_signal_callback,
)
from src.strategies.market_regime import classify_market_regime
from src.strategies.signal_planner import plan_atr_levels

LOGGER = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
BAR_MINUTES = 5
SESSION_START = dtime(9, 15)
LAST_BAR_START = dtime(15, 10)
DEFAULT_CANDIDATES = "HDFCBANK:vwap_trend,INFY:vwap_pullback"
DEFAULT_SLIPPAGE_BPS = (2.0, 5.0, 8.0)
MINIMUM_NET_RR = 1.5
MINIMUM_COMPLETED_FOR_REVIEW = 30
JOURNAL_FIELDS = [
    "signal_id", "recorded_at", "signal_time", "symbol", "strategy", "side",
    "reference_entry", "stop_loss", "target_1", "target_2", "confidence",
    "market_regime", "reason", "provider", "data_verified_live", "paper_status",
]
AUDIT_FIELDS = [
    "recorded_at", "symbol", "strategy", "candle_time", "status", "reason", "signal_id",
]
def now_ist() -> pd.Timestamp:
    return pd.Timestamp(datetime.now(IST))


def parse_candidates(value: str) -> dict[str, tuple[str, ...]]:
    """Parse SYMBOL:strategy pairs, allowing one or more strategies per symbol."""
    grouped: dict[str, list[str]] = {}
    seen: set[tuple[str, str]] = set()
    for item in value.split(","):
        item = item.strip()
        if not item or ":" not in item:
            raise ValueError("candidates must use SYMBOL:strategy pairs separated by commas")
        symbol, strategy = (part.strip() for part in item.split(":", maxsplit=1))
        symbol, strategy = symbol.upper(), strategy.lower()
        if not symbol or strategy not in SUPPORTED_STRATEGIES:
            raise ValueError(
                f"invalid candidate {item!r}; strategies must be one of {SUPPORTED_STRATEGIES}"
            )
        key = (symbol, strategy)
        if key in seen:
            raise ValueError(f"duplicate candidate {symbol}:{strategy}")
        seen.add(key)
        grouped.setdefault(symbol, []).append(strategy)
    if not grouped:
        raise ValueError("at least one candidate is required")
    return {symbol: tuple(strategies) for symbol, strategies in grouped.items()}


def _read_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": 1, "initialized": False, "last_processed": {}, "outcomes_dirty": False}
    try:
        state = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read evaluator state {path}: {exc}") from exc
    if not isinstance(state, dict) or state.get("schema_version") != 1:
        raise ValueError(f"Unsupported or invalid evaluator state file: {path}")
    if not isinstance(state.get("last_processed"), dict):
        raise ValueError(f"Evaluator state has no last_processed map: {path}")
    return state


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def _ensure_csv(path: Path, columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.stat().st_size == 0:
        with path.open("w", encoding="utf-8", newline="") as handle:
            csv.writer(handle).writerow(columns)
        return
    try:
        existing = list(pd.read_csv(path, nrows=0).columns)
    except (OSError, pd.errors.ParserError) as exc:
        raise ValueError(f"Could not read CSV header {path}: {exc}") from exc
    if existing != columns:
        raise ValueError(f"Unexpected CSV schema in {path}; preserve it and use a new output directory.")


def _append_rows(path: Path, columns: list[str], rows: list[dict[str, Any]]) -> None:
    _ensure_csv(path, columns)
    if not rows:
        return
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in columns})


def _candle_modes(candles: pd.DataFrame) -> set[str]:
    if "provider_mode" not in candles.columns:
        return set()
    return {
        str(value).strip().casefold()
        for value in candles["provider_mode"].dropna().unique()
        if str(value).strip()
    }


def _require_kite_archive(candles: pd.DataFrame, symbol: str) -> None:
    modes = _candle_modes(candles)
    if modes != {"kite"}:
        raise ValueError(
            f"{symbol}: prospective strategy evaluation requires a Kite-only archive; "
            f"found provider modes {sorted(modes) if modes else ['missing']}"
        )


def _load_symbol_archive(symbol: str, candles_dir: Path) -> tuple[pd.DataFrame | None, Path | None]:
    path = resolve_candle_file(symbol, candles_dir)
    if path is None:
        return None, None
    candles = load_candles(path)
    _require_kite_archive(candles, symbol)
    if candles.index.tz is None:
        candles.index = candles.index.tz_localize(IST)
    else:
        candles.index = candles.index.tz_convert(IST)
    return candles.sort_index(), path


def _bootstrap_state(
    state: dict[str, Any],
    candidates: dict[str, tuple[str, ...]],
    candles_dir: Path,
    state_path: Path,
    recorded_at: pd.Timestamp,
) -> dict[str, Any]:
    """Seed each symbol at its present archive head; never signal on pre-start bars."""
    watermarks = state.setdefault("last_processed", {})
    seeded: dict[str, str | None] = {}
    for symbol in candidates:
        try:
            frame, _ = _load_symbol_archive(symbol, candles_dir)
            watermark = frame.index[-1].isoformat() if frame is not None and not frame.empty else None
        except (OSError, ValueError) as exc:
            LOGGER.warning("%s: cannot seed archive watermark: %s", symbol, exc)
            watermark = None
        watermarks[symbol] = watermark
        seeded[symbol] = watermark
    state.update({
        "initialized": True,
        "initialized_at": recorded_at.isoformat(),
        "last_processed": watermarks,
        "outcomes_dirty": False,
    })
    _write_state(state_path, state)
    LOGGER.info(
        "Prospective evaluator initialized. Existing candles were not backfilled; watermarks=%s",
        seeded,
    )
    return state


def _causal_indicators(
    candles: pd.DataFrame,
    stamp: pd.Timestamp,
) -> tuple[pd.DataFrame | None, str, str]:
    """Return indicators using only complete prior sessions and bars through stamp."""
    current = candles.loc[(candles.index.date == stamp.date()) & (candles.index <= stamp)].copy()
    if current.empty:
        return None, "CURRENT_SESSION_CANDLES_MISSING", "No current-session bars were found."
    start = stamp.normalize() + pd.Timedelta(hours=9, minutes=15)
    expected = pd.date_range(start=start, end=stamp, freq=f"{BAR_MINUTES}min")
    if not current.index.equals(expected):
        missing = expected.difference(current.index)
        return (
            None,
            "INCOMPLETE_CURRENT_SESSION_CANDLES",
            f"Current session is missing required bars through {stamp.isoformat()}: "
            f"{[value.isoformat() for value in missing[:5]]}",
        )
    prior = candles.loc[candles.index.date < stamp.date()].copy()
    if not prior.empty:
        prior, _ = filter_incomplete_sessions(prior)
    causal = pd.concat([prior, current]).sort_index()
    if causal.index.has_duplicates:
        return None, "DUPLICATE_TIMESTAMP", "Causal history contains duplicate timestamps."
    try:
        indicators = add_indicators(causal)
    except ValueError as exc:
        return None, "INVALID_CAUSAL_HISTORY", str(exc)
    return indicators, "READY", ""


def _candidate_id(symbol: str, strategy: str, side: str, stamp: pd.Timestamp) -> str:
    identity = "|".join((symbol.upper(), strategy.lower(), side.upper(), stamp.isoformat()))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def _make_candidate(
    symbol: str,
    strategy: str,
    stamp: pd.Timestamp,
    indicators: pd.DataFrame,
    recorded_at: pd.Timestamp,
) -> dict[str, Any] | None:
    """Generate one signal from the exact callback used by the baseline backtester."""
    callback = make_signal_callback(
        symbol,
        strategy,
        target_atr_multiple=2.5,
        reject_nontrending_regimes=False,
    )
    signal = callback(indicators, len(indicators) - 1)
    if signal is None:
        return None

    last = indicators.iloc[-1]
    close = float(last["close"])
    atr = float(last["atr_14"])
    levels = plan_atr_levels(
        signal["side"],
        entry=close,
        atr=atr,
        stop_atr_multiple=1.0,
        target_1_atr_multiple=2.0,
        target_2_atr_multiple=2.5,
    )
    # The baseline callback also plans its single historical target at 2.5 ATR.
    if not math.isclose(float(signal["target"]), levels.target_2, rel_tol=1e-9, abs_tol=0.02):
        raise ValueError(f"{symbol}:{strategy}: target level differs from the baseline callback")
    return {
        "signal_id": _candidate_id(symbol, strategy, signal["side"], stamp),
        "recorded_at": recorded_at.isoformat(),
        "signal_time": stamp.isoformat(),
        "symbol": symbol,
        "strategy": strategy,
        "side": signal["side"],
        "reference_entry": close,
        "stop_loss": levels.stop_loss,
        "target_1": levels.target_1,
        "target_2": levels.target_2,
        "confidence": signal.get("confidence", ""),
        "market_regime": classify_market_regime(indicators),
        "reason": signal.get("reason", ""),
        "provider": "Kite Connect candle archive",
        # Candle provenance is verified as Kite, but this is not a fresh-quote or fill claim.
        "data_verified_live": False,
        "paper_status": "RESEARCH_ONLY_NO_ORDER",
    }


def _load_existing_signal_ids(journal_path: Path) -> set[str]:
    if not journal_path.exists() or journal_path.stat().st_size == 0:
        return set()
    frame = pd.read_csv(journal_path, dtype={"signal_id": str})
    if "signal_id" not in frame.columns:
        raise ValueError(f"Existing signal journal has no signal_id column: {journal_path}")
    return set(frame["signal_id"].dropna().astype(str))


def _process_new_bars_for_symbol(
    symbol: str,
    strategies: tuple[str, ...],
    candles: pd.DataFrame,
    watermark: str,
    *,
    now: pd.Timestamp,
    max_late_seconds: float,
    existing_ids: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    """Process only bars after watermark; use no data after each signal timestamp."""
    last_stamp = pd.Timestamp(watermark)
    if last_stamp.tzinfo is None:
        last_stamp = last_stamp.tz_localize(IST)
    else:
        last_stamp = last_stamp.tz_convert(IST)
    new_stamps = candles.index[candles.index > last_stamp]
    signal_rows: list[dict[str, Any]] = []
    audit_rows: list[dict[str, Any]] = []
    if len(new_stamps) == 0:
        return signal_rows, audit_rows, candles.index[-1].isoformat()

    for stamp in new_stamps:
        if stamp.time().replace(tzinfo=None) < SESSION_START or stamp.time().replace(tzinfo=None) > LAST_BAR_START:
            for strategy in strategies:
                audit_rows.append({
                    "recorded_at": now.isoformat(), "symbol": symbol, "strategy": strategy,
                    "candle_time": stamp.isoformat(), "status": "OUTSIDE_SESSION",
                    "reason": "Only regular-session 5-minute bars are evaluated.", "signal_id": "",
                })
            continue

        bar_end = stamp + pd.Timedelta(minutes=BAR_MINUTES)
        lateness_seconds = (now - bar_end).total_seconds()
        if lateness_seconds > max_late_seconds:
            for strategy in strategies:
                audit_rows.append({
                    "recorded_at": now.isoformat(), "symbol": symbol, "strategy": strategy,
                    "candle_time": stamp.isoformat(), "status": "LATE_CANDLE_SKIPPED",
                    "reason": (
                        f"Candle was processed {lateness_seconds:.1f}s after its end, exceeding "
                        f"the {max_late_seconds:.1f}s prospective limit; no retrospective signal was generated."
                    ),
                    "signal_id": "",
                })
            continue

        indicators, quality_status, quality_note = _causal_indicators(candles, stamp)
        if indicators is None:
            for strategy in strategies:
                audit_rows.append({
                    "recorded_at": now.isoformat(), "symbol": symbol, "strategy": strategy,
                    "candle_time": stamp.isoformat(), "status": quality_status,
                    "reason": quality_note, "signal_id": "",
                })
            continue

        for strategy in strategies:
            candidate = _make_candidate(symbol, strategy, stamp, indicators, now)
            if candidate is None:
                audit_rows.append({
                    "recorded_at": now.isoformat(), "symbol": symbol, "strategy": strategy,
                    "candle_time": stamp.isoformat(), "status": "NO_SIGNAL",
                    "reason": "Baseline strategy callback produced no directional signal.", "signal_id": "",
                })
                continue
            signal_id = str(candidate["signal_id"])
            if signal_id in existing_ids:
                audit_rows.append({
                    "recorded_at": now.isoformat(), "symbol": symbol, "strategy": strategy,
                    "candle_time": stamp.isoformat(), "status": "DUPLICATE_SKIPPED",
                    "reason": "The deterministic signal_id already exists in the append-only journal.",
                    "signal_id": signal_id,
                })
                continue
            signal_rows.append(candidate)
            existing_ids.add(signal_id)
            audit_rows.append({
                "recorded_at": now.isoformat(), "symbol": symbol, "strategy": strategy,
                "candle_time": stamp.isoformat(), "status": "CANDIDATE_RECORDED",
                "reason": candidate["reason"], "signal_id": signal_id,
            })
    return signal_rows, audit_rows, candles.index[-1].isoformat()


def _outcome_metrics(outcomes: pd.DataFrame) -> list[dict[str, Any]]:
    if outcomes.empty:
        return []
    completed_statuses = {
        "COMPLETED_RESEARCH_ONLY",
        "COMPLETED_RESEARCH_ONLY_GATE_BYPASSED",
    }
    metrics: list[dict[str, Any]] = []
    for (symbol, strategy, slippage), group in outcomes.groupby(
        ["symbol", "strategy", "slippage_bps_per_side"], dropna=False, sort=True
    ):
        completed = group.loc[
            group["outcome_status"].isin(completed_statuses)
            & pd.to_numeric(group["net_pnl"], errors="coerce").notna()
        ].copy()
        pnl = pd.to_numeric(completed.get("net_pnl", pd.Series(dtype=float)), errors="coerce").dropna()
        winners = int((pnl > 0).sum())
        losers = int((pnl < 0).sum())
        gross_wins = float(pnl[pnl > 0].sum())
        gross_losses = float(-pnl[pnl < 0].sum())
        pf = gross_wins / gross_losses if gross_losses > 0 else (None if len(pnl) == 0 else None)
        gate = group["entry_gate_accepted"]
        gate_accepted = int(gate.map(lambda value: str(value).casefold() == "true").sum())
        rejection = group["entry_gate_rejection_reason"].fillna("").astype(str)
        rr_rejected = int(rejection.str.contains("Target 1 net R:R is below", regex=False).sum())
        net_sum = float(pnl.sum()) if len(pnl) else 0.0
        expectancy = float(pnl.mean()) if len(pnl) else None
        n = int(len(pnl))
        metrics.append({
            "symbol": str(symbol),
            "strategy": str(strategy),
            "slippage_bps_per_side": float(slippage),
            "signals_observed": int(len(group)),
            "completed_signal_outcomes": n,
            "gate_accepted_signals": gate_accepted,
            "gate_rejected_for_net_rr": rr_rejected,
            "other_or_pending_outcomes": int(len(group) - n),
            "wins": winners,
            "losses": losers,
            "win_rate_pct": round(100.0 * winners / n, 2) if n else None,
            "expectancy_per_completed_signal_inr": round(expectancy, 4) if expectancy is not None else None,
            "profit_factor": round(gross_wins / gross_losses, 4) if gross_losses > 0 else None,
            "sum_of_signal_net_pnl_inr_not_portfolio_pnl": round(net_sum, 4),
            "minimum_completed_signals_for_review": MINIMUM_COMPLETED_FOR_REVIEW,
            "sample_status": (
                "INSUFFICIENT_SAMPLE"
                if n < MINIMUM_COMPLETED_FOR_REVIEW
                else "READY_FOR_HUMAN_REVIEW_NOT_PROMOTION"
            ),
        })
    return metrics


def refresh_outcomes(
    journal_path: Path,
    candles_dir: Path,
    output_path: Path,
    *,
    slippage_values: tuple[float, ...] = DEFAULT_SLIPPAGE_BPS,
) -> dict[str, Any]:
    """Re-score the prospective journal under fixed cost scenarios and write reports."""
    configured, charge_rates = load_phase2_settings()
    for value in slippage_values:
        if not math.isfinite(value) or value < 0 or value > 100:
            raise ValueError("slippage scenarios must be finite and between 0 and 100 bps per side")
    if not journal_path.exists() or journal_path.stat().st_size == 0:
        journal = pd.DataFrame(columns=JOURNAL_FIELDS)
    else:
        journal = pd.read_csv(journal_path, dtype={"signal_id": str, "symbol": str, "side": str})
        missing = set(JOURNAL_FIELDS).difference(journal.columns)
        if missing:
            raise ValueError(f"Prospective journal is missing columns: {sorted(missing)}")

    candle_cache: dict[str, tuple[pd.DataFrame | None, Path | None]] = {}
    for symbol in sorted(set(journal["symbol"].astype(str).str.upper())) if not journal.empty else []:
        candle_cache[symbol] = _load_symbol_archive(symbol, candles_dir)

    cutoff = parse_market_time(configured.entry_cutoff_ist, label="entry cutoff")
    rows: list[dict[str, Any]] = []
    for _, source in journal.iterrows():
        symbol = str(source["symbol"]).strip().upper()
        candles, candle_path = candle_cache.get(symbol, (None, None))
        for slippage in slippage_values:
            settings = configured.__class__(
                capital_inr=configured.capital_inr,
                risk_per_trade_inr=configured.risk_per_trade_inr,
                minimum_net_risk_reward=MINIMUM_NET_RR,
                require_margin_verification=False,
                skip_if_margin_unavailable=False,
                slippage_bps_per_side=float(slippage),
                entry_cutoff_ist=configured.entry_cutoff_ist,
                square_off_time_ist=configured.square_off_time_ist,
            )
            outcome = evaluate_signal(
                source,
                candles,
                candle_path=candle_path,
                settings=settings,
                charge_rates=charge_rates,
                slippage_bps_per_side=float(slippage),
                entry_cutoff=cutoff,
                simulate_net_rr_rejections=True,
            )
            outcome.update({
                "candidate": f"{symbol}:{str(source['strategy']).lower()}",
                "slippage_bps_per_side": float(slippage),
                "minimum_net_reward_risk_gate": MINIMUM_NET_RR,
                "gate_decision": (
                    "ACCEPTED_BY_CONFIGURED_GATE"
                    if str(outcome.get("entry_gate_accepted", "")).casefold() == "true"
                    else "REJECTED_NET_RR_SHADOW_ONLY"
                    if "Target 1 net R:R is below" in str(outcome.get("entry_gate_rejection_reason", ""))
                    else "REJECTED_OTHER_OR_NOT_YET_ASSESSED"
                ),
                "signal_level_outcome_not_portfolio": True,
                "research_only": True,
            })
            rows.append(outcome)

    outcomes = pd.DataFrame(rows)
    if outcomes.empty:
        outcomes = pd.DataFrame(columns=[
            "signal_id", "symbol", "strategy", "candidate", "slippage_bps_per_side",
            "outcome_status", "net_pnl", "entry_gate_accepted",
            "entry_gate_rejection_reason", "gate_decision",
            "signal_level_outcome_not_portfolio", "research_only",
        ])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    outcomes.to_csv(output_path, index=False)
    summary = {
        "generated_at": now_ist().isoformat(),
        "signal_journal": str(journal_path),
        "candles_directory": str(candles_dir),
        "outcomes_csv": str(output_path),
        "signals_observed": int(len(journal)),
        "outcome_rows": int(len(outcomes)),
        "candidate_slippage_metrics": _outcome_metrics(outcomes),
        "slippage_bps_per_side": list(slippage_values),
        "minimum_net_reward_risk_gate": MINIMUM_NET_RR,
        "minimum_completed_signals_for_review": MINIMUM_COMPLETED_FOR_REVIEW,
        "starting_capital_for_individual_signal_sizing_inr": float(configured.capital_inr),
        "risk_budget_per_signal_inr": float(configured.risk_per_trade_inr),
        "method": (
            "Frozen vwap_trend/vwap_pullback callbacks from src.strategy_backtest; only newly appended "
            "Kite candles; no pre-start signal backfill; completed signal close followed by next-bar-open "
            "entry; 1 ATR stop, 2 ATR target 1 and 2.5 ATR target 2; stop-first same-bar handling; "
            "conservative stop-gap fills; target 2 full exit; session square-off; no scoring through data gaps. "
            "The configured 1.5 minimum net R:R gate is recorded independently. When and only when this gate "
            "is the rejection reason, a separate research-only shadow outcome is simulated for diagnostics."
        ),
        "warning": (
            "All results are hypothetical, signal-level observations; signals can overlap. The sum of signal "
            "P&Ls is not portfolio P&L and must not be used as a portfolio equity curve. Pending, invalid, "
            "late, or data-gap events are not wins or losses. This report does not place or recommend orders."
        ),
        "status": (
            "INSUFFICIENT_SAMPLE"
            if not any(item["completed_signal_outcomes"] >= MINIMUM_COMPLETED_FOR_REVIEW for item in _outcome_metrics(outcomes))
            else "READY_FOR_HUMAN_REVIEW_NOT_PROMOTION"
        ),
    }
    summary_path = output_path.with_name(f"{output_path.stem}_summary.json")
    summary_path.write_text(json.dumps(summary, indent=2, allow_nan=False), encoding="utf-8")
    LOGGER.info("Prospective outcomes refreshed: signals=%d output=%s summary=%s",
                len(journal), output_path, summary_path)
    return summary


def run_once(
    *,
    candidates: dict[str, tuple[str, ...]],
    candles_dir: Path,
    output_dir: Path,
    state_path: Path,
    now: pd.Timestamp | None = None,
    max_late_seconds: float = 120.0,
    slippage_values: tuple[float, ...] = DEFAULT_SLIPPAGE_BPS,
) -> dict[str, Any]:
    """Run one poll cycle; bootstrap skips all already-present archive bars."""
    recorded_at = now if now is not None else now_ist()
    if recorded_at.tzinfo is None:
        recorded_at = recorded_at.tz_localize(IST)
    else:
        recorded_at = recorded_at.tz_convert(IST)

    signal_path = output_dir / "prospective_strategy_signals.csv"
    audit_path = output_dir / "prospective_strategy_audit.csv"
    outcome_path = output_dir / "prospective_strategy_outcomes.csv"
    _ensure_csv(signal_path, JOURNAL_FIELDS)
    _ensure_csv(audit_path, AUDIT_FIELDS)

    state = _read_state(state_path)
    if not state.get("initialized", False):
        state = _bootstrap_state(state, candidates, candles_dir, state_path, recorded_at)
        refresh_outcomes(signal_path, candles_dir, outcome_path, slippage_values=slippage_values)
        return {"bootstrapped": True, "signals_added": 0, "candles_processed": 0}

    watermarks: dict[str, str | None] = state.setdefault("last_processed", {})
    known_ids = _load_existing_signal_ids(signal_path)
    new_signals: list[dict[str, Any]] = []
    audit_rows: list[dict[str, Any]] = []
    processed_count = 0
    for symbol, strategies in candidates.items():
        try:
            candles, _ = _load_symbol_archive(symbol, candles_dir)
        except (OSError, ValueError) as exc:
            LOGGER.warning("%s: archive unavailable or untrusted; fail closed: %s", symbol, exc)
            continue
        if candles is None or candles.empty:
            LOGGER.info("%s: no candle archive yet", symbol)
            continue

        watermark = watermarks.get(symbol)
        if watermark is None:
            watermarks[symbol] = candles.index[-1].isoformat()
            audit_rows.extend({
                "recorded_at": recorded_at.isoformat(), "symbol": symbol, "strategy": strategy,
                "candle_time": candles.index[-1].isoformat(), "status": "ARCHIVE_FIRST_SEEN_SEEDED",
                "reason": "Archive appeared after startup; existing bars were skipped to prevent backfill.",
                "signal_id": "",
            } for strategy in strategies)
            state["outcomes_dirty"] = True
            continue

        try:
            signals, audits, new_watermark = _process_new_bars_for_symbol(
                symbol, strategies, candles, watermark, now=recorded_at,
                max_late_seconds=max_late_seconds, existing_ids=known_ids,
            )
        except (ValueError, KeyError, IndexError) as exc:
            LOGGER.exception("%s: prospective strategy evaluation failed closed: %s", symbol, exc)
            continue
        new_signals.extend(signals)
        audit_rows.extend(audits)
        processed_count += sum(
            1 for stamp in candles.index
            if stamp > (pd.Timestamp(watermark).tz_localize(IST) if pd.Timestamp(watermark).tzinfo is None else pd.Timestamp(watermark).tz_convert(IST))
        )
        if new_watermark != watermark:
            watermarks[symbol] = new_watermark
            state["outcomes_dirty"] = True

    _append_rows(signal_path, JOURNAL_FIELDS, new_signals)
    _append_rows(audit_path, AUDIT_FIELDS, audit_rows)
    state["last_processed"] = watermarks
    _write_state(state_path, state)

    summary = None
    if state.get("outcomes_dirty", False):
        summary = refresh_outcomes(signal_path, candles_dir, outcome_path, slippage_values=slippage_values)
        state["outcomes_dirty"] = False
        state["last_outcome_refresh_at"] = recorded_at.isoformat()
        _write_state(state_path, state)
    LOGGER.info(
        "Prospective cycle complete: bars_processed=%d new_signals=%d audit_rows=%d outcomes_refreshed=%s",
        processed_count, len(new_signals), len(audit_rows), bool(summary),
    )
    return {
        "bootstrapped": False,
        "signals_added": len(new_signals),
        "candles_processed": processed_count,
        "audit_rows": len(audit_rows),
        "outcomes_refreshed": bool(summary),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prospectively evaluate frozen VWAP strategies from newly appended Kite candles; never places orders."
    )
    parser.add_argument("--candles-dir", type=Path, default=Path("data/paper_candles_kite"))
    parser.add_argument("--output-dir", type=Path, default=Path("reports/prospective_strategy_evaluation"))
    parser.add_argument("--state", type=Path, default=Path("logs/prospective_strategy_evaluator_state.json"))
    parser.add_argument("--candidates", default=DEFAULT_CANDIDATES,
                        help="Frozen SYMBOL:strategy pairs; default HDFCBANK:vwap_trend,INFY:vwap_pullback.")
    parser.add_argument("--slippage-bps", default="2,5,8",
                        help="Comma-separated per-side slippage scenarios; default 2,5,8 bps.")
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument("--max-late-seconds", type=float, default=120.0,
                        help="Skip signal generation when a candle is this many seconds late after its end.")
    parser.add_argument("--once", action="store_true", help="Run one cycle then exit; the first run only seeds watermarks.")
    args = parser.parse_args()
    try:
        candidates = parse_candidates(args.candidates)
        slippage_values = tuple(float(item.strip()) for item in args.slippage_bps.split(",") if item.strip())
        if not slippage_values or any(not math.isfinite(value) or value < 0 or value > 100 for value in slippage_values):
            raise ValueError("slippage-bps must contain values between 0 and 100")
        if not math.isfinite(args.poll_seconds) or args.poll_seconds < 1:
            raise ValueError("poll-seconds must be at least 1")
        if not math.isfinite(args.max_late_seconds) or args.max_late_seconds < 0:
            raise ValueError("max-late-seconds must be non-negative")
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        LOGGER.info(
            "Research-only forward evaluator starting; candidates=%s slippage_bps_per_side=%s",
            args.candidates, slippage_values,
        )
        while True:
            try:
                run_once(
                    candidates=candidates,
                    candles_dir=args.candles_dir,
                    output_dir=args.output_dir,
                    state_path=args.state,
                    max_late_seconds=args.max_late_seconds,
                    slippage_values=slippage_values,
                )
            except (ValueError, OSError, FileNotFoundError) as exc:
                LOGGER.exception("Prospective evaluator cycle failed; no orders were sent: %s", exc)
                if args.once:
                    raise
            if args.once:
                break
            time.sleep(args.poll_seconds)
    except (ValueError, OSError, FileNotFoundError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
