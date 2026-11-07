import pandas as pd
import pytest

from src.cost_feasibility import audit_candidates, summarize_gate_rates


def sample():
    return pd.DataFrame([{
        "date": "2026-11-05", "symbol": "TEST", "side": "LONG",
        "entry": 100, "stop": 99, "target_2": 103, "quantity": 10,
        "bid": 99.9, "ask": 100.1,
    }])


def test_audit_reports_stop_cost_and_net_rr():
    result = audit_candidates(sample())
    assert result.loc[0, "stop_distance_pct"] == pytest.approx(1)
    assert result.loc[0, "slippage_bps_per_side"] >= 5
    assert result.loc[0, "net_rr_t2"] > 1.5
    assert result.loc[0, "slippage_bps_per_side_8bps"] >= 8
    assert "passes_1_5_net_rr_8bps" in result


def test_audit_refuses_lower_slippage_floor():
    with pytest.raises(ValueError, match="between 5 and 100"):
        audit_candidates(sample(), slippage_floor_bps=4)


def test_audit_requires_candidate_levels():
    with pytest.raises(ValueError, match="entry"):
        audit_candidates(pd.DataFrame([{"symbol": "X"}]))


def test_audit_reads_append_only_signal_journal_and_calculates_t2_rr():
    journal = pd.DataFrame([{
        "signal_time": "2026-11-05T10:00:00+05:30",
        "symbol": "INFY", "side": "LONG",
        "reference_entry": 1500.0, "stop_loss": 1490.0,
        "target_1": 1520.0, "target_2": 1540.0,
        "quantity": 10, "paper_status": "PAPER_ONLY_NOT_EXECUTED",
    }])

    result = audit_candidates(journal)

    assert result.loc[0, "date"] == "2026-11-05"
    assert result.loc[0, "symbol"] == "INFY"
    assert result.loc[0, "status"] == "AUDITABLE"
    assert result.loc[0, "net_rr_t2"] > result.loc[0, "net_rr_t2_8bps"]


def test_zero_quantity_journal_rows_are_retained_but_not_counted_as_gate_passes():
    journal = pd.DataFrame([{
        "signal_time": "2026-11-05T10:00:00+05:30",
        "symbol": "INFY", "side": "LONG",
        "reference_entry": 1500.0, "stop_loss": 1490.0,
        "target_1": 1520.0, "target_2": 1540.0,
        "quantity": 0,
    }])

    result = audit_candidates(journal)
    summary = summarize_gate_rates(result)

    assert result.loc[0, "status"] == "UNSCORABLE_NONPOSITIVE_OR_INVALID_QUANTITY"
    assert summary.loc[0, "candidate_count"] == 0
    assert summary.loc[0, "unscorable_count"] == 1
    assert pd.isna(summary.loc[0, "pass_rate_5bps"])
