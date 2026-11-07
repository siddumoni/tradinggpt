from __future__ import annotations

import pandas as pd

from src import prospective_strategy_evaluator as evaluator



def test_forward_evaluator_uses_selected_account_profile_and_minimum_slippage(
    tmp_path, monkeypatch
):
    captured = []
    journal_path = tmp_path / "signals.csv"
    row = {field: "" for field in evaluator.JOURNAL_FIELDS}
    row.update({
        "signal_id": "signal-1",
        "recorded_at": "2026-11-06T10:06:00+05:30",
        "signal_time": "2026-11-06T10:00:00+05:30",
        "symbol": "HDFCBANK",
        "strategy": "vwap_trend",
        "strategy_version": evaluator.STRATEGY_VERSION,
        "side": "LONG",
        "reference_entry": 100.0,
        "stop_loss": 99.0,
        "target_1": 102.0,
        "target_2": 102.5,
        "confidence": 70,
        "market_regime": "BULLISH",
        "reason": "test",
        "provider": "Kite Connect candle archive",
        "data_verified_live": False,
        "paper_status": "RESEARCH_ONLY_NO_ORDER",
    })
    pd.DataFrame([row], columns=evaluator.JOURNAL_FIELDS).to_csv(journal_path, index=False)
    monkeypatch.setattr(evaluator, "_load_symbol_archive", lambda _symbol, _directory: (None, None))

    def fake_evaluate(_source, _candles, **kwargs):
        settings = kwargs["settings"]
        captured.append((settings.capital_inr, settings.risk_per_trade_inr, settings.minimum_net_reward_risk))
        return {
            "signal_id": "signal-1", "symbol": "HDFCBANK", "strategy": "vwap_trend",
            "outcome_status": "PENDING", "net_pnl": None,
            "entry_gate_accepted": False, "entry_gate_rejection_reason": "pending",
        }

    monkeypatch.setattr(evaluator, "evaluate_signal", fake_evaluate)
    research_summary = evaluator.refresh_outcomes(
        journal_path, tmp_path / "candles",
        tmp_path / "research" / "outcomes.csv",
        slippage_values=(5.0,), profile="research",
    )
    paper_summary = evaluator.refresh_outcomes(
        journal_path, tmp_path / "candles",
        tmp_path / "paper" / "outcomes.csv",
        slippage_values=(5.0,), profile="paper",
    )

    assert captured == [(20_000.0, 200.0, 1.5), (100_000.0, 500.0, 1.5)]
    assert research_summary["account_profile"] == "research"
    assert research_summary["starting_capital_for_individual_signal_sizing_inr"] == 20_000
    assert paper_summary["account_profile"] == "paper"
    assert paper_summary["risk_budget_per_signal_inr"] == 500


def test_forward_evaluator_rejects_slippage_below_five_bps(tmp_path):
    import pytest

    journal_path = tmp_path / "empty.csv"
    pd.DataFrame(columns=evaluator.JOURNAL_FIELDS).to_csv(journal_path, index=False)
    with pytest.raises(ValueError, match="between 5 and 100"):
        evaluator.refresh_outcomes(
            journal_path, tmp_path / "candles", tmp_path / "outcomes.csv",
            slippage_values=(2.0,),
        )
