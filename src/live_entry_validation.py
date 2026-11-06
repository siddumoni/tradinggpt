"""Validate advisory candidates against the latest executable NSE quote.

This module only computes hypothetical entry/risk checks. It never creates,
submits, modifies, or cancels orders.
"""
from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime
from math import isfinite
from typing import Any

from src.cost_calculator import ChargeRates
from src.risk_engine import RiskSettings, assess_trade

from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def inspect_executable_quote(
    quote: Any,
    side: str,
    *,
    now: datetime | None = None,
    max_age_minutes: float = 15.0,
) -> dict[str, Any]:
    """Return best bid/ask and executable entry, failing closed without depth."""
    direction = str(side).strip().upper()
    if direction not in {"LONG", "SHORT"}:
        return {"valid": False, "reason": "Candidate side must be LONG or SHORT."}
    if not isinstance(quote, dict):
        return {"valid": False, "reason": "Latest Kite quote is missing."}

    depth = quote.get("depth")
    buys = depth.get("buy", []) if isinstance(depth, dict) else []
    sells = depth.get("sell", []) if isinstance(depth, dict) else []

    def best_price(levels: Any) -> float | None:
        if not isinstance(levels, list) or not levels or not isinstance(levels[0], dict):
            return None
        try:
            value = float(levels[0].get("price"))
        except (TypeError, ValueError, OverflowError):
            return None
        return value if isfinite(value) and value > 0 else None

    bid = best_price(buys)
    ask = best_price(sells)
    if bid is None or ask is None:
        return {
            "valid": False,
            "bid": bid,
            "ask": ask,
            "reason": "Executable bid/ask depth is unavailable; latest LTP cannot replace an executable quote.",
        }
    if ask < bid:
        return {
            "valid": False,
            "bid": bid,
            "ask": ask,
            "reason": f"Order book is crossed (best bid ₹{bid:.2f} exceeds best ask ₹{ask:.2f}); entry validation failed.",
        }

    timestamp_value = quote.get("timestamp") or quote.get("last_trade_time")
    try:
        if isinstance(timestamp_value, datetime):
            quote_time = timestamp_value
        elif isinstance(timestamp_value, str) and timestamp_value.strip():
            quote_time = datetime.fromisoformat(timestamp_value.strip().replace("Z", "+00:00"))
        else:
            raise ValueError("quote timestamp is missing")
        quote_time = quote_time.replace(tzinfo=IST) if quote_time.tzinfo is None else quote_time.astimezone(IST)
    except (TypeError, ValueError, OverflowError) as exc:
        return {
            "valid": False,
            "bid": bid,
            "ask": ask,
            "reason": f"Executable quote timestamp is invalid: {exc}.",
        }

    quote_age_seconds: float | None = None
    if now is not None:
        current = now.replace(tzinfo=IST) if now.tzinfo is None else now.astimezone(IST)
        quote_age_seconds = (current - quote_time).total_seconds()
        if quote_age_seconds < -60:
            return {
                "valid": False,
                "bid": bid,
                "ask": ask,
                "quote_timestamp": quote_time.isoformat(),
                "quote_age_seconds": round(quote_age_seconds, 2),
                "reason": f"Executable quote timestamp is {abs(quote_age_seconds):.1f}s in the future.",
            }
        if quote_age_seconds > max_age_minutes * 60:
            return {
                "valid": False,
                "bid": bid,
                "ask": ask,
                "quote_timestamp": quote_time.isoformat(),
                "quote_age_seconds": round(quote_age_seconds, 2),
                "reason": f"Executable quote is stale ({quote_age_seconds:.1f}s old; limit {max_age_minutes * 60:.0f}s).",
            }

    mid = (bid + ask) / 2.0
    return {
        "valid": True,
        "side": direction,
        "bid": bid,
        "ask": ask,
        "executable_entry_price": ask if direction == "LONG" else bid,
        "entry_price_source": "best_ask" if direction == "LONG" else "best_bid",
        "spread_bps": ((ask - bid) / mid * 10_000.0) if mid > 0 else None,
        "quote_timestamp": quote_time.isoformat(),
        "quote_age_seconds": round(quote_age_seconds, 2) if quote_age_seconds is not None else None,
    }


