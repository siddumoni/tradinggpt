from __future__ import annotations

import pytest

from src.risk_engine import RiskSettings, assess_trade, load_phase2_settings


def test_missing_margin_fails_closed() -> None:
    result = assess_trade(side="LONG", entry_price=100, stop_loss=99,
        target_1=102, target_2=103, estimated_margin_per_share=None)
    assert not result.accepted and result.quantity == 0
    assert "Margin estimate unavailable" in result.reason


def test_cash_notional_caps_quantity_even_when_margin_is_low() -> None:
    settings = RiskSettings(capital_inr=1_000, risk_per_trade_inr=100)
    result = assess_trade(side="LONG", entry_price=100, stop_loss=90,
        target_1=120, target_2=130, settings=settings,
        estimated_margin_per_share=1)
    assert result.quantity > 0
    assert result.quantity * result.entry_price <= settings.capital_inr
    assert result.estimated_margin_inr <= settings.capital_inr


def test_net_risk_includes_charges_and_slippage() -> None:
    settings = RiskSettings(capital_inr=20_000, risk_per_trade_inr=100,
        slippage_bps_per_side=5)
    result = assess_trade(side="LONG", entry_price=100, stop_loss=99,
        target_1=103, target_2=104, settings=settings,
        estimated_margin_per_share=50)
    assert result.accepted
    assert result.net_risk_inr == pytest.approx(
        result.gross_risk_inr + result.costs_at_stop.total + result.slippage_at_stop_inr
    )
    assert result.net_risk_inr <= settings.risk_per_trade_inr
    assert result.slippage_at_stop_inr > 0


def test_net_rewards_subtract_costs_and_slippage() -> None:
    result = assess_trade(side="LONG", entry_price=100, stop_loss=99,
        target_1=103, target_2=104, estimated_margin_per_share=50)
    assert result.net_reward_t1_inr == pytest.approx(
        result.gross_reward_t1_inr - result.costs_at_t1.total - result.slippage_at_t1_inr
    )
    assert result.net_reward_t2_inr == pytest.approx(
        result.gross_reward_t2_inr - result.costs_at_t2.total - result.slippage_at_t2_inr
    )
    assert result.net_rr_t2 >= result.net_rr_t1


def test_short_trade_risk_and_slippage_are_bounded() -> None:
    result = assess_trade(side="SHORT", entry_price=100, stop_loss=101,
        target_1=97, target_2=96, estimated_margin_per_share=50)
    assert result.accepted
    assert result.net_risk_inr <= RiskSettings().risk_per_trade_inr
    assert result.slippage_at_stop_inr > 0


def test_invalid_slippage_rejected() -> None:
    settings = RiskSettings(slippage_bps_per_side=-1)
    with pytest.raises(ValueError, match="slippage_bps_per_side"):
        assess_trade(side="LONG", entry_price=100, stop_loss=99,
            target_1=102, target_2=103, settings=settings,
            estimated_margin_per_share=50)


def test_invalid_level_order_is_rejected() -> None:
    with pytest.raises(ValueError, match="invalid price levels"):
        assess_trade(side="LONG", entry_price=100, stop_loss=101,
            target_1=102, target_2=103, estimated_margin_per_share=50)


def test_net_rr_filter_rejects_targets_too_close_to_entry() -> None:
    result = assess_trade(side="LONG", entry_price=100, stop_loss=99,
        target_1=100.2, target_2=100.3, estimated_margin_per_share=50)
    assert not result.accepted
    assert "net R:R" in result.reason

@pytest.mark.parametrize("risk_budget", [100.0, 200.0, 500.0])
def test_risk_budget_is_configurable_without_a_hardcoded_ceiling(risk_budget: float) -> None:
    settings = RiskSettings(
        capital_inr=20_000,
        risk_per_trade_inr=risk_budget,
        minimum_net_risk_reward=1.5,
    )
    result = assess_trade(
        side="LONG", entry_price=100, stop_loss=99,
        target_1=103, target_2=104, settings=settings,
        estimated_margin_per_share=50,
    )
    assert result.accepted
    assert result.net_risk_inr <= risk_budget



def test_non_finite_risk_settings_raise_clear_error():
    settings = RiskSettings(capital_inr=float("nan"))
    with pytest.raises(ValueError, match="settings must be finite"):
        assess_trade(
            side="LONG", entry_price=100, stop_loss=99,
            target_1=102, target_2=103, settings=settings,
            estimated_margin_per_share=50,
        )


