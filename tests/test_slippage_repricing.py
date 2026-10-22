import math

import pandas as pd
import pytest

from src.cost_calculator import ChargeRates
from src.slippage_repricing import (
    parse_slippage_values,
    reprice_trade_set,
    summarize_repricing,
)


def sample_trades():
    return pd.DataFrame([
        {
            "symbol": "INFY", "side": "LONG",
            "entry_time": "2026-10-12 10:00:00+05:30",
            "exit_time": "2026-10-12 10:10:00+05:30",
            "entry": 100.0, "exit": 102.0, "quantity": 10,
            "gross_pnl": 20.0, "exit_reason": "TARGET",
        },
        {
            "symbol": "INFY", "side": "SHORT",
            "entry_time": "2026-10-12 11:00:00+05:30",
            "exit_time": "2026-10-12 11:05:00+05:30",
            "entry": 100.0, "exit": 101.0, "quantity": 5,
            "gross_pnl": -5.0, "exit_reason": "STOP_LOSS",
        },
    ])


def test_parse_slippage_values_accepts_ordered_scenarios():
    assert parse_slippage_values("2, 5,8") == (2.0, 5.0, 8.0)


@pytest.mark.parametrize("value", ["", "abc", "2,2", "-1", "101", "nan", "inf"])
def test_parse_slippage_values_rejects_invalid_values(value):
    with pytest.raises(ValueError):
        parse_slippage_values(value)


def test_repricing_freezes_trade_membership_and_changes_only_costs():
    trades = sample_trades()
    result = reprice_trade_set(
        trades, slippage_values=(2.0, 8.0),
        charge_rates=ChargeRates(), source_label="baseline_trades.csv",
    )

    assert len(result) == len(trades) * 2
    assert set(result["trade_id"]) == {1, 2}
    counts = result.groupby("slippage_bps_per_side")["trade_id"].nunique().to_dict()
    assert counts == {2.0: 2, 8.0: 2}

    by_trade = result.pivot(index="trade_id", columns="slippage_bps_per_side", values="net_pnl")
    assert (by_trade[8.0] < by_trade[2.0]).all()
    assert (result["gross_pnl"].iloc[0::2].to_numpy() == result["gross_pnl"].iloc[1::2].to_numpy()).all()


def test_repricing_reconciles_each_trade():
    result = reprice_trade_set(
        sample_trades(), slippage_values=(5.0,), charge_rates=ChargeRates(),
    )
    assert (result["gross_pnl"] - result["total_estimated_costs"] - result["net_pnl"]).abs().max() < 1e-9
    assert (result["transaction_charges"] + result["slippage_cost"] - result["total_estimated_costs"]).abs().max() < 1e-9


def test_repricing_rejects_mismatched_gross_pnl():
    trades = sample_trades()
    trades.loc[0, "gross_pnl"] = 999
    with pytest.raises(ValueError, match="does not reconcile"):
        reprice_trade_set(trades, slippage_values=(2,), charge_rates=ChargeRates())


def test_repricing_rejects_invalid_quantity_and_side():
    trades = sample_trades()
    trades["quantity"] = trades["quantity"].astype(float)
    trades.loc[0, "quantity"] = 1.5
    with pytest.raises(ValueError, match="quantity"):
        reprice_trade_set(trades, slippage_values=(2,), charge_rates=ChargeRates())

    trades = sample_trades()
    trades.loc[0, "side"] = "BUY"
    with pytest.raises(ValueError, match="side"):
        reprice_trade_set(trades, slippage_values=(2,), charge_rates=ChargeRates())


def test_summary_keeps_each_source_file_separate_and_reports_drawdown():
    trades = sample_trades()
    details = reprice_trade_set(
        trades, slippage_values=(2, 8), charge_rates=ChargeRates(),
        source_label="INFY_baseline_trades.csv",
    )
    summary = summarize_repricing(details, slippage_values=(2, 8), initial_capital=20_000)
    assert len(summary) == 2
    assert summary["frozen_trade_count"].tolist() == [2, 2]
    assert summary["max_drawdown"].ge(0).all()
    assert summary["ending_equity"].iloc[1] < summary["ending_equity"].iloc[0]
