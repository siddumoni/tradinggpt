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
