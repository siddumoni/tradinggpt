import gzip
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from src.microstructure_reports import write_microstructure_reports
IST = ZoneInfo("Asia/Kolkata")

def test_writes_volume_profile_and_order_flow_summaries_for_session(tmp_path):
    root = tmp_path
    archive = root / "data" / "paper_candles_kite"
    archive.mkdir(parents=True)
    cache = root / ".local" / "nse_instruments.json"
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps({"date":"2026-10-28","instruments":[{"tradingsymbol":"RELIANCE","tick_size":0.05}]}), encoding="utf-8")
    (archive / "RELIANCE.csv").write_text(
        "date,open,high,low,close,volume\n"
        "2026-10-28T09:15:00+0530,100.00,100.10,99.95,100.05,1000\n"
        "2026-10-28T09:20:00+0530,100.05,100.15,100.00,100.10,1200\n", encoding="utf-8")
    events_dir = root / ".local" / "order_flow" / "2026-10-28"
    events_dir.mkdir(parents=True)
    events = [
        {"symbol":"RELIANCE","timestamp":"2026-10-28T09:20:00+05:30","last_trade_time":"2026-10-28T09:20:00+05:30","last_price":100.05,"last_quantity":10,"best_bid":100.00,"best_ask":100.10,"depth_buy":[{"price":100.00,"quantity":500}],"depth_sell":[{"price":100.10,"quantity":200}]},
        {"symbol":"RELIANCE","timestamp":"2026-10-28T09:20:01+05:30","last_trade_time":"2026-10-28T09:20:01+05:30","last_price":100.10,"last_quantity":20,"best_bid":100.05,"best_ask":100.10,"depth_buy":[{"price":100.05,"quantity":600}],"depth_sell":[{"price":100.10,"quantity":150}]},
    ]
    with gzip.open(events_dir / "RELIANCE.jsonl.gz", "wt", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event) + "\n")
    records = write_microstructure_reports(root=root, report_date="2026-10-28", candle_archive_dir=archive,
        staging_dir=root/"reports"/"github_upload_staging"/"2026-10-28",
        now=datetime(2026,10,28,15,35,tzinfo=IST))
    by_category = {record["category"]:Path(record["path"]) for record in records}
    volume = json.loads(by_category["volume_profile"].read_text(encoding="utf-8"))
    flow = json.loads(by_category["order_flow"].read_text(encoding="utf-8"))
    assert volume["status"] == "READY"
    assert volume["symbols"]["RELIANCE"]["approximate"] is True
    assert "poc" in volume["symbols"]["RELIANCE"]
    assert flow["status"] == "READY"
    assert flow["symbols"]["RELIANCE"]["estimated_delta"] > 0

def test_missing_sources_still_produce_explicit_no_data_reports(tmp_path):
    records = write_microstructure_reports(root=tmp_path, report_date="2026-10-28",
        candle_archive_dir=tmp_path/"missing-candles", staging_dir=tmp_path/"reports"/"staging",
        now=datetime(2026,10,28,15,35,tzinfo=IST))
    payloads = [json.loads(Path(record["path"]).read_text(encoding="utf-8")) for record in records]
    assert [payload["status"] for payload in payloads] == ["NO_DATA","NO_DATA"]
    assert all(record["exists"] and record["upload"] for record in records)
