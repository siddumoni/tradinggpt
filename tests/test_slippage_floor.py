import pytest

from src.backtest import BacktestConfig
from src.multi_stock_validation import run_validation
from src.risk_engine import RiskSettings, assess_trade, calculate_net_reward_risk
from src.strategy_backtest import run_slippage_sensitivity, run_yahoo_backtest
from src.paper_broker import PaperBroker
from src.candidate_outcome_tracker import CandidateOutcomeTracker


def test_backtest_config_rejects_slippage_below_floor():
    with pytest.raises(ValueError, match="between 5 and 100"):
        BacktestConfig(slippage_bps_per_side=4.99)


def test_risk_engine_rejects_slippage_below_floor():
    settings = RiskSettings(slippage_bps_per_side=4.99)
    with pytest.raises(ValueError, match="between 5 and 100"):
        assess_trade(
            side="LONG", entry_price=100, stop_loss=99,
            target_1=102, target_2=103, settings=settings,
            estimated_margin_per_share=50,
        )


def test_net_rr_calculator_rejects_slippage_below_floor():
    with pytest.raises(ValueError, match="between 5 and 100"):
        calculate_net_reward_risk(
            side="LONG", entry_price=100, stop_loss=99,
            target_1=102, target_2=103, quantity=1,
            slippage_bps_per_side=4.99,
        )


def test_multi_stock_sensitivity_rejects_slippage_below_floor():
    with pytest.raises(ValueError, match="between 5 and 100"):
        run_validation(
            symbols=("TEST",), strategies=("vwap_trend",),
            slippage_values=(2.0,),
        )


def test_single_strategy_sensitivity_rejects_slippage_below_floor():
    with pytest.raises(ValueError, match="between 5 and 100"):
        run_slippage_sensitivity("TEST", "vwap_trend", slippage_values=(2.0,))



def test_backtest_config_rejects_net_rr_below_one_point_five():
    with pytest.raises(ValueError, match="cannot be below 1.5"):
        BacktestConfig(minimum_net_risk_reward=1.49)


def test_multi_stock_sensitivity_rejects_net_rr_below_one_point_five():
    with pytest.raises(ValueError, match="at least 1.5"):
        run_validation(
            symbols=("TEST",), strategies=("vwap_trend",),
            minimum_net_risk_reward=1.49,
        )


def test_single_strategy_sensitivity_rejects_net_rr_below_one_point_five():
    with pytest.raises(ValueError, match="at least 1.5"):
        run_slippage_sensitivity(
            "TEST", "vwap_trend", minimum_net_risk_reward=1.49,
        )



def test_single_yahoo_backtest_rejects_slippage_below_floor():
    with pytest.raises(ValueError, match="between 5 and 100"):
        run_yahoo_backtest("TEST", "vwap_trend", slippage_bps_per_side=2.0)


def test_paper_broker_rejects_slippage_below_floor(tmp_path):
    with pytest.raises(ValueError, match="between 5 and 100"):
        PaperBroker(db_path=tmp_path / "paper.sqlite3", slippage_bps=4.99)



def test_candidate_shadow_tracker_rejects_slippage_below_floor(tmp_path):
    with pytest.raises(ValueError, match="between 5 and 100"):
        CandidateOutcomeTracker(tmp_path / "candidate.sqlite3", slippage_bps=4.99)
