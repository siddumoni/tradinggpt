"""Prospective challenger-strategy shadow portfolios, isolated from the champion account."""
from __future__ import annotations
import hashlib, math, sqlite3
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo
import pandas as pd
from src.cost_calculator import estimate_intraday_round_trip_cost
from src.market_scanner import add_scanner_indicators
from src.paper_outcomes import load_candles
from src.risk_engine import RiskSettings, assess_trade, load_phase2_settings
from src.strategies.signal_planner import plan_atr_levels
IST=ZoneInfo("Asia/Kolkata")
SHADOW_CAPITAL=100_000.0
SHADOW_RISK=500.0
MAX_OPEN_PER_STRATEGY=3
MIN_NET_RR=1.5
SLIPPAGE_BPS=5.0
ENTRY_CUTOFF=dtime(14,45)
LAST_BAR=dtime(15,10)
DEFINITIONS={"challenger_ema_9_21_v1":"EMA 9/21 crossover with VWAP and MACD confirmation","challenger_vwap_momentum_v1":"VWAP crossover with EMA and MACD confirmation","challenger_ema_pullback_v1":"Trend-aligned EMA21 pullback with ATR-scaled levels"}
def _signal_id(version:str,symbol:str,side:str,stamp:str)->str:
 return hashlib.sha256("|".join((version,symbol.upper(),side.upper(),stamp)).encode()).hexdigest()[:32]
def _valid(row:pd.Series,names:list[str])->bool:
 return all(name in row.index and pd.notna(row[name]) and math.isfinite(float(row[name])) for name in names)
def generate_challenger_signals(symbol:str,candles:pd.DataFrame)->list[dict[str,Any]]:
 """Generate deterministic shadow-only setups from the latest completed candle."""
 if candles.empty or len(candles)<35:return []
 try: frame=add_scanner_indicators(candles)
 except (ValueError,KeyError):return []
 if len(frame)<35:return []
 cur,prev=frame.iloc[-1],frame.iloc[-2]
 names=["close","high","low","ema_9","ema_21","vwap","macd_histogram","atr_14"]
 if not _valid(cur,names) or not _valid(prev,names):return []
 close=float(cur["close"]);vwap=float(cur["vwap"]);ema9=float(cur["ema_9"]);ema21=float(cur["ema_21"])
 hist=float(cur["macd_histogram"]);atr=float(cur["atr_14"]);prevclose=float(prev["close"])
 pe9=float(prev["ema_9"]);pe21=float(prev["ema_21"]);pvwap=float(prev["vwap"]);phist=float(prev["macd_histogram"])
 prev_atr=float(prev["atr_14"])
 if not math.isfinite(atr) or atr<=0 or not math.isfinite(prev_atr) or prev_atr<=0:return []
 setups=[]
 if pe9<=pe21 and ema9>ema21 and close>vwap and hist>0: setups.append(("challenger_ema_9_21_v1","EMA 9 crossed above EMA 21; close above VWAP and MACD positive","LONG"))
 elif pe9>=pe21 and ema9<ema21 and close<vwap and hist<0: setups.append(("challenger_ema_9_21_v1","EMA 9 crossed below EMA 21; close below VWAP and MACD negative","SHORT"))
 if prevclose<=pvwap and close>vwap and ema9>ema21 and hist>0: setups.append(("challenger_vwap_momentum_v1","Price crossed above VWAP with bullish EMA and MACD alignment","LONG"))
 elif prevclose>=pvwap and close<vwap and ema9<ema21 and hist<0: setups.append(("challenger_vwap_momentum_v1","Price crossed below VWAP with bearish EMA and MACD alignment","SHORT"))
 tol=.15*atr
 lnow=float(cur["low"])<=ema21+tol and close>ema21 and ema9>ema21 and hist>0
 lprev=float(prev["low"])<=pe21+.15*prev_atr and prevclose>pe21 and pe9>pe21 and phist>0
 snow=float(cur["high"])>=ema21-tol and close<ema21 and ema9<ema21 and hist<0
 sprev=float(prev["high"])>=pe21-.15*prev_atr and prevclose<pe21 and pe9<pe21 and phist<0
 if lnow and not lprev:setups.append(("challenger_ema_pullback_v1","Bullish EMA trend with fresh EMA21 pullback and positive MACD","LONG"))
 elif snow and not sprev:setups.append(("challenger_ema_pullback_v1","Bearish EMA trend with fresh EMA21 pullback and negative MACD","SHORT"))
 stamp=frame.index[-1].isoformat();out=[]
 for version,reason,side in setups:
  try:levels=plan_atr_levels(side,entry=close,atr=atr,stop_atr_multiple=1.0,target_1_atr_multiple=2.5,target_2_atr_multiple=4.0)
  except (ValueError,TypeError):continue
  out.append({"signal_id":_signal_id(version,symbol,side,stamp),"strategy_version":version,"strategy_name":DEFINITIONS[version],"symbol":symbol.upper(),"side":side,"signal_time":stamp,"signal_close":close,"stop_loss":float(levels.stop_loss),"target_1":float(levels.target_1),"target_2":float(levels.target_2),"reason":reason})
 return out
