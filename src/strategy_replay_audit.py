"""Archived-candle audit for shadow-strategy fills and blocked candidates.

This module checks recorded shadow trades against the same conservative OHLC
exit rules used by the paper strategy lab. Rejected/expired candidates are only
analysed as no-fill counterfactuals; no P&L or filled position is invented.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import time as dtime
import math
from pathlib import Path
from typing import Any

import pandas as pd

from src.exit_policy import evaluate_ohlc_exit, load_exit_policy
from src.paper_outcomes import load_candles
from src.research_timing import next_five_minute_bar_after, to_ist

ENTRY_CUTOFF = dtime(14, 45)
SLIPPAGE_BPS = 5.0
EXIT_POLICY = load_exit_policy()


def _positive(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _rounded(value: float | None, digits: int = 4) -> float | None:
    return round(value, digits) if value is not None and math.isfinite(value) else None


def _session_frame(frame: pd.DataFrame, stamp: pd.Timestamp) -> pd.DataFrame:
    return frame.loc[frame.index.date == stamp.date()]


def _entry_levels_valid(side: str, entry: float, stop: float, target: float) -> bool:
    if side == "LONG":
        return stop < entry < target
    if side == "SHORT":
        return target < entry < stop
    return False


def _replay_exit(
    frame: pd.DataFrame,
    *,
    side: str,
    stop: float,
    target: float,
    entry_ts: pd.Timestamp,
) -> tuple[str, pd.Timestamp | None, float | None, str]:
    """Return the first conservative exit on continuous archived 5-minute bars."""
    bars = _session_frame(frame, entry_ts).loc[lambda x: x.index >= entry_ts]
    if bars.empty or bars.index[0] != entry_ts:
        return "MISSING_ENTRY_BAR", None, None, "The exact eligible entry candle is absent."
    previous = None
    for stamp, bar in bars.iterrows():
        if previous is not None and stamp != previous + pd.Timedelta(minutes=5):
            return "DATA_GAP", None, None, f"Missing candle after {previous.isoformat()}."
        bar_end = stamp + pd.Timedelta(minutes=5)
        decision = evaluate_ohlc_exit(
            side=side,
            stop_loss=stop,
            target_2=target,
            open_price=float(bar["open"]),
            high=float(bar["high"]),
            low=float(bar["low"]),
            close=float(bar["close"]),
            bar_end_time=bar_end.time(),
            policy=EXIT_POLICY,
        )
        if decision is not None:
            return decision.reason, bar_end, float(decision.reference_price), decision.detail
        previous = stamp
    return "NO_EXIT_BY_ARCHIVE_END", None, None, "No stop, target or time exit appears in the archived bars."


def _closed_trade_audit(row: dict[str, Any], frame: pd.DataFrame) -> dict[str, Any]:
    result = {
        "signal_id": row.get("signal_id"),
        "strategy_version": row.get("strategy_version"),
        "symbol": row.get("symbol"),
        "side": row.get("side"),
        "recorded_status": row.get("status"),
        "audit_status": "UNVERIFIED",
        "recorded_exit_reason": row.get("exit_reason"),
        "recorded_exit_time": row.get("exit_time"),
        "recorded_net_pnl": row.get("net_pnl"),
        "mfe_points": None,
        "mae_points": None,
        "mfe_r": None,
        "mae_r": None,
        "replayed_exit_reason": None,
        "replayed_exit_time": None,
        "replayed_exit_reference_price": None,
        "note": "",
    }
    try:
        entry_ts = to_ist(row.get("entry_time"), label="shadow entry time")
        exit_ts = to_ist(row.get("exit_time"), label="shadow exit time")
        entry_ref = _positive(row.get("entry_reference_price"))
        stop = _positive(row.get("stop_loss"))
        target = _positive(row.get("target_2"))
    except (TypeError, ValueError) as exc:
        result.update(audit_status="INVALID_TRADE_RECORD", note=str(exc))
        return result
    if not entry_ref or not stop or not target or exit_ts <= entry_ts:
        result.update(audit_status="INVALID_TRADE_RECORD", note="Missing or invalid entry/exit levels.")
        return result

    session = _session_frame(frame, entry_ts)
    exit_bar_start = exit_ts - pd.Timedelta(minutes=5)
    bars = session.loc[(session.index >= entry_ts) & (session.index <= exit_bar_start)]
    if entry_ts not in bars.index:
        result.update(audit_status="MISSING_ENTRY_BAR", note="Exact recorded entry candle is missing.")
        return result
    if exit_bar_start not in bars.index:
        result.update(audit_status="MISSING_EXIT_BAR", note="Recorded exit candle is missing.")
        return result
    expected = pd.date_range(entry_ts, exit_bar_start, freq="5min")
    if not bars.index.equals(expected):
        result.update(audit_status="DATA_GAP", note="A 5-minute candle is missing between entry and exit.")
        return result

    highs = [float(x) for x in bars["high"]]
    lows = [float(x) for x in bars["low"]]
    if row.get("side") == "LONG":
        mfe = max(highs) - entry_ref
        mae = entry_ref - min(lows)
    else:
        mfe = entry_ref - min(lows)
        mae = max(highs) - entry_ref
    risk_points = abs(entry_ref - stop)
    result.update(
        mfe_points=_rounded(max(0.0, mfe)),
        mae_points=_rounded(max(0.0, mae)),
        mfe_r=_rounded(max(0.0, mfe) / risk_points) if risk_points > 0 else None,
        mae_r=_rounded(max(0.0, mae) / risk_points) if risk_points > 0 else None,
    )
    replay_reason, replay_time, replay_price, detail = _replay_exit(
        frame, side=str(row["side"]), stop=stop, target=target, entry_ts=entry_ts
    )
    result.update(
        replayed_exit_reason=replay_reason,
        replayed_exit_time=replay_time.isoformat() if replay_time else None,
        replayed_exit_reference_price=_rounded(replay_price),
    )
    recorded_price = _positive(row.get("exit_reference_price"))
    time_matches = replay_time is not None and replay_time == exit_ts
    reason_matches = replay_reason == str(row.get("exit_reason") or "")
    price_matches = (
        replay_price is not None
        and recorded_price is not None
        and abs(replay_price - recorded_price) <= max(0.000001, recorded_price * 1e-8)
    )
    if time_matches and reason_matches and price_matches:
        result["audit_status"] = "MATCHED"
        result["note"] = "Archived candles reproduce the recorded conservative exit."
    else:
        result["audit_status"] = "EXIT_MISMATCH"
        result["note"] = (
            f"{detail} Recorded exit={row.get('exit_reason')} at {row.get('exit_time')} "
            f"price={row.get('exit_reference_price')}; archived replay={replay_reason} "
            f"at {replay_time.isoformat() if replay_time else 'none'} price={replay_price}."
        )
    return result


def _blocked_candidate_audit(
    row: dict[str, Any], frame: pd.DataFrame, *, publication_at: Any | None = None
) -> dict[str, Any]:
    result = {
        "signal_id": row.get("signal_id"),
        "strategy_version": row.get("strategy_version"),
        "symbol": row.get("symbol"),
        "side": row.get("side"),
        "recorded_status": row.get("status"),
        "audit_status": "UNVERIFIED",
        "recorded_rejection_reason": row.get("reason"),
        "counterfactual_exit": None,
        "counterfactual_exit_time": None,
        "counterfactual_entry_time": None,
        "note": "",
        "counterfactual_pnl": None,
    }
    try:
        signal_ts = to_ist(row.get("signal_time"), label="shadow signal time")
        available_at = publication_at if publication_at is not None else row.get("created_at")
        entry_ts = next_five_minute_bar_after(signal_ts, available_at)
    except (TypeError, ValueError) as exc:
        result.update(audit_status="INVALID_SIGNAL_RECORD", note=str(exc))
        return result
    result["counterfactual_entry_time"] = entry_ts.isoformat()
    if entry_ts.date() != signal_ts.date() or entry_ts.time().replace(tzinfo=None) >= ENTRY_CUTOFF:
        result.update(
            audit_status="LATE_CUTOFF",
            note=f"Next eligible entry is {entry_ts.strftime('%H:%M')} IST, at or after the {ENTRY_CUTOFF.strftime('%H:%M')} cutoff.",
        )
        return result

    session = _session_frame(frame, signal_ts)
    if signal_ts not in session.index or entry_ts not in session.index:
        result.update(audit_status="MISSING_SIGNAL_OR_ENTRY_BAR", note="Exact signal or eligible entry candle is missing.")
        return result
    observed_close = float(session.loc[signal_ts]["close"])
    expected_close = _positive(row.get("signal_close"))
    tolerance = max(0.02, expected_close * 5.0 / 10000.0) if expected_close else 0.02
    if expected_close is None or abs(observed_close - expected_close) > tolerance:
        result.update(audit_status="SIGNAL_CLOSE_MISMATCH", note="Archived signal close differs from the stored signal.")
        return result
    between = session.loc[(session.index >= signal_ts) & (session.index <= entry_ts)]
    expected = pd.date_range(signal_ts, entry_ts, freq="5min")
    if not between.index.equals(expected):
        result.update(audit_status="DATA_GAP_BEFORE_ENTRY", note="Signal-to-entry candles are not continuous.")
        return result

    side = str(row.get("side") or "").upper()
    entry_ref = float(session.loc[entry_ts]["open"])
    entry_fill = entry_ref * (1.0 + SLIPPAGE_BPS / 10000.0) if side == "LONG" else entry_ref * (1.0 - SLIPPAGE_BPS / 10000.0)
    stop = _positive(row.get("stop_loss"))
    target = _positive(row.get("target_2"))
    if stop is None or target is None or not _entry_levels_valid(side, entry_ref, stop, target) or not _entry_levels_valid(side, entry_fill, stop, target):
        result.update(audit_status="PRICE_LEVELS_INVALIDATED", note="The next-bar open or adverse slippage invalidated the stored stop/target levels.")
        return result
    replay_reason, replay_time, replay_price, detail = _replay_exit(
        frame, side=side, stop=stop, target=target, entry_ts=entry_ts
    )
    result.update(
        audit_status=f"COUNTERFACTUAL_{replay_reason}",
        counterfactual_exit=replay_reason,
        counterfactual_exit_time=replay_time.isoformat() if replay_time else None,
        note=detail + " No fill, quantity or P&L is assigned to this rejected/expired candidate.",
    )
    return result


def _summarize(items: list[dict[str, Any]]) -> dict[str, Any]:
    closed = [item for item in items if item.get("recorded_status") == "CLOSED"]
    blocked = [item for item in items if item.get("recorded_status") in {"ENTRY_REJECTED", "EXPIRED_NO_ENTRY"}]
    matched = [item for item in closed if item.get("audit_status") == "MATCHED"]
    pnl = [
        float(item["recorded_net_pnl"])
        for item in closed
        if item.get("recorded_net_pnl") is not None
        and math.isfinite(float(item["recorded_net_pnl"]))
    ]
    wins = sum(value > 0 for value in pnl)
    losses = sum(value < 0 for value in pnl)
    mfe = [float(x["mfe_r"]) for x in closed if x.get("mfe_r") is not None]
    mae = [float(x["mae_r"]) for x in closed if x.get("mae_r") is not None]
    return {
        "observations": len(items),
        "closed_trades": len(closed),
        "recorded_wins": wins,
        "recorded_losses": losses,
        "recorded_net_pnl": _rounded(sum(pnl), 2) if pnl else None,
        "recorded_average_net_pnl": _rounded(sum(pnl) / len(pnl), 2) if pnl else None,
        "recorded_exit_reason_counts": dict(Counter(
            str(x.get("recorded_exit_reason") or "UNKNOWN") for x in closed
        )),
        "closed_replays_matched": len(matched),
        "closed_replay_mismatches_or_gaps": len(closed) - len(matched),
        "closed_replay_match_rate_pct": _rounded(100.0 * len(matched) / len(closed), 2) if closed else None,
        "average_mfe_r": _rounded(sum(mfe) / len(mfe)) if mfe else None,
        "average_mae_r": _rounded(sum(mae) / len(mae)) if mae else None,
        "blocked_candidates": len(blocked),
        "blocked_counterfactual_exit_counts": dict(Counter(
            str(x.get("audit_status")) for x in blocked
        )),
        "blocked_rejection_reason_counts": dict(Counter(
            str(x.get("recorded_rejection_reason") or "UNSPECIFIED") for x in blocked
        )),
    }


def audit_shadow_observations(
    observations: list[dict[str, Any]],
    candle_archive_dir: Path,
) -> dict[str, Any]:
    """Replay today's Challenger/Microstructure shadows; never include Champion rows."""
    archive = Path(candle_archive_dir)
    if not archive.is_dir():
        return {
            "status": "ARCHIVE_NOT_AVAILABLE",
            "archive_dir": str(archive),
            "observations": len(observations),
            "closed_trades_replayed": 0,
            "closed_replays_matched": 0,
            "closed_replay_mismatches": 0,
            "by_strategy": {},
            "details": [],
            "note": "No candle archive directory was available; no outcomes were inferred.",
        }

    by_symbol: dict[str, pd.DataFrame | None] = {}
    details: list[dict[str, Any]] = []
    for row in observations:
        version = str(row.get("strategy_version") or "")
        if not (version.startswith("challenger_") or version.startswith("micro_")):
            continue
        symbol = str(row.get("symbol") or "").strip().upper()
        if symbol not in by_symbol:
            path = archive / f"{symbol}.csv"
            try:
                by_symbol[symbol] = load_candles(path) if path.is_file() else None
            except (OSError, ValueError):
                by_symbol[symbol] = None
        frame = by_symbol[symbol]
        if frame is None:
            details.append({
                "signal_id": row.get("signal_id"),
                "strategy_version": version,
                "symbol": symbol,
                "recorded_status": row.get("status"),
                "audit_status": "ARCHIVE_SYMBOL_MISSING_OR_INVALID",
                "note": f"No valid single-symbol archived candle file was found for {symbol}.",
            })
            continue
        status = str(row.get("status") or "")
        if status == "CLOSED":
            details.append(_closed_trade_audit(row, frame))
        elif status in {"ENTRY_REJECTED", "EXPIRED_NO_ENTRY"}:
            details.append(_blocked_candidate_audit(row, frame))
        else:
            details.append({
                "signal_id": row.get("signal_id"),
                "strategy_version": version,
                "symbol": symbol,
                "recorded_status": status,
                "audit_status": "NOT_CLOSED",
                "note": "Observation has not completed a simulated trade; no outcome was assigned.",
            })

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in details:
        grouped[str(item.get("strategy_version") or "UNKNOWN")].append(item)
    closed = [item for item in details if item.get("recorded_status") == "CLOSED"]
    matched = [item for item in closed if item.get("audit_status") == "MATCHED"]
    return {
        "status": "COMPLETE" if all(item.get("audit_status") not in {
            "ARCHIVE_SYMBOL_MISSING_OR_INVALID", "MISSING_ENTRY_BAR", "MISSING_EXIT_BAR", "DATA_GAP",
            "MISSING_SIGNAL_OR_ENTRY_BAR", "DATA_GAP_BEFORE_ENTRY",
        } for item in details) else "PARTIAL",
        "archive_dir": str(archive),
        "observations": len(details),
        "closed_trades_replayed": len(closed),
        "closed_replays_matched": len(matched),
        "closed_replay_mismatches": sum(item.get("audit_status") == "EXIT_MISMATCH" for item in closed),
        "by_strategy": {key: _summarize(value) for key, value in sorted(grouped.items())},
        "details": details,
        "note": (
            "Closed trades are checked against recorded entry/exit times and conservative stop-first OHLC exits. "
            "Rejected/expired candidates are evaluated only as counterfactual paths; no fill, quantity or P&L is assigned. "
            "MFE/MAE use the full high/low range of each 5-minute candle through the exit candle; "
            "the intrabar order of the high, low and exit is unknown, so excursions are diagnostic ranges, not executable fills. "
            "This audit does not change any strategy or Champion decisions."
        ),
    }



