from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from types import SimpleNamespace
from src import paper_scan_scheduler as scheduler
def test_report_path_is_resolved_from_scanner_stdout(tmp_path):
 d=tmp_path/"reports";d.mkdir();p=d/"phase6_scan_test.json";p.write_text("{}",encoding="utf-8")
 assert scheduler._extract_report_path("Status: RESEARCH_SCAN_ONLY\nReport: reports/phase6_scan_test.json\n",tmp_path)==p
def test_missing_report_path_returns_none(tmp_path):
 assert scheduler._extract_report_path("Status: RESEARCH_SCAN_ONLY\n",tmp_path) is None
def test_scanner_command_processes_virtual_broker_and_callback(tmp_path,monkeypatch):
 report=tmp_path/"scan.json";report.write_text("{}",encoding="utf-8")
 captured={"command":None,"processed":[],"after":[]}
 def fake_run(command,**kwargs):
  captured["command"]=command
  return SimpleNamespace(returncode=0,stdout=f"Status: RESEARCH_SCAN_ONLY\nReport: {report}\n",stderr="")
 class Broker:
  def process_scan_report(self,path):captured["processed"].append(path);return {"opened":1,"rejected":0,"neutral":0}
 monkeypatch.setattr(scheduler.subprocess,"run",fake_run)
 boundary=datetime(2026,10,26,10,5,tzinfo=ZoneInfo("Asia/Kolkata"))
 code=scheduler.run_scanner_process(boundary,repo_root=tmp_path,symbols="INFY",paper_journal=tmp_path/"journal.csv",scan_ledger=tmp_path/"ledger.csv",candle_archive_dir=tmp_path/"candles",timeout_seconds=10,paper_broker=Broker(),after_report=lambda path:captured["after"].append(path),account_capital=100000,risk_per_trade=500,account_profile="paper",minimum_net_rr=1.7,slippage_bps_per_side=6.5,archive_symbols=["SYRMA"])
 assert code==0 and captured["processed"]==[report] and captured["after"]==[report]
 assert "--profile" in captured["command"] and "paper" in captured["command"]
 assert "--account-capital" in captured["command"] and "100000" in captured["command"]
 assert "--minimum-net-rr" in captured["command"] and "1.7" in captured["command"]
 assert "--slippage-bps-per-side" in captured["command"] and "6.5" in captured["command"]
 assert "--universe" in captured["command"] and "nse_equity" in captured["command"]
 assert "--universe-max-symbols" in captured["command"] and "50" in captured["command"]
 assert "--archive-symbols" in captured["command"] and "SYRMA" in captured["command"]
 assert "--symbols" not in captured["command"]


def test_dynamic_websocket_subscriptions_follow_screened_set_and_open_positions():
 from src import paper_trading_service as service

 class Broker:
  selected = ["RELIANCE", "SBIN"]
  opened = ["RELIANCE"]
  def monitored_symbols(self): return sorted(set(self.selected + self.opened))

 class Shadow:
  opened = ["OLDWATCH"]
  def open_symbols(self): return list(self.opened)

 class FakeWs:
  MODE_FULL = "full"
  def __init__(self): self.added=[]; self.modes=[]; self.removed=[]
  def subscribe(self,tokens): self.added.append(list(tokens))
  def set_mode(self,mode,tokens): self.modes.append((mode,list(tokens)))
  def unsubscribe(self,tokens): self.removed.append(list(tokens))

 broker=Broker(); shadow=Shadow(); ws=FakeWs()
 token_map={"RELIANCE":1,"SBIN":2,"OLDWATCH":3,"TCS":5}
 active=set()
 service._sync_websocket_subscriptions(ws,token_map,active,broker,shadow,["INFY"],"nse_equity")
 assert active=={1,2,3}
 assert ws.added==[[1,2,3]]
 broker.selected=["TCS"]
 broker.opened=["RELIANCE"]
 shadow.opened=[]
 service._sync_websocket_subscriptions(ws,token_map,active,broker,shadow,["INFY"],"nse_equity")
 assert active=={1,5}
 assert ws.added[-1]==[5]
 assert ws.removed[-1]==[2,3]



