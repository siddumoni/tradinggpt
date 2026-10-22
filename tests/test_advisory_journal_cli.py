import sys
from pathlib import Path

from src import advisory_engine


def test_cli_appends_scan_to_paper_journal_when_option_enabled(monkeypatch, tmp_path, capsys):
    report_path = tmp_path / "scan.json"
    report_path.write_text("{}", encoding="utf-8")
    captured = {}

    monkeypatch.setattr(
        sys, "argv",
        [
            "advisory_engine",
            "--symbols", "INFY",
            "--paper-journal", str(tmp_path / "journal.csv"),
        ],
    )
    monkeypatch.setattr(
        advisory_engine,
        "run_scan",
        lambda **kwargs: {
            "status": "RESEARCH_SCAN_ONLY",
            "as_of": "2026-10-22T10:05:00+05:30",
            "report_path": str(report_path),
            "candidates": [],
        },
    )

    def fake_append(scan_path, journal_path):
        captured["scan_path"] = scan_path
        captured["journal_path"] = journal_path
        return {
            "journal_path": str(journal_path),
            "new_events": 2,
            "duplicates_skipped": 1,
        }

    monkeypatch.setattr(advisory_engine, "append_scan_report", fake_append)
    assert advisory_engine.main() == 0
    assert captured["scan_path"] == Path(report_path)
    assert captured["journal_path"] == tmp_path / "journal.csv"
    assert "new events: 2" in capsys.readouterr().out


def test_cli_does_not_journal_market_closed_report_without_saved_snapshot(monkeypatch, tmp_path):
    monkeypatch.setattr(
        sys, "argv",
        [
            "advisory_engine",
            "--symbols", "INFY",
            "--paper-journal", str(tmp_path / "journal.csv"),
        ],
    )
    monkeypatch.setattr(
        advisory_engine,
        "run_scan",
        lambda **kwargs: {
            "status": "MARKET_CLOSED",
            "as_of": "2026-10-24T12:00:00+05:30",
            "candidates": [],
        },
    )
    monkeypatch.setattr(
        advisory_engine,
        "append_scan_report",
        lambda *args: (_ for _ in ()).throw(AssertionError("journal should not run")),
    )
    assert advisory_engine.main() == 0
