from decimal import Decimal

from __future__ import annotations

import pytest

from src.cost_calculator import ChargeRates, estimate_intraday_round_trip_cost


def test_round_trip_costs_are_positive_and_include_required_heads() -> None:
    result = estimate_intraday_round_trip_cost(
        side="LONG",
        entry_price=100.0,
        exit_price=102.0,
        quantity=100,
    )

    assert result.total > 0
    assert result.brokerage > 0
    assert result.stt == 3.0
    assert result.exchange_charges > 0
    assert result.sebi_charges >= 0
    assert result.gst > 0
    assert result.stamp_duty > 0
    assert result.buy_turnover == pytest.approx(10_000.0)
    assert result.sell_turnover == pytest.approx(10_200.0)


def test_stt_rounds_to_nearest_rupee() -> None:
    # Sell turnover = ₹10,200; STT = ₹2.55, rounded to ₹3.
    result = estimate_intraday_round_trip_cost(
        side="LONG",
        entry_price=100.0,
        exit_price=102.0,
        quantity=100,
    )
    assert result.stt == 3.0


def test_short_trade_applies_buy_stamp_duty_to_exit_turnover() -> None:
    result = estimate_intraday_round_trip_cost(
        side="SHORT",
        entry_price=100.0,
        exit_price=95.0,
        quantity=2,
    )

    assert result.buy_turnover == pytest.approx(190.0)
    assert result.sell_turnover == pytest.approx(200.0)


@pytest.mark.parametrize(
    "kwargs",
    [
        {
            "side": "BUY",
            "entry_price": 100,
            "exit_price": 101,
            "quantity": 1,
        },
        {
            "side": "LONG",
            "entry_price": 0,
            "exit_price": 101,
            "quantity": 1,
        },
        {
            "side": "LONG",
            "entry_price": 100,
            "exit_price": 101,
            "quantity": 0,
        },
    ],
)
def test_invalid_cost_inputs_raise(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        estimate_intraday_round_trip_cost(**kwargs)



@pytest.mark.parametrize("rate", [Decimal("-0.1"), Decimal("NaN"), float("inf")])
def test_invalid_charge_rate_configuration_is_rejected(rate):
    with pytest.raises(ValueError, match="brokerage_rate"):
        ChargeRates(brokerage_rate=rate)


@pytest.mark.parametrize(
    "entry,exit_price,quantity",
    [
        (float("nan"), 100.0, 1),
        (100.0, float("inf"), 1),
        (100.0, 101.0, 1.5),
        (100.0, 101.0, float("nan")),
    ],
)
def test_non_finite_prices_and_fractional_quantity_are_rejected(entry, exit_price, quantity):
    with pytest.raises(ValueError):
        estimate_intraday_round_trip_cost(
            side="LONG",
            entry_price=entry,
            exit_price=exit_price,
            quantity=quantity,
        )
