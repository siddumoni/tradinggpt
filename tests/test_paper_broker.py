import json
from datetime import datetime,timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import pytest
from src.paper_broker import PaperBroker
from src import paper_broker as paper_broker_module
IST=ZoneInfo("Asia/Kolkata")
def stamp(): return datetime.now(IST).isoformat()
def report_file(tmp_path,candidates,snapshots=None,*,provider="kite",orders=False):
 p=tmp_path/"scan.json"; p.write_text(json.dumps({"status":"RESEARCH_SCAN_ONLY","as_of":stamp(),"provider":"Kite","data_provider_mode":provider,"advisory_only":True,"orders_placed_or_modified":orders,"candidates":candidates,"market_snapshots":snapshots or {}}),encoding="utf-8"); return p
def candidate(symbol="INFY",signal_time="2026-10-26T10:00:00+05:30",**changes):
 x={"symbol":symbol,"action":"CONDITIONAL_BUY","side":"LONG","strategy":"vwap_trend","confidence":80,"market_regime":"BULLISH","signal_time":signal_time,"reference_entry":100.0,"stop_loss":98.0,"target_1":104.0,"target_2":108.0,"quantity":99,"data_verified_live":True,"margin_verified":True,"company_specific_risk":"LOW","reasons":["15-minute trend bullish","VWAP/EMA aligned"]}; x.update(changes); return x
def quote(**changes):
 x={"last_price":100.0,"bid":99.99,"ask":100.01,"timestamp":stamp(),"verified_live":True}; x.update(changes); return x
def test_virtual_trade_opens_then_closes_with_cost_breakdown(tmp_path):
 b=PaperBroker(tmp_path/"paper.sqlite3"); p=report_file(tmp_path,[candidate()],{"INFY":quote()})
 r=b.process_scan_report(p); assert r["opened"]==1 and r["rejected"]==0
 s=b.snapshot(); assert s["account"]["starting_capital"]==100000 and s["account"]["open_positions"]==1
 trade=s["open_positions"][0]; assert trade["quantity"]>0 and trade["entry_fill_price"]>trade["entry_reference_price"]
 assert trade["target_1"]==pytest.approx(104.0) and trade["target_2"]==pytest.approx(108.0)
 assert trade["net_rr_t1"]>0 and trade["net_rr_t2"]>trade["net_rr_t1"]
 assert b.update_market_price("INFY",108.1,bid=108.0,ask=108.1,market_time=datetime.now(IST))==1
 final=b.snapshot(); assert final["account"]["open_positions"]==0 and final["account"]["closed_trades"]==1
 t=final["closed_trades"][0]; assert t["exit_reason"]=="TARGET_2" and t["gross_pnl"]>0 and t["transaction_charges"]>0 and t["slippage_cost"]>0
 assert t["total_estimated_costs"]==pytest.approx(t["transaction_charges"]+t["slippage_cost"])
 assert t["net_pnl"]==pytest.approx(t["gross_pnl"]-t["total_estimated_costs"])
def test_duplicate_signal_is_not_opened_twice(tmp_path):
 b=PaperBroker(tmp_path/"paper.sqlite3"); assert b.process_scan_report(report_file(tmp_path,[candidate()],{"INFY":quote()}))["opened"]==1
 r=b.process_scan_report(report_file(tmp_path,[candidate()],{"INFY":quote()})); assert r["opened"]==0 and r["rejected"]==1
def test_stale_quote_cannot_open_position(tmp_path):
 b=PaperBroker(tmp_path/"paper.sqlite3",max_entry_quote_age_seconds=1)
 old=(datetime.now(IST)-timedelta(minutes=5)).isoformat()
 r=b.process_scan_report(report_file(tmp_path,[candidate()],{"INFY":quote(timestamp=old)})); assert r["opened"]==0 and r["rejected"]==1
def test_fails_closed_for_non_kite_or_order_submitting_reports(tmp_path):
 b=PaperBroker(tmp_path/"paper.sqlite3")
 with pytest.raises(ValueError,match="only accepts Kite"): b.process_scan_report(report_file(tmp_path,[],provider="yahoo"))
 with pytest.raises(ValueError,match="no-order assertion"): b.process_scan_report(report_file(tmp_path,[],orders=True))
