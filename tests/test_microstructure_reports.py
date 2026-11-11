import gzip
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from src.microstructure_reports import _refresh_eod_candles, write_microstructure_reports

IST = ZoneInfo("Asia/Kolkata")


def test_writes_volume_profile_and_order_flow_summaries_for_session(tmp_path):
    root = tmp_path
    archive = root / "data" / "paper_candles_kite"
    archive.mkdir(parents=True)
    cache = root / ".local" / "nse_instruments.json"
    cache.parent.mkdir(parents=True)
    cache.write_text(
        json.dumps(
            {
                "date": "2026-10-28",
                "instruments": [{"tradingsymbol": "RELIANCE", "tick_size": 0.05}],
            }
        ),
        encoding="utf-8",
    )
    (archive / "RELIANCE.csv").write_text(
        "date,open,high,low,close,volume\n"
        "2026-10-28T09:15:00+0530,100.00,100.10,99.95,100.05,1000\n"
        "2026-10-28T09:20:00+0530,100.05,100.15,100.00,100.10,1200\n",
        encoding="utf-8",
    )

    events_dir = root / ".local" / "order_flow" / "2026-10-28"
    events_dir.mkdir(parents=True)
    events = [
        {
            "symbol": "RELIANCE",
            "timestamp": "2026-10-28T09:20:00+05:30",
            "last_trade_time": "2026-10-28T09:20:00+05:30",
            "last_price": 100.05,
            "last_quantity": 10,
            "best_bid": 100.00,
            "best_ask": 100.10,
            "depth_buy": [{"price": 100.00, "quantity": 500}],
            "depth_sell": [{"price": 100.10, "quantity": 200}],
        },
        {
            "symbol": "RELIANCE",
            "timestamp": "2026-10-28T09:20:01+05:30",
            "last_trade_time": "2026-10-28T09:20:01+05:30",
            "last_price": 100.10,
            "last_quantity": 20,
            "best_bid": 100.05,
            "best_ask": 100.10,
            "depth_buy": [{"price": 100.05, "quantity": 600}],
            "depth_sell": [{"price": 100.10, "quantity": 150}],
        },
    ]
    with gzip.open(events_dir / "RELIANCE.jsonl.gz", "wt", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event) + "\n")

    records = write_microstructure_reports(
        root=root,
        report_date="2026-10-28",
        candle_archive_dir=archive,
        staging_dir=root / "reports" / "github_upload_staging" / "2026-10-28",
        now=datetime(2026, 10, 28, 15, 35, tzinfo=IST),
    )
    by_category = {record["category"]: Path(record["path"]) for record in records}
    volume = json.loads(by_category["volume_profile"].read_text(encoding="utf-8"))
    flow = json.loads(by_category["order_flow"].read_text(encoding="utf-8"))

    assert volume["status"] == "PARTIAL"
    assert volume["coverage"]["symbols"]["RELIANCE"]["full_session_coverage"] is False
    assert volume["symbols"]["RELIANCE"]["approximate"] is True
    assert "poc" in volume["symbols"]["RELIANCE"]
    assert flow["status"] == "READY"
    assert flow["symbols"]["RELIANCE"]["estimated_delta"] > 0
    validation = json.loads(by_category["microstructure_validation"].read_text(encoding="utf-8"))
    assert validation["status"] == "NOT_TESTED"
    assert validation["volume_profile"]["status"] == "NOT_TESTED"


def test_missing_sources_still_produce_explicit_no_data_reports(tmp_path):
    records = write_microstructure_reports(
        root=tmp_path,
        report_date="2026-10-28",
        candle_archive_dir=tmp_path / "missing-candles",
        staging_dir=tmp_path / "reports" / "staging",
        now=datetime(2026, 10, 28, 15, 35, tzinfo=IST),
    )
    payloads = [
        json.loads(Path(record["path"]).read_text(encoding="utf-8"))
        for record in records
    ]
    assert [payload["status"] for payload in payloads] == ["NO_DATA", "NO_DATA"]
    assert all(record["exists"] and record["upload"] for record in records)


def test_eod_refresh_adds_final_session_candles_to_existing_kite_archive(tmp_path, monkeypatch):
    archive = tmp_path / "data" / "paper_candles_kite"
    archive.mkdir(parents=True)
    report_day = "2026-11-11"
    starts = [
        datetime(2026, 11, 11, 9, 15, tzinfo=IST) + timedelta(minutes=5 * i)
        for i in range(72)
    ]
    with (archive / "RELIANCE.csv").open("w", encoding="utf-8", newline="") as handle:
        handle.write("date,open,high,low,close,volume,provider_mode\\n")
        for stamp in starts:
            handle.write(
                f"{stamp.strftime('%Y-%m-%dT%H:%M:%S%z')},100,100.1,99.9,100,1000,kite\\n"
            )

    class FakeProvider:
        def instruments(self, now=None):
            return [{
                "tradingsymbol": "RELIANCE",
                "instrument_token": 123,
                "segment": "NSE",
                "instrument_type": "EQ",
            }]

        def historical_candles(self, token, start, end, interval):
            final_starts = [
                datetime(2026, 11, 11, 15, 15, tzinfo=IST),
                datetime(2026, 11, 11, 15, 20, tzinfo=IST),
                datetime(2026, 11, 11, 15, 25, tzinfo=IST),
                datetime(2026, 11, 11, 15, 30, tzinfo=IST),
            ]
            return [
                {"date": stamp, "open": 100, "high": 100.1, "low": 99.9, "close": 100, "volume": 1000}
                for stamp in starts + final_starts
            ]

    monkeypatch.setattr(
        "src.microstructure_reports.KiteMarketData.from_cached_session",
        lambda: FakeProvider(),
    )
    warnings = _refresh_eod_candles(
        root=tmp_path,
        candle_archive_dir=archive,
        report_date=report_day,
        current=datetime(2026, 11, 11, 15, 36, tzinfo=IST),
    )

    import csv
    with (archive / "RELIANCE.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 75
    assert rows[-1]["date"] == "2026-11-11T15:25:00+0530"
    assert any("updated 1" in warning for warning in warnings)
