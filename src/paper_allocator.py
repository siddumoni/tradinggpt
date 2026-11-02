"""Quality-first ranking for shared-capital paper portfolio entries.

The score is a deterministic research heuristic, not a profitability forecast:
35% scanner confidence, 35% after-cost net R:R, 15% quoted spread quality,
10% stop-distance-to-spread robustness, and 5% company/news risk quality.
The broker still revalidates every candidate and enforces cash, per-trade,
portfolio-risk, daily-loss, and open-position limits before opening it.
"""
from __future__ import annotations

import math
from typing import Any


def _number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _risk_quality(value: Any) -> float:
    risk = str(value or "UNKNOWN").strip().upper()
    return {"LOW": 100.0, "MEDIUM": 50.0, "HIGH": 0.0}.get(risk, 70.0)


def score_paper_candidate(
    candidate: dict[str, Any], snapshot: dict[str, Any] | None
) -> dict[str, Any]:
    """Return an auditable quality score and its component measurements."""
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    validation = candidate.get("entry_validation")
    validation = validation if isinstance(validation, dict) else {}
    breakdown = validation.get("risk_breakdown")
    breakdown = breakdown if isinstance(breakdown, dict) else {}

    confidence = _clamp(_number(candidate.get("confidence")))
    net_rr = _number(
        breakdown.get("net_rr_t1"),
        _number(candidate.get("net_rr_t1")),
    )
    rr_quality = _clamp(max(0.0, net_rr) / 3.0 * 100.0)

    bid = _number(snapshot.get("bid"))
    ask = _number(snapshot.get("ask"))
    quote_valid = bid > 0 and ask > 0 and ask >= bid
    if quote_valid:
        midpoint = (bid + ask) / 2.0
        spread_bps = ((ask - bid) / midpoint * 10_000.0) if midpoint > 0 else math.inf
        spread_quality = _clamp(100.0 - spread_bps * 5.0)
        side = str(candidate.get("side") or "").upper()
        entry = ask if side == "LONG" else bid if side == "SHORT" else 0.0
        stop = _number(candidate.get("stop_loss"))
        spread = ask - bid
        stop_distance = abs(entry - stop) if entry > 0 and stop > 0 else 0.0
        stop_spread_ratio = (
            stop_distance / spread if spread > 0 and stop_distance > 0
            else 4.0 if spread == 0 and stop_distance > 0
            else 0.0
        )
        stop_quality = _clamp((stop_spread_ratio - 1.0) / 3.0 * 100.0)
    else:
        spread_bps = math.inf
        spread_quality = 0.0
        stop_spread_ratio = 0.0
        stop_quality = 0.0

    company_quality = _risk_quality(candidate.get("company_specific_risk", candidate.get("news_risk")))
    market_quality = _risk_quality(candidate.get("market_context_risk"))
    risk_quality = (company_quality + market_quality) / 2.0

    score = (
        confidence * 0.35
        + rr_quality * 0.35
        + spread_quality * 0.15
        + stop_quality * 0.10
        + risk_quality * 0.05
    )
    return {
        "allocation_score": round(score, 4),
        "confidence": confidence,
        "net_rr_t1": net_rr,
        "spread_bps": round(spread_bps, 4) if math.isfinite(spread_bps) else None,
        "stop_distance_to_spread": round(stop_spread_ratio, 4),
        "components": {
            "confidence": round(confidence, 2),
            "after_cost_net_rr": round(rr_quality, 2),
            "spread_quality": round(spread_quality, 2),
            "stop_robustness": round(stop_quality, 2),
            "news_and_market_risk": round(risk_quality, 2),
        },
    }


def rank_paper_candidates(
    candidates: list[dict[str, Any]], snapshots: dict[str, Any]
) -> list[dict[str, Any]]:
    """Rank conditional candidates deterministically before shared allocation."""
    ranked: list[dict[str, Any]] = []
    for candidate in candidates:
        symbol = str(candidate.get("symbol") or "").strip().upper()
        metrics = score_paper_candidate(candidate, snapshots.get(symbol))
        ranked.append({
            "candidate": candidate,
            "symbol": symbol,
            **metrics,
        })

    ranked.sort(
        key=lambda item: (
            -item["allocation_score"],
            -item["confidence"],
            -item["net_rr_t1"],
            item["spread_bps"] if item["spread_bps"] is not None else math.inf,
            item["symbol"],
            str(item["candidate"].get("signal_time") or ""),
        )
    )
    for rank, item in enumerate(ranked, start=1):
        item["allocation_rank"] = rank
    return ranked
