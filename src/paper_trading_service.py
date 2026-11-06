"""Integrated live-Kite paper-trading service and localhost dashboard.

Uses only Kite market-data/WebSocket interfaces. No real order API is called.
"""
from __future__ import annotations
import argparse,json,logging,math,threading,time
from datetime import datetime,time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo
from src.auth import load_credentials,load_token_cache
from src.data_fetcher import KiteMarketData
from src.paper_broker import DEFAULT_DB,PaperBroker
from src.paper_dashboard import make_server
from src.paper_daily_report import generate_daily_report
from src.paper_upload_checklist import write_upload_checklist
from src.paper_github_uploader import upload_daily_artifacts
from src.paper_strategy_lab import process_archive, open_shadow_symbols
from src.candidate_outcome_tracker import CandidateOutcomeTracker
from src.watch_shadow_portfolio import WatchShadowPortfolio
from src.paper_scan_scheduler import latest_completed_boundary,process_due_boundary,run_scanner_process,_resolve_path
from src.telegram_alerts import TelegramEventNotifier
from src.risk_engine import load_account_profile, load_phase2_settings

IST=ZoneInfo("Asia/Kolkata")
LOGGER=logging.getLogger("paper_trading_service")

def _desired_market_symbols(broker,watch_shadow,symbols:list[str],universe_mode:str)->list[str]:
    """Latest screened symbol set plus every position that still needs tick monitoring."""
    desired=set(broker.monitored_symbols())
    if universe_mode=="watchlist": desired.update(symbols)
    desired.update(watch_shadow.open_symbols())
    return sorted(value for value in desired if value)


def _sync_websocket_subscriptions(ws,symbol_to_token,active_tokens,broker,watch_shadow,symbols,universe_mode):
    desired_symbols=_desired_market_symbols(broker,watch_shadow,symbols,universe_mode)
    missing=sorted(set(desired_symbols)-set(symbol_to_token))
    if missing:
        LOGGER.warning("Selected/open symbols absent from Kite NSE EQ instrument list: %s",",".join(missing))
    desired_tokens={symbol_to_token[symbol] for symbol in desired_symbols if symbol in symbol_to_token}
    additions=sorted(desired_tokens-active_tokens)
    removals=sorted(active_tokens-desired_tokens)
    if additions:
        ws.subscribe(additions)
        ws.set_mode(ws.MODE_FULL,additions)
        active_tokens.update(additions)
        LOGGER.info("Kite WebSocket subscribed to %s additional selected/open symbol(s).",len(additions))
    if removals:
        ws.unsubscribe(removals)
        active_tokens.difference_update(removals)
        LOGGER.info("Kite WebSocket unsubscribed %s symbols no longer selected or open.",len(removals))
    return active_tokens