def test_finalization_uploads_only_after_report_and_checklist_generation(tmp_path, monkeypatch):
 from src import paper_trading_service as service

 sequence=[]
 report={
  "markdown_path":str(tmp_path/"reports"/"paper_daily_2026-11-03.md"),
  "json_path":str(tmp_path/"reports"/"paper_daily_2026-11-03.json"),
  "csv_path":str(tmp_path/"reports"/"paper_daily_2026-11-03_trades.csv"),
 }

 class FakeBroker:
  def __init__(self,*args,**kwargs): pass
  def snapshot(self):
   return {"account":{
    "starting_capital":100000.0,"risk_per_trade":500.0,
    "max_daily_loss":2000.0,"max_open_positions":3,
   }}

 class FakeWatch:
  def __init__(self,*args,**kwargs): pass
  def snapshot(self):
   return {"account":{"starting_capital":100000.0},"research_only":True,
           "orders_sent_to_broker":False}

 def make_report(*args,**kwargs):
  sequence.append("report")
  return report

 def make_checklist(**kwargs):
  sequence.append("checklist")
  return {
   "text":"ready",
   "checklist_path":str(tmp_path/"reports"/"paper_upload_checklist_2026-11-03.txt"),
   "ready_files":12,"total_files":12,"missing_required_files":0,
   "artifacts":[{"path":str(tmp_path/"reports"/"paper_daily_2026-11-03.md")}],
  }

 def upload(**kwargs):
  sequence.append("upload")
  assert kwargs["report_date"].isoformat()=="2026-11-03"
  assert kwargs["artifacts"][0]["path"].endswith("paper_daily_2026-11-03.md")
  return {"ok":True,"repository":"siddumoni/tradinggpt-research-data",
          "remote_folder":"daily/2026-11-03","uploaded_files":12,
          "unchanged_files":0,"commit_sha":"daily-upload-sha"}

 monkeypatch.setattr(service,"process_archive",lambda *a,**k:{"symbols":3})
 monkeypatch.setattr(service,"generate_daily_report",make_report)
 monkeypatch.setattr(service,"PaperBroker",FakeBroker)
 monkeypatch.setattr(service,"WatchShadowPortfolio",FakeWatch)
 monkeypatch.setattr(service,"write_upload_checklist",make_checklist)
 monkeypatch.setattr(service,"upload_daily_artifacts",upload)
 now=datetime(2026,11,3,15,35,tzinfo=ZoneInfo("Asia/Kolkata"))

 result=service._finalize_reports(
  root=tmp_path,db_path=tmp_path/"data"/"paper.sqlite3",
  archive=tmp_path/"data"/"paper_candles_kite",reports=tmp_path/"reports",
  daily_state_file=tmp_path/"logs"/"paper_daily_report_state.json",
  paper_journal=tmp_path/"logs"/"paper_signals.csv",
  scan_ledger=tmp_path/"logs"/"paper_scan_runs.csv",
  scheduler_state_file=tmp_path/"logs"/"paper_scan_scheduler_state.json",
  service_log=tmp_path/"logs"/"paper_trading_service.log",
  symbols=["RELIANCE"],reason="market close daily summary",
 )
 assert result==report
 assert sequence==["report","checklist","upload"]


def test_finalization_skips_upload_when_required_artifacts_are_missing(tmp_path, monkeypatch):
 from src import paper_trading_service as service

 class FakeBroker:
  def __init__(self,*args,**kwargs): pass
  def snapshot(self):
   return {"account":{"starting_capital":100000.0,"risk_per_trade":500.0,
                      "max_daily_loss":2000.0,"max_open_positions":3}}

 class FakeWatch:
  def __init__(self,*args,**kwargs): pass
  def snapshot(self):
   return {"account":{"starting_capital":100000.0}}

 called=[]
 monkeypatch.setattr(service,"process_archive",lambda *a,**k:{})
 monkeypatch.setattr(service,"generate_daily_report",lambda *a,**k:{"markdown_path":"report.md"})
 monkeypatch.setattr(service,"PaperBroker",FakeBroker)
 monkeypatch.setattr(service,"WatchShadowPortfolio",FakeWatch)
 monkeypatch.setattr(service,"write_upload_checklist",lambda **k:{
  "text":"missing","checklist_path":"checklist.txt","ready_files":4,
  "total_files":12,"missing_required_files":2,"artifacts":[],
 })
 monkeypatch.setattr(service,"upload_daily_artifacts",lambda **k:called.append(k))
 service._finalize_reports(
  root=tmp_path,db_path=tmp_path/"paper.sqlite3",archive=tmp_path/"candles",
  reports=tmp_path/"reports",daily_state_file=tmp_path/"daily.json",
  paper_journal=tmp_path/"signals.csv",scan_ledger=tmp_path/"runs.csv",
  scheduler_state_file=tmp_path/"scheduler.json",service_log=tmp_path/"service.log",
  symbols=["RELIANCE"],reason="market close daily summary",
 )
 assert called==[]