def ensure_shadow_schema(db:sqlite3.Connection)->None:
 db.execute("""CREATE TABLE IF NOT EXISTS shadow_signals(
 signal_id TEXT PRIMARY KEY,strategy_version TEXT NOT NULL,strategy_name TEXT NOT NULL,symbol TEXT NOT NULL,
 side TEXT NOT NULL CHECK(side IN ('LONG','SHORT')),signal_time TEXT NOT NULL,signal_close REAL NOT NULL,
 stop_loss REAL NOT NULL,target_1 REAL NOT NULL,target_2 REAL NOT NULL,status TEXT NOT NULL,
 entry_time TEXT,entry_reference_price REAL,entry_fill_price REAL,quantity INTEGER,exit_time TEXT,
 exit_reference_price REAL,exit_fill_price REAL,exit_reason TEXT,exit_detail TEXT,gross_pnl REAL,brokerage REAL,
 stt REAL,exchange_charges REAL,sebi_charges REAL,gst REAL,stamp_duty REAL,ipft REAL,transaction_charges REAL,
 slippage_cost REAL,total_estimated_costs REAL,net_pnl REAL,reason TEXT NOT NULL,created_at TEXT NOT NULL)""")
 db.execute("CREATE INDEX IF NOT EXISTS idx_shadow_version_status ON shadow_signals(strategy_version,status)")
def _available_capital(db:sqlite3.Connection,version:str)->float:
 closed=db.execute("SELECT COALESCE(SUM(net_pnl),0) pnl FROM shadow_signals WHERE strategy_version=? AND status='CLOSED'",(version,)).fetchone()
 opened=list(db.execute("SELECT entry_fill_price,quantity FROM shadow_signals WHERE strategy_version=? AND status='OPEN'",(version,)))
 return max(0.0,SHADOW_CAPITAL+float(closed["pnl"])-sum(float(r["entry_fill_price"])*int(r["quantity"]) for r in opened))
def _insert_new_signals(db:sqlite3.Connection,symbol:str,frame:pd.DataFrame)->int:
 count=0
 for x in generate_challenger_signals(symbol,frame):
  cur=db.execute("""INSERT OR IGNORE INTO shadow_signals(signal_id,strategy_version,strategy_name,symbol,side,signal_time,signal_close,stop_loss,target_1,target_2,status,reason,created_at)
  VALUES(?,?,?,?,?,?,?,?,?,?,'PENDING_ENTRY',?,?)""",
   (x["signal_id"],x["strategy_version"],x["strategy_name"],x["symbol"],x["side"],x["signal_time"],x["signal_close"],x["stop_loss"],x["target_1"],x["target_2"],x["reason"],pd.Timestamp.now(tz=IST).isoformat()))
  count+=cur.rowcount
 return count
