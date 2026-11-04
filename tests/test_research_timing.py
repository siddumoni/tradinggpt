import pandas as pd

from src.research_timing import next_five_minute_bar_after


def test_entry_bar_waits_for_publication_and_signal_bar_completion():
    result = next_five_minute_bar_after(
        "2026-11-04T11:05:00+05:30", "2026-11-04T11:13:27+05:30"
    )
    assert result == pd.Timestamp("2026-11-04T11:15:00+05:30")


def test_publication_exactly_at_bar_start_skips_that_bar():
    result = next_five_minute_bar_after(
        "2026-11-04T11:05:00+05:30", "2026-11-04T11:15:00+05:30"
    )
    assert result == pd.Timestamp("2026-11-04T11:20:00+05:30")


def test_signal_bar_completion_is_minimum_possible_entry_time():
    result = next_five_minute_bar_after(
        "2026-11-04T11:05:00+05:30", "2026-11-04T11:06:00+05:30"
    )
    assert result == pd.Timestamp("2026-11-04T11:10:00+05:30")
