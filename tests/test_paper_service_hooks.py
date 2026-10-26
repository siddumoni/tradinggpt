from pathlib import Path
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
 code=scheduler.run_scanner_process(None,repo_root=tmp_path,symbols="INFY",paper_journal=tmp_path/"journal.csv",scan_ledger=tmp_path/"ledger.csv",candle_archive_dir=tmp_path/"candles",timeout_seconds=10,paper_broker=Broker(),after_report=lambda path:captured["after"].append(path),account_capital=100000,risk_per_trade=500)
 assert code==0 and captured["processed"]==[report] and captured["after"]==[report]
 assert "--account-capital" in captured["command"] and "100000" in captured["command"]
