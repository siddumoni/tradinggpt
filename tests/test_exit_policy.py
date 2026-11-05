from dataclasses import replace
from datetime import time

import pytest

from src.exit_policy import ExitPolicy, evaluate_ohlc_exit, load_exit_policy, plan_policy_levels


def test_configured_exit_policy_is_versioned_and_keeps_t1_non_executing():
    policy = load_exit_policy()
    assert policy.version == "nse_intraday_exit_v1"
    assert policy.stop_atr_multiple == 1.0
    assert policy.target_1_atr_multiple == 2.0
    assert policy.t1_action == "none"
    assert policy.target_2_atr_multiple == 2.5
    assert policy.time_exit_ist == "15:15"
    assert policy.gate_target == "target_2"


def test_policy_levels_are_shared_for_long_and_short():
    policy = load_exit_policy()
    long = plan_policy_levels("LONG", entry=100.0, atr=2.0, policy=policy)
    short = plan_policy_levels("SHORT", entry=100.0, atr=2.0, policy=policy)
    assert (long.stop_loss, long.target_1, long.target_2) == pytest.approx((98.0, 104.0, 105.0))
    assert (short.stop_loss, short.target_1, short.target_2) == pytest.approx((102.0, 96.0, 95.0))


def test_stop_wins_when_stop_and_target_touch_same_candle_and_gap_is_adverse():
    policy = load_exit_policy()
    result = evaluate_ohlc_exit(
        side="LONG", stop_loss=98.0, target_2=105.0,
        open_price=97.0, high=106.0, low=96.0, close=100.0,
        bar_end_time=time(10, 5), policy=policy,
    )
    assert result is not None
    assert result.reason == "STOP_LOSS"
    assert result.reference_price == 97.0


def test_target_exit_uses_target_price_without_favorable_gap_credit():
    result = evaluate_ohlc_exit(
        side="LONG", stop_loss=98.0, target_2=105.0,
        open_price=106.0, high=107.0, low=105.5, close=106.5,
        bar_end_time=time(10, 5),
    )
    assert result is not None
    assert result.reason == "TARGET_2"
    assert result.reference_price == 105.0


def test_time_exit_uses_configured_candle_close():
    result = evaluate_ohlc_exit(
        side="SHORT", stop_loss=102.0, target_2=95.0,
        open_price=100.0, high=101.0, low=99.0, close=100.5,
        bar_end_time=time(15, 15),
    )
    assert result is not None
    assert result.reason == "TIME_SQUARE_OFF"
    assert result.reference_price == 100.5


def test_partial_t1_action_is_rejected_until_implemented():
    with pytest.raises(ValueError, match="Only t1_action='none'"):
        replace(load_exit_policy(), t1_action="partial_50")  # type: ignore[arg-type]


def test_invalid_ohlc_is_rejected():
    with pytest.raises(ValueError, match="Invalid OHLC"):
        evaluate_ohlc_exit(
            side="LONG", stop_loss=98.0, target_2=105.0,
            open_price=100.0, high=99.0, low=101.0, close=100.0,
            bar_end_time=time(10, 5),
        )