def _ticker_thread(broker:PaperBroker,watch_shadow:WatchShadowPortfolio,symbols:list[str],stop:threading.Event,*,universe_mode:str="nse_equity")->None:
    """Monitor top-N screened symbols and all open virtual positions; never place orders."""
    while not stop.is_set():
        ticker=None
        try:
            api_key,_=load_credentials()
            cached=load_token_cache(api_key)
            if cached is None:
                LOGGER.error("Kite session is missing/expired. Run python -m src.main login; tick monitor retries in 30 seconds.")
                stop.wait(30)
                continue
            try:
                from kiteconnect import KiteTicker
            except ImportError:
                LOGGER.exception("kiteconnect dependency missing for WebSocket monitor")
                stop.wait(30)
                continue

            fallback=symbols if universe_mode=="watchlist" else []
            desired=_desired_market_symbols(broker,watch_shadow,fallback,universe_mode)
            if not desired:
                # The first five-minute universe scan populates the persisted top-N list.
                stop.wait(5)
                continue
            provider=KiteMarketData.from_cached_session()
            instruments=provider.instruments()
            symbol_to_token={
                str(row["tradingsymbol"]).strip().upper():int(row["instrument_token"])
                for row in instruments if row.get("instrument_token") and row.get("tradingsymbol")
                and row.get("segment")=="NSE" and row.get("instrument_type")=="EQ"
            }
            initial_tokens=sorted({symbol_to_token[s] for s in desired if s in symbol_to_token})
            if not initial_tokens:
                LOGGER.warning("No monitored symbols resolve to current Kite NSE EQ instruments: %s",",".join(desired[:20]))
                stop.wait(10)
                continue

            token_to_symbol={token:symbol for symbol,token in symbol_to_token.items()}
            reconnect_exhausted=threading.Event()
            connected=threading.Event()
            active_tokens:set[int]=set()
            subscription_lock=threading.Lock()
            ticker=KiteTicker(api_key,cached.access_token)

            def sync(ws):
                with subscription_lock:
                    _sync_websocket_subscriptions(
                        ws,symbol_to_token,active_tokens,broker,watch_shadow,fallback,universe_mode
                    )

            def on_connect(ws,response):
                with subscription_lock:
                    active_tokens.clear()
                    connected.set()
                    _sync_websocket_subscriptions(
                        ws,symbol_to_token,active_tokens,broker,watch_shadow,fallback,universe_mode
                    )
                LOGGER.info("Kite WebSocket connected; subscribed to %s current selected/open symbols.",len(active_tokens))

            def on_ticks(ws,ticks):
                now=datetime.now(IST)
                for tick in ticks:
                    try:
                        symbol=token_to_symbol.get(int(tick.get("instrument_token",0)))
                        if not symbol: continue
                        stamp=tick.get("exchange_timestamp") or tick.get("last_trade_time") or tick.get("timestamp")
                        if stamp is None: continue
                        stamp=stamp.replace(tzinfo=IST) if stamp.tzinfo is None else stamp.astimezone(IST)
                        age=(now-stamp).total_seconds()
                        if age < -2 or age > 60: continue
                        last=float(tick.get("last_price"))
                        depth=tick.get("depth") or {}
                        buys=depth.get("buy",[]) if isinstance(depth,dict) else []
                        sells=depth.get("sell",[]) if isinstance(depth,dict) else []
                        bid=float(buys[0]["price"]) if buys and isinstance(buys[0],dict) and buys[0].get("price") else None
                        ask=float(sells[0]["price"]) if sells and isinstance(sells[0],dict) and sells[0].get("price") else None
                        closed=broker.update_market_price(symbol,last,bid=bid,ask=ask,market_time=stamp,received_at=now)
                        shadow_closed=watch_shadow.update_market_price(symbol,last,bid=bid,ask=ask,market_time=stamp,received_at=now)
                        if closed: LOGGER.info("Kite ticks triggered %s Champion paper exit(s) for %s",closed,symbol)
                        if shadow_closed: LOGGER.info("Kite ticks triggered %s Watch-Shadow exit(s) for %s",shadow_closed,symbol)
                    except (TypeError,ValueError,KeyError,OverflowError):
                        LOGGER.exception("Ignored malformed Kite tick")

            def on_error(ws,code,reason):
                LOGGER.error("Kite WebSocket error code=%s reason=%s",code,reason)

            def on_close(ws,code,reason):
                connected.clear()
                LOGGER.warning("Kite WebSocket closed code=%s reason=%s",code,reason)

            def on_noreconnect(ws):
                connected.clear()
                LOGGER.error("Kite WebSocket exhausted reconnect attempts; rebuilding connection.")
                reconnect_exhausted.set()

            ticker.on_connect=on_connect
            ticker.on_ticks=on_ticks
            ticker.on_error=on_error
            ticker.on_close=on_close
            ticker.on_noreconnect=on_noreconnect
            LOGGER.info("Starting dynamic Kite tick monitor for %s screened/open symbol(s).",len(initial_tokens))
            ticker.connect(threaded=True)
            while not stop.is_set() and not reconnect_exhausted.wait(1):
                if connected.is_set():
                    try: sync(ticker)
                    except Exception: LOGGER.exception("Could not update dynamic Kite WebSocket subscriptions.")
                stop.wait(4)
        except Exception:
            LOGGER.exception("Kite tick monitor could not start; retrying after a short delay")
        finally:
            if ticker is not None:
                try: ticker.close()
                except Exception: pass
        stop.wait(5)

def _configure_logging(path:Path)->None:
 import logging.handlers
 path.parent.mkdir(parents=True,exist_ok=True)
 formatter=logging.Formatter("%(asctime)s %(levelname)s %(message)s")
 handler=logging.handlers.RotatingFileHandler(path,maxBytes=5_000_000,backupCount=5,encoding="utf-8");handler.setFormatter(formatter)
 console=logging.StreamHandler();console.setFormatter(formatter)
 logging.basicConfig(level=logging.INFO,handlers=[handler,console],force=True)