def test_daily_loss_limit_blocks_new_entries_after_loss(tmp_path):
 b=PaperBroker(tmp_path/"paper.sqlite3",max_daily_loss=50)
 b.process_scan_report(report_file(tmp_path,[candidate()],{"INFY":quote()}))
 b.update_market_price("INFY",96.0,bid=95.9,ask=96.1,market_time=datetime.now(IST))
 assert b.snapshot()["account"]["realized_pnl"]<0
 second=candidate("HDFCBANK","2026-10-26T10:05:00+05:30")
 r=b.process_scan_report(report_file(tmp_path,[second],{"HDFCBANK":quote()})); assert r["opened"]==0 and r["rejected"]==1
 assert any("Daily paper loss limit" in x["message"] for x in b.snapshot()["events"])
 assert any(x["event_type"]=="RISK_OVERRUN" for x in b.snapshot()["events"])
def test_non_directional_scan_stays_no_trade(tmp_path):
 b=PaperBroker(tmp_path/"paper.sqlite3"); c=candidate(action="NO_TRADE",side=None)
 r=b.process_scan_report(report_file(tmp_path,[c],{"INFY":quote()})); assert r["opened"]==0 and r["neutral"]==1 and b.snapshot()["account"]["open_positions"]==0


def test_scan_action_counts_separate_watch_candidates_from_no_trade(tmp_path):
 b=PaperBroker(tmp_path/"paper.sqlite3")
 watch=candidate("RELIANCE",action="WATCH_CANDIDATE")
 no_trade=candidate("HDFCBANK",action="NO_TRADE")
 r=b.process_scan_report(report_file(tmp_path,[watch,no_trade]))
 assert r["opened"]==0
 assert r["watch_candidates"]==1
 assert r["no_trade"]==1
 assert r["conditional_candidates"]==0
 assert r["other_actions"]==0
 assert r["neutral"]==2


def test_risk_assessment_uses_reference_price_and_slipped_fill_for_cash_cap(tmp_path,monkeypatch):
 captured={}
 real_assess=paper_broker_module.assess_trade
 def spy_assess(**kwargs):
  captured.update(kwargs)
  return real_assess(**kwargs)
 monkeypatch.setattr(paper_broker_module,"assess_trade",spy_assess)
 broker=PaperBroker(tmp_path/"paper.sqlite3")
 q=quote(ask=100.01,bid=100.00)
 result=broker.process_scan_report(report_file(tmp_path,[candidate()],{"INFY":q}))
 assert result["opened"]==1
 assert captured["entry_price"]==pytest.approx(100.01)
 assert captured["estimated_margin_per_share"]==pytest.approx(100.01*(1+5/10000))


def test_future_dated_tick_is_rejected(tmp_path):
 broker=PaperBroker(tmp_path/"paper.sqlite3")
 with pytest.raises(ValueError,match="future-dated"):
  broker.update_market_price("INFY",100.0,
   market_time=datetime.now(IST)+timedelta(minutes=5),received_at=datetime.now(IST))


def test_open_position_daily_pnl_uses_conservative_net_liquidation_mark(tmp_path):
 broker=PaperBroker(tmp_path/"paper.sqlite3")
 broker.process_scan_report(report_file(tmp_path,[candidate()],{"INFY":quote()}))
 now=datetime.now(IST)
 with broker._connect() as db:
  # Write a non-triggering liquidation mark directly: the public tick handler
  # would correctly square off immediately after 15:15.
  db.execute(
   "INSERT INTO market_marks(symbol,last_price,bid,ask,market_time,received_at) VALUES(?,?,?,?,?,?)",
   ("INFY",101.0,100.95,101.05,now.isoformat(),now.isoformat()),
  )
  trade=db.execute("SELECT * FROM trades WHERE status='OPEN'").fetchone()
  assert trade is not None
  result=broker._today_net_pnl(db,now)
  expected_exit_fill=100.95*(1-5/10000)
  from src.cost_calculator import estimate_intraday_round_trip_cost
  from src.risk_engine import load_phase2_settings
  _,rates=load_phase2_settings()
  fees=estimate_intraday_round_trip_cost(side="LONG",entry_price=trade["entry_fill_price"],
   exit_price=expected_exit_fill,quantity=trade["quantity"],rates=rates)
  gross=(100.95-trade["entry_reference_price"])*trade["quantity"]
  slippage=abs(trade["entry_fill_price"]-trade["entry_reference_price"])*trade["quantity"] + abs(expected_exit_fill-100.95)*trade["quantity"]
  expected=gross-fees.total-slippage
 assert result==pytest.approx(expected)
