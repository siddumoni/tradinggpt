import json
from datetime import datetime
from zoneinfo import ZoneInfo
from src.paper_daily_report import generate_daily_report
def test_daily_report_writes_json_csv_markdown(tmp_path):
 out=generate_daily_report(tmp_path/"paper.sqlite3",tmp_path/"reports",datetime(2026,10,26,16,0,tzinfo=ZoneInfo("Asia/Kolkata")))
 assert out["champion_closed_trades"]==0 and out["shadow_closed_trades"]==0
 base=tmp_path/"reports"/"paper_daily_2026-10-26"
 assert (tmp_path/"reports"/"paper_daily_2026-10-26.json").is_file()
 assert (tmp_path/"reports"/"paper_daily_2026-10-26.md").is_file()
 assert (tmp_path/"reports"/"paper_daily_2026-10-26_trades.csv").is_file()
 payload=json.loads((tmp_path/"reports"/"paper_daily_2026-10-26.json").read_text(encoding="utf-8"))
 assert payload["orders_sent_to_broker"] is False
