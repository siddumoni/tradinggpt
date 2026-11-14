from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src.exit_policy import load_exit_policy
from src.paper_broker import PaperBroker

IST = ZoneInfo("Asia/Kolkata")


def _open_trade(broker: PaperBroker, entry_time: datetime) -> None:
    signal_time = entry_time.replace(minute=entry_time.minute - 5)
    with broker._connect() as db:
        db.execute(
            """INSERT INTO trades(
                signal_id,strategy,strategy_version,symbol,side,status,signal_time,entry_time,
                signal_reference_entry,entry_reference_price,entry_fill_price,stop_loss,target_1,target_2,
                quantity,risk_budget_inr,position_value_inr,planned_net_risk_inr,market_regime,
                entry_reason,created_at
            ) VALUES(?,?,?,?,?,'OPEN',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                f"partial-candle-{entry_time.isoformat()}",
                "vwap_trend",
                "champion_v1",
                "INFY",
                "LONG",
                signal_time.isoformat(),
                entry_time.isoformat(),
                100.0,
                100.0,
                100.05,
                98.0,
                104.0,
                108.0,
                10,
                500.0,
                1000.5,
                25.0,
                "BULLISH",
                "partial-candle regression fixture",
                entry_time.isoformat(),
            ),
        )


def _broker(tmp_path, *, time_exit="23:59"):
    policy = replace(load_exit_policy(), time_exit_ist=time_exit)
    return PaperBroker(tmp_path / "paper.sqlite3", exit_policy=policy)


def test_partial_entry_candle_does_not_award_a_target_hit(tmp_path):
    broker = _broker(tmp_path)
    entry_time = datetime(2026, 11, 5, 10, 2, tzinfo=IST)
    _open_trade(broker, entry_time)

    closed = broker.process_candle(
        "INFY",
        datetime(2026, 11, 5, 10, 0, tzinfo=IST),
        open_price=100.0,
        high=109.0,
        low=99.0,
        close=108.5,
    )

    assert closed == 0
    assert broker.snapshot()["account"]["open_positions"] == 1


def test_partial_entry_candle_stop_uses_stop_level_not_pre_entry_open(tmp_path):
    broker = _broker(tmp_path)
    entry_time = datetime(2026, 11, 5, 10, 2, tzinfo=IST)
    _open_trade(broker, entry_time)

    closed = broker.process_candle(
        "INFY",
        datetime(2026, 11, 5, 10, 0, tzinfo=IST),
        open_price=90.0,
        high=101.0,
        low=97.0,
        close=100.0,
    )

    assert closed == 1
    trade = broker.snapshot()["closed_trades"][0]
    assert trade["exit_reason"] == "STOP_LOSS"
    assert trade["exit_reference_price"] == pytest.approx(98.0)


def test_candle_ending_at_entry_time_is_not_used_for_exit(tmp_path):
    broker = _broker(tmp_path)
    entry_time = datetime(2026, 11, 5, 10, 5, tzinfo=IST)
    _open_trade(broker, entry_time)

    closed = broker.process_candle(
        "INFY",
        datetime(2026, 11, 5, 10, 0, tzinfo=IST),
        open_price=100.0,
        high=109.0,
        low=99.0,
        close=108.5,
    )

    assert closed == 0
    assert broker.snapshot()["account"]["open_positions"] == 1
