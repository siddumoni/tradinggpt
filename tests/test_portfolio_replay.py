import pandas as pd
import pytest

from src.cost_calculator import ChargeRates
from src.portfolio_replay import simulate_portfolio


def trade_row(symbol, entry_time, exit_time, entry, exit_price, stop_loss, side="LONG", quantity=10):
    gross_pnl = (exit_price - entry) * quantity if side == "LONG" else (entry - exit_price) * quantity
    return {
        "symbol": symbol,
        "side": side,
        "entry_time": entry_time,
        "exit_time": exit_time,
        "entry": entry,
        "exit": exit_price,
        "stop_loss": stop_loss,
        "target": entry + 5 if side == "LONG" else entry - 5,
        "quantity": quantity,
        "gross_pnl": gross_pnl,
        "exit_reason": "TARGET" if gross_pnl > 0 else "STOP_LOSS",
    }


def write_log(path, rows):
    pd.DataFrame(rows).to_csv(path, index=False)
    return path


def simulate(paths, **overrides):
    defaults = {
        "initial_capital": 20_000,
        "risk_per_trade": 200,
        "slippage_bps_per_side": 5,
        "max_concurrent_positions": 3,
        "charge_rates": ChargeRates(),
    }
    defaults.update(overrides)
    return simulate_portfolio(paths, **defaults)


def test_portfolio_shares_capital_and_resizes_overlapping_entries(tmp_path):
    one = write_log(tmp_path / "INFY_trades.csv", [
        trade_row("INFY", "2026-10-12 10:00:00+05:30", "2026-10-12 10:30:00+05:30",
                  1000, 1010, 990, quantity=10),
    ])
    two = write_log(tmp_path / "RELIANCE_trades.csv", [
        trade_row("RELIANCE", "2026-10-12 10:05:00+05:30", "2026-10-12 10:20:00+05:30",
                  1500, 1490, 1510, side="SHORT", quantity=10),
    ])

    summary, details = simulate([one, two])

    assert summary["candidate_trades"] == 2
    assert summary["accepted_trades"] == 2
    assert details["quantity"].tolist() == [10, 6]
    assert details["reserved_capital"].iloc[1] <= details["available_capital_at_entry"].iloc[1]
    assert summary["ending_equity"] == pytest.approx(
        summary["initial_capital"] + summary["net_pnl"]
    )


def test_portfolio_rejects_concurrent_same_symbol_and_accepts_after_exit(tmp_path):
    path = write_log(tmp_path / "INFY_trades.csv", [
        trade_row("INFY", "2026-10-12 10:00:00+05:30", "2026-10-12 10:30:00+05:30",
                  1000, 1010, 990, quantity=1),
        trade_row("INFY", "2026-10-12 10:15:00+05:30", "2026-10-12 10:25:00+05:30",
                  1000, 990, 990, quantity=1),
        trade_row("INFY", "2026-10-12 10:30:00+05:30", "2026-10-12 10:40:00+05:30",
                  1000, 1010, 990, quantity=1),
    ])

    summary, details = simulate([path])

    assert details["accepted"].tolist() == [True, False, True]
    assert details["rejection_reason"].iloc[1] == "same_symbol_position_already_open"
    assert summary["same_symbol_conflicts"] == 1


def test_portfolio_enforces_max_concurrent_position_limit(tmp_path):
    paths = []
    for symbol, start in (("AAA", "10:00"), ("BBB", "10:01"), ("CCC", "10:02")):
        paths.append(write_log(tmp_path / f"{symbol}_trades.csv", [
            trade_row(symbol, f"2026-10-12 {start}:00+05:30", "2026-10-12 10:30:00+05:30",
                      100, 101, 99, quantity=2),
        ]))

    summary, details = simulate(paths, max_concurrent_positions=1)

    assert summary["accepted_trades"] == 1
    assert summary["rejected_trades"] == 2
    assert "max_concurrent_positions_reached" in set(details["rejection_reason"])


def test_portfolio_releases_reserved_capital_after_exit(tmp_path):
    path = write_log(tmp_path / "INFY_trades.csv", [
        trade_row("INFY", "2026-10-12 10:00:00+05:30", "2026-10-12 10:30:00+05:30",
                  1000, 1001, 990, quantity=20),
        trade_row("INFY", "2026-10-12 10:30:00+05:30", "2026-10-12 11:00:00+05:30",
                  1000, 1001, 990, quantity=20),
    ])

    summary, details = simulate([path])

    assert details["accepted"].tolist() == [True, True]
    assert summary["accepted_trades"] == 2


def test_portfolio_rejects_gross_pnl_mismatch(tmp_path):
    row = trade_row("INFY", "2026-10-12 10:00:00+05:30", "2026-10-12 10:30:00+05:30",
                    100, 101, 99, quantity=1)
    row["gross_pnl"] = 500
    path = write_log(tmp_path / "INFY_trades.csv", [row])

    with pytest.raises(ValueError, match="gross P&L mismatch"):
        simulate([path])


def test_portfolio_rejects_exit_before_entry(tmp_path):
    path = write_log(tmp_path / "INFY_trades.csv", [
        trade_row("INFY", "2026-10-12 10:30:00+05:30", "2026-10-12 10:15:00+05:30",
                  100, 101, 99, quantity=1),
    ])

    with pytest.raises(ValueError, match="exit_time precedes entry_time"):
        simulate([path])
