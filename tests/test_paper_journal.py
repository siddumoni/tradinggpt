import csv
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src.paper_journal import SCAN_LEDGER_FIELDS, JOURNAL_FIELDS, append_scan_ledger, append_scan_report, load_scan_report

IST = ZoneInfo("Asia/Kolkata")


def candidate(**overrides):
    value = {
        "symbol": "INFY",
        "action": "WATCH_CANDIDATE",
        "side": "LONG",
        "strategy": "vwap_trend",
        "confidence": 75,
        "market_regime": "BULLISH",
        "signal_time": "2026-10-22T10:00:00+05:30",
        "reference_entry": 1500.0,
        "stop_loss": 1490.0,
        "target_1": 1525.0,
        "target_2": 1540.0,
        "quantity": 10,
        "estimated_net_risk_inr": 120.0,
        "estimated_net_reward_t1_inr": 220.0,
        "net_rr_t1": 1.8,
        "margin_verified": False,
        "data_verified_live": False,
        "news_status": "UNAVAILABLE",
        "news_risk": "UNKNOWN",
    }
    value.update(overrides)
    return value


def write_report(path, candidates, **overrides):
    report = {
        "status": "RESEARCH_SCAN_ONLY",
        "as_of": "2026-10-22T10:05:00+05:30",
        "provider": "Yahoo Finance unofficial; not verified for live use",
        "candidates": candidates,
    }
    report.update(overrides)
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


def read_rows(path):
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def test_journal_records_directional_candidate_as_watch_only_when_unverified(tmp_path):
    report = write_report(tmp_path / "scan.json", [candidate()])
    journal = tmp_path / "logs" / "paper_signals.csv"

    result = append_scan_report(
        report, journal,
        recorded_at=datetime(2026, 10, 22, 10, 6, tzinfo=IST),
    )

    rows = read_rows(journal)
    assert result["new_events"] == 1
    assert len(rows) == 1
    assert rows[0]["paper_status"] == "WATCH_ONLY_UNVERIFIED_DATA"
    assert rows[0]["data_verified_live"] == "False"
    assert rows[0]["report_status"] == "RESEARCH_SCAN_ONLY"
    assert rows[0]["signal_id"]


def test_journal_deduplicates_same_signal_candle_across_reports(tmp_path):
    first = write_report(tmp_path / "scan1.json", [candidate()])
    second = write_report(
        tmp_path / "scan2.json",
        [candidate()],
        as_of="2026-10-22T10:06:00+05:30",
    )
    journal = tmp_path / "journal.csv"

    one = append_scan_report(first, journal)
    two = append_scan_report(second, journal)

    assert one["new_events"] == 1
    assert two["new_events"] == 0
    assert two["duplicates_skipped"] == 1
    assert len(read_rows(journal)) == 1


def test_journal_deduplicates_duplicate_signal_rows_in_one_report(tmp_path):
    report = write_report(tmp_path / "scan.json", [candidate(), candidate()])
    journal = tmp_path / "journal.csv"

    result = append_scan_report(report, journal)

    assert result["directional_candidates"] == 2
    assert result["new_events"] == 1
    assert result["duplicates_skipped"] == 1
    assert len(read_rows(journal)) == 1


def test_journal_treats_string_true_as_unverified(tmp_path):
    report = write_report(tmp_path / "scan.json", [
        candidate(
            action="CONDITIONAL_BUY",
            data_verified_live="True",
            margin_verified="True",
            news_status="CHECKED_NO_MATCH",
        ),
    ])
    journal = tmp_path / "journal.csv"
    append_scan_report(report, journal)
    row = read_rows(journal)[0]

    assert row["data_verified_live"] == "False"
    assert row["margin_verified"] == "False"
    assert row["paper_status"] == "WATCH_ONLY_UNVERIFIED_DATA"


def test_journal_marks_approved_candidate_paper_only_never_filled(tmp_path):
    report = write_report(tmp_path / "scan.json", [
        candidate(
            action="CONDITIONAL_BUY",
            data_verified_live=True,
            margin_verified=True,
            news_status="CHECKED_NO_MATCH",
        ),
    ])
    journal = tmp_path / "journal.csv"
    append_scan_report(report, journal)
    row = read_rows(journal)[0]
    assert row["paper_status"] == "PAPER_ONLY_NOT_EXECUTED"
    assert "no actual or simulated fill" in row["status_note"]


def test_journal_skips_non_directional_and_deduplicates(tmp_path):
    report = write_report(tmp_path / "scan.json", [
        candidate(side=None, strategy=None),
        candidate(action="NO_TRADE", side=None, strategy=None),
    ])
    journal = tmp_path / "journal.csv"
    result = append_scan_report(report, journal)
    assert result["directional_candidates"] == 0
    assert result["non_directional_skipped"] == 2
    assert read_rows(journal) == []
    with journal.open("r", encoding="utf-8", newline="") as handle:
        assert next(csv.reader(handle)) == JOURNAL_FIELDS