def _finalize_reports(
    *, root: Path, db_path: Path, archive: Path, reports: Path,
    daily_state_file: Path, paper_journal: Path, scan_ledger: Path,
    scheduler_state_file: Path, service_log: Path, symbols: list[str], reason: str,
) -> dict | None:
    """Refresh all daily artifacts and print an actionable upload checklist."""
    now = datetime.now(IST)
    shadow_update: dict = {}
    try:
        finalize_session = now.weekday() < 5 and now.time().replace(tzinfo=None) >= dtime(15, 15)
        shadow_update = process_archive(
            archive, db_path, now=now, finalize_session=finalize_session
        )
        LOGGER.info("Challenger lab final update: %s", shadow_update)
    except Exception as exc:
        LOGGER.exception("Challenger lab final update failed; reporting will continue.")
        shadow_update = {"error": f"{type(exc).__name__}: {exc}"}

    candidate_outcome_update: dict = {}
    try:
        candidate_outcome_update = CandidateOutcomeTracker(db_path).process_archive(
            archive, now=now, finalize_session=finalize_session
        )
        LOGGER.info("Independent candidate outcome update: %s", candidate_outcome_update)
    except Exception as exc:
        LOGGER.exception("Independent candidate outcome finalization failed; reporting will continue.")
        candidate_outcome_update = {"error": f"{type(exc).__name__}: {exc}"}

    report_result = None
    try:
        report_result = generate_daily_report(db_path, reports, now)
        LOGGER.info("Daily report generated: %s", report_result["markdown_path"])
    except Exception:
        LOGGER.exception("Daily report generation failed; upload checklist will still be printed.")

    try:
        champion_account = PaperBroker(db_path).snapshot()["account"]
        watch_state = WatchShadowPortfolio(
            db_path,
            initial_capital=float(champion_account["starting_capital"]),
            risk_per_trade=float(champion_account.get("risk_per_trade", 500.0)),
            max_daily_loss=float(champion_account.get("max_daily_loss", 2000.0)),
            max_open_positions=int(champion_account.get("max_open_positions", 3)),
        ).snapshot()
    except Exception:
        LOGGER.exception("Could not snapshot the watch-candidate shadow book.")
        watch_state = {"error": "Watch-shadow snapshot unavailable", "research_only": True,
                       "orders_sent_to_broker": False}

    state_payload = {
        "report_date": now.date().isoformat(),
        "generated_at": now.isoformat(),
        "finalization_reason": reason,
        "orders_sent_to_broker": False,
        "summary": report_result or {"error": "Daily report generation failed"},
        "shadow_update": shadow_update,
        "candidate_outcome_update": candidate_outcome_update,
        "watch_shadow": watch_state,
    }
    try:
        daily_state_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = daily_state_file.with_suffix(daily_state_file.suffix + ".tmp")
        temporary.write_text(json.dumps(state_payload, indent=2, ensure_ascii=False, default=str),
                             encoding="utf-8")
        temporary.replace(daily_state_file)
    except Exception:
        LOGGER.exception("Could not persist the final daily report state.")

    checklist = None
    try:
        checklist = write_upload_checklist(
            root=root, reports_dir=reports, report_result=report_result,
            daily_state_file=daily_state_file, scan_ledger=scan_ledger,
            paper_journal=paper_journal, scheduler_state_file=scheduler_state_file,
            service_log=service_log, candle_archive_dir=archive, db_path=db_path,
            symbols=symbols, now=now, reason=reason,
        )
        print("\n" + checklist["text"], flush=True)
        LOGGER.info(
            "Upload checklist saved: %s; ready=%s/%s; missing required=%s",
            checklist["checklist_path"], checklist["ready_files"], checklist["total_files"],
            checklist["missing_required_files"],
        )
    except Exception:
        LOGGER.exception("Could not write the upload checklist.")
        print("\nTradingGPT final report attempted. Check the service log for report errors.", flush=True)

    # Upload only after the daily report and checklist have both been finalized.
    # A missing report or required local artifact skips the upload; any GitHub
    # failure is logged but must never interrupt the paper service or its shutdown.
    if report_result is None:
        LOGGER.warning("Private GitHub upload skipped because daily report generation failed.")
    elif checklist is None:
        LOGGER.warning("Private GitHub upload skipped because the upload checklist was not generated.")
    elif checklist.get("missing_required_files", 1):
        LOGGER.warning(
            "Private GitHub upload skipped: %s required artifact(s) are missing.",
            checklist.get("missing_required_files"),
        )
    else:
        try:
            upload_result = upload_daily_artifacts(
                root=root,
                artifacts=checklist["artifacts"],
                report_date=now.date(),
                now=now,
            )
            if upload_result.get("ok"):
                LOGGER.info(
                    "Private GitHub daily research upload completed: repo=%s folder=%s "
                    "uploaded=%s unchanged=%s commit=%s",
                    upload_result.get("repository"),
                    upload_result.get("remote_folder"),
                    upload_result.get("uploaded_files"),
                    upload_result.get("unchanged_files"),
                    upload_result.get("commit_sha") or "no changes",
                )
                if upload_result.get("skipped_files"):
                    LOGGER.warning(
                        "Some optional GitHub upload files were skipped: %s",
                        "; ".join(upload_result["skipped_files"]),
                    )
            elif not upload_result.get("configured"):
                LOGGER.warning("%s", upload_result.get("message", "Private GitHub uploader is not configured."))
            else:
                LOGGER.error(
                    "Private GitHub daily research upload failed: %s",
                    upload_result.get("message", "unknown upload error"),
                )
        except Exception:
            LOGGER.exception("Unexpected private GitHub upload failure; paper service remains available.")
    return report_result


