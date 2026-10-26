from pathlib import Path
from src.paper_scan_scheduler import _extract_report_path
def test_report_path_is_resolved_from_scanner_stdout(tmp_path):
 d=tmp_path/"reports";d.mkdir();p=d/"phase6_scan_test.json";p.write_text("{}",encoding="utf-8")
 assert _extract_report_path("Status: RESEARCH_SCAN_ONLY\nReport: reports/phase6_scan_test.json\n",tmp_path)==p
def test_missing_report_path_returns_none(tmp_path):
 assert _extract_report_path("Status: RESEARCH_SCAN_ONLY\n",tmp_path) is None
