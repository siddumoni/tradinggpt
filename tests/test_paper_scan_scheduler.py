from datetime import datetime
import logging
from zoneinfo import ZoneInfo

from src.paper_scan_scheduler import (
    latest_completed_boundary,
    process_due_boundary,
    read_last_attempted_boundary,
)

IST = ZoneInfo("Asia/Kolkata")


def dt(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=IST)


def test_waits_for_grace_after_first_completed_candle():
    assert latest_completed_boundary(dt("2026-10-26T09:19:59")) is None
    assert latest_completed_boundary(dt("2026-10-26T09:20:09")) is None
    assert latest_completed_boundary(dt("2026-10-26T09:20:10")) == dt("2026-10-26T09:20:00")


def test_returns_latest_boundary_after_candle_completes():
    assert latest_completed_boundary(dt("2026-10-26T10:31:00")) == dt("2026-10-26T10:30:00")
    assert latest_completed_boundary(dt("2026-10-26T14:46:00")) == dt("2026-10-26T14:45:00")


def test_stale_boundaries_are_skipped_until_next_candle():
    assert latest_completed_boundary(dt("2026-10-26T09:24:00")) is None
    assert latest_completed_boundary(dt("2026-10-26T09:25:10")) == dt("2026-10-26T09:25:00")


def test_never_schedules_weekends_or_after_last_supported_boundary():
    assert latest_completed_boundary(dt("2026-10-24T10:00:10")) is None
    assert latest_completed_boundary(dt("2026-10-26T15:20:10")) is None
    assert latest_completed_boundary(dt("2026-10-26T15:15:10")) == dt("2026-10-26T15:15:00")


def test_rejects_invalid_scheduler_timing_configuration():
    import pytest

    with pytest.raises(ValueError, match="grace_seconds"):
        latest_completed_boundary(dt("2026-10-26T10:00:00"), grace_seconds=-1)
    with pytest.raises(ValueError, match="max_lateness_seconds"):
        latest_completed_boundary(dt("2026-10-26T10:00:00"), grace_seconds=20, max_lateness_seconds=10)


def test_persistent_state_prevents_duplicate_attempt_after_restart(tmp_path, monkeypatch):
    state_file = tmp_path / "logs" / "state.json"
    boundary = dt("2026-10-26T10:30:00")
    calls = []

    monkeypatch.setattr("src.paper_scan_scheduler.LOGGER", logging.getLogger("scheduler-test"))

    def fake_scan(value):
        calls.append(value)
        return 0

    assert process_due_boundary(boundary, state_file, fake_scan) is True
    assert process_due_boundary(boundary, state_file, fake_scan) is False
    assert calls == [boundary]
    assert read_last_attempted_boundary(state_file) == boundary.isoformat()


def test_failed_attempt_is_not_repeated_for_same_candle(tmp_path):
    state_file = tmp_path / "state.json"
    boundary = dt("2026-10-26T10:35:00")
    calls = []

    assert process_due_boundary(boundary, state_file, lambda _: calls.append(1) or 1) is True
    assert process_due_boundary(boundary, state_file, lambda _: calls.append(2) or 0) is False
    assert calls == [1]
