"""Integrated live-Kite paper-trading service and localhost dashboard.

Uses only Kite market-data/WebSocket interfaces. No real order API is called.
"""
from __future__ import annotations
import argparse,json,logging,threading,time
from datetime import datetime,time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo
from src.auth import load_credentials,load_token_cache
from src.data_fetcher import KiteMarketData
from src.paper_broker import DEFAULT_DB,PaperBroker
from src.paper_dashboard import make_server
from src.paper_daily_report import generate_daily_report
from src.paper_strategy_lab import process_archive
from src.paper_scan_scheduler import latest_completed_boundary,process_due_boundary,run_scanner_process,_resolve_path

IST=ZoneInfo("Asia/Kolkata")
LOGGER=logging.getLogger("paper_trading_service")

def _ticker_thread(broker:PaperBroker,symbols:list[str],stop:threading.Event)->None:
 """Monitor fresh Kite WebSocket ticks and close virtual positions only."""
 while not stop.is_set():
  ticker=None
  try:
   api_key,_=load_credentials();cached=load_token_cache(api_key)
   if cached is None:
    LOGGER.error("Kite session is missing/expired. Run python -m src.main login; tick monitor retries in 30 seconds.")
    stop.wait(30);continue
   try:from kiteconnect import KiteTicker
   except ImportError:
    LOGGER.exception("kiteconnect dependency missing for WebSocket monitor");stop.wait(30);continue
   provider=KiteMarketData.from_cached_session();instruments=provider.instruments()
   token_map={int(r["instrument_token"]):str(r["tradingsymbol"]).upper() for r in instruments
    if r.get("instrument_token") and r.get("tradingsymbol") and r.get("segment")=="NSE"
    and r.get("instrument_type")=="EQ" and str(r["tradingsymbol"]).upper() in symbols}
   if not token_map:
    LOGGER.error("No requested NSE EQ instruments resolved for tick monitor: %s",symbols);stop.wait(30);continue
   tokens=list(token_map);reconnect_exhausted=threading.Event();ticker=KiteTicker(api_key,cached.access_token)
   def on_connect(ws,response):
    ws.subscribe(tokens);ws.set_mode(ws.MODE_FULL,tokens)
    LOGGER.info("Kite WebSocket connected. Subscribed instruments: %s",",".join(token_map.values()))
   def on_ticks(ws,ticks):
    now=datetime.now(IST)
    for tick in ticks:
     try:
      symbol=token_map.get(int(tick.get("instrument_token",0)))
      if not symbol:continue
      stamp=tick.get("exchange_timestamp") or tick.get("last_trade_time") or tick.get("timestamp")
      if stamp is None:continue
      stamp=stamp.replace(tzinfo=IST) if stamp.tzinfo is None else stamp.astimezone(IST)
      age=(now-stamp).total_seconds()
      if age < -2 or age > 60:continue
      last=float(tick.get("last_price"))
      depth=tick.get("depth") or {}
      buys=depth.get("buy",[]) if isinstance(depth,dict) else []
      sells=depth.get("sell",[]) if isinstance(depth,dict) else []
      bid=float(buys[0]["price"]) if buys and isinstance(buys[0],dict) and buys[0].get("price") else None
      ask=float(sells[0]["price"]) if sells and isinstance(sells[0],dict) and sells[0].get("price") else None
      closed=broker.update_market_price(symbol,last,bid=bid,ask=ask,market_time=stamp,received_at=now)
      if closed:LOGGER.info("Kite ticks triggered %s virtual exit(s) for %s",closed,symbol)
     except (TypeError,ValueError,KeyError,OverflowError):LOGGER.exception("Ignored malformed Kite tick")
   def on_error(ws,code,reason):LOGGER.error("Kite WebSocket error code=%s reason=%s",code,reason)
   def on_close(ws,code,reason):LOGGER.warning("Kite WebSocket closed code=%s reason=%s",code,reason)
   def on_noreconnect(ws):
    LOGGER.error("Kite WebSocket exhausted reconnect attempts; rebuilding connection.")
    reconnect_exhausted.set()
   ticker.on_connect=on_connect;ticker.on_ticks=on_ticks;ticker.on_error=on_error;ticker.on_close=on_close;ticker.on_noreconnect=on_noreconnect
   LOGGER.info("Starting live tick monitor for %s",",".join(token_map.values()))
   ticker.connect(threaded=True)
   while not stop.is_set() and not reconnect_exhausted.wait(1):pass
  except Exception:LOGGER.exception("Kite tick monitor could not start; retrying after a short delay")
  finally:
   if ticker is not None:
    try:ticker.close()
    except Exception:pass
  stop.wait(10)

def _configure_logging(path:Path)->None:
 import logging.handlers
 path.parent.mkdir(parents=True,exist_ok=True)
 formatter=logging.Formatter("%(asctime)s %(levelname)s %(message)s")
 handler=logging.handlers.RotatingFileHandler(path,maxBytes=5_000_000,backupCount=5,encoding="utf-8");handler.setFormatter(formatter)
 console=logging.StreamHandler();console.setFormatter(formatter)
 logging.basicConfig(level=logging.INFO,handlers=[handler,console],force=True)

