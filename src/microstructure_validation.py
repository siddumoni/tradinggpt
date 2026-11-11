"""Research-only forward-return validation for intraday Order Flow estimates."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, time as dtime, timedelta
import math
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

from src.order_flow import OrderFlowError, analyze_order_flow

IST = ZoneInfo("Asia/Kolkata")


def _stamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=IST)
    return parsed.astimezone(IST)


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def evaluate_order_flow_predictive_accuracy(
    events: Iterable[Mapping[str, Any]],
    candles: Iterable[Mapping[str, Any]],
    *,
    min_predictions: int = 100,
    min_sessions: int = 20,
    flat_threshold_bps: float = 5.0,
) -> dict[str, Any]:
    """Compare each 5-minute Order Flow delta sign with the next 5-minute close.

    The prediction uses only snapshots inside the signal bucket. The outcome uses
    the close of the immediately following candle. This is a research diagnostic,
    not a trading signal or a Champion integration.
    """
    if min_predictions < 1 or min_sessions < 1:
        raise ValueError("minimum prediction and session counts must be positive")
    if not math.isfinite(flat_threshold_bps) or flat_threshold_bps < 0:
        raise ValueError("flat_threshold_bps must be finite and non-negative")

    candle_closes: dict[datetime, float] = {}
    for candle in candles:
        stamp = _stamp(candle.get("date") or candle.get("timestamp"))
        close = _finite(candle.get("close"))
        if stamp is not None and close is not None and close > 0:
            candle_closes[stamp] = close

    buckets: dict[tuple[str, datetime], list[Mapping[str, Any]]] = defaultdict(list)
    input_count = 0
    out_of_session_count = 0
    for event in events:
        input_count += 1
        stamp = _stamp(event.get("timestamp") or event.get("exchange_timestamp"))
        if stamp is None:
            continue
        clock = stamp.time().replace(tzinfo=None)
        if not (dtime(9, 15) <= clock <= dtime(15, 30)):
            out_of_session_count += 1
            continue
        bucket_start = stamp.replace(
            minute=(stamp.minute // 5) * 5, second=0, microsecond=0
        )
        buckets[(stamp.date().isoformat(), bucket_start)].append(event)

    predictions = 0
    correct = 0
    flat_outcomes = 0
    evaluated_sessions: set[str] = set()
    per_session: dict[str, dict[str, int]] = defaultdict(lambda: {"predictions": 0, "correct": 0})

    for (session_date, bucket_start), bucket_events in sorted(buckets.items()):
        future_start = bucket_start + timedelta(minutes=5)
        future_close = candle_closes.get(future_start)
        if future_close is None:
            continue
        ordered = sorted(
            bucket_events,
            key=lambda event: _stamp(event.get("timestamp") or event.get("exchange_timestamp"))
            or bucket_start,
        )
        latest = ordered[-1]
        reference_price = _finite(latest.get("last_price", latest.get("price")))
        if reference_price is None or reference_price <= 0:
            continue
        try:
            flow = analyze_order_flow(ordered, depth_levels=5)
        except OrderFlowError:
            continue
        delta = _finite(flow.get("estimated_delta"))
        if delta is None or delta == 0:
            continue
        future_return_bps = (future_close / reference_price - 1.0) * 10_000.0
        if abs(future_return_bps) < flat_threshold_bps:
            flat_outcomes += 1
            continue
        predicted_direction = 1 if delta > 0 else -1
        actual_direction = 1 if future_return_bps > 0 else -1
        predictions += 1
        evaluated_sessions.add(session_date)
        per_session[session_date]["predictions"] += 1
        if predicted_direction == actual_direction:
            correct += 1
            per_session[session_date]["correct"] += 1

    session_count = len(evaluated_sessions)
    accuracy = (correct / predictions * 100.0) if predictions else None
    if not predictions:
        status = "NOT_TESTED"
    elif session_count < min_sessions or predictions < min_predictions:
        status = "INSUFFICIENT_PROSPECTIVE_EVIDENCE"
    else:
        status = "READY_FOR_REVIEW"

    return {
        "status": status,
        "method": "ORDER_FLOW_DELTA_SIGN_VS_NEXT_5M_CLOSE",
        "horizon": "next completed 5-minute candle close after each 5-minute snapshot bucket",
        "input_snapshot_count": input_count,
        "out_of_session_snapshot_count": out_of_session_count,
        "predictions": predictions,
        "correct_predictions": correct,
        "accuracy_pct": accuracy,
        "flat_outcomes_excluded": flat_outcomes,
        "sessions_evaluated": session_count,
        "session_dates": sorted(evaluated_sessions),
        "minimum_predictions_required": min_predictions,
        "minimum_independent_sessions_required": min_sessions,
        "per_session": dict(per_session),
        "limitations": [
            "Kite snapshot aggressor-side volume is estimated and may omit exchange events.",
            "The next-candle close is a diagnostic label, not an executable fill or net-P&L result.",
            "Flat outcomes inside the configured basis-point band are excluded from directional accuracy.",
            "A minimum of 100 matched predictions across 20 independent sessions is required before review.",
            "This evaluator does not alter or feed Champion decisions.",
        ],
    }
