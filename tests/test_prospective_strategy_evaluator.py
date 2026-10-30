from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from src import prospective_strategy_evaluator as evaluator


IST = "Asia/Kolkata"


def make_candles(periods: int = 26) -> pd.DataFrame:
    index = pd.date_range("2026-10-30 09:15", periods=periods, freq="5min", tz=IST)
    close = np.linspace(100.0, 110.0, periods)
    return pd.DataFrame(
        {
            "open": close - 0.1,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": 1000,
            "provider_mode": "kite",
        },
        index=index,
    )


def save_candles(path: Path, frame: pd.DataFrame) -> None:
    saved = frame.copy()
    saved.insert(0, "date", saved.index.astype(str))
    saved.to_csv(path, index=False)


def test_candidate_parser_requires_frozen_supported_strategy_pairs():
    assert evaluator.parse_candidates("HDFCBANK:vwap_trend,INFY:vwap_pullback") == {
        "HDFCBANK": ("vwap_trend",),
        "INFY": ("vwap_pullback",),
    }
    import pytest

    with pytest.raises(ValueError, match="duplicate candidate"):
        evaluator.parse_candidates("INFY:vwap_pullback,INFY:vwap_pullback")
    with pytest.raises(ValueError, match="invalid candidate"):
        evaluator.parse_candidates("INFY:unknown_strategy")


def test_bootstrap_seeds_existing_archive_head_without_backfilling(tmp_path):
    candles_dir = tmp_path / "candles"
    candles_dir.mkdir()
    frame = make_candles(26)
    save_candles(candles_dir / "HDFCBANK.csv", frame)
    state_path = tmp_path / "state.json"
    output_dir = tmp_path / "out"
    now = frame.index[-1] + pd.Timedelta(minutes=5, seconds=20)

    result = evaluator.run_once(
        candidates={"HDFCBANK": ("vwap_trend",), "INFY": ("vwap_pullback",)},
        candles_dir=candles_dir,
        output_dir=output_dir,
        state_path=state_path,
        now=now,
    )

    assert result["bootstrapped"] is True
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["last_processed"]["HDFCBANK"] == frame.index[-1].isoformat()
    journal = pd.read_csv(output_dir / "prospective_strategy_signals.csv")
    assert journal.empty
    audit = pd.read_csv(output_dir / "prospective_strategy_audit.csv")
    assert "ARCHIVE_FIRST_SEEN_SEEDED" not in set(audit.get("status", []))


def test_only_new_candle_generates_candidate_using_causal_history(tmp_path, monkeypatch):
    candles_dir = tmp_path / "candles"
    candles_dir.mkdir()
    frame = make_candles(26)
    save_candles(candles_dir / "HDFCBANK.csv", frame)
    state_path = tmp_path / "state.json"
    output_dir = tmp_path / "out"
    candidates = {"HDFCBANK": ("vwap_trend",)}
    first_now = frame.index[-1] + pd.Timedelta(minutes=5, seconds=20)

    evaluator.run_once(
        candidates=candidates, candles_dir=candles_dir, output_dir=output_dir,
        state_path=state_path, now=first_now,
    )

    next_stamp = frame.index[-1] + pd.Timedelta(minutes=5)
    next_close = float(frame.iloc[-1]["close"]) + 0.25
    next_bar = pd.DataFrame(
        {
            "open": [next_close - 0.1], "high": [next_close + 0.5],
            "low": [next_close - 0.5], "close": [next_close], "volume": [1000],
            "provider_mode": ["kite"],
        },
        index=[next_stamp],
    )
    extended = pd.concat([frame, next_bar])
    save_candles(candles_dir / "HDFCBANK.csv", extended)

    seen_lengths = []

    def fake_callback_factory(symbol, strategy_name, **kwargs):
        def callback(history, _index):
            seen_lengths.append(len(history))
            if history.index[-1] != next_stamp:
                return None
            close = float(history.iloc[-1]["close"])
            atr = float(history.iloc[-1]["atr_14"])
            return {
                "side": "LONG",
                "stop_loss": close - atr,
                "target": close + 2.5 * atr,
                "strategy": strategy_name,
                "confidence": 60,
                "reason": "Deterministic test signal",
            }
        return callback

    monkeypatch.setattr(evaluator, "make_signal_callback", fake_callback_factory)
    now = next_stamp + pd.Timedelta(minutes=5, seconds=25)
    result = evaluator.run_once(
        candidates=candidates, candles_dir=candles_dir, output_dir=output_dir,
        state_path=state_path, now=now,
    )

    journal = pd.read_csv(output_dir / "prospective_strategy_signals.csv")
    assert result["signals_added"] == 1
    assert len(journal) == 1
    assert journal.iloc[0]["signal_time"] == next_stamp.isoformat()
    assert journal.iloc[0]["strategy"] == "vwap_trend"
    assert journal.iloc[0]["provider"] == "Kite Connect candle archive"
    assert journal.iloc[0]["paper_status"] == "RESEARCH_ONLY_NO_ORDER"
    assert max(seen_lengths) == len(extended)
    assert len(set(journal["signal_id"])) == 1