def test_journal_rejects_invalid_levels_and_report_schema(tmp_path):
    report = write_report(tmp_path / "scan.json", [candidate(stop_loss=1510.0)])
    with pytest.raises(ValueError, match="LONG candidate levels"):
        append_scan_report(report, tmp_path / "journal.csv")

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"status": "RESEARCH_SCAN_ONLY", "as_of": "not-a-date"}))
    with pytest.raises(ValueError, match="invalid as_of"):
        load_scan_report(bad)


def test_journal_rejects_incompatible_existing_csv_schema(tmp_path):
    report = write_report(tmp_path / "scan.json", [candidate()])
    journal = tmp_path / "journal.csv"
    journal.write_text("signal_id,wrong_column\nabc,def\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unexpected schema"):
        append_scan_report(report, journal)



def test_scan_ledger_records_no_trade_runs_and_verification_health(tmp_path):
    report = write_report(
        tmp_path / "scan.json",
        [
            candidate(
                action="CONDITIONAL_BUY",
                data_verified_live=True,
                margin_verified=True,
                news_status="CHECKED_NO_MATCH",
            ),
            candidate(symbol="HDFCBANK", action="WATCH_CANDIDATE", side="SHORT"),
            candidate(symbol="RELIANCE", action="NO_TRADE", side=None, strategy=None),
        ],
        provider="Kite",
        symbols_requested=["INFY", "HDFCBANK", "RELIANCE"],
        symbols_evaluated=3,
        fetch_errors=[],
        margin_checks=[
            {"symbol": "INFY", "verified": True, "margin_per_share_inr": 15.0},
            {"symbol": "HDFCBANK", "verified": False, "reason": "test unavailable"},
        ],
        candle_archive={
            "results": [
                {"symbol": "INFY", "rows_written": 5},
                {"symbol": "HDFCBANK", "rows_written": 5},
            ],
            "errors": [],
        },
    )
    ledger = tmp_path / "logs" / "paper_scan_runs.csv"
    journal_result = {
        "journal_path": "logs/paper_signals.csv",
        "new_events": 2,
        "duplicates_skipped": 0,
        "non_directional_skipped": 1,
    }

    result = append_scan_ledger(
        report,
        ledger,
        signal_journal_result=journal_result,
        recorded_at=datetime(2026, 10, 22, 10, 6, tzinfo=IST),
    )
    rows = read_rows(ledger)

    assert result["new_runs"] == 1
    assert len(rows) == 1
    assert rows[0]["symbols_requested"] == "INFY;HDFCBANK;RELIANCE"
    assert rows[0]["candidate_count"] == "3"
    assert rows[0]["directional_candidates"] == "2"
    assert rows[0]["conditional_candidates"] == "1"
    assert rows[0]["watch_candidates"] == "1"
    assert rows[0]["no_trade_candidates"] == "1"
    assert rows[0]["data_verified_candidates"] == "1"
    assert rows[0]["margin_verified_candidates"] == "1"
    assert rows[0]["margin_checks_attempted"] == "2"
    assert rows[0]["margin_checks_passed"] == "1"
    assert rows[0]["margin_checks_failed"] == "1"
    assert rows[0]["fetch_error_count"] == "0"
    assert rows[0]["archived_symbols"] == "HDFCBANK;INFY"
    assert rows[0]["archived_rows_written"] == "10"
    assert rows[0]["signal_events_new"] == "2"
    assert rows[0]["signal_events_non_directional_skipped"] == "1"
    with ledger.open("r", encoding="utf-8", newline="") as handle:
        assert next(csv.reader(handle)) == SCAN_LEDGER_FIELDS


def test_scan_ledger_records_successful_no_trade_scans_and_deduplicates(tmp_path):
    report = write_report(
        tmp_path / "scan.json",
        [candidate(symbol="INFY", action="NO_TRADE", side=None, strategy=None),
         candidate(symbol="RELIANCE", action="NO_TRADE", side=None, strategy=None)],
        symbols_requested=["INFY", "RELIANCE"],
        symbols_evaluated=2,
        fetch_errors=[],
        margin_checks=[],
    )
    ledger = tmp_path / "ledger.csv"

    first = append_scan_ledger(report, ledger)
    second = append_scan_ledger(report, ledger)

    assert first["new_runs"] == 1
    assert first["directional_candidates"] == 0
    assert first["no_trade_candidates"] == 2
    assert second["new_runs"] == 0
    assert second["duplicates_skipped"] == 1
    assert len(read_rows(ledger)) == 1


def test_scan_ledger_rejects_incompatible_existing_schema(tmp_path):
    report = write_report(tmp_path / "scan.json", [])
    ledger = tmp_path / "ledger.csv"
    ledger.write_text("scan_id,wrong_column\nabc,def\n", encoding="utf-8")

    with pytest.raises(ValueError, match="unexpected schema"):
        append_scan_ledger(report, ledger)
