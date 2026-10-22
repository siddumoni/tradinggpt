import json
from datetime import time
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from src.cost_calculator import ChargeRates
from src.paper_outcomes import (
    evaluate_journal,
    evaluate_signal,
    load_candles,
    resolve_candle_file,
    strict_csv_bool,
)
from src.risk_engine import RiskSettings

IST = ZoneInfo("Asia/Kolkata")
SIGNAL_TIME = pd.Timestamp("2026-10-12 10:00:00", tz=IST)


def signal_row(**overrides):
    row = {
        "signal_id": "abc123",
        "symbol": "INFY",
        "side": "LONG",
        "strategy": "vwap_trend",
        "action": "WATCH_CANDIDATE",
        "signal_time": SIGNAL_TIME.isoformat(),
        "reference_entry": 100.0,
        "stop_loss": 99.0,
        "target_1": 103.0,
        "target_2": 104.0,
        "data_verified_live": "False",
        "paper_status": "WATCH_ONLY_UNVERIFIED_DATA",
    }
    row.update(overrides)
    return pd.Series(row)


def make_session():
    index = pd.date_range("2026-10-12 09:15:00", periods=72, freq="5min", tz=IST)
    frame = pd.DataFrame({
        "open": 100.0,
        "high": 100.5,
        "low": 99.5,
        "close": 100.0,
        "volume": 1000,
    }, index=index)
    frame.index.name = "timestamp"
    return frame


def write_candles(path, frame):
    saved = frame.copy()
    saved.insert(0, "date", saved.index.astype(str))
    saved.to_csv(path, index=False)
    return path


def evaluate(row, frame, tmp_path):
    settings = RiskSettings(
        capital_inr=20_000,
        risk_per_trade_inr=200,
        minimum_net_risk_reward=1.5,
        require_margin_verification=False,
        skip_if_margin_unavailable=False,
        slippage_bps_per_side=5,
        entry_cutoff_ist="14:45",
        square_off_time_ist="15:15",
    )
    return evaluate_signal(
        row, frame, candle_path=tmp_path / "INFY.csv",
        settings=settings, charge_rates=ChargeRates(),
        slippage_bps_per_side=5, entry_cutoff=time(14, 45),
    )


def test_target_two_exit_recalculates_costs_and_marks_unverified_source(tmp_path):
    frame = make_session()
    target_time = pd.Timestamp("2026-10-12 10:10:00", tz=IST)
    frame.loc[target_time, ["high", "low", "close"]] = [105.0, 100.0, 104.5]

    result = evaluate(signal_row(), frame, tmp_path)

    assert result["outcome_status"] == "COMPLETED_RESEARCH_ONLY"
    assert result["exit_reason"] == "TARGET_2"
    assert result["fill_exit"] == 104.0
    assert result["exit_time"] == (target_time + pd.Timedelta(minutes=5)).isoformat()
    assert result["gross_pnl"] > 0
    assert result["net_pnl"] < result["gross_pnl"]
    assert result["research_only"] is True
    assert result["source_live_data_verified"] is False


def test_stop_first_when_stop_and_target_hit_same_bar(tmp_path):
    frame = make_session()
    both_time = pd.Timestamp("2026-10-12 10:10:00", tz=IST)
    frame.loc[both_time, ["high", "low", "close"]] = [105.0, 98.0, 100.0]

    result = evaluate(signal_row(), frame, tmp_path)

    assert result["outcome_status"] == "COMPLETED_RESEARCH_ONLY"
    assert result["exit_reason"] == "STOP_LOSS"
    assert result["fill_exit"] == 99.0
    assert result["net_pnl"] < 0


def test_opening_gap_through_stop_rejects_hypothetical_entry(tmp_path):
    frame = make_session()
    entry_time = pd.Timestamp("2026-10-12 10:05:00", tz=IST)
    frame.loc[entry_time, ["open", "high", "low", "close"]] = [98.5, 99.0, 98.0, 98.8]

    result = evaluate(signal_row(), frame, tmp_path)

    assert result["outcome_status"] == "ENTRY_OPEN_INVALIDATED_LEVELS"
    assert result["net_pnl"] is None


def test_too_small_net_reward_to_risk_is_rejected(tmp_path):
    frame = make_session()
    row = signal_row(target_1=100.1, target_2=100.2)

    result = evaluate(row, frame, tmp_path)

    assert result["outcome_status"] == "ENTRY_REJECTED_BY_RISK_FILTER"
    assert result["net_pnl"] is None


def test_mismatch_between_signal_close_and_selected_cache_is_not_scored(tmp_path):
    frame = make_session()
    result = evaluate(signal_row(reference_entry=101.0), frame, tmp_path)
    assert result["outcome_status"] == "REFERENCE_CLOSE_MISMATCH"


def test_missing_next_candle_remains_pending(tmp_path):
    frame = make_session().loc[:SIGNAL_TIME]
    result = evaluate(signal_row(), frame, tmp_path)
    assert result["outcome_status"] == "PENDING_NEXT_BAR"


