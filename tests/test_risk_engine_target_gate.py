from __future__ import annotations

from src.risk_engine import RiskSettings, assess_trade



def test_target_2_net_rr_gate_can_match_single_target_backtest():
    default_result = assess_trade(
        side="LONG", entry_price=100, stop_loss=99,
        target_1=100.2, target_2=104, estimated_margin_per_share=50,
    )
    target_2_result = assess_trade(
        side="LONG", entry_price=100, stop_loss=99,
        target_1=100.2, target_2=104,
        settings=RiskSettings(minimum_net_rr_target="target_2"),
        estimated_margin_per_share=50,
    )

    assert not default_result.accepted
    assert "Target 1 net R:R is below" in default_result.reason
    assert target_2_result.accepted
    assert target_2_result.net_rr_t2 >= 1.5