def _entry_shadow(db:sqlite3.Connection,row:sqlite3.Row,frame:pd.DataFrame)->None:
 sig=pd.Timestamp(row["signal_time"])
 entry_ts=sig+pd.Timedelta(minutes=5)
 # Decide eligibility before looking for the next candle. Signals whose next-bar
 # entry is at/after the cutoff can never become valid, even when that candle
 # is absent from the archive (the 15:10 signal was previously left pending).
 if entry_ts.date()!=sig.date() or entry_ts.time().replace(tzinfo=None)>=ENTRY_CUTOFF:
  db.execute("UPDATE shadow_signals SET status='EXPIRED_NO_ENTRY',reason=? WHERE signal_id=?",
   (f"Next-bar entry at {entry_ts.strftime('%H:%M')} IST is outside the same-session entry window (must be before {ENTRY_CUTOFF.strftime('%H:%M')}); no entry was simulated.",row["signal_id"]));return
 if sig not in frame.index:return
 idx=frame.index.get_indexer([entry_ts])[0]
 if idx<0:
  if not frame.empty and frame.index[-1]>sig:
   db.execute("UPDATE shadow_signals SET status='INCOMPLETE_DATA_GAP',reason=? WHERE signal_id=?",
    ("Exact next five-minute candle missing; no entry fabricated.",row["signal_id"]))
  return
 if db.execute("SELECT 1 FROM shadow_signals WHERE strategy_version=? AND symbol=? AND status='OPEN'",(row["strategy_version"],row["symbol"])).fetchone():
  db.execute("UPDATE shadow_signals SET status='ENTRY_REJECTED',reason=? WHERE signal_id=?",("Same-symbol shadow position already open for this strategy.",row["signal_id"]));return
 n=db.execute("SELECT COUNT(*) n FROM shadow_signals WHERE strategy_version=? AND status='OPEN'",(row["strategy_version"],)).fetchone()["n"]
 if int(n)>=MAX_OPEN_PER_STRATEGY:
  db.execute("UPDATE shadow_signals SET status='ENTRY_REJECTED',reason=? WHERE signal_id=?",("Shadow book position limit reached.",row["signal_id"]));return
 entry_ref=float(frame.iloc[idx]["open"]);side=row["side"];entry_fill=entry_ref*(1+SLIPPAGE_BPS/10000) if side=="LONG" else entry_ref*(1-SLIPPAGE_BPS/10000)
 stop,t1,t2=float(row["stop_loss"]),float(row["target_1"]),float(row["target_2"])
 if not (stop<entry_fill<t1<=t2 if side=="LONG" else t2<=t1<entry_fill<stop):
  db.execute("UPDATE shadow_signals SET status='ENTRY_REJECTED',reason=? WHERE signal_id=?",("Next-bar open invalidated price levels.",row["signal_id"]));return
 _,rates=load_phase2_settings()
 settings=RiskSettings(capital_inr=_available_capital(db,row["strategy_version"]),risk_per_trade_inr=SHADOW_RISK,minimum_net_risk_reward=MIN_NET_RR,require_margin_verification=False,skip_if_margin_unavailable=False,slippage_bps_per_side=SLIPPAGE_BPS,entry_cutoff_ist="14:45",square_off_time_ist="15:15")
 assess=assess_trade(side=side,entry_price=entry_fill,stop_loss=stop,target_1=t1,target_2=t2,settings=settings,estimated_margin_per_share=entry_fill,charge_rates=rates)
 if not assess.accepted or assess.quantity<1:
  db.execute("UPDATE shadow_signals SET status='ENTRY_REJECTED',reason=? WHERE signal_id=?",("Cash-only risk/after-cost R:R gate: "+assess.reason,row["signal_id"]));return
 db.execute("UPDATE shadow_signals SET status='OPEN',entry_time=?,entry_reference_price=?,entry_fill_price=?,quantity=?,reason=? WHERE signal_id=?",(entry_ts.isoformat(),entry_ref,entry_fill,int(assess.quantity),str(row["reason"])+" Entry uses next-bar open with adverse slippage.",row["signal_id"]))