def revalidate_candidate_at_quote(
    candidate: Any,
    quote: Any,
    *,
    margin_per_share: float | None,
    settings: RiskSettings,
    charge_rates: ChargeRates,
    quote_verified: bool,
    now: datetime,
    max_quote_age_minutes: float = 15.0,
) -> Any:
    """Recompute position size, costs and net R:R at the latest executable quote."""
    if (
        not getattr(candidate, "side", None)
        or not getattr(candidate, "reference_entry", None)
        or not getattr(candidate, "stop_loss", None)
        or not getattr(candidate, "target_1", None)
        or not getattr(candidate, "target_2", None)
    ):
        return candidate

    reference_entry = float(candidate.reference_entry)
    details = inspect_executable_quote(
        quote,
        candidate.side,
        now=now,
        max_age_minutes=max_quote_age_minutes,
    )
    prior_validation = dict(getattr(candidate, "entry_validation", {}) or {})
    reasons = [
        reason for reason in candidate.reasons
        if not any(token in reason for token in (
            "Margin estimate unavailable",
            "Margin estimate invalid",
            "Rejected:",
            "Accepted by configured risk",
            "Quantity is zero",
            "No quantity fits",
            "Estimated required margin",
            "Conditional advisory only:",
            "Not approved for entry.",
        ))
    ]

    def failed(status: str, reason: str, *, verified: bool | None = None) -> Any:
        reasons.append(reason)
        validation = {
            **prior_validation,
            **details,
            "status": status,
            "reason": reason,
            "reference_entry": round(reference_entry, 4),
            "executable_entry_price": details.get("executable_entry_price"),
            "price_drift_pct": None,
            "margin_per_share_inr": margin_per_share,
            "risk_breakdown": None,
        }
        return replace(
            candidate,
            action="WATCH_CANDIDATE",
            quantity=0,
            estimated_net_risk_inr=None,
            estimated_net_reward_t1_inr=None,
            net_rr_t1=None,
            margin_verified=bool(margin_per_share is not None and isfinite(float(margin_per_share)) and margin_per_share > 0),
            data_verified_live=(candidate.data_verified_live if verified is None else verified),
            reasons=reasons,
            entry_validation=validation,
        )

    if not quote_verified:
        return failed(
            "REJECTED_UNVERIFIED_QUOTE",
            "Latest quote failed live freshness verification; directional setup remains watch-only.",
            verified=False,
        )
    if not details.get("valid"):
        return failed(
            "REJECTED_EXECUTABLE_QUOTE",
            str(details.get("reason") or "Executable bid/ask quote is unavailable."),
        )

    entry_price = float(details["executable_entry_price"])
    drift_pct = ((entry_price / reference_entry) - 1.0) * 100.0
    stop = float(candidate.stop_loss)
    target_1 = float(candidate.target_1)
    target_2 = float(candidate.target_2)

    levels_valid = (
        stop < entry_price < target_1 <= target_2
        if candidate.side == "LONG"
        else target_2 <= target_1 < entry_price < stop
    )
    summary = (
        f"Live {details['entry_price_source'].replace('_', ' ')} ₹{entry_price:.2f}; "
        f"candle reference ₹{reference_entry:.2f}; price drift {drift_pct:+.2f}%."
    )
    reasons.append(summary)
    if not levels_valid:
        return failed(
            "REJECTED_INVALIDATED_SETUP",
            f"Current executable entry ₹{entry_price:.2f} is outside the original stop/target levels; the candle-based setup is invalidated.",
        )

    try:
        assessment = assess_trade(
            side=candidate.side,
            entry_price=entry_price,
            stop_loss=stop,
            target_1=target_1,
            target_2=target_2,
            settings=settings,
            estimated_margin_per_share=margin_per_share,
            charge_rates=charge_rates,
        )
    except (TypeError, ValueError, OverflowError) as exc:
        return failed("REJECTED_RISK_CALCULATION", f"Live-entry risk calculation failed closed: {exc}.")

    margin_verified = (
        margin_per_share is not None
        and isfinite(float(margin_per_share))
        and float(margin_per_share) > 0
    )
    non_price_gates_pass = (
        candidate.data_verified_live
        and candidate.news_status not in ("NOT_CHECKED", "UNAVAILABLE")
        and candidate.company_specific_risk != "HIGH"
    )
    risk_breakdown = {
        "assessment_accepted": assessment.accepted,
        "proposed_quantity": assessment.quantity,
        "approved_quantity": assessment.quantity if assessment.accepted and non_price_gates_pass else 0,
        "estimated_margin_required_inr": round(assessment.estimated_margin_inr, 2)
            if assessment.estimated_margin_inr is not None else None,
        "gross_risk_inr": round(assessment.gross_risk_inr, 2),
        "net_risk_inr": round(assessment.net_risk_inr, 2),
        "gross_reward_t1_inr": round(assessment.gross_reward_t1_inr, 2),
        "gross_reward_t2_inr": round(assessment.gross_reward_t2_inr, 2),
        "net_reward_t1_inr": round(assessment.net_reward_t1_inr, 2),
        "net_reward_t2_inr": round(assessment.net_reward_t2_inr, 2),
        "net_rr_t1": round(assessment.net_rr_t1, 4),
        "net_rr_t2": round(assessment.net_rr_t2, 4),
        "slippage_at_stop_inr": round(assessment.slippage_at_stop_inr, 2),
        "slippage_at_t1_inr": round(assessment.slippage_at_t1_inr, 2),
        "slippage_at_t2_inr": round(assessment.slippage_at_t2_inr, 2),
        "costs_at_stop": asdict(assessment.costs_at_stop) if assessment.costs_at_stop else None,
        "costs_at_t1": asdict(assessment.costs_at_t1) if assessment.costs_at_t1 else None,
        "costs_at_t2": asdict(assessment.costs_at_t2) if assessment.costs_at_t2 else None,
    }

    approved = assessment.accepted and non_price_gates_pass
    if approved:
        status = "REVALIDATED"
        action = "CONDITIONAL_BUY" if candidate.side == "LONG" else "CONDITIONAL_SHORT"
        reason = (
            f"Live entry revalidated at ₹{entry_price:.2f}; quantity {assessment.quantity}, "
            f"net R:R to T1 {assessment.net_rr_t1:.2f} after estimated charges and slippage."
        )
    else:
        action = "WATCH_CANDIDATE"
        if not assessment.accepted:
            status = (
                "REJECTED_NET_RR"
                if "net R:R" in assessment.reason
                else "REJECTED_RISK_GATE"
            )
            reason = assessment.reason
        else:
            status = "REJECTED_NON_PRICE_GATE"
            reason = "Live price/risk checks passed, but a separate data or news gate remains unresolved."

    reasons.append(reason)
    validation = {
        **prior_validation,
        **details,
        "status": status,
        "reason": reason,
        "reference_entry": round(reference_entry, 4),
        "executable_entry_price": round(entry_price, 4),
        "price_drift_pct": round(drift_pct, 4),
        "margin_per_share_inr": round(float(margin_per_share), 4) if margin_verified else None,
        "risk_breakdown": risk_breakdown,
    }
    has_costs = assessment.costs_at_stop is not None
    return replace(
        candidate,
        action=action,
        quantity=assessment.quantity if approved else 0,
        estimated_net_risk_inr=round(assessment.net_risk_inr, 2) if has_costs else None,
        estimated_net_reward_t1_inr=round(assessment.net_reward_t1_inr, 2) if has_costs else None,
        net_rr_t1=round(assessment.net_rr_t1, 2) if has_costs else None,
        margin_verified=margin_verified,
        reasons=reasons,
        entry_validation=validation,
    )