def main(argv=None)->int:
 p=argparse.ArgumentParser(description="Integrated read-only Kite paper broker, tick monitor and local dashboard.")
 p.add_argument("--symbols",default="RELIANCE,HDFCBANK,INFY",help="Used only with --universe watchlist")
 p.add_argument("--universe",choices=("nse_equity","watchlist"),default="nse_equity",help="Default screens the liquid NSE cash-equity universe")
 p.add_argument("--universe-max-symbols",type=int,default=50)
 p.add_argument("--universe-min-price",type=float,default=50.0)
 p.add_argument("--universe-max-price",type=float,default=10000.0)
 p.add_argument("--universe-min-volume",type=int,default=10000)
 p.add_argument("--universe-min-turnover",type=float,default=5000000.0)
 p.add_argument("--universe-max-quote-age-minutes",type=float,default=15.0)
 p.add_argument("--paper-entry-max-quote-age-minutes",type=float,default=2.0)
 p.add_argument("--poll-seconds",type=float,default=5);p.add_argument("--grace-seconds",type=int,default=10)
 p.add_argument("--max-lateness-seconds",type=int,default=120);p.add_argument("--scan-timeout-seconds",type=int,default=300)
 p.add_argument("--paper-journal",type=Path,default=Path("logs/paper_signals.csv"));p.add_argument("--scan-ledger",type=Path,default=Path("logs/paper_scan_runs.csv"))
 p.add_argument("--candle-archive-dir",type=Path,default=Path("data/paper_candles_kite"))
 p.add_argument("--scan-state-file",type=Path,default=Path("logs/paper_scan_scheduler_state.json"))
 p.add_argument("--daily-state-file",type=Path,default=Path("logs/paper_daily_report_state.json"))
 p.add_argument("--db-path",type=Path,default=DEFAULT_DB);p.add_argument("--log-file",type=Path,default=Path("logs/paper_trading_service.log"))
 p.add_argument("--reports-dir",type=Path,default=Path("reports"))
 p.add_argument("--dashboard-host",choices=("127.0.0.1","localhost"),default="127.0.0.1");p.add_argument("--dashboard-port",type=int,default=8000)
 p.add_argument("--profile",choices=("research","paper","live-advisory"),default="paper",help="Named account profile; explicit risk flags override profile values")
 p.add_argument("--account-capital",type=float,default=None);p.add_argument("--risk-per-trade",type=float,default=None)
 p.add_argument("--daily-loss-limit",type=float,default=None);p.add_argument("--max-open-positions",type=int,default=None)
 p.add_argument("--max-portfolio-risk",type=float,default=None,help="Maximum combined planned stop-out risk across open Champion positions")
 p.add_argument("--minimum-net-rr",type=float,default=None);p.add_argument("--slippage-bps-per-side",type=float,default=None)
 a=p.parse_args(argv)
 profile=load_account_profile(a.profile)
 profile_risk,_=load_phase2_settings(profile=a.profile)
 account_capital=a.account_capital if a.account_capital is not None else float(profile["capital_inr"])
 risk_per_trade=a.risk_per_trade if a.risk_per_trade is not None else float(profile["risk_per_trade_inr"])
 daily_loss_limit=a.daily_loss_limit if a.daily_loss_limit is not None else float(profile.get("max_daily_loss_inr",2000.0))
 max_open_positions=a.max_open_positions if a.max_open_positions is not None else int(profile.get("max_open_positions",3))
 max_portfolio_risk=a.max_portfolio_risk if a.max_portfolio_risk is not None else profile.get("max_portfolio_risk_inr")
 minimum_net_rr=a.minimum_net_rr if a.minimum_net_rr is not None else profile_risk.minimum_net_risk_reward
 slippage_bps=a.slippage_bps_per_side if a.slippage_bps_per_side is not None else profile_risk.slippage_bps_per_side
 if a.poll_seconds<=0 or a.grace_seconds<0 or a.max_lateness_seconds<a.grace_seconds or a.scan_timeout_seconds<=0:p.error("Invalid scheduler timing configuration")
 if any(not math.isfinite(float(v)) or float(v)<=0 for v in (account_capital,risk_per_trade,daily_loss_limit,minimum_net_rr)):p.error("Capital and risk limits must be finite and positive")
 if max_open_positions<1:p.error("--max-open-positions must be positive")
 if max_portfolio_risk is not None and (not math.isfinite(float(max_portfolio_risk)) or float(max_portfolio_risk)<=0):p.error("--max-portfolio-risk must be finite and positive")
 if not math.isfinite(float(slippage_bps)) or not 0<=slippage_bps<=100:p.error("--slippage-bps-per-side must be between 0 and 100")
 if a.universe_max_symbols<1:p.error("--universe-max-symbols must be at least 1")
 if a.universe_max_quote_age_minutes<=0 or a.paper_entry_max_quote_age_minutes<=0:p.error("Quote age limits must be positive")
 if a.universe=="watchlist" and (not a.symbols.strip() or not all(s.strip() for s in a.symbols.split(","))):p.error("--symbols must contain comma-separated non-empty symbols in watchlist mode")
 root=Path(__file__).resolve().parents[1]
 journal=_resolve_path(root,a.paper_journal);ledger=_resolve_path(root,a.scan_ledger);archive=_resolve_path(root,a.candle_archive_dir)
 scan_state=_resolve_path(root,a.scan_state_file);daily_state=_resolve_path(root,a.daily_state_file);db_path=_resolve_path(root,a.db_path)
 reports=_resolve_path(root,a.reports_dir);log_path=_resolve_path(root,a.log_file);_configure_logging(log_path)
 symbols=list(dict.fromkeys(s.strip().upper() for s in a.symbols.split(",") if s.strip()))
 broker=PaperBroker(db_path,initial_capital=account_capital,risk_per_trade=risk_per_trade,max_daily_loss=daily_loss_limit,max_open_positions=max_open_positions,max_portfolio_risk_inr=max_portfolio_risk,minimum_net_rr=minimum_net_rr,slippage_bps=slippage_bps)
 actual_capital=float(broker.snapshot()["account"]["starting_capital"])
 if abs(actual_capital-account_capital)>0.01:
  raise ValueError(f"Paper database was initialized with INR {actual_capital:.2f}, but profile {a.profile!r} requests INR {account_capital:.2f}. Use the matching profile or a separate --db-path; existing paper history is never reset automatically.")
 telegram_notifier=TelegramEventNotifier(broker.snapshot,state_path=root/"logs"/"telegram_alert_state.json")
 telegram_notifier.start()
 watch_shadow=WatchShadowPortfolio(db_path,initial_capital=account_capital,risk_per_trade=risk_per_trade,max_daily_loss=daily_loss_limit,max_open_positions=max_open_positions)
 candidate_outcomes=CandidateOutcomeTracker(db_path)
 stop=threading.Event();server=None;server_thread=None;reported_dates=set()
 try:
  server=make_server(broker,a.dashboard_host,a.dashboard_port,watch_shadow=watch_shadow)
  server_thread=threading.Thread(target=server.serve_forever,kwargs={"poll_interval":.5},daemon=True,name="paper-dashboard")
  server_thread.start();LOGGER.info("Local dashboard listening at http://%s:%s",a.dashboard_host,a.dashboard_port)
 except OSError:LOGGER.exception("Dashboard could not bind; scan service continues but dashboard is unavailable")
 tick_thread=threading.Thread(target=_ticker_thread,args=(broker,watch_shadow,symbols,stop),kwargs={"universe_mode":a.universe},daemon=True,name="kite-tick-monitor");tick_thread.start()
 LOGGER.info("Paper service started: profile=%s capital=%.2f risk/trade=%.2f min-net-RR=%.2f portfolio-risk-cap=%.2f daily-loss=%.2f max-positions=%s universe=%s top_n=%s",a.profile,account_capital,risk_per_trade,minimum_net_rr,broker.max_portfolio_risk_inr,daily_loss_limit,max_open_positions,a.universe,a.universe_max_symbols)
 LOGGER.info("Paper-only boundary enforced: no real order placement/modification/cancellation.")
 try:
  while True:
   now=datetime.now(IST)
   try:
    boundary=latest_completed_boundary(now,grace_seconds=a.grace_seconds,max_lateness_seconds=a.max_lateness_seconds)
    def after_report(path):
     try:
      watch_result=watch_shadow.process_scan_report(path)
      LOGGER.info("Watch-candidate shadow book after %s: %s",path.name,watch_result)
     except Exception:
      LOGGER.exception("Watch-candidate shadow processing failed; champion account remains unchanged.")
     try:
      candidate_result=candidate_outcomes.process_scan_report(path)
      LOGGER.info("Independent candidate outcomes after %s: %s",path.name,candidate_result)
     except Exception:
      LOGGER.exception("Independent candidate outcome ingestion failed; other paper books continue.")
     result=process_archive(archive,db_path)
     LOGGER.info("Challenger lab updated from archive after %s: %s",path.name,result)
     try:
      outcome_result=candidate_outcomes.process_archive(archive)
      LOGGER.info("Independent candidate outcomes updated after %s: %s",path.name,outcome_result)
     except Exception:
      LOGGER.exception("Independent candidate outcomes could not be advanced from candle archive.")
    def scan(at):
     archive_symbols=sorted(
      set(open_shadow_symbols(db_path))
      | set(watch_shadow.open_symbols())
      | set(candidate_outcomes.open_symbols())
     )
     return run_scanner_process(at,repo_root=root,symbols=a.symbols,paper_journal=journal,scan_ledger=ledger,
      candle_archive_dir=archive,timeout_seconds=a.scan_timeout_seconds,paper_broker=broker,after_report=after_report,
      archive_symbols=archive_symbols,account_capital=account_capital,risk_per_trade=risk_per_trade,
      account_profile=a.profile,minimum_net_rr=minimum_net_rr,
      universe_mode=a.universe,universe_max_symbols=a.universe_max_symbols,
      universe_min_price=a.universe_min_price,universe_max_price=a.universe_max_price,
      universe_min_volume=a.universe_min_volume,universe_min_turnover=a.universe_min_turnover,
      universe_max_quote_age_minutes=a.universe_max_quote_age_minutes,
      paper_entry_max_quote_age_minutes=a.paper_entry_max_quote_age_minutes)
    process_due_boundary(boundary,scan_state,scan)
    closed=broker.square_off_due_positions(now)
    shadow_closed=watch_shadow.square_off_due_positions(now)
    if closed:LOGGER.info("Square-off closed %s champion virtual position(s)",closed)
    if shadow_closed:LOGGER.info("Square-off closed %s watch-shadow virtual position(s)",shadow_closed)
    if now.weekday()<5 and now.time().replace(tzinfo=None)>=dtime(15,20):
     date_key=now.date().isoformat()
     if date_key not in reported_dates:
      _finalize_reports(root=root,db_path=db_path,archive=archive,reports=reports,
       daily_state_file=daily_state,paper_journal=journal,scan_ledger=ledger,
       scheduler_state_file=scan_state,service_log=log_path,symbols=(broker.monitored_symbols() or symbols),
       reason="market close daily summary")
      reported_dates.add(date_key)
   except Exception:LOGGER.exception("Paper service cycle failed; continuing")
   time.sleep(a.poll_seconds)
 except KeyboardInterrupt:
  LOGGER.info("Paper service stopped by keyboard interrupt");return 0
 finally:
  telegram_notifier.stop()
  stop.set()
  if server:server.shutdown();server.server_close()
  if server_thread:server_thread.join(timeout=3)
  tick_thread.join(timeout=3)
  _finalize_reports(root=root,db_path=db_path,archive=archive,reports=reports,
   daily_state_file=daily_state,paper_journal=journal,scan_ledger=ledger,
   scheduler_state_file=scan_state,service_log=log_path,symbols=(broker.monitored_symbols() or symbols),
   reason="manual stop / graceful shutdown")
 return 0
if __name__=="__main__":raise SystemExit(main())