def _reconcile_pending_entries(
 db:sqlite3.Connection,*,now:datetime|pd.Timestamp|None=None,finalize_session:bool=False
)->dict[str,int]:
 """Resolve impossible or end-of-session pending entries without inventing fills."""
 current=pd.Timestamp(now) if now is not None else pd.Timestamp.now(tz=IST)
 current=current.tz_localize(IST) if current.tzinfo is None else current.tz_convert(IST)
 expired=incomplete=0
 pending=list(db.execute("SELECT signal_id,signal_time FROM shadow_signals WHERE status='PENDING_ENTRY'"))
 for row in pending:
  sig=pd.Timestamp(row["signal_time"])
  entry_ts=sig+pd.Timedelta(minutes=5)
  if entry_ts.date()!=sig.date() or entry_ts.time().replace(tzinfo=None)>=ENTRY_CUTOFF:
   db.execute("UPDATE shadow_signals SET status='EXPIRED_NO_ENTRY',reason=? WHERE signal_id=?",
    (f"Next-bar entry at {entry_ts.strftime('%H:%M')} IST is outside the same-session entry window (must be before {ENTRY_CUTOFF.strftime('%H:%M')}); no entry was simulated.",row["signal_id"]))
   expired+=1
  elif finalize_session and sig.date()<=current.date():
   db.execute("UPDATE shadow_signals SET status='INCOMPLETE_DATA_GAP',reason=? WHERE signal_id=?",
    ("Session finalized before the expected eligible next-bar candle was present in the Kite archive; no entry was fabricated and this observation is unscored.",row["signal_id"]))
   incomplete+=1
 return {"expired_no_entry":expired,"incomplete_pending":incomplete}

def _close_shadow(db:sqlite3.Connection,row:sqlite3.Row,exit_ref:float,reason:str,detail:str,exit_ts:pd.Timestamp)->None:
 side=row["side"];qty=int(row["quantity"]);entry_ref=float(row["entry_reference_price"]);entry_fill=float(row["entry_fill_price"])
 exit_fill=exit_ref*(1-SLIPPAGE_BPS/10000) if side=="LONG" else exit_ref*(1+SLIPPAGE_BPS/10000)
 gross=((exit_ref-entry_ref) if side=="LONG" else (entry_ref-exit_ref))*qty
 _,rates=load_phase2_settings();fees=estimate_intraday_round_trip_cost(side=side,entry_price=entry_fill,exit_price=exit_fill,quantity=qty,rates=rates)
 slip=abs(entry_fill-entry_ref)*qty+abs(exit_fill-exit_ref)*qty;total=fees.total+slip;net=gross-total
 db.execute("""UPDATE shadow_signals SET status='CLOSED',exit_time=?,exit_reference_price=?,exit_fill_price=?,exit_reason=?,exit_detail=?,gross_pnl=?,brokerage=?,stt=?,exchange_charges=?,sebi_charges=?,gst=?,stamp_duty=?,ipft=?,transaction_charges=?,slippage_cost=?,total_estimated_costs=?,net_pnl=? WHERE signal_id=?""",(exit_ts.isoformat(),exit_ref,exit_fill,reason,detail,gross,fees.brokerage,fees.stt,fees.exchange_charges,fees.sebi_charges,fees.gst,fees.stamp_duty,fees.ipft,fees.total,slip,total,net,row["signal_id"]))
def _monitor_shadow(db:sqlite3.Connection,row:sqlite3.Row,frame:pd.DataFrame)->None:
 entry_ts=pd.Timestamp(row["entry_time"]);session=frame.loc[frame.index.date==entry_ts.date()];bars=session.loc[entry_ts:]
 if bars.empty:return
 previous=None
 for stamp,bar in bars.iterrows():
  if previous is not None and stamp!=previous+pd.Timedelta(minutes=5):
   db.execute("UPDATE shadow_signals SET status='INCOMPLETE_DATA_GAP',reason=? WHERE signal_id=?",(f"Missing five-minute candle after {previous.isoformat()}; outcome unscored.",row["signal_id"]));return
  op=float(bar["open"]);hi=float(bar["high"]);lo=float(bar["low"]);side=row["side"]
  stop_hit=(lo<=float(row["stop_loss"])) if side=="LONG" else (hi>=float(row["stop_loss"]))
  target_hit=(hi>=float(row["target_2"])) if side=="LONG" else (lo<=float(row["target_2"]))
  if stop_hit:
   ref=min(float(row["stop_loss"]),op) if side=="LONG" else max(float(row["stop_loss"]),op)
   _close_shadow(db,row,ref,"STOP_LOSS","Stop-first OHLC handling; adverse stop gaps use worse bar open.",stamp+pd.Timedelta(minutes=5));return
  if target_hit:
   _close_shadow(db,row,float(row["target_2"]),"TARGET_2","Target 2 touched; no favorable gap improvement.",stamp+pd.Timedelta(minutes=5));return
  if stamp.time().replace(tzinfo=None)==LAST_BAR:
   _close_shadow(db,row,float(bar["close"]),"TIME_SQUARE_OFF","Closed at final regular-session candle close.",stamp+pd.Timedelta(minutes=5));return
  previous=stamp