def test_websocket_naive_sdk_timestamp_is_converted_from_host_timezone():
    from datetime import timedelta
    from src import paper_trading_service as service

    received_at = datetime.now(ZoneInfo("Asia/Kolkata"))
    sdk_local_naive = datetime.now().replace(microsecond=0)
    stamp = service._websocket_quote_timestamp(
        {
            "exchange_timestamp": sdk_local_naive,
            "last_trade_time": received_at - timedelta(minutes=5),
        },
        received_at,
    )

    assert stamp is not None
    assert abs((received_at - stamp).total_seconds()) < 3


def test_old_last_trade_time_is_not_used_as_websocket_quote_timestamp():
    from datetime import timedelta
    from src import paper_trading_service as service

    received_at = datetime.now(ZoneInfo("Asia/Kolkata"))
    stamp = service._websocket_quote_timestamp(
        {"last_trade_time": received_at - timedelta(minutes=10)},
        received_at,
    )

    assert stamp == received_at


def test_stale_open_position_quote_is_refreshed_from_kite_rest_snapshot():
    from datetime import timedelta
    from src import paper_trading_service as service

    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    quote_timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
    class FakeProvider:
        requested = None
        def quotes(self, symbols):
            self.requested = list(symbols)
            return {
                "NSE:TDPOWERSYS": {
                    "timestamp": quote_timestamp,
                    "last_price": 663.10,
                    "depth": {
                        "buy": [{"price": 663.05, "quantity": 100}],
                        "sell": [{"price": 663.15, "quantity": 90}],
                    },
                }
            }

    class FakeBroker:
        def __init__(self):
            self.updates = []
        def snapshot(self):
            return {"open_positions": [{"symbol": "TDPowersys"}]}
        def update_market_price(self, symbol, last, **kwargs):
            self.updates.append((symbol, last, kwargs))
            return 0

    class FakeWatch:
        def open_symbols(self):
            return []
        def update_market_price(self, symbol, last, **kwargs):
            return 0

    provider, broker, watch = FakeProvider(), FakeBroker(), FakeWatch()
    refreshed = service._refresh_stale_position_quotes(
        provider, broker, watch, {}, now=now
    )

    assert refreshed == 1
    assert provider.requested == ["TDPOWERSYS"]
    assert broker.updates[0][0] == "TDPowersys"
    assert broker.updates[0][1] == 663.10
    assert broker.updates[0][2]["bid"] == 663.05
    assert broker.updates[0][2]["ask"] == 663.15


def test_rest_fallback_rejects_stale_quote_timestamp():
    from datetime import timedelta
    from src import paper_trading_service as service

    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    stale_timestamp = (now - timedelta(minutes=3)).strftime("%Y-%m-%d %H:%M:%S")
    class FakeProvider:
        def quotes(self, symbols):
            return {"NSE:INFY": {"timestamp": stale_timestamp, "last_price": 100.0}}

    class FakeBroker:
        def __init__(self):
            self.updates = []
        def snapshot(self):
            return {"open_positions": [{"symbol": "INFY"}]}
        def update_market_price(self, *args, **kwargs):
            self.updates.append((args, kwargs))
            return 0

    class FakeWatch:
        def open_symbols(self):
            return []
        def update_market_price(self, *args, **kwargs):
            return 0

    broker = FakeBroker()
    refreshed = service._refresh_stale_position_quotes(
        FakeProvider(), broker, FakeWatch(), {}, now=now
    )
    assert refreshed == 0
    assert broker.updates == []
