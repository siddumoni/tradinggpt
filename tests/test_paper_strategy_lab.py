import sqlite3
import pandas as pd
from zoneinfo import ZoneInfo
from src.paper_strategy_lab import generate_challenger_signals,ensure_shadow_schema
def test_challenger_lab_requires_indicator_warmup():
 idx=pd.date_range("2026-10-26 09:15",periods=20,freq="5min",tz=ZoneInfo("Asia/Kolkata"))
 frame=pd.DataFrame({"open":range(100,120),"high":range(101,121),"low":range(99,119),"close":range(100,120),"volume":[1000]*20},index=idx)
 assert generate_challenger_signals("INFY",frame)==[]
def test_shadow_schema_is_idempotent(tmp_path):
 with sqlite3.connect(tmp_path/"db.sqlite3") as db:
  ensure_shadow_schema(db);ensure_shadow_schema(db)
  cols=[r[1] for r in db.execute("PRAGMA table_info(shadow_signals)")]
  assert "signal_id" in cols and "net_pnl" in cols