def test_late_bar_is_audited_but_never_backfilled(tmp_path, monkeypatch):
    candles_dir = tmp_path / "candles"
    candles_dir.mkdir()
    frame = make_candles(26)
    save_candles(candles_dir / "HDFCBANK.csv", frame)
    state_path = tmp_path / "state.json"
    output_dir = tmp_path / "out"
    candidates = {"HDFCBANK": ("vwap_trend",)}
    evaluator.run_once(
        candidates=candidates, candles_dir=candles_dir, output_dir=output_dir,
        state_path=state_path, now=frame.index[-1] + pd.Timedelta(minutes=5, seconds=20),
    )

    next_stamp = frame.index[-1] + pd.Timedelta(minutes=5)
    close = float(frame.iloc[-1]["close"]) + 0.25
    next_bar = pd.DataFrame(
        {
            "open": [close - 0.1], "high": [close + 0.5], "low": [close - 0.5],
            "close": [close], "volume": [1000], "provider_mode": ["kite"],
        }, index=[next_stamp],
    )
    save_candles(candles_dir / "HDFCBANK.csv", pd.concat([frame, next_bar]))
    monkeypatch.setattr(
        evaluator, "make_signal_callback",
        lambda *args, **kwargs: lambda history, idx: {
            "side": "LONG", "stop_loss": float(history.iloc[-1]["close"]) - float(history.iloc[-1]["atr_14"]),
            "target": float(history.iloc[-1]["close"]) + 2.5 * float(history.iloc[-1]["atr_14"]),
            "strategy": "vwap_trend", "confidence": 60, "reason": "late test",
        },
    )
    result = evaluator.run_once(
        candidates=candidates, candles_dir=candles_dir, output_dir=output_dir,
        state_path=state_path, now=next_stamp + pd.Timedelta(minutes=8),
        max_late_seconds=120,
    )
    assert result["signals_added"] == 0
    audit = pd.read_csv(output_dir / "prospective_strategy_audit.csv")
    assert "LATE_CANDLE_SKIPPED" in set(audit["status"])
    journal = pd.read_csv(output_dir / "prospective_strategy_signals.csv")
    assert journal.empty

def test_summary_documents_final_modeled_candle_and_kite_provenance(tmp_path):
    journal_path = tmp_path / "prospective_strategy_signals.csv"
    pd.DataFrame(columns=evaluator.JOURNAL_FIELDS).to_csv(journal_path, index=False)
    outcome_path = tmp_path / "prospective_strategy_outcomes.csv"

    evaluator.refresh_outcomes(journal_path, tmp_path / "candles", outcome_path)
    summary = json.loads(
        (tmp_path / "prospective_strategy_outcomes_summary.json").read_text(encoding="utf-8")
    )

    assert summary["session_model"]["last_eligible_signal_candle_start_ist"] == "15:10"
    assert summary["session_model"]["last_eligible_signal_candle_end_ist"] == "15:15"
    assert summary["session_model"]["configured_square_off_time_ist"] == "15:15"
    assert summary["data_provenance"]["required_archive_provider_mode"] == "kite"
    assert summary["data_provenance"]["provider_mode_purity_enforced"] is True
    assert summary["data_provenance"]["fresh_quote_verification_performed"] is False
    assert summary["data_provenance"]["data_verified_live"] is False


def test_final_modeled_signal_candle_is_1510_and_later_start_is_outside_session(monkeypatch):
    frame = make_candles(71)  # 09:15 through 15:05
    last_eligible_stamp = frame.index[-1] + pd.Timedelta(minutes=5)
    after_session_stamp = last_eligible_stamp + pd.Timedelta(minutes=5)

    def bar(stamp, close):
        return pd.DataFrame(
            {
                "open": [close - 0.1], "high": [close + 0.5],
                "low": [close - 0.5], "close": [close], "volume": [1000],
                "provider_mode": ["kite"],
            },
            index=[stamp],
        )

    frame = pd.concat([frame, bar(last_eligible_stamp, 111.0), bar(after_session_stamp, 112.0)])
    monkeypatch.setattr(evaluator, "_make_candidate", lambda *args, **kwargs: None)

    _signals, audits, watermark = evaluator._process_new_bars_for_symbol(
        "HDFCBANK", ("vwap_trend",), frame, frame.index[70].isoformat(),
        now=pd.Timestamp("2026-10-30 15:15:30", tz=IST),
        max_late_seconds=120, existing_ids=set(),
    )

    statuses = {row["candle_time"]: row["status"] for row in audits}
    assert statuses[last_eligible_stamp.isoformat()] == "NO_SIGNAL"
    assert statuses[after_session_stamp.isoformat()] == "OUTSIDE_SESSION"
    assert pd.Timestamp(watermark) == after_session_stamp