def main(argv=None)->int:
 p=argparse.ArgumentParser(description="Integrated read-only Kite paper broker, tick monitor and local dashboard.")
 p.add_argument("--symbols",default="RELIANCE,HDFCBANK,INFY")
 p.add_argument("--poll-seconds",type=float,default=5);p.add_argument("--grace-seconds",type=int,default=10)
 p.add_argument("--max-lateness-seconds",type=int,default=120);p.add_argument("--scan-timeout-seconds",type=int,default=240)
 p.add_argument("--paper-journal",type=Path,default=Path("logs/paper_signals.csv"));p.add_argument("--scan-ledger",type=Path,default=Path("logs/paper_scan_runs.csv"))
 p.add_argument("--candle-archive-dir",type=Path,default=Path("data/paper_candles_kite"))
 p.add_argument("--scan-state-file",type=Path,default=Path("logs/paper_scan_scheduler_state.json"))
 p.add_argument("--daily-state-file",type=Path,default=Path("logs/paper_daily_report_state.json"))
 p.add_argument("--db-path",type=Path,default=DEFAULT_DB);p.add_argument("--log-file",type=Path,default=Path("logs/paper_trading_service.log"))
 p.add_argument("--reports-dir",type=Path,default=Path("reports"))
 p.add_argument("--dashboard-host",choices=("127.0.0.1","localhost"),default="127.0.0.1");p.add_argument("--dashboard-port",type=int,default=8000)
 p.add_argument("--account-capital",type=float,default=100000);p.add_argument("--risk-per-trade",type=float,default=500)
 p.add_argument("--daily-loss-limit",type=float,default=2000);p.add_argument("--max-open-positions",type=int,default=3)
 a=p.parse_args(argv)
 if a.poll_seconds<=0 or a.grace_seconds<0 or a.max_lateness_seconds<a.grace_seconds or a.scan_timeout_seconds<=0:p.error("Invalid scheduler timing configuration")
 if a.account_capital<=0 or a.risk_per_trade<=0 or a.daily_loss_limit<=0 or a.max_open_positions<1:p.error("Capital and risk limits must be positive")
 if not a.symbols.strip() or not all(s.strip() for s in a.symbols.split(",")):p.error("--symbols must contain comma-separated non-empty symbols")
 root=Path(__file__).resolve().parents[1]
 journal=_resolve_path(root,a.paper_journal);ledger=_resolve_path(root,a.scan_ledger);archive=_resolve_path(root,a.candle_archive_dir)
 scan_state=_resolve_path(root,a.scan_state_file);daily_state=_resolve_path(root,a.daily_state_file);db_path=_resolve_path(root,a.db_path)
 reports=_resolve_path(root,a.reports_dir);log_path=_resolve_path(root,a.log_file);_configure_logging(log_path)
 symbols=list(dict.fromkeys(s.strip().upper() for s in a.symbols.split(",") if s.strip()))
 broker=PaperBroker(db_path,initial_capital=a.account_capital,risk_per_trade=a.risk_per_trade,max_daily_loss=a.daily_loss_limit,max_open_positions=a.max_open_positions)
 stop=threading.Event();server=None;server_thread=None
 try:
  server=make_server(broker,a.dashboard_host,a.dashboard_port)
  server_thread=threading.Thread(target=server.serve_forever,kwargs={"poll_interval":.5},daemon=True,name="paper-dashboard")
  server_thread.start();LOGGER.info("Local dashboard listening at http://%s:%s",a.dashboard_host,a.dashboard_port)
 except OSError:LOGGER.exception("Dashboard could not bind; scan service continues but dashboard is unavailable")
 tick_thread=threading.Thread(target=_ticker_thread,args=(broker,symbols,stop),daemon=True,name="kite-tick-monitor");tick_thread.start()
 LOGGER.info("Paper service started: capital=%.2f risk=%.2f daily-loss=%.2f symbols=%s",a.account_capital,a.risk_per_trade,a.daily_loss_limit,",".join(symbols))
 LOGGER.info("Paper-only boundary enforced: no real order placement/modification/cancellation.")
 try:
  while True:
   now=datetime.now(IST)
   try:
    boundary=latest_completed_boundary(now,grace_seconds=a.grace_seconds,max_lateness_seconds=a.max_lateness_seconds)
    def after_report(path):
     result=process_archive(archive,db_path)
     LOGGER.info("Challenger lab updated from archive after %s: %s",path.name,result)
    def scan(at):
     return run_scanner_process(at,repo_root=root,symbols=a.symbols,paper_journal=journal,scan_ledger=ledger,
      candle_archive_dir=archive,timeout_seconds=a.scan_timeout_seconds,paper_broker=broker,after_report=after_report,
      account_capital=a.account_capital,risk_per_trade=a.risk_per_trade)
    process_due_boundary(boundary,scan_state,scan)
    closed=broker.square_off_due_positions(now)
    if closed:LOGGER.info("Square-off closed %s virtual position(s)",closed)
    if now.weekday()<5 and now.time().replace(tzinfo=None)>=dtime(15,20):
     date_key=now.date().isoformat();existing=""
     if daily_state.exists():
      try:existing=str(json.loads(daily_state.read_text(encoding="utf-8")).get("report_date",""))
      except (OSError,json.JSONDecodeError):existing=""
     if existing!=date_key:
      try:
       shadow=process_archive(archive,db_path);summary=generate_daily_report(db_path,reports,now)
       daily_state.parent.mkdir(parents=True,exist_ok=True);temp=daily_state.with_suffix(".tmp")
       temp.write_text(json.dumps({"report_date":date_key,"generated_at":now.isoformat(),"summary":summary,"shadow_update":shadow},indent=2),encoding="utf-8");temp.replace(daily_state)
       LOGGER.info("Daily report generated: %s",summary["markdown_path"])
      except Exception:LOGGER.exception("Daily paper report failed; will retry next scheduler cycle")
   except Exception:LOGGER.exception("Paper service cycle failed; continuing")
   time.sleep(a.poll_seconds)
 except KeyboardInterrupt:
  LOGGER.info("Paper service stopped by keyboard interrupt");return 0
 finally:
  stop.set()
  if server:server.shutdown();server.server_close()
  if server_thread:server_thread.join(timeout=3)
  tick_thread.join(timeout=3)
 return 0
if __name__=="__main__":raise SystemExit(main())