def test_slippage_above_supported_range_is_rejected():
    settings = RiskSettings(slippage_bps_per_side=101)
    with pytest.raises(ValueError, match="between 0 and 100"):
        assess_trade(
            side="LONG", entry_price=100, stop_loss=99,
            target_1=102, target_2=103, settings=settings,
            estimated_margin_per_share=50,
        )



def test_settings_load_shared_schedule_and_ipft_rate(tmp_path):
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(
        """
account:
  capital_inr: 20000
  risk_per_trade_inr: 350
  minimum_net_risk_reward: 1.5
schedule:
  no_new_entries_after: "14:40"
  intended_square_off_by: "15:20"
risk:
  slippage_bps_per_side: 7
costs:
  ipft_rate: "0.000000001"
""",
        encoding="utf-8",
    )

    settings, charges = load_phase2_settings(settings_path)

    assert settings.risk_per_trade_inr == 350
    assert settings.slippage_bps_per_side == 7
    assert settings.entry_cutoff_ist == "14:40"
    # The legacy schedule field no longer overrides the versioned exit policy.
    assert settings.square_off_time_ist == "15:15"
    assert str(charges.ipft_rate) == "1E-9"



def test_net_reward_risk_helper_matches_assess_trade_calculation() -> None:
    from src.risk_engine import calculate_net_reward_risk

    settings = RiskSettings(capital_inr=20_000, risk_per_trade_inr=200,
                            slippage_bps_per_side=5)
    assessment = assess_trade(
        side="LONG", entry_price=100, stop_loss=99,
        target_1=103, target_2=104, settings=settings,
        estimated_margin_per_share=50,
    )
    metrics = calculate_net_reward_risk(
        side="LONG", entry_price=assessment.entry_price, stop_loss=assessment.stop_loss,
        target_1=assessment.target_1, target_2=assessment.target_2,
        quantity=assessment.quantity, slippage_bps_per_side=settings.slippage_bps_per_side,
    )
    assert metrics["net_rr_t1"] == pytest.approx(assessment.net_rr_t1)
    assert metrics["net_rr_t2"] == pytest.approx(assessment.net_rr_t2)
    assert metrics["net_risk_inr"] == pytest.approx(assessment.net_risk_inr)


def test_named_account_profiles_resolve_expected_risk_budgets():
    from src.risk_engine import load_account_profile

    research = load_account_profile("research")
    paper = load_account_profile("paper")
    live_advisory = load_account_profile("live-advisory")

    assert (research["capital_inr"], research["risk_per_trade_inr"]) == (20000, 200)
    assert (paper["capital_inr"], paper["risk_per_trade_inr"]) == (100000, 500)
    assert paper["max_daily_loss_inr"] == 2000
    assert paper["max_open_positions"] == 3
    assert paper["max_portfolio_risk_inr"] == 1500
    assert (live_advisory["capital_inr"], live_advisory["risk_per_trade_inr"]) == (20000, 200)
    assert all(profile["advisory_only"] is True for profile in (research, paper, live_advisory))
    from src.risk_engine import load_phase2_settings
    paper_risk, _ = load_phase2_settings(profile="paper")
    assert paper_risk.capital_inr == 100000
    assert paper_risk.risk_per_trade_inr == 500
    assert paper_risk.minimum_net_risk_reward == 1.5


def test_profile_settings_override_legacy_account_and_cli_can_override_profile(tmp_path):
    import yaml
    from src.risk_engine import load_account_profile

    path = tmp_path / "settings.yaml"
    path.write_text(
        yaml.safe_dump({
            "account": {"capital_inr": 20000, "risk_per_trade_inr": 200, "minimum_net_risk_reward": 1.5},
            "profiles": {
                "paper": {
                    "capital_inr": 125000,
                    "risk_per_trade_inr": 600,
                    "minimum_net_risk_reward": 1.8,
                    "max_daily_loss_inr": 2500,
                    "max_open_positions": 4,
                }
            },
        }),
        encoding="utf-8",
    )
    profile = load_account_profile("paper", path)
    assert profile["capital_inr"] == 125000
    assert profile["risk_per_trade_inr"] == 600
    assert profile["minimum_net_risk_reward"] == 1.8
    assert profile["max_daily_loss_inr"] == 2500
    assert profile["max_open_positions"] == 4


def test_unknown_account_profile_fails_closed():
    from src.risk_engine import load_account_profile

    with pytest.raises(ValueError, match="profile must be"):
        load_account_profile("live")
