"""Create daily CSV, JSON and Markdown summaries for champion and shadow paper trading."""
from __future__ import annotations
import argparse,csv,json,sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from src.paper_broker import DEFAULT_DB,PaperBroker
IST=ZoneInfo("Asia/Kolkata")
def generate_daily_report(db_path:Path=DEFAULT_DB,output_dir:Path=Path("reports"),now:datetime|None=None)->dict:
 current=now or datetime.now(IST);current=current.replace(tzinfo=IST) if current.tzinfo is None else current.astimezone(IST)
 day=current.date().isoformat();output_dir=Path(output_dir);output_dir.mkdir(parents=True,exist_ok=True);broker=PaperBroker(db_path);state=broker.snapshot()
 with sqlite3.connect(db_path) as db:
  db.row_factory=sqlite3.Row
  trades=[dict(r) for r in db.execute("SELECT * FROM trades WHERE substr(entry_time,1,10)=? OR substr(exit_time,1,10)=? ORDER BY COALESCE(exit_time,entry_time)",(day,day))]
  shadows=[dict(r) for r in db.execute("SELECT * FROM shadow_signals WHERE substr(signal_time,1,10)=? OR substr(exit_time,1,10)=? ORDER BY COALESCE(exit_time,signal_time)",(day,day))]
  events=[dict(r) for r in db.execute("SELECT event_time,event_type,severity,symbol,message FROM events WHERE substr(event_time,1,10)=? ORDER BY id",(day,))]
  closed=[r for r in trades if r["status"]=="CLOSED" and str(r.get("exit_time") or "").startswith(day)]
  sh_closed=[r for r in shadows if r["status"]=="CLOSED" and str(r.get("exit_time") or "").startswith(day)]
  def pnl(items):return round(sum(float(r.get("net_pnl") or 0) for r in items),2)
  payload={"report_date":day,"generated_at":current.isoformat(),"research_only":True,"orders_sent_to_broker":False,
   "champion":{"starting_capital":state["account"]["starting_capital"],"closed_trades":len(closed),"net_pnl":pnl(closed),"all_time_realized_net_pnl":state["account"]["realized_pnl"],"open_positions":state["account"]["open_positions"]},
   "challengers":{"closed_shadow_trades":len(sh_closed),"net_pnl":pnl(sh_closed),"strategy_summary":[{"strategy":v,"closed":sum(1 for r in sh_closed if r["strategy_version"]==v),"net_pnl":pnl([r for r in sh_closed if r["strategy_version"]==v])} for v in sorted({r["strategy_version"] for r in shadows})]},
   "champion_trades":closed,"shadow_signals":shadows,"events":events,
   "warning":"Fills and P&L are hypothetical estimates. Challenger books are separate and do not change the frozen champion."}
  prefix=f"paper_daily_{day}";jp=output_dir/f"{prefix}.json";mp=output_dir/f"{prefix}.md";cp=output_dir/f"{prefix}_trades.csv"
  jp.write_text(json.dumps(payload,indent=2,ensure_ascii=False,default=str,allow_nan=False),encoding="utf-8")
  columns=["track","strategy","strategy_version","symbol","side","status","signal_time","entry_time","entry_fill_price","quantity","exit_time","exit_fill_price","exit_reason","gross_pnl","transaction_charges","slippage_cost","total_estimated_costs","net_pnl","reason"];rows=[]
  for r in trades:rows.append({"track":"CHAMPION","strategy":r["strategy"],"strategy_version":r["strategy_version"],"symbol":r["symbol"],"side":r["side"],"status":r["status"],"signal_time":r["signal_time"],"entry_time":r["entry_time"],"entry_fill_price":r["entry_fill_price"],"quantity":r["quantity"],"exit_time":r["exit_time"],"exit_fill_price":r["exit_fill_price"],"exit_reason":r["exit_reason"],"gross_pnl":r["gross_pnl"],"transaction_charges":r["transaction_charges"],"slippage_cost":r["slippage_cost"],"total_estimated_costs":r["total_estimated_costs"],"net_pnl":r["net_pnl"],"reason":r["entry_reason"]})
  for r in shadows:rows.append({"track":"SHADOW","strategy":r["strategy_name"],"strategy_version":r["strategy_version"],"symbol":r["symbol"],"side":r["side"],"status":r["status"],"signal_time":r["signal_time"],"entry_time":r["entry_time"],"entry_fill_price":r["entry_fill_price"],"quantity":r["quantity"],"exit_time":r["exit_time"],"exit_fill_price":r["exit_fill_price"],"exit_reason":r["exit_reason"],"gross_pnl":r["gross_pnl"],"transaction_charges":r["transaction_charges"],"slippage_cost":r["slippage_cost"],"total_estimated_costs":r["total_estimated_costs"],"net_pnl":r["net_pnl"],"reason":r["reason"]})
  with cp.open("w",encoding="utf-8",newline="") as f:
   w=csv.DictWriter(f,fieldnames=columns);w.writeheader();w.writerows(rows)
  lines="\n".join("| "+str(r["strategy_name"])+" | "+r["symbol"]+" | "+r["side"]+" | "+r["status"]+" | "+str(r.get("net_pnl") if r.get("net_pnl") is not None else "—")+" |" for r in shadows) or "| No shadow observations | — | — | — | — |"
  md=f"""# TradingGPT paper-trading daily report — {day}

Generated: {current.isoformat()}

> RESEARCH ONLY. No real Kite orders were submitted. Fills, costs and slippage are hypothetical estimates.

## Champion account
- Virtual starting capital: INR {state['account']['starting_capital']:,.2f}
- Closed champion trades today: {len(closed)}
- Net P&L for closed champion trades today: INR {pnl(closed):,.2f}
- All-time realised net P&L: INR {state['account']['realized_pnl']:,.2f}
- Open champion positions: {state['account']['open_positions']}

## Shadow-strategy observations
- Closed shadow trades today: {len(sh_closed)}
- Shadow net P&L today: INR {pnl(sh_closed):,.2f}

| Strategy | Symbol | Side | Status | Net P&L |
|---|---|---|---|---:|
{lines}

Output files:
- JSON report: {jp}
- CSV trade extract: {cp}

Each challenger has a separate shadow account and does not alter the champion strategy. Positive paper results are not a guarantee of profitability.
"""
  mp.write_text(md,encoding="utf-8")
 return {"report_date":day,"json_path":str(jp),"markdown_path":str(mp),"csv_path":str(cp),"champion_closed_trades":len(closed),"champion_net_pnl":pnl(closed),"shadow_closed_trades":len(sh_closed),"shadow_net_pnl":pnl(sh_closed)}
def main(argv=None):
 p=argparse.ArgumentParser(description="Generate a research-only daily paper-trading report.")
 p.add_argument("--db-path",type=Path,default=DEFAULT_DB);p.add_argument("--output-dir",type=Path,default=Path("reports"));a=p.parse_args(argv)
 print(json.dumps(generate_daily_report(a.db_path,a.output_dir),indent=2));return 0
if __name__=="__main__":raise SystemExit(main())
