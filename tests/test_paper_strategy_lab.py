import sqlite3
import pandas as pd
from zoneinfo import ZoneInfo
from src.paper_strategy_lab import generate_challenger_signals,ensure_shadow_schema,process_archive
def test_challenger_lab_requires_indicator_warmup():
 idx=pd.date_range("2026-10-26 09:15",periods=20,freq="5min",tz=ZoneInfo("Asia/Kolkata"))
 frame=pd.DataFrame({"open":range(100,120),"high":range(101,121),"low":range(99,119),"close":range(100,120),"volume":[1000]*20},index=idx)
 assert generate_challenger_signals("INFY",frame)==[]
def test_shadow_schema_is_idempotent(tmp_path):
 with sqlite3.connect(tmp_path/"db.sqlite3") as db:
  ensure_shadow_schema(db);ensure_shadow_schema(db)
  cols=[r[1] for r in db.execute("PRAGMA table_info(shadow_signals)")]
  assert "signal_id" in cols and "net_pnl" in cols

def _insert_pending_shadow(db_path,signal_time):
 with sqlite3.connect(db_path) as db:
  db.row_factory=sqlite3.Row
  ensure_shadow_schema(db)
  db.execute(
   """INSERT INTO shadow_signals(
   signal_id,strategy_version,strategy_name,symbol,side,signal_time,signal_close,
   stop_loss,target_1,target_2,status,reason,created_at
   ) VALUES(?,?,?,?,?,?,?,?,?,?,'PENDING_ENTRY',?,?)""",
   ("pending-test","challenger_ema_pullback_v1","EMA pullback","HDFCBANK","LONG",
    signal_time,100.0,99.0,102.5,104.0,"test pending signal",signal_time)
  )


def _shadow_status(db_path):
 with sqlite3.connect(db_path) as db:
  return db.execute("SELECT status,reason FROM shadow_signals WHERE signal_id='pending-test'").fetchone()


def test_late_signal_expires_without_waiting_for_unavailable_next_bar(tmp_path):
 db_path=tmp_path/"paper.sqlite3"
 _insert_pending_shadow(db_path,"2026-10-30T15:10:00+05:30")
 result=process_archive(tmp_path/"no-archive",db_path,
  now=pd.Timestamp("2026-10-30 15:20",tz=ZoneInfo("Asia/Kolkata")))
 status,reason=_shadow_status(db_path)
 assert status=="EXPIRED_NO_ENTRY"
 assert "outside the same-session entry window" in reason
 assert result["expired_no_entry"]==1
 assert result["open_or_pending"]==0


def test_session_finalization_marks_missing_eligible_next_bar_as_data_gap(tmp_path):
 db_path=tmp_path/"paper.sqlite3"
 _insert_pending_shadow(db_path,"2026-10-30T14:35:00+05:30")
 now=pd.Timestamp("2026-10-30 15:20",tz=ZoneInfo("Asia/Kolkata"))
 result=process_archive(tmp_path/"no-archive",db_path,now=now,finalize_session=True)
 status,reason=_shadow_status(db_path)
 assert status=="INCOMPLETE_DATA_GAP"
 assert "Session finalized" in reason
 assert result["incomplete_pending"]==1
 assert result["open_or_pending"]==0


def test_eligible_pending_entry_remains_pending_before_session_finalization(tmp_path):
 db_path=tmp_path/"paper.sqlite3"
 _insert_pending_shadow(db_path,"2026-10-30T14:35:00+05:30")
 result=process_archive(tmp_path/"no-archive",db_path,
  now=pd.Timestamp("2026-10-30 14:40",tz=ZoneInfo("Asia/Kolkata")))
 status,_reason=_shadow_status(db_path)
 assert status=="PENDING_ENTRY"
 assert result["open_or_pending"]==1
