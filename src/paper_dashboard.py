"""Local-only localhost dashboard for the persistent virtual paper account."""
from __future__ import annotations
import argparse,json
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse
from src.paper_broker import DEFAULT_DB,PaperBroker

DASHBOARD_HTML = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>TradingGPT Paper Desk</title>
<style>
:root{color-scheme:dark;--bg:#0b1020;--panel:#141b2e;--text:#edf2ff;--muted:#98a6c5;--line:#2b3855;--green:#6de0b0;--red:#ff8492}*{box-sizing:border-box}body{margin:0;background:linear-gradient(150deg,#0b1020,#111b31);color:var(--text);font:14px/1.5 Segoe UI,Arial,sans-serif}main{max-width:1500px;margin:auto;padding:24px}.top{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;margin-bottom:20px}h1{font-size:26px;margin:0}h2{font-size:16px;margin:0 0 14px}.sub,.small,footer{color:var(--muted)}.sub{margin-top:5px}.pill{border:1px solid #2b755d;color:var(--green);padding:5px 10px;border-radius:999px;font-size:12px;background:#103127}.cards{display:grid;grid-template-columns:repeat(4,minmax(160px,1fr));gap:12px;margin-bottom:14px}.card,.panel{background:linear-gradient(155deg,var(--panel),#11192a);border:1px solid var(--line);border-radius:14px;padding:16px;box-shadow:0 8px 24px #0002}.metric-label{color:var(--muted);font-size:12px}.metric{font-size:25px;font-weight:700;margin-top:4px;overflow-wrap:anywhere}.grid2{display:grid;grid-template-columns:1.2fr .8fr;gap:14px;margin:14px 0}.panel{min-width:0;overflow:hidden}.scroll{overflow:auto;max-height:480px}table{width:100%;border-collapse:collapse;white-space:nowrap;font-variant-numeric:tabular-nums}th,td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--line);font-size:12px}th{position:sticky;top:0;background:#172139;color:var(--muted);font-weight:600}td.reason{white-space:normal;min-width:260px;max-width:520px}.green{color:var(--green)}.red{color:var(--red)}.small{font-size:12px}.section-title{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:12px}.empty{color:var(--muted);padding:18px}footer{font-size:12px;margin:20px 0 8px}@media(max-width:950px){main{padding:14px}.cards{grid-template-columns:repeat(2,minmax(140px,1fr))}.grid2{grid-template-columns:1fr}}@media(max-width:460px){.cards{grid-template-columns:1fr 1fr}.metric{font-size:20px}}
</style></head><body><main>
<div class="top"><div><h1>TradingGPT Paper Desk</h1><div class="sub">Kite market observations · ₹1,00,000 virtual capital · research only</div></div><div class="pill">● PAPER MODE · NO BROKER ORDERS</div></div>
<div class="cards" id="metrics"></div>
<div class="grid2"><section class="panel"><div class="section-title"><h2>Open positions</h2><span class="small" id="position-count">0 positions</span></div><div class="scroll"><table><thead><tr><th>Symbol</th><th>Side</th><th>Qty</th><th>Entry fill</th><th>Last</th><th>Unrealised net</th><th>Stop</th><th>Target</th></tr></thead><tbody id="open-rows"></tbody></table></div></section>
<section class="panel"><div class="section-title"><h2>Performance</h2><span class="small">closed trades only</span></div><div class="scroll"><table><thead><tr><th>Metric</th><th>Value</th></tr></thead><tbody id="performance-rows"></tbody></table></div></section></div>
<section class="panel" style="margin-bottom:14px"><div class="section-title"><h2>Challenger shadow positions</h2><span class="small">separate virtual books</span></div><div class="scroll"><table><thead><tr><th>Strategy</th><th>Symbol</th><th>Side</th><th>Qty</th><th>Entry fill</th><th>Last</th><th>Unrealised net</th></tr></thead><tbody id="shadow-open-rows"></tbody></table></div></section>
<section class="panel" style="margin-bottom:14px"><div class="section-title"><h2>Trade history</h2><span class="small">up to 100 latest completed trades</span></div><div class="scroll"><table><thead><tr><th>Exit time</th><th>Symbol</th><th>Strategy</th><th>Side</th><th>Qty</th><th>Entry</th><th>Exit</th><th>Gross P&amp;L</th><th>Brokerage</th><th>Charges</th><th>Slippage</th><th>Net P&amp;L</th><th>Exit reason</th></tr></thead><tbody id="closed-rows"></tbody></table></div></section>
<div class="grid2"><section class="panel"><div class="section-title"><h2>Strategy leaderboard</h2><span class="small">versioned paper results</span></div><div class="scroll"><table><thead><tr><th>Track</th><th>Strategy</th><th>Version</th><th>Trades</th><th>Win rate</th><th>Expectancy</th><th>Net P&amp;L</th></tr></thead><tbody id="strategy-rows"></tbody></table></div></section>
<section class="panel"><div class="section-title"><h2>Strategy registry</h2><span class="small">challengers stay in shadow track</span></div><div class="scroll"><table><thead><tr><th>Name</th><th>Track</th><th>Status</th><th>Description</th></tr></thead><tbody id="registry-rows"></tbody></table></div></section></div>
<section class="panel" style="margin-bottom:14px"><div class="section-title"><h2>Challenger trade history</h2><span class="small">shadow results, not champion P&amp;L</span></div><div class="scroll"><table><thead><tr><th>Exit time</th><th>Strategy</th><th>Symbol</th><th>Side</th><th>Qty</th><th>Entry</th><th>Exit</th><th>Charges</th><th>Slippage</th><th>Net P&amp;L</th><th>Exit reason</th></tr></thead><tbody id="shadow-closed-rows"></tbody></table></div></section>
<section class="panel" style="margin-top:14px"><div class="section-title"><h2>Decision and system events</h2><span class="small">latest 100</span></div><div class="scroll"><table><thead><tr><th>Time</th><th>Type</th><th>Severity</th><th>Symbol</th><th>Message</th><th>Details</th></tr></thead><tbody id="event-rows"></tbody></table></div></section>
<footer><div id="updated">Waiting for local data…</div><div>Paper executions are hypothetical. Costs/slippage are estimates. A paper profit is not a guarantee of repeatable edge. API binds to localhost only.</div></footer>
</main><script>
const money=v=>Number.isFinite(Number(v))?'₹'+Number(v).toLocaleString('en-IN',{minimumFractionDigits:2,maximumFractionDigits:2}):'—';const num=v=>Number.isFinite(Number(v))?Number(v).toLocaleString('en-IN',{maximumFractionDigits:2}):'—';const pct=v=>Number.isFinite(Number(v))?Number(v).toFixed(1)+'%':'—';const when=v=>{if(!v)return '—';try{return new Date(v).toLocaleString('en-IN',{hour12:false})}catch{return String(v)}};
function td(row,value,cls=''){const c=document.createElement('td');c.textContent=value==null?'—':String(value);if(cls)c.className=cls;row.appendChild(c);return c}
function populate(id,rows,cols){const body=document.getElementById(id);body.replaceChildren();if(!rows.length){const r=body.insertRow();const c=td(r,'No records yet');c.colSpan=cols.length;c.className='empty';return}for(const item of rows){const r=body.insertRow();for(const col of cols){const c=td(r,col.value?col.value(item):item[col.key],col.cls?col.cls(item):'');if(col.title)c.title=col.title(item)}}}
function metric(label,value,cls=''){const d=document.createElement('div');d.className='card';const l=document.createElement('div');l.className='metric-label';l.textContent=label;const v=document.createElement('div');v.className='metric '+cls;v.textContent=value;d.append(l,v);return d}
function render(s){const a=s.account||{};const m=document.getElementById('metrics');m.replaceChildren(metric('Virtual equity',money(a.equity)),metric('Net realised P&L',money(a.realized_pnl),a.realized_pnl>0?'green':a.realized_pnl<0?'red':''),metric('Unrealised net P&L',money(a.unrealized_net_pnl),a.unrealized_net_pnl>0?'green':a.unrealized_net_pnl<0?'red':''),metric('Available virtual cash',money(a.available_cash)),metric('Open exposure',money(a.open_exposure)),metric('Closed trades',num(a.closed_trades)),metric('Win rate',pct(a.win_rate_pct)),metric('Today net P&L',money(a.today_net_pnl),a.today_net_pnl>0?'green':a.today_net_pnl<0?'red':''));document.getElementById('position-count').textContent=(s.open_positions||[]).length+' positions';
populate('open-rows',s.open_positions||[],[{key:'symbol'},{key:'side'},{key:'quantity'},{value:x=>money(x.entry_fill_price)},{value:x=>money(x.last_price)},{value:x=>money(x.unrealized_net_pnl),cls:x=>x.unrealized_net_pnl>0?'green':x.unrealized_net_pnl<0?'red':''},{value:x=>money(x.stop_loss)},{value:x=>money(x.target_2)}]);
populate('performance-rows',[{label:'Starting capital',value:money(a.starting_capital)},{label:'Realised P&L',value:money(a.realized_pnl)},{label:'Expectancy / closed trade',value:money(a.expectancy_per_trade)},{label:'Profit factor',value:a.profit_factor==='infinite'?'∞':num(a.profit_factor)},{label:'Maximum drawdown',value:money(a.max_drawdown)},{label:'Planned risk / trade',value:money(a.risk_per_trade)},{label:'Daily loss limit',value:money(a.max_daily_loss)},{label:'Open positions / limit',value:(a.open_positions||0)+' / '+(a.max_open_positions||0)}],[{value:x=>x.label},{value:x=>x.value}]);
populate('closed-rows',s.closed_trades||[],[{value:x=>when(x.exit_time)},{key:'symbol'},{key:'strategy'},{key:'side'},{key:'quantity'},{value:x=>money(x.entry_fill_price)},{value:x=>money(x.exit_fill_price)},{value:x=>money(x.gross_pnl)},{value:x=>money(x.brokerage)},{value:x=>money(x.transaction_charges)},{value:x=>money(x.slippage_cost)},{value:x=>money(x.net_pnl),cls:x=>x.net_pnl>0?'green':x.net_pnl<0?'red':''},{key:'exit_reason'}]);
populate('shadow-open-rows',s.shadow_open_positions||[],[{key:'strategy_name'},{key:'symbol'},{key:'side'},{key:'quantity'},{value:x=>money(x.entry_fill_price)},{value:x=>money(x.last_price)},{value:x=>money(x.unrealized_net_pnl),cls:x=>x.unrealized_net_pnl>0?'green':x.unrealized_net_pnl<0?'red':''}]);
populate('shadow-closed-rows',s.shadow_closed_trades||[],[{value:x=>when(x.exit_time)},{key:'strategy_name'},{key:'symbol'},{key:'side'},{key:'quantity'},{value:x=>money(x.entry_fill_price)},{value:x=>money(x.exit_fill_price)},{value:x=>money(x.transaction_charges)},{value:x=>money(x.slippage_cost)},{value:x=>money(x.net_pnl),cls:x=>x.net_pnl>0?'green':x.net_pnl<0?'red':''},{key:'exit_reason'}]);
populate('strategy-rows',s.strategies||[],[{key:'track'},{key:'strategy'},{key:'strategy_version'},{key:'trades'},{value:x=>pct(x.win_rate_pct)},{value:x=>money(x.expectancy)},{value:x=>money(x.net_pnl),cls:x=>x.net_pnl>0?'green':x.net_pnl<0?'red':''}]);
populate('registry-rows',s.strategy_registry||[],[{key:'strategy_name'},{key:'track'},{key:'status'},{key:'description'}]);
populate('event-rows',s.events||[],[{value:x=>when(x.event_time)},{key:'event_type'},{key:'severity'},{key:'symbol'},{key:'message'},{value:x=>JSON.stringify(x.details||{}),title:x=>JSON.stringify(x.details||{})}]);
document.getElementById('updated').textContent='Last refresh: '+when(s.updated_at)+' · research-only: '+s.research_only+' · real broker orders sent: '+s.orders_sent_to_broker}
async function refresh(){try{const r=await fetch('/api/state',{cache:'no-store'});if(!r.ok)throw new Error('HTTP '+r.status);render(await r.json())}catch(e){document.getElementById('updated').textContent='Dashboard API unavailable: '+e.message}}refresh();setInterval(refresh,3000);
</script></body></html>"""

def make_handler(broker:PaperBroker)->type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version="TradingGPTPaperDesk"; sys_version=""
        def do_GET(self):
            route=urlparse(self.path).path
            if route=="/":
                body=DASHBOARD_HTML.encode()
                self.send_response(200); self.send_header("Content-Type","text/html; charset=utf-8"); self.send_header("Cache-Control","no-store"); self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body); return
            if route=="/api/state":
                try: body=json.dumps(broker.snapshot(),ensure_ascii=False,default=str,allow_nan=False).encode(); status=200
                except Exception as exc: body=json.dumps({"error":f"Dashboard state unavailable: {type(exc).__name__}"}).encode(); status=500
                self.send_response(status); self.send_header("Content-Type","application/json; charset=utf-8"); self.send_header("Cache-Control","no-store"); self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body); return
            if route=="/health": body=b'{"ok":true,"research_only":true,"orders_sent_to_broker":false}'; status=200
            else: body=b'{"error":"not found"}'; status=404
            self.send_response(status); self.send_header("Content-Type","application/json; charset=utf-8"); self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body)
        def log_message(self,fmt,*args): return
    return Handler

def make_server(broker:PaperBroker,host:str="127.0.0.1",port:int=8000)->ThreadingHTTPServer:
    if host not in {"127.0.0.1","localhost","::1"}: raise ValueError("Dashboard must bind to localhost; remote interfaces are disabled.")
    server=ThreadingHTTPServer((host,int(port)),make_handler(broker)); server.daemon_threads=True; return server

def main(argv=None)->int:
    parser=argparse.ArgumentParser(description="Serve the local TradingGPT paper-trading dashboard.")
    parser.add_argument("--db-path",default=str(DEFAULT_DB)); parser.add_argument("--host",default="127.0.0.1",choices=("127.0.0.1","localhost")); parser.add_argument("--port",type=int,default=8000)
    args=parser.parse_args(argv); broker=PaperBroker(args.db_path); server=make_server(broker,args.host,args.port)
    print(f"TradingGPT paper dashboard: http://{args.host}:{args.port}"); print("Research-only mode. No broker orders are sent.")
    try: server.serve_forever(poll_interval=.5)
    except KeyboardInterrupt: return 0
    finally: server.server_close()
    return 0
if __name__=="__main__": raise SystemExit(main())
