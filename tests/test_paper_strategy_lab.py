import pytest
import sqlite3
import pandas as pd
from zoneinfo import ZoneInfo
from src.paper_strategy_lab import generate_challenger_signals,ensure_shadow_schema,process_archive,_entry_shadow,_monitor_shadow
def test_challenger_lab_requires_indicator_warmup():
 idx=pd.date_range("2026-10-26 09:15",periods=20,freq="5min",tz=ZoneInfo("Asia/Kolkata"))
 frame=pd.DataFrame({"open":range(100,120),"high":range(101,121),"low":range(99,119),"close":range(100,120),"volume":[1000]*20},index=idx)
 assert generate_challenger_signals("INFY",frame)==[]
def test_shadow_schema_is_idempotent(tmp_path):
 with sqlite3.connect(tmp_path/"db.sqlite3") as db:
  ensure_shadow_schema(db);ensure_shadow_schema(db)
  cols=[r[1] for r in db.execute("PRAGMA table_info(shadow_signals)")]
  assert "signal_id" in cols and "net_pnl" in cols

def _insert_pending_shadow(db_path,signal_time,created_at=None):
 with sqlite3.connect(db_path) as db:
  db.row_factory=sqlite3.Row
  ensure_shadow_schema(db)
  db.execute(
   """INSERT INTO shadow_signals(
   signal_id,strategy_version,strategy_name,symbol,side,signal_time,signal_close,
   stop_loss,target_1,target_2,status,reason,created_at
   ) VALUES(?,?,?,?,?,?,?,?,?,?,'PENDING_ENTRY',?,?)""",
   ("pending-test","challenger_ema_pullback_v1","EMA pullback","HDFCBANK","LONG",
    signal_time,100.0,99.0,102.5,104.0,"test pending signal",created_at or signal_time)
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


def test_open_challenger_position_is_marked_unscored_at_session_finalization(tmp_path):
 from src.paper_strategy_lab import open_shadow_symbols
 db_path=tmp_path/"paper.sqlite3"
 archive=tmp_path/"candles"
 with sqlite3.connect(db_path) as db:
  db.row_factory=sqlite3.Row
  ensure_shadow_schema(db)
  db.execute(
   """INSERT INTO shadow_signals(
      signal_id,strategy_version,strategy_name,symbol,side,signal_time,signal_close,
      stop_loss,target_1,target_2,status,entry_time,entry_reference_price,
      entry_fill_price,quantity,reason,created_at
   ) VALUES(?,?,?,?,?,?,?,?,?,?,'OPEN',?,?,?,?,?,?)""",
   ("open-gap","challenger_vwap_momentum_v1","VWAP momentum","SYRMA","SHORT",
    "2026-11-03T09:30:00+05:30",100.0,101.0,98.0,97.0,
    "2026-11-03T09:35:00+05:30",100.0,99.95,10,"test open","2026-11-03T09:30:00+05:30"),
  )
 index=pd.date_range("2026-11-03 09:15",periods=36,freq="5min",tz=ZoneInfo("Asia/Kolkata"))
 frame=pd.DataFrame({
  "open":[100.0]*len(index),"high":[100.2]*len(index),
  "low":[99.8]*len(index),"close":[100.0]*len(index),
  "volume":[1000]*len(index),"provider_mode":["kite"]*len(index),
 },index=index)
 frame.index.name="date"
 archive.mkdir()
 frame.reset_index().to_csv(archive/"SYRMA.csv",index=False)

 assert open_shadow_symbols(db_path)==["SYRMA"]
 result=process_archive(
  archive,db_path,
  now=pd.Timestamp("2026-11-03 15:20",tz=ZoneInfo("Asia/Kolkata")),
  finalize_session=True,
 )
 with sqlite3.connect(db_path) as db:
  row=db.execute("SELECT status,net_pnl,reason FROM shadow_signals WHERE signal_id='open-gap'").fetchone()
 assert row[0]=="INCOMPLETE_DATA_GAP"
 assert row[1] is None
 assert "15:10 IST" in row[2]
 assert result["incomplete_open"]==1
 assert result["open_or_pending"]==0
 assert open_shadow_symbols(db_path)==[]


def test_challenger_entry_uses_first_bar_after_created_at(tmp_path):
 db_path=tmp_path/"paper.sqlite3"
 _insert_pending_shadow(db_path,"2026-10-30T09:30:00+05:30",created_at="2026-10-30T09:42:00+05:30")
 index=pd.date_range("2026-10-30 09:15",periods=10,freq="5min",tz=ZoneInfo("Asia/Kolkata"))
 frame=pd.DataFrame({"open":[100.0]*len(index),"high":[100.2]*len(index),"low":[99.8]*len(index),
  "close":[100.0]*len(index),"volume":[1000]*len(index)},index=index)
 with sqlite3.connect(db_path) as db:
  db.row_factory=sqlite3.Row
  row=db.execute("SELECT * FROM shadow_signals WHERE signal_id='pending-test'").fetchone()
  _entry_shadow(db,row,frame)
  state=db.execute("SELECT status,entry_time FROM shadow_signals WHERE signal_id='pending-test'").fetchone()
 assert state[0]=="OPEN"
 assert state[1]=="2026-10-30T09:45:00+05:30"


def test_challenger_monitor_does_not_skip_missing_exact_entry_bar(tmp_path):
 db_path=tmp_path/"paper.sqlite3"
 with sqlite3.connect(db_path) as db:
  db.row_factory=sqlite3.Row
  ensure_shadow_schema(db)
  db.execute("""INSERT INTO shadow_signals(
   signal_id,strategy_version,strategy_name,symbol,side,signal_time,signal_close,
   stop_loss,target_1,target_2,status,entry_time,entry_reference_price,entry_fill_price,
   quantity,reason,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,'OPEN',?,?,?,?,?,?)""",
   ("missing-entry","challenger_vwap_momentum_v1","VWAP momentum","INFY","LONG",
    "2026-10-30T09:30:00+05:30",100.0,99.0,102.5,104.0,"2026-10-30T09:35:00+05:30",
    100.0,100.05,10,"test","2026-10-30T09:30:00+05:30"))
  row=db.execute("SELECT * FROM shadow_signals WHERE signal_id='missing-entry'").fetchone()
  idx=pd.date_range("2026-10-30 09:40",periods=4,freq="5min",tz=ZoneInfo("Asia/Kolkata"))
  frame=pd.DataFrame({"open":[100.0]*4,"high":[100.1]*4,"low":[99.9]*4,"close":[100.0]*4,"volume":[1000]*4},index=idx)
  _monitor_shadow(db,row,frame)
  status,reason=db.execute("SELECT status,reason FROM shadow_signals WHERE signal_id='missing-entry'").fetchone()
 assert status=="INCOMPLETE_DATA_GAP"
 assert "Exact simulated entry candle is missing" in reason


def test_challenger_signal_close_mismatch_is_not_entered(tmp_path):
 db_path=tmp_path/"paper.sqlite3"
 _insert_pending_shadow(db_path,"2026-10-30T09:30:00+05:30",created_at="2026-10-30T09:30:00+05:30")
 index=pd.date_range("2026-10-30 09:15",periods=10,freq="5min",tz=ZoneInfo("Asia/Kolkata"))
 frame=pd.DataFrame({"open":[100.0]*len(index),"high":[100.2]*len(index),"low":[99.8]*len(index),
  "close":[101.0]*len(index),"volume":[1000]*len(index)},index=index)
 with sqlite3.connect(db_path) as db:
  db.row_factory=sqlite3.Row
  row=db.execute("SELECT * FROM shadow_signals WHERE signal_id='pending-test'").fetchone()
  _entry_shadow(db,row,frame)
  status=db.execute("SELECT status FROM shadow_signals WHERE signal_id='pending-test'").fetchone()[0]
 assert status=="INCOMPLETE_DATA_GAP"


def test_challenger_rejects_reference_price_outside_levels_without_raising(tmp_path):
 db_path=tmp_path/"paper.sqlite3"
 _insert_pending_shadow(db_path,"2026-10-30T09:30:00+05:30",created_at="2026-10-30T09:30:00+05:30")
 index=pd.date_range("2026-10-30 09:15",periods=10,freq="5min",tz=ZoneInfo("Asia/Kolkata"))
 frame=pd.DataFrame({"open":[100.0]*len(index),"high":[100.2]*len(index),"low":[99.8]*len(index),
  "close":[100.0]*len(index),"volume":[1000]*len(index)},index=index)
 frame.loc[pd.Timestamp("2026-10-30 09:35",tz=ZoneInfo("Asia/Kolkata")),"open"]=100.005
 with sqlite3.connect(db_path) as db:
  db.row_factory=sqlite3.Row
  db.execute("UPDATE shadow_signals SET stop_loss=100.01 WHERE signal_id='pending-test'")
  row=db.execute("SELECT * FROM shadow_signals WHERE signal_id='pending-test'").fetchone()
  _entry_shadow(db,row,frame)
  status,reason=db.execute("SELECT status,reason FROM shadow_signals WHERE signal_id='pending-test'").fetchone()
 assert status=="ENTRY_REJECTED"
 assert "Reference next-bar open" in reason



def test_atr_seed_challenger_versions_keep_plain_one_atr_stop_control():
 from src.paper_strategy_lab import DEFINITIONS,EXIT_POLICY
 assert {
  "challenger_ema_9_21_atr_seed_v2",
  "challenger_vwap_momentum_atr_seed_v2",
  "challenger_ema_pullback_atr_seed_v2",
 } <= set(DEFINITIONS)
 assert EXIT_POLICY.stop_atr_multiple==pytest.approx(1.0)



def test_challenger_capacity_is_shared_across_atr_seed_version_boundary(tmp_path):
 from src.paper_strategy_lab import VERSION_FAMILY
 db_path=tmp_path/"paper.sqlite3"
 old_version="challenger_ema_9_21_v1"
 new_version="challenger_ema_9_21_atr_seed_v2"
 with sqlite3.connect(db_path) as db:
  db.row_factory=sqlite3.Row
  ensure_shadow_schema(db)
  for index,symbol in enumerate(("AAA","BBB","CCC")):
   db.execute(
    """INSERT INTO shadow_signals(
     signal_id,strategy_version,strategy_name,symbol,side,signal_time,signal_close,
     stop_loss,target_1,target_2,status,entry_time,entry_reference_price,
     entry_fill_price,quantity,reason,created_at
    ) VALUES(?,?,?,?,?,?,?,?,?,?,'OPEN',?,?,?,?,?,?)""",
    (f"old-{index}",old_version,"EMA momentum",symbol,"LONG",
     "2026-10-30T09:30:00+05:30",100.0,99.0,102.5,104.0,
     "2026-10-30T09:35:00+05:30",100.0,100.05,10,"old version open",
     "2026-10-30T09:30:00+05:30"),
   )
  db.execute(
   """INSERT INTO shadow_signals(
    signal_id,strategy_version,strategy_name,symbol,side,signal_time,signal_close,
    stop_loss,target_1,target_2,status,reason,created_at
   ) VALUES(?,?,?,?,?,?,?,?,?,?,'PENDING_ENTRY',?,?)""",
   ("new-pending",new_version,"EMA momentum","INFY","LONG",
    "2026-10-30T09:30:00+05:30",100.0,99.0,102.5,104.0,
    "new version pending","2026-10-30T09:30:00+05:30"),
  )
  row=db.execute("SELECT * FROM shadow_signals WHERE signal_id='new-pending'").fetchone()
  index=pd.date_range("2026-10-30 09:15",periods=10,freq="5min",tz=ZoneInfo("Asia/Kolkata"))
  frame=pd.DataFrame(
   {"open":[100.0]*10,"high":[100.2]*10,"low":[99.8]*10,
    "close":[100.0]*10,"volume":[1000]*10},index=index
  )
  _entry_shadow(db,row,frame)
  status,reason=db.execute(
   "SELECT status,reason FROM shadow_signals WHERE signal_id='new-pending'"
  ).fetchone()
 assert VERSION_FAMILY[new_version]==(old_version,new_version)
 assert status=="ENTRY_REJECTED"
 assert "position limit reached" in reason



def test_microstructure_signal_is_expired_at_generation_when_next_entry_is_late():
 from src.paper_strategy_lab import _late_microstructure_entry

 published="2026-11-13T14:41:00+05:30"
 late=_late_microstructure_entry("2026-11-13T14:40:00+05:30",published)
 assert late is not None
 assert late.isoformat()=="2026-11-13T14:45:00+05:30"

 eligible=_late_microstructure_entry(
  "2026-11-13T14:30:00+05:30","2026-11-13T14:31:00+05:30"
 )
 assert eligible is None


def test_microstructure_late_entry_candidate_is_recorded_as_expired_not_pending(tmp_path):
 from src.paper_strategy_lab import _insert_microstructure_signals
 from src.microstructure_strategy import MicrostructureSettings

 db_path=tmp_path/"paper.sqlite3"
 with sqlite3.connect(db_path) as db:
  db.row_factory=sqlite3.Row
  ensure_shadow_schema(db)
  import src.paper_strategy_lab as lab
  original=lab.generate_microstructure_signals
  lab.generate_microstructure_signals=lambda *a,**k:[{
   "signal_id":"late-micro-signal","strategy_version":"micro_orderflow_price_continuation_v1",
   "strategy_name":"Order Flow continuation","symbol":"INFY","side":"LONG",
   "signal_time":"2026-11-13T14:40:00+05:30","signal_close":100.0,
   "stop_loss":99.0,"target_1":102.0,"target_2":103.0,"reason":"test signal",
  }]
  try:
   inserted,late=_insert_microstructure_signals(
    db,"INFY",pd.DataFrame(),[],tick_size=0.05,
    settings=MicrostructureSettings(),published_at="2026-11-13T14:41:00+05:30",
   )
  finally:
   lab.generate_microstructure_signals=original
  row=db.execute(
   "SELECT status,reason,created_at FROM shadow_signals WHERE signal_id='late-micro-signal'"
  ).fetchone()
 assert inserted==1 and late==1
 assert row["status"]=="EXPIRED_NO_ENTRY"
 assert "14:45 IST" in row["reason"]