def test_missing_candle_inside_trade_is_not_scored(tmp_path):
    frame = make_session()
    missing = pd.Timestamp("2026-10-12 10:10:00", tz=IST)
    frame = frame.drop(missing)

    result = evaluate(signal_row(), frame, tmp_path)

    assert result["outcome_status"] == "INCOMPLETE_DATA_GAP"
    assert result["net_pnl"] is None


def test_entry_after_cutoff_is_rejected(tmp_path):
    late = pd.Timestamp("2026-10-12 14:40:00", tz=IST)
    frame = make_session()
    row = signal_row(signal_time=late.isoformat())
    frame.loc[late, "close"] = 100.0
    frame.loc[late + pd.Timedelta(minutes=5), "open"] = 100.0
    result = evaluate(row, frame, tmp_path)
    assert result["outcome_status"] == "ENTRY_AFTER_CUTOFF"


def test_incomplete_latest_session_is_left_pending_until_more_candles(tmp_path):
    frame = make_session()
    truncated = frame.loc[:pd.Timestamp("2026-10-12 12:00:00", tz=IST)]
    result = evaluate(signal_row(), truncated, tmp_path)
    assert result["outcome_status"] == "PENDING_MORE_CANDLES"


def test_candle_loader_rejects_duplicates_and_invalid_ohlc(tmp_path):
    frame = make_session().iloc[:3].copy()
    path = write_candles(tmp_path / "INFY.csv", frame)
    load_candles(path)

    duplicate = pd.concat([frame, frame.iloc[[0]]]).sort_index()
    path2 = write_candles(tmp_path / "INFY_DUP.csv", duplicate)
    with pytest.raises(ValueError, match="duplicate timestamps"):
        load_candles(path2)

    invalid = frame.copy()
    invalid.iloc[0, invalid.columns.get_loc("high")] = 90.0
    path3 = write_candles(tmp_path / "INFY_BAD.csv", invalid)
    with pytest.raises(ValueError, match="inconsistent OHLC"):
        load_candles(path3)


def test_ambiguous_candle_files_fail_instead_of_choosing_arbitrarily(tmp_path):
    (tmp_path / "INFY_60d_5minute.csv").write_text("placeholder", encoding="utf-8")
    (tmp_path / "INFY_90d_5minute.csv").write_text("placeholder", encoding="utf-8")
    with pytest.raises(ValueError, match="Multiple candle files"):
        resolve_candle_file("INFY", tmp_path)


def test_outcome_evaluator_rejects_candle_source_mismatch(tmp_path):
    frame = make_session()
    frame["provider_mode"] = "kite"
    row = signal_row(provider="Yahoo Finance unofficial; not verified live exchange data")

    result = evaluate(row, frame, tmp_path)

    assert result["outcome_status"] == "CANDLE_PROVIDER_MISMATCH"
    assert result["candle_provider_mode"] == "kite"
    assert result["net_pnl"] is None


def test_csv_verification_flag_fails_closed():
    assert strict_csv_bool("True") is True
    assert strict_csv_bool("true") is True
    assert strict_csv_bool("False") is False
    assert strict_csv_bool("yes") is False
    assert strict_csv_bool("") is False


def test_full_evaluation_writes_outcomes_and_portfolio_compatible_trades(tmp_path):
    candle_dir = tmp_path / "candles"
    candle_dir.mkdir()
    frame = make_session()
    target_time = pd.Timestamp("2026-10-12 10:10:00", tz=IST)
    frame.loc[target_time, ["high", "low", "close"]] = [105.0, 100.0, 104.5]
    write_candles(candle_dir / "INFY.csv", frame)

    journal = tmp_path / "paper_signals.csv"
    pd.DataFrame([signal_row().to_dict()]).to_csv(journal, index=False)
    output = tmp_path / "reports" / "outcomes.csv"

    outcomes, portfolio, summary = evaluate_journal(
        journal, candle_dir, output_path=output,
        initial_capital=20_000, risk_per_trade=200,
        minimum_net_risk_reward=1.5, slippage_bps_per_side=5,
    )

    assert output.is_file()
    assert output.with_name("outcomes_portfolio_candidates.csv").is_file()
    assert output.with_name("outcomes_summary.json").is_file()
    assert outcomes.iloc[0]["outcome_status"] == "COMPLETED_RESEARCH_ONLY"
    assert len(portfolio) == 1
    assert set(["symbol", "side", "entry_time", "exit_time", "entry", "exit",
                "stop_loss", "quantity", "gross_pnl", "exit_reason"]).issubset(portfolio.columns)
    assert summary["completed_research_only_outcomes"] == 1
    parsed = json.loads(output.with_name("outcomes_summary.json").read_text(encoding="utf-8"))
    assert parsed["warning"].startswith("Research-only")
