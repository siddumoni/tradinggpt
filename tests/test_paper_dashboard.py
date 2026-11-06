import json,threading
from urllib.request import urlopen
import pytest
from src.paper_broker import PaperBroker
from src.paper_dashboard import make_server
def test_dashboard_is_localhost_only(tmp_path):
 with pytest.raises(ValueError,match="localhost"): make_server(PaperBroker(tmp_path/"paper.db"),host="0.0.0.0",port=8000)
def test_dashboard_home_health_and_state_api(tmp_path):
 b=PaperBroker(tmp_path/"paper.db"); s=make_server(b,port=0); t=threading.Thread(target=s.serve_forever,kwargs={"poll_interval":.02},daemon=True); t.start()
 try:
  base=f"http://127.0.0.1:{s.server_address[1]}"
  with urlopen(base+"/",timeout=3) as r: assert r.status==200 and "TradingGPT Paper Desk" in r.read().decode()
  with urlopen(base+"/health",timeout=3) as r: assert json.loads(r.read())["orders_sent_to_broker"] is False
  with urlopen(base+"/api/state",timeout=3) as r:
   state=json.loads(r.read()); assert state["research_only"] is True and state["account"]["starting_capital"]==100000 and state["account"]["open_positions"]==0
 finally: s.shutdown(); s.server_close(); t.join(timeout=2)


def test_dashboard_has_clear_separate_navigation_and_explanations():
    from src.paper_dashboard import DASHBOARD_HTML

    assert 'data-view-target="overview"' in DASHBOARD_HTML
    assert 'data-view-target="champion"' in DASHBOARD_HTML
    assert 'data-view-target="challenger"' in DASHBOARD_HTML
    assert 'data-view-target="watch"' in DASHBOARD_HTML
    assert 'data-view-target="activity"' in DASHBOARD_HTML
    assert "Challenger = alternative strategies. Shadow = how they are tested." in DASHBOARD_HTML
    assert "Watch = opportunities Champion did not take." in DASHBOARD_HTML
    assert "No real orders are sent." in DASHBOARD_HTML


def test_dashboard_displays_both_targets_net_rrr_and_dark_theme():
    from src.paper_dashboard import DASHBOARD_HTML

    assert "Net RRR T1" in DASHBOARD_HTML
    assert "Net RRR T2" in DASHBOARD_HTML
    assert 'color-scheme:dark' in DASHBOARD_HTML
    assert "<th>Target 1</th><th>Target 2</th>" in DASHBOARD_HTML


def test_dashboard_manual_close_endpoint_closes_position_and_emits_paper_exit(tmp_path):
    from urllib.request import Request
    from datetime import datetime
    from zoneinfo import ZoneInfo

    broker = PaperBroker(tmp_path / "paper.db")
    now = datetime.now(ZoneInfo("Asia/Kolkata")).isoformat()
    candidate = {
        "symbol": "INFY", "action": "CONDITIONAL_BUY", "side": "LONG",
        "strategy": "vwap_trend", "confidence": 80, "market_regime": "BULLISH",
        "signal_time": now, "reference_entry": 100.0, "stop_loss": 98.0,
        "target_1": 104.0, "target_2": 108.0, "quantity": 99,
        "data_verified_live": True, "margin_verified": True,
        "company_specific_risk": "LOW", "reasons": ["dashboard test"],
    }
    quote = {"last_price": 100.0, "bid": 99.99, "ask": 100.01,
             "timestamp": now, "verified_live": True}
    report = tmp_path / "scan.json"
    report.write_text(json.dumps({
        "status": "RESEARCH_SCAN_ONLY", "as_of": now, "provider": "Kite",
        "data_provider_mode": "kite", "advisory_only": True,
        "orders_placed_or_modified": False, "candidates": [candidate],
        "market_snapshots": {"INFY": quote},
    }), encoding="utf-8")
    assert broker.process_scan_report(report)["opened"] == 1
    trade = broker.snapshot()["open_positions"][0]

    server = make_server(broker, port=0)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
    thread.start()
    try:
        request = Request(
            f"http://127.0.0.1:{server.server_address[1]}/api/positions/{trade['id']}/close",
            data=b"{}", method="POST", headers={"Content-Type": "application/json"},
        )
        with urlopen(request, timeout=3) as response:
            result = json.loads(response.read())
            assert response.status == 200
            assert result["exit_reason"] == "MANUAL_EXIT"
        state = broker.snapshot()
        assert state["account"]["open_positions"] == 0
        assert any(e["event_type"] == "PAPER_EXIT" for e in state["events"])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_dashboard_manual_close_endpoint_rejects_invalid_route(tmp_path):
    from urllib.request import Request
    from urllib.error import HTTPError

    server = make_server(PaperBroker(tmp_path / "paper.db"), port=0)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
    thread.start()
    try:
        request = Request(
            f"http://127.0.0.1:{server.server_address[1]}/api/not-a-position/close",
            data=b"{}", method="POST", headers={"Content-Type": "application/json"},
        )
        with pytest.raises(HTTPError) as exc:
            urlopen(request, timeout=3)
        assert exc.value.code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_dashboard_manual_close_requires_json_content_type(tmp_path):
    from urllib.request import Request
    from urllib.error import HTTPError

    server = make_server(PaperBroker(tmp_path / "paper.db"), port=0)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
    thread.start()
    try:
        request = Request(
            f"http://127.0.0.1:{server.server_address[1]}/api/positions/1/close",
            data=b"{}", method="POST", headers={"Content-Type": "text/plain"},
        )
        with pytest.raises(HTTPError) as exc:
            urlopen(request, timeout=3)
        assert exc.value.code == 415
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