def _update_review_status(db:sqlite3.Connection,version:str)->None:
 rows=list(db.execute("SELECT net_pnl,exit_time FROM shadow_signals WHERE strategy_version=? AND status='CLOSED' ORDER BY exit_time",(version,)))
 if len(rows)<50:return
 vals=[float(r["net_pnl"] or 0) for r in rows];days={str(r["exit_time"])[:10] for r in rows}
 gains=sum(x for x in vals if x>0);loss=-sum(x for x in vals if x<0);pf=gains/loss if loss else (float("inf") if gains else 0.0)
 equity=peak=SHADOW_CAPITAL;dd=0.0
 for v in vals:equity+=v;peak=max(peak,equity);dd=max(dd,peak-equity)
 if len(days)>=20 and sum(vals)>0 and pf>=1.2 and dd<=SHADOW_CAPITAL*.10:
  db.execute("UPDATE strategy_registry SET status='REVIEW_CANDIDATE',description=? WHERE strategy_version=?",
   ("Initial paper screen passed (50+ closed trades, 20+ exit dates, positive net P&L, PF >=1.2, drawdown <=10%). Requires untouched out-of-sample review; never auto-promoted.",version))
def process_archive(
 archive_dir:Path,db_path:Path,*,now:datetime|pd.Timestamp|None=None,finalize_session:bool=False
)->dict[str,int]:
 """Generate prospective challenger observations from completed Kite candles.

 When finalize_session is true, any still-pending eligible entry is classified as
 an incomplete-data observation. It is never filled from a later session or guessed.
 """
 from src.paper_broker import PaperBroker
 PaperBroker(db_path);archive=Path(archive_dir)
 inserted=closed=0;symbols=0
 with sqlite3.connect(db_path,timeout=15) as db:
  db.row_factory=sqlite3.Row;ensure_shadow_schema(db)
  if archive.is_dir():
   for path in sorted(archive.glob("*.csv")):
    try:candles=load_candles(path)
    except (ValueError,OSError):continue
    modes={str(v).strip().lower() for v in candles["provider_mode"].dropna().unique()} if "provider_mode" in candles.columns else set()
    if modes and modes!={"kite"}:continue
    if len(candles)<35:continue
    symbols+=1;symbol=path.stem.upper();frame=add_scanner_indicators(candles)
    inserted+=_insert_new_signals(db,symbol,frame)
    before=db.execute("SELECT COUNT(*) n FROM shadow_signals WHERE symbol=? AND status='CLOSED'",(symbol,)).fetchone()["n"]
    pending=list(db.execute("SELECT * FROM shadow_signals WHERE symbol=? AND status IN ('PENDING_ENTRY','OPEN')",(symbol,)))
    for row in pending:
     if row["status"]=="PENDING_ENTRY":_entry_shadow(db,row,frame)
     row=db.execute("SELECT * FROM shadow_signals WHERE signal_id=?",(row["signal_id"],)).fetchone()
     if row and row["status"]=="OPEN":_monitor_shadow(db,row,frame)
    after=db.execute("SELECT COUNT(*) n FROM shadow_signals WHERE symbol=? AND status='CLOSED'",(symbol,)).fetchone()["n"]
    closed+=int(after-before)
  pending_result=_reconcile_pending_entries(db,now=now,finalize_session=finalize_session)
  for version in DEFINITIONS:_update_review_status(db,version)
  opens=db.execute("SELECT COUNT(*) FROM shadow_signals WHERE status IN ('OPEN','PENDING_ENTRY')").fetchone()[0]
 return {
  "symbols":symbols,"new_signals":inserted,"newly_closed":closed,
  "open_or_pending":int(opens),**pending_result
}
if __name__=="__main__":
 import argparse,json
 p=argparse.ArgumentParser(description="Update prospective shadow-strategy outcomes from Kite candles.")
 p.add_argument("--candles-dir",type=Path,default=Path("data/paper_candles_kite"));p.add_argument("--db-path",type=Path,default=Path("data/paper_trading.sqlite3"))
 a=p.parse_args();print(json.dumps(process_archive(a.candles_dir,a.db_path),indent=2))
