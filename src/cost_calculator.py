
"""Approximate Zerodha NSE equity-intraday transaction costs.

Rates are configurable because exchange and statutory tariffs can change.
Actual contract notes remain authoritative. This module contains no
order-execution code.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from math import isfinite
from typing import Literal

TradeSide = Literal["LONG", "SHORT"]


@dataclass(frozen=True)
class ChargeRates:
    """Rates as fractions of turnover, e.g. 0.00025 means 0.025%."""

    brokerage_rate: Decimal = Decimal("0.0003")
    brokerage_cap_per_order: Decimal = Decimal("20")
    stt_sell_rate: Decimal = Decimal("0.00025")
    nse_transaction_rate: Decimal = Decimal("0.0000307")
    sebi_rate: Decimal = Decimal("0.000001")
    gst_rate: Decimal = Decimal("0.18")
    stamp_buy_rate: Decimal = Decimal("0.00003")
    ipft_rate: Decimal = Decimal("0.000000001")  # ₹0.01 per crore of turnover

    def __post_init__(self) -> None:
        """Normalize rate inputs and reject invalid tariff configuration."""
        rate_fields = (
            "brokerage_rate",
            "stt_sell_rate",
            "nse_transaction_rate",
            "sebi_rate",
            "gst_rate",
            "stamp_buy_rate",
            "ipft_rate",
        )
        for field_name in rate_fields:
            try:
                value = Decimal(str(getattr(self, field_name)))
            except Exception as exc:
                raise ValueError(f"{field_name} must be a finite non-negative decimal") from exc
            if not value.is_finite() or value < 0:
                raise ValueError(f"{field_name} must be a finite non-negative decimal")
            object.__setattr__(self, field_name, value)

        try:
            cap = Decimal(str(self.brokerage_cap_per_order))
        except Exception as exc:
            raise ValueError("brokerage_cap_per_order must be a finite positive decimal") from exc
        if not cap.is_finite() or cap <= 0:
            raise ValueError("brokerage_cap_per_order must be a finite positive decimal")
        object.__setattr__(self, "brokerage_cap_per_order", cap)


@dataclass(frozen=True)
class CostBreakdown:
    brokerage: float
    stt: float
    exchange_charges: float
    sebi_charges: float
    gst: float
    stamp_duty: float
    ipft: float
    total: float
    buy_turnover: float
    sell_turnover: float


def _money(value: Decimal) -> Decimal:
    """Round a charge to paise."""
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _stt_money(value: Decimal) -> Decimal:
    """Round STT to the nearest whole rupee."""
    return value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def estimate_intraday_round_trip_cost(
    *,
    side: TradeSide,
    entry_price: float,
    exit_price: float,
    quantity: int,
    rates: ChargeRates = ChargeRates(),
) -> CostBreakdown:
    """Estimate charges for one entry and one exit in NSE equity MIS.

    LONG: buy at entry, sell at exit.
    SHORT: sell at entry, buy at exit.

    Assumes one executed entry order and one executed exit order.
    Partial fills or multiple exit orders can increase brokerage.
    """
    if side not in ("LONG", "SHORT"):
        raise ValueError("side must be 'LONG' or 'SHORT'")
    try:
        entry_float = float(entry_price)
        exit_float = float(exit_price)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("entry_price and exit_price must be finite and positive") from exc
    if not isfinite(entry_float) or not isfinite(exit_float) or entry_float <= 0 or exit_float <= 0:
        raise ValueError("entry_price and exit_price must be finite and positive")
    if isinstance(quantity, bool):
        raise ValueError("quantity must be a positive integer")
    try:
        quantity_float = float(quantity)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("quantity must be a positive integer") from exc
    if not isfinite(quantity_float) or quantity_float < 1 or not quantity_float.is_integer():
        raise ValueError("quantity must be a positive integer")

    entry = Decimal(str(entry_price))
    exit_ = Decimal(str(exit_price))
    qty = Decimal(int(quantity_float))

    entry_turnover = entry * qty
    exit_turnover = exit_ * qty

    if side == "LONG":
        buy_turnover = entry_turnover
        sell_turnover = exit_turnover
    else:
        buy_turnover = exit_turnover
        sell_turnover = entry_turnover

    brokerage = (
        min(
            entry_turnover * rates.brokerage_rate,
            rates.brokerage_cap_per_order,
        )
        + min(
            exit_turnover * rates.brokerage_rate,
            rates.brokerage_cap_per_order,
        )
    )

    stt = sell_turnover * rates.stt_sell_rate
    exchange = (entry_turnover + exit_turnover) * rates.nse_transaction_rate
    sebi = (entry_turnover + exit_turnover) * rates.sebi_rate
    gst = (brokerage + exchange + sebi + (entry_turnover + exit_turnover) * rates.ipft_rate) * rates.gst_rate
    stamp = buy_turnover * rates.stamp_buy_rate
    ipft = (entry_turnover + exit_turnover) * rates.ipft_rate

    rounded = {
        "brokerage": _money(brokerage),
        "stt": _stt_money(stt),
        "exchange_charges": _money(exchange),
        "sebi_charges": _money(sebi),
        "gst": _money(gst),
        "stamp_duty": _money(stamp),
        "ipft": _money(ipft),
    }

    total = sum(rounded.values(), Decimal("0.00"))

    return CostBreakdown(
        **{name: float(value) for name, value in rounded.items()},
        total=float(total),
        buy_turnover=float(buy_turnover),
        sell_turnover=float(sell_turnover),
    )
