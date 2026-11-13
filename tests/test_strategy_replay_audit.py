from __future__ import annotations

import csv
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from src.strategy_replay_audit import audit_shadow_observations

IST = "+05:30"


def _write_candles(path: Path, *, target_on_second_bar: bool = False) -> None:
    start = datetime.fromisoformat("2026-11-13T09:15:00+05:30")
    rows = []
    for index in range(75):
        stamp = start + timedelta(minutes=5 * index)
        open_price = 100.0
        high = 101.0
        low = 99.5
        close = 100.0
        if index == 0:
            high, low, close = 100.5, 99.5, 100.0
        elif index == 2:
            high, low = (103.0, 99.5) if target_on_second_bar else (101.5, 98.5)
            close = 101.0
        rows.append({
            "date": stamp.isoformat(),
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "volume": 1000,
            "provider_mode": "kite",
        })
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _closed_trade() -> dict:
    return {
        "signal_id": "closed-1",
        "strategy_version": "micro_orderflow_price_continuation_v1",
        "symbol": "INFY",
        "side": "LONG",
        "status": "CLOSED",
        "signal_time": f"2026-11-13T09:15:00{IST}",
        "created_at": f"2026-11-13T09:20:00{IST}",
        "entry_time": f"2026-11-13T09:25:00{IST}",
        "entry_reference_price": 100.0,
        "stop_loss": 99.0,
        "target_2": 102.0,
        "exit_time": f"2026-11-13T09:30:00{IST}",
        "exit_reference_price": 99.0,
        "exit_reason": "STOP_LOSS",
        "net_pnl": -120.0,
    }


def _blocked_candidate(signal_id: str = "blocked-1") -> dict:
    return {
        "signal_id": signal_id,
        "strategy_version": "challenger_vwap_momentum_atr_seed_v2",
        "symbol": "SBIN",
        "side": "LONG",
        "status": "ENTRY_REJECTED",
        "signal_time": f"2026-11-13T09:15:00{IST}",
        "created_at": f"2026-11-13T09:20:00{IST}",
        "signal_close": 100.0,
        "stop_loss": 99.0,
        "target_2": 102.0,
        "reason": "Cash-only risk/after-cost R:R gate rejected the setup.",
    }


def test_closed_shadow_trade_replay_matches_recorded_stop_and_calculates_excursion(tmp_path):
    _write_candles(tmp_path / "INFY.csv")
    result = audit_shadow_observations([_closed_trade()], tmp_path)

    assert result["status"] == "COMPLETE"
    assert result["closed_trades_replayed"] == 1
    assert result["closed_replays_matched"] == 1
    detail = result["details"][0]
    assert detail["audit_status"] == "MATCHED"
    assert detail["replayed_exit_reason"] == "STOP_LOSS"
    assert detail["replayed_exit_time"] == f"2026-11-13T09:30:00{IST}"
    assert detail["mfe_points"] == pytest.approx(1.5)
    assert detail["mae_points"] == pytest.approx(1.5)
    assert detail["mfe_r"] == pytest.approx(1.5)
    assert detail["mae_r"] == pytest.approx(1.5)
    summary = result["by_strategy"]["micro_orderflow_price_continuation_v1"]
    assert summary["recorded_wins"] == 0
    assert summary["recorded_losses"] == 1
    assert summary["recorded_net_pnl"] == pytest.approx(-120.0)
    assert summary["recorded_exit_reason_counts"] == {"STOP_LOSS": 1}
    assert summary["closed_replay_match_rate_pct"] == pytest.approx(100.0)


def test_rejected_candidate_is_counterfactual_only_and_has_no_pnl(tmp_path):
    _write_candles(tmp_path / "SBIN.csv")
    result = audit_shadow_observations([_blocked_candidate()], tmp_path)

    detail = result["details"][0]
    assert detail["audit_status"] == "COUNTERFACTUAL_STOP_LOSS"
    assert detail["counterfactual_exit"] == "STOP_LOSS"
    assert detail["counterfactual_exit_time"] == f"2026-11-13T09:30:00{IST}"
    assert detail["counterfactual_pnl"] is None
    assert "No fill" in detail["note"]


def test_rejected_candidate_counterfactual_can_show_target_without_pnl(tmp_path):
    _write_candles(tmp_path / "SBIN.csv", target_on_second_bar=True)
    result = audit_shadow_observations([_blocked_candidate()], tmp_path)

    detail = result["details"][0]
    assert detail["audit_status"] == "COUNTERFACTUAL_TARGET_2"
    assert detail["counterfactual_exit"] == "TARGET_2"
    assert detail["counterfactual_pnl"] is None


def test_late_candidate_is_classified_before_any_counterfactual_entry(tmp_path):
    _write_candles(tmp_path / "SBIN.csv")
    row = _blocked_candidate("late-1")
    row.update(
        signal_time=f"2026-11-13T14:40:00{IST}",
        created_at=f"2026-11-13T14:41:00{IST}",
    )
    result = audit_shadow_observations([row], tmp_path)

    detail = result["details"][0]
    assert detail["audit_status"] == "LATE_CUTOFF"
    assert detail["counterfactual_entry_time"] == f"2026-11-13T14:45:00{IST}"
    assert detail["counterfactual_exit"] is None
    assert detail["counterfactual_pnl"] is None


def test_replay_ignores_champion_rows_and_reports_missing_archive(tmp_path):
    rows = [
        {**_closed_trade(), "strategy_version": "nse_champion_v2"},
        _blocked_candidate("missing-1"),
    ]
    result = audit_shadow_observations(rows, tmp_path)

    assert result["observations"] == 1
    assert result["details"][0]["audit_status"] == "ARCHIVE_SYMBOL_MISSING_OR_INVALID"
    assert result["status"] == "PARTIAL"


def test_replay_reports_data_gap_instead_of_skipping_missing_candle(tmp_path):
    _write_candles(tmp_path / "INFY.csv")
    path = tmp_path / "INFY.csv"
    with path.open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    rows = [row for row in rows if row["date"] != f"2026-11-13T09:25:00{IST}"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    result = audit_shadow_observations([_closed_trade()], tmp_path)
    assert result["details"][0]["audit_status"] == "MISSING_ENTRY_BAR"
    assert result["closed_replays_matched"] == 0
