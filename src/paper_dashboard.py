"""Local-only dashboard for the persistent virtual paper account."""
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from src.paper_broker import DEFAULT_DB, PaperBroker
from src.watch_shadow_portfolio import WatchShadowPortfolio


DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#070d18">
<title>TradingGPT Paper Desk</title>
<style>
:root{color-scheme:light;--canvas:#f4f6fb;--surface:#fff;--ink:#172033;--muted:#69758b;--line:#e5e9f1;--nav:#111827;--nav-muted:#aab4c6;--accent:#4f46e5;--accent-soft:#eef2ff;--green:#087f5b;--green-bg:#e7f7f0;--red:#c33b4b;--red-bg:#fff0f1;--amber:#9a6700;--amber-bg:#fff6df;--shadow:0 8px 28px rgba(25,38,66,.055)}
*{box-sizing:border-box}
body{margin:0;background:var(--canvas);color:var(--ink);font:14px/1.5 Inter,Segoe UI,Arial,sans-serif}
button,input{font:inherit}
button{cursor:pointer}
.app{min-height:100vh;display:grid;grid-template-columns:230px minmax(0,1fr)}
.sidebar{height:100vh;position:sticky;top:0;background:var(--nav);color:#fff;padding:24px 16px;display:flex;flex-direction:column;gap:25px}
.brand{display:flex;align-items:center;gap:11px;padding:0 9px}
.brand-mark{width:34px;height:34px;border-radius:11px;background:linear-gradient(135deg,#818cf8,#4f46e5);display:grid;place-items:center;font-weight:800;font-size:16px;box-shadow:0 5px 16px #4f46e555}
.brand-name{font-size:15px;font-weight:750;letter-spacing:.01em}
.brand-sub{font-size:11px;color:var(--nav-muted);margin-top:1px}
.nav-label{font-size:10px;font-weight:700;letter-spacing:.13em;text-transform:uppercase;color:#8290a6;padding:0 11px;margin-bottom:9px}
.nav{display:grid;gap:5px}
.nav-link{border:0;background:transparent;color:var(--nav-muted);text-align:left;padding:11px 12px;border-radius:10px;display:flex;align-items:center;gap:12px;width:100%;font-weight:600;transition:background .15s,color .15s}
.nav-link:hover{background:#ffffff0d;color:#fff}
.nav-link.active{background:#343b67;color:#fff;box-shadow:inset 3px 0 #a5b4fc}
.nav-icon{width:20px;text-align:center;font-size:16px;opacity:.95}
.sidebar-note{margin-top:auto;border:1px solid #ffffff1a;background:#ffffff08;border-radius:12px;padding:13px;display:flex;gap:10px;align-items:flex-start}
.sidebar-note strong{font-size:12px;display:block}
.sidebar-note small{display:block;color:var(--nav-muted);font-size:11px;margin-top:2px}
.dot{width:8px;height:8px;border-radius:50%;background:#34d399;box-shadow:0 0 0 4px #34d39920;margin-top:5px;flex:none}
.main{min-width:0}
.topbar{background:#ffffffd9;backdrop-filter:blur(12px);border-bottom:1px solid var(--line);padding:20px 32px;display:flex;align-items:center;justify-content:space-between;gap:20px;position:sticky;top:0;z-index:10}
.eyebrow{font-size:10px;letter-spacing:.14em;text-transform:uppercase;color:var(--accent);font-weight:800;margin-bottom:4px}
h1{font-size:23px;line-height:1.2;margin:0;font-weight:750;letter-spacing:-.025em}
.topbar p{color:var(--muted);font-size:12px;margin:5px 0 0}
.top-status{display:flex;align-items:flex-end;flex-direction:column;gap:6px;white-space:nowrap}
.status{display:inline-flex;gap:7px;align-items:center;border:1px solid #c8eadc;background:var(--green-bg);color:var(--green);border-radius:999px;padding:5px 10px;font-size:11px;font-weight:750}
.status i{width:6px;height:6px;border-radius:50%;background:currentColor}
.status.offline{color:var(--red);background:var(--red-bg);border-color:#f3c8ce}
.updated{font-size:11px;color:var(--muted)}
.content{max-width:1600px;margin:0 auto;padding:26px 32px 42px}
.view{display:none}
.view.active{display:block}
.welcome{display:flex;justify-content:space-between;align-items:center;gap:20px;background:linear-gradient(115deg,#202b55,#353d78);border-radius:17px;padding:23px 25px;color:#fff;margin-bottom:22px;box-shadow:0 12px 32px #27336b22}
.welcome h2{font-size:19px;margin:0 0 6px;letter-spacing:-.02em}
.welcome p{margin:0;color:#e0e5ff;font-size:12px;max-width:760px}
.welcome-tag{background:#ffffff18;border:1px solid #ffffff35;border-radius:999px;padding:7px 11px;font-size:11px;font-weight:700;white-space:nowrap}
.section-head{display:flex;align-items:flex-end;justify-content:space-between;gap:14px;margin:23px 0 12px}
.section-head h2{font-size:16px;margin:0;font-weight:750;letter-spacing:-.01em}
.section-head p{font-size:11px;color:var(--muted);margin:3px 0 0}
.section-kicker{font-size:10px;color:var(--accent);font-weight:800;text-transform:uppercase;letter-spacing:.12em;margin-bottom:3px}
.btn-link{border:1px solid #dfe3f0;background:#fff;color:var(--accent);border-radius:9px;padding:8px 11px;font-size:11px;font-weight:700}
.btn-link:hover{background:var(--accent-soft);border-color:#c7d2fe}
.btn-close{border:1px solid #efb6bd;background:#fff0f1;color:#a3293b;border-radius:7px;padding:6px 9px;font-size:10px;font-weight:750}.btn-close:hover{background:#ffe1e4}.btn-close:disabled{opacity:.55;cursor:wait}
.kpis{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}
.metric-card,.panel,.track-card{background:var(--surface);border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow)}
.metric-card{padding:15px 16px;min-width:0}
.metric-label{font-size:11px;color:var(--muted);font-weight:600}
.metric-value{font-size:22px;font-weight:780;letter-spacing:-.025em;margin-top:7px;line-height:1.15;overflow-wrap:anywhere;font-variant-numeric:tabular-nums}
.metric-note{font-size:10px;color:var(--muted);margin-top:6px}
.positive{color:var(--green)!important}.negative{color:var(--red)!important}
.panel{padding:17px;min-width:0}
.panel-head{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:13px}
.panel-head h3{font-size:13px;margin:0;font-weight:750}
.panel-head p{color:var(--muted);font-size:10px;margin:3px 0 0}
.pill{font-size:10px;font-weight:750;padding:4px 8px;border-radius:999px;background:#f0f2f7;color:#657087;white-space:nowrap}
.pill.blue{color:var(--accent);background:var(--accent-soft)}
.pill.green{color:var(--green);background:var(--green-bg)}
.pill.amber{color:var(--amber);background:var(--amber-bg)}
.pill.red{color:var(--red);background:var(--red-bg)}
.split{display:grid;grid-template-columns:1.15fr .85fr;gap:14px;margin-top:14px}
.track-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
.track-card{padding:16px;text-align:left;width:100%;color:inherit;transition:border .15s,transform .15s}
.track-card:hover{border-color:#c7cafa;transform:translateY(-1px)}
.track-card h3{font-size:13px;margin:0 0 5px}
.track-card p{font-size:11px;color:var(--muted);margin:0;min-height:34px}
.track-meta{display:flex;justify-content:space-between;align-items:center;margin-top:14px;font-size:10px;color:var(--muted);gap:10px}
.track-arrow{color:var(--accent);font-weight:800}
.risk-wrap{margin:12px 0}
.risk-labels{display:flex;justify-content:space-between;gap:10px;font-size:11px;color:var(--muted)}
.progress{height:9px;border-radius:999px;background:#edf0f6;overflow:hidden;margin:8px 0 6px}
.progress-fill{height:100%;border-radius:999px;background:linear-gradient(90deg,#818cf8,#4f46e5);width:0;transition:width .25s}
.helper{font-size:10px;color:var(--muted)}
.stat-list{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:11px}
.stat-item{padding:10px 11px;background:#f8f9fc;border-radius:10px}
.stat-item span{display:block;color:var(--muted);font-size:10px}
.stat-item strong{display:block;margin-top:4px;font-size:13px;font-variant-numeric:tabular-nums}
.info-box{padding:14px 16px;background:#f7f8ff;border:1px solid #e2e6ff;border-radius:12px;color:#414b71;font-size:11px;margin-bottom:15px}
.info-box strong{color:#27346d}
.info-box p{margin:4px 0 0;line-height:1.6}
.strategy-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin-bottom:14px}
.strategy-card{background:#fff;border:1px solid var(--line);border-radius:13px;padding:15px;min-width:0}
.strategy-top{display:flex;justify-content:space-between;align-items:flex-start;gap:8px}
.strategy-card h3{font-size:12px;margin:0;line-height:1.45}
.strategy-card .desc{font-size:10px;color:var(--muted);margin:7px 0 13px;min-height:30px}
.strategy-stats{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}
.strategy-stats div{background:#f7f8fb;border-radius:8px;padding:8px}
.strategy-stats span{display:block;font-size:9px;color:var(--muted)}
.strategy-stats strong{display:block;font-size:12px;margin-top:3px;overflow-wrap:anywhere}
.table-panel{margin-top:14px}
.table-tools{display:flex;align-items:center;gap:8px}
.search{width:210px;max-width:45vw;border:1px solid var(--line);border-radius:8px;padding:7px 9px;background:#fff;font-size:11px;outline:none}
.search:focus{border-color:#9aa4f5;box-shadow:0 0 0 3px #4f46e514}
.table-wrap{overflow:auto;max-width:100%}
table{width:100%;border-collapse:separate;border-spacing:0;white-space:nowrap;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:10px 11px;border-bottom:1px solid #edf0f5;font-size:10px}
th{font-size:9px;text-transform:uppercase;letter-spacing:.06em;color:#778197;background:#f8f9fc;font-weight:800;position:sticky;top:0}
td{color:#344057}
tbody tr:hover td{background:#fafbff}
td.wrap{white-space:normal;min-width:190px;max-width:340px;line-height:1.45}
td.mono{font-variant-numeric:tabular-nums}
td .pill{display:inline-block}
.empty-cell{text-align:center;color:var(--muted);padding:22px!important;white-space:normal}
.subsection{margin-top:21px}
.subsection h3{font-size:13px;margin:0 0 4px}
.subsection p{font-size:10px;color:var(--muted);margin:0 0 10px}
.footer{margin:24px 2px 0;color:var(--muted);font-size:10px;line-height:1.7}
.footer strong{color:#44506a}
.mobile-nav{display:none}
.api-error{display:none;background:var(--red-bg);border:1px solid #f2c5cb;color:var(--red);border-radius:10px;padding:10px 13px;font-size:11px;margin-bottom:14px}
@media(max-width:1100px){.app{grid-template-columns:200px minmax(0,1fr)}.content{padding:22px}.topbar{padding:18px 22px}.kpis{grid-template-columns:repeat(2,minmax(0,1fr))}.strategy-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:760px){.app{display:block}.sidebar{height:auto;position:sticky;top:0;z-index:20;padding:12px 14px;gap:10px}.brand{padding:0 3px}.sidebar .nav-label,.sidebar-note{display:none}.nav{display:flex;overflow-x:auto;gap:5px;padding-bottom:2px}.nav-link{width:auto;white-space:nowrap;padding:8px 10px;font-size:11px}.nav-icon{width:auto}.topbar{position:static;padding:17px 16px}.top-status{align-items:flex-end}.content{padding:16px}.welcome{padding:18px;align-items:flex-start;flex-direction:column}.split{grid-template-columns:1fr}.track-grid{grid-template-columns:1fr 1fr}}
@media(max-width:480px){.kpis{grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.metric-card{padding:12px}.metric-value{font-size:18px}.strategy-grid{grid-template-columns:1fr}.track-grid{grid-template-columns:1fr}.topbar{gap:10px}.topbar h1{font-size:20px}.updated{white-space:normal;text-align:right;max-width:130px}.section-head{align-items:flex-start}.search{width:145px}.panel{padding:13px}}

/* TradingGPT dark desk theme */
:root{color-scheme:dark;--canvas:#080d18;--surface:#101827;--ink:#e8eef9;--muted:#9aa9bf;--line:#263449;--nav:#070c16;--nav-muted:#a7b5c9;--accent:#60a5fa;--accent-soft:#102943;--green:#4ade80;--green-bg:#0d2b22;--red:#fb7185;--red-bg:#351b25;--amber:#fbbf24;--amber-bg:#352a17;--shadow:0 10px 30px rgba(0,0,0,.24)}
body{background:radial-gradient(ellipse at 70% -20%,#172744 0,transparent 45%),var(--canvas);color:var(--ink)}
.topbar{background:rgba(8,13,24,.92);border-bottom-color:var(--line)}
.welcome{background:linear-gradient(115deg,#102c3d,#172a4c);box-shadow:0 12px 32px rgba(0,0,0,.25)}
.welcome p{color:#c8d7ee}.welcome-tag{background:rgba(96,165,250,.12);border-color:rgba(125,180,245,.28);color:#cfe6ff}
.btn-link,.search{background:#111c2d;border-color:#2a3a50;color:#dce8fa}
.btn-link:hover{background:#142b43;border-color:#3b82f6}
.metric-card,.panel,.track-card{background:linear-gradient(150deg,#121d2d,#0e1725);border-color:#26364b;box-shadow:var(--shadow);color:var(--ink)}
.track-card:hover{border-color:#4777a8}
.metric-label,.metric-note,.panel-head p,.section-head p,.helper,.updated,.footer{color:var(--muted)}
.positive{color:var(--green)!important}.negative{color:var(--red)!important}
.status{border-color:#15543d;background:#0d2b22;color:#4ade80}.status.offline{color:var(--red);background:var(--red-bg);border-color:#713345}
.pill{background:#1a2638;color:#c0cce0}.pill.blue{color:#93c5fd;background:#142c48}.pill.green{color:#4ade80;background:#103526}.pill.amber{color:#fbbf24;background:#392d17}.pill.red{color:#fb7185;background:#3b1c27}
.progress{background:#26354a}.progress-fill{background:linear-gradient(90deg,#2dd4bf,#3b82f6)}
.stat-item,.strategy-stats div{background:#0b1422;border:1px solid #202f43}
.info-box{background:#102239;border-color:#274664;color:#c5d7ed}.info-box strong{color:#e7f2ff}
.strategy-card{background:linear-gradient(150deg,#121d2d,#0e1725);border-color:#26364b;color:var(--ink)}
.strategy-card .desc,.strategy-stats span{color:var(--muted)}
.search:focus{border-color:#60a5fa;box-shadow:0 0 0 3px #3b82f622}.search::placeholder{color:#73839b}
th{background:#172437;color:#a9bad0;border-bottom-color:#2a3a50}
td{color:#d5deec;border-bottom-color:#223147}tbody tr:hover td{background:#17263a}
.empty-cell{color:#94a3b8}.api-error{background:var(--red-bg);border-color:#713345;color:#fecdd3}
.footer strong{color:#dbe7f7}
.nav-link.active{background:linear-gradient(90deg,#123a35,#182d49);box-shadow:inset 3px 0 #34d399}
.brand-mark{background:linear-gradient(135deg,#34d399,#2563eb);box-shadow:0 5px 16px #2563eb44}
.sidebar-note{border-color:#26364b;background:#111c2b}

</style>
</head>
<body>
<div class="app">
<aside class="sidebar">
  <div class="brand"><div class="brand-mark">T</div><div><div class="brand-name">TradingGPT</div><div class="brand-sub">Paper trading desk</div></div></div>
  <div><div class="nav-label">Workspace</div>
    <nav class="nav" id="primary-nav" aria-label="Dashboard sections">
      <button class="nav-link active" type="button" data-view-target="overview"><span class="nav-icon">▦</span>Overview</button>
      <button class="nav-link" type="button" data-view-target="champion"><span class="nav-icon">🏆</span>Champion</button>
      <button class="nav-link" type="button" data-view-target="challenger"><span class="nav-icon">⚗</span>Challenger Lab</button>
      <button class="nav-link" type="button" data-view-target="watch"><span class="nav-icon">◉</span>Watch Book</button>
      <button class="nav-link" type="button" data-view-target="activity"><span class="nav-icon">☷</span>Activity</button>
    </nav>
  </div>
  <div class="sidebar-note"><span class="dot"></span><div><strong>Paper mode only</strong><small>Read-only Kite data. No real orders are sent.</small></div></div>
</aside>
<main class="main">
  <header class="topbar">
    <div><div class="eyebrow">TradingGPT / Paper trading</div><h1 id="page-title">Overview</h1><p id="page-description">A clear summary of your main paper account and research tracks.</p></div>
    <div class="top-status"><span class="status" id="connection-status"><i></i><span>Connecting</span></span><span class="updated" id="updated">Waiting for local data…</span></div>
  </header>
  <div class="content">
    <div class="api-error" id="api-error"></div>

    <section class="view active" data-view="overview">
      <div class="welcome">
        <div><h2>Your paper trading, separated clearly.</h2><p><strong>Champion</strong> is the main virtual account. <strong>Challenger</strong> tests alternative strategies in separate shadow books. <strong>Watch</strong> tracks selected opportunities Champion did not take. Their P&amp;L is never mixed.</p></div>
        <span class="welcome-tag">RESEARCH ONLY · NO LIVE ORDERS</span>
      </div>
      <div class="section-head"><div><div class="section-kicker">Main account</div><h2>Champion at a glance</h2><p>Your primary ₹1 lakh virtual portfolio. These are the figures to check first.</p></div><button class="btn-link" type="button" data-view-target="champion">Open Champion →</button></div>
      <div class="kpis" id="overview-kpis"></div>
      <div class="split">
        <section class="panel">
          <div class="panel-head"><div><h3>Champion risk limits</h3><p>How much planned risk is currently in use.</p></div><span class="pill blue">Main account</span></div>
          <div class="risk-wrap"><div class="risk-labels"><span>Open planned risk</span><strong id="overview-risk-text">—</strong></div><div class="progress"><div class="progress-fill" id="overview-risk-fill"></div></div><div class="helper" id="overview-risk-note">—</div></div>
          <div class="stat-list" id="overview-guardrails"></div>
        </section>
        <section class="panel">
          <div class="panel-head"><div><h3>Open Champion positions</h3><p>Only positions currently held in the main virtual account.</p></div><span class="pill" id="overview-open-count">0 open</span></div>
          <div class="table-wrap"><table><thead><tr><th>Stock</th><th>Side</th><th>Qty</th><th>Net P&amp;L</th></tr></thead><tbody id="overview-open-rows"></tbody></table></div>
        </section>
      </div>
      <div class="section-head"><div><div class="section-kicker">Research tracks</div><h2>What the other sections mean</h2><p>These are separate experiments. Do not add their results to Champion's P&amp;L.</p></div></div>
      <div class="track-grid">
        <button class="track-card" type="button" data-view-target="challenger"><h3>⚗ Challenger Lab</h3><p>Three alternative strategies are tested with simulated trades. “Shadow” simply means paper-only and separate from Champion.</p><div class="track-meta"><span id="overview-challenger-meta">3 experimental strategies</span><span class="track-arrow">View lab →</span></div></button>
        <button class="track-card" type="button" data-view-target="watch"><h3>◉ Watch Book</h3><p>Tracks eligible scanner candidates Champion skipped, mainly because their Target 1 net reward-to-risk was too low.</p><div class="track-meta"><span id="overview-watch-meta">Separate what-if book</span><span class="track-arrow">View watch →</span></div></button>
      </div>
    </section>

    <section class="view" data-view="champion">
      <div class="info-box"><strong>Champion = your main paper account.</strong><p>Only trades accepted by the Champion allocation and risk checks appear here. Its virtual capital, positions and profit/loss are independent of Challenger and Watch.</p></div>
      <div class="kpis" id="champion-kpis"></div>
      <div class="split">
        <section class="panel"><div class="panel-head"><div><h3>Portfolio risk usage</h3><p>Planned risk reserved by open positions.</p></div><span class="pill blue">Risk controls</span></div><div class="risk-wrap"><div class="risk-labels"><span>Used risk budget</span><strong id="champion-risk-text">—</strong></div><div class="progress"><div class="progress-fill" id="champion-risk-fill"></div></div><div class="helper" id="champion-risk-note">—</div></div><div class="stat-list" id="champion-guardrails"></div></section>
        <section class="panel"><div class="panel-head"><div><h3>Performance summary</h3><p>Calculated from closed Champion trades.</p></div><span class="pill">Closed trades only</span></div><div class="stat-list" id="champion-performance"></div></section>
      </div>
      <section class="panel table-panel"><div class="panel-head"><div><h3>Open positions</h3><p>Both targets and after-cost reward-to-risk ratios. Net RRR includes estimated charges and slippage.</p></div><span class="pill" id="champion-open-count">0 open</span></div><div class="table-wrap"><table><thead><tr><th>Stock</th><th>Side</th><th>Qty</th><th>Entry</th><th>Last price</th><th>Unrealised net</th><th>Stop-loss</th><th>Target 1</th><th>Target 2</th><th>Net RRR T1</th><th>Net RRR T2</th><th>Action</th></tr></thead><tbody id="champion-open-rows"></tbody></table></div></section>
      <section class="panel table-panel"><div class="panel-head"><div><h3>Champion trade history</h3><p>Completed trades from the main virtual account. Net P&amp;L is after estimated charges and slippage.</p></div><input class="search" type="search" placeholder="Search history…" aria-label="Search Champion history" data-filter-for="champion-history-rows"></div><div class="table-wrap"><table><thead><tr><th>Exit time</th><th>Stock</th><th>Strategy</th><th>Side</th><th>Qty</th><th>Entry</th><th>Exit</th><th>Charges</th><th>Slippage</th><th>Net P&amp;L</th><th>Exit reason</th></tr></thead><tbody id="champion-history-rows"></tbody></table></div></section>
    </section>

    <section class="view" data-view="challenger">
      <div class="info-box"><strong>Challenger = alternative strategies. Shadow = how they are tested.</strong><p>Each Challenger strategy has its own simulated paper book. These trades do not use Champion's cash and do not change Champion's profit/loss. A strategy marked “Review candidate” has only passed an initial screen; it is not automatically promoted.</p></div>
      <div class="section-head"><div><div class="section-kicker">Three experiments</div><h2>Challenger strategies</h2><p>Compare their results individually. More trades and more market days make the comparison more useful.</p></div><span class="pill blue" id="challenger-count">3 strategies</span></div>
      <div class="strategy-grid" id="challenger-strategy-cards"></div>
      <section class="panel table-panel"><div class="panel-head"><div><h3>Open Challenger positions</h3><p>Both target levels and after-cost RRR for each experimental strategy.</p></div><span class="pill" id="challenger-open-count">0 open</span></div><div class="table-wrap"><table><thead><tr><th>Strategy</th><th>Stock</th><th>Side</th><th>Qty</th><th>Entry</th><th>Last</th><th>Stop-loss</th><th>Target 1</th><th>Target 2</th><th>Net RRR T1</th><th>Net RRR T2</th><th>Unrealised net</th><th>Status</th></tr></thead><tbody id="challenger-open-rows"></tbody></table></div></section>
      <section class="panel table-panel"><div class="panel-head"><div><h3>Challenger trade history</h3><p>Closed shadow trades. Use this to compare each experimental strategy over time.</p></div><input class="search" type="search" placeholder="Search history…" aria-label="Search Challenger history" data-filter-for="challenger-history-rows"></div><div class="table-wrap"><table><thead><tr><th>Exit time</th><th>Strategy</th><th>Stock</th><th>Side</th><th>Qty</th><th>Entry</th><th>Exit</th><th>Charges</th><th>Slippage</th><th>Net P&amp;L</th><th>Exit reason</th></tr></thead><tbody id="challenger-history-rows"></tbody></table></div></section>
      <section class="panel table-panel"><div class="panel-head"><div><h3>Strategy status</h3><p>What each strategy is currently being considered for.</p></div><span class="pill">No automatic promotion</span></div><div class="table-wrap"><table><thead><tr><th>Strategy</th><th>Status</th><th>What it means</th></tr></thead><tbody id="challenger-registry-rows"></tbody></table></div><div class="footer">Initial review screen: at least 50 closed trades across 20 exit dates, positive net P&amp;L, profit factor ≥ 1.2 and drawdown ≤ 10%. Passing this screen only makes a strategy a review candidate. It still needs untouched out-of-sample validation and manual review.</div></section>
    </section>

    <section class="view" data-view="watch">
      <div class="info-box"><strong>Watch = opportunities Champion did not take.</strong><p>The system records scanner candidates rejected specifically by the Target 1 net reward-to-risk rule. It opens a hypothetical Watch position only when live data, margin, news, quote, price levels and account checks also pass. Other candidates appear below as “Not tracked”, with the reason.</p></div>
      <div class="kpis" id="watch-kpis"></div>
      <section class="panel table-panel"><div class="panel-head"><div><h3>Open Watch positions</h3><p>What-if paper trades, separate from Champion. Net RRR includes estimated costs and slippage.</p></div><span class="pill" id="watch-open-count">0 open</span></div><div class="table-wrap"><table><thead><tr><th>Stock</th><th>Side</th><th>Qty</th><th>Entry</th><th>Last</th><th>Stop-loss</th><th>Target 1</th><th>Target 2</th><th>Net RRR T1</th><th>Net RRR T2</th><th>Unrealised net</th></tr></thead><tbody id="watch-open-rows"></tbody></table></div></section>
      <section class="panel table-panel"><div class="panel-head"><div><h3>Watch decision history</h3><p>Shows what was tracked and what was excluded, including the reason.</p></div><input class="search" type="search" placeholder="Search candidates…" aria-label="Search Watch candidates" data-filter-for="watch-audit-rows"></div><div class="table-wrap"><table><thead><tr><th>Time</th><th>Stock</th><th>Side</th><th>Decision</th><th>Target 1 net R:R</th><th>Reason / explanation</th></tr></thead><tbody id="watch-audit-rows"></tbody></table></div></section>
      <section class="panel table-panel"><div class="panel-head"><div><h3>Closed Watch results</h3><p>Outcome of Watch positions after estimated charges and slippage.</p></div><input class="search" type="search" placeholder="Search closed trades…" aria-label="Search Watch history" data-filter-for="watch-history-rows"></div><div class="table-wrap"><table><thead><tr><th>Exit time</th><th>Stock</th><th>Strategy</th><th>Side</th><th>Qty</th><th>Entry</th><th>Exit</th><th>Charges</th><th>Slippage</th><th>Net P&amp;L</th><th>Exit reason</th></tr></thead><tbody id="watch-history-rows"></tbody></table></div></section>
    </section>

    <section class="view" data-view="activity">
      <div class="info-box"><strong>Activity = what the system has been doing.</strong><p>Scanner decisions, paper-account events, warnings and system messages appear here. Check this page when a scan fails or a position does not appear as expected.</p></div>
      <section class="panel"><div class="panel-head"><div><h3>Recent system events</h3><p>Latest 100 recorded events from the paper service.</p></div><input class="search" type="search" placeholder="Search events…" aria-label="Search activity events" data-filter-for="activity-rows"></div><div class="table-wrap"><table><thead><tr><th>Time</th><th>Type</th><th>Severity</th><th>Stock</th><th>Message</th><th>Details</th></tr></thead><tbody id="activity-rows"></tbody></table></div></section>
    </section>

    <div class="footer"><strong>Important:</strong> This dashboard is for research and paper trading only. Positions and P&amp;L are simulated; charges and slippage are estimates. No real orders are placed, modified or cancelled. Keep the service running and the computer awake for continuous updates. Dashboard API is restricted to localhost.</div>
  </div>
</main>
</div>
<script>
const PAGE_META={
  overview:["Overview","A clear summary of your main paper account and research tracks."],
  champion:["Champion","Your main virtual account: positions, risk limits and completed trades."],
  challenger:["Challenger Lab","Compare the three experimental strategies in their separate shadow books."],
  watch:["Watch Book","Review opportunities Champion skipped and what happened to them."],
  activity:["Activity","See scanner decisions, warnings and paper-service events."]
};
const byId=id=>document.getElementById(id);
const val=v=>(v===null||v===undefined||v==="")?null:Number(v);
const fmtMoney=v=>{const n=val(v);if(n===null||!Number.isFinite(n))return "—";return (n<0?"−":"")+"₹"+Math.abs(n).toLocaleString("en-IN",{minimumFractionDigits:2,maximumFractionDigits:2});};
const fmtNum=v=>{const n=val(v);return n===null||!Number.isFinite(n)?"—":n.toLocaleString("en-IN",{maximumFractionDigits:2});};
const fmtPct=v=>{const n=val(v);return n===null||!Number.isFinite(n)?"—":n.toFixed(1)+"%";};
const fmtRrr=v=>{const n=val(v);return n===null||!Number.isFinite(n)?"—":n.toFixed(2)+":1";};
const fmtTime=v=>{if(!v)return "—";const d=new Date(v);return Number.isNaN(d.getTime())?String(v):d.toLocaleString("en-IN",{hour12:false});};
const pnlClass=v=>{const n=val(v);return n===null?"":n>0?"positive":n<0?"negative":"";};
const safeText=v=>(v===null||v===undefined||v==="")?"—":String(v);
function goTo(name){
  if(!PAGE_META[name])return;
  document.querySelectorAll(".view").forEach(v=>v.classList.toggle("active",v.dataset.view===name));
  document.querySelectorAll("[data-view-target]").forEach(b=>b.classList.toggle("active",b.dataset.viewTarget===name));
  byId("page-title").textContent=PAGE_META[name][0];
  byId("page-description").textContent=PAGE_META[name][1];
}
document.addEventListener("click",e=>{const b=e.target.closest("[data-view-target]");if(b)goTo(b.dataset.viewTarget);});
document.addEventListener("click",async e=>{
  const button=e.target.closest("[data-close-trade]");
  if(!button)return;
  const tradeId=button.dataset.closeTrade;
  const symbol=button.closest("tr")?.cells?.[0]?.textContent||"this position";
  if(!window.confirm("Close the "+symbol+" paper position at the latest available quote? Estimated slippage and charges will be applied. This does not place a real order."))return;
  button.disabled=true;button.textContent="Closing…";
  try{
    const response=await fetch("/api/positions/"+encodeURIComponent(tradeId)+"/close",{method:"POST",headers:{"Content-Type":"application/json"}});
    const result=await response.json();
    if(!response.ok)throw new Error(result.error||("HTTP "+response.status));
    window.alert("Paper position closed: "+result.symbol+" · Exit ₹"+Number(result.exit_fill_price).toFixed(2)+" · Net P&L ₹"+Number(result.net_pnl).toFixed(2)+".");
    await refresh();
  }catch(error){
    window.alert("Could not close paper position: "+error.message+" The position remains open.");
    button.disabled=false;button.textContent="Close";
  }
});
document.querySelectorAll("[data-filter-for]").forEach(input=>input.addEventListener("input",()=>{
  const body=byId(input.dataset.filterFor);if(!body)return;const q=input.value.trim().toLowerCase();
  Array.from(body.rows).forEach(row=>{row.hidden=!!q&&!row.textContent.toLowerCase().includes(q);});
}));
function metricCard(label,value,note="",tone=""){
  const card=document.createElement("div");card.className="metric-card";
  const l=document.createElement("div");l.className="metric-label";l.textContent=label;
  const v=document.createElement("div");v.className="metric-value "+tone;v.textContent=value;
  card.append(l,v);
  if(note){const n=document.createElement("div");n.className="metric-note";n.textContent=note;card.append(n);}
  return card;
}
function setMetrics(id,items){
  const root=byId(id);root.replaceChildren();
  items.forEach(x=>root.append(metricCard(x.label,x.value,x.note||"",x.tone||"")));
}
function statGrid(id,items){
  const root=byId(id);root.replaceChildren();
  items.forEach(item=>{const box=document.createElement("div");box.className="stat-item";const label=document.createElement("span");label.textContent=item.label;const value=document.createElement("strong");value.textContent=item.value;box.append(label,value);root.append(box);});
}
function cellText(td,text,cls,title){
  td.textContent=safeText(text);if(cls)td.className=cls;if(title)td.title=title;
}
function renderTable(id,rows,columns,emptyMessage){
  const body=byId(id);body.replaceChildren();
  if(!rows||!rows.length){const tr=document.createElement("tr");const td=document.createElement("td");td.className="empty-cell";td.colSpan=columns.length;td.textContent=emptyMessage||"No records yet. New records will appear here as the service runs.";tr.append(td);body.append(tr);return;}
  rows.forEach(item=>{
    const tr=document.createElement("tr");
    columns.forEach(col=>{const td=document.createElement("td");if(col.render){col.render(td,item);}else{const value=col.value?col.value(item):item[col.key];cellText(td,value,col.cls?col.cls(item):"",col.title?col.title(item):"");}tr.append(td);});
    body.append(tr);
  });
  const inputs=document.querySelectorAll('[data-filter-for="'+id+'"]');
  inputs.forEach(input=>{const q=input.value.trim().toLowerCase();if(q)Array.from(body.rows).forEach(row=>row.hidden=!row.textContent.toLowerCase().includes(q));});
}
function sideCell(td,item){const side=String(item.side||"").toUpperCase();const span=document.createElement("span");span.className="pill "+(side==="LONG"?"green":side==="SHORT"?"red":"");span.textContent=safeText(side);td.append(span);}
function statusCell(td,label,tone){const span=document.createElement("span");span.className="pill "+(tone||"");span.textContent=label;td.append(span);}
function setRisk(prefix,used,cap,available){
  const u=val(used)||0,c=val(cap)||0,a=val(available)||0,pct=c>0?Math.max(0,Math.min(100,u/c*100)):0;
  byId(prefix+"-risk-text").textContent=fmtMoney(u)+" / "+fmtMoney(c);
  byId(prefix+"-risk-fill").style.width=pct.toFixed(1)+"%";
  byId(prefix+"-risk-note").textContent=fmtMoney(a)+" planned-risk budget remains. This is reserved risk, not cash invested.";
}
function render(s){
  const a=s.account||{},opens=s.open_positions||[],closed=s.closed_trades||[];
  const watch=s.watch_shadow||{},wa=watch.account||{};
  const shadowOpen=s.shadow_open_positions||[],shadowClosed=s.shadow_closed_trades||[];
  const strategies=(s.strategies||[]).filter(x=>String(x.track).toUpperCase()==="SHADOW");
  const championNet=(val(a.realized_pnl)||0)+(val(a.unrealized_net_pnl)||0);

  setMetrics("overview-kpis",[
    {label:"Virtual equity",value:fmtMoney(a.equity),note:"Starting capital + realised + unrealised net P&L"},
    {label:"Champion net P&L",value:fmtMoney(championNet),tone:pnlClass(championNet),note:"Realised and open-position P&L combined"},
    {label:"Today's net P&L",value:fmtMoney(a.today_net_pnl),tone:pnlClass(a.today_net_pnl),note:"Champion account only"},
    {label:"Open positions",value:fmtNum(a.open_positions),note:"Maximum "+safeText(a.max_open_positions)+" positions"}
  ]);
  const riskUsed=val(a.open_planned_risk_inr)||0,riskCap=val(a.max_portfolio_risk_inr)||0,riskLeft=val(a.available_portfolio_risk_inr)||0;
  setRisk("overview",riskUsed,riskCap,riskLeft);
  statGrid("overview-guardrails",[
    {label:"Risk per trade",value:fmtMoney(a.risk_per_trade)},
    {label:"Daily loss limit",value:fmtMoney(a.max_daily_loss)},
    {label:"Available virtual cash",value:fmtMoney(a.available_cash)},
    {label:"Open exposure",value:fmtMoney(a.open_exposure)}
  ]);
  byId("overview-open-count").textContent=opens.length+" open";
  renderTable("overview-open-rows",opens.slice(0,5),[
    {key:"symbol"},{render:sideCell},{value:x=>fmtNum(x.quantity)},{value:x=>fmtMoney(x.unrealized_net_pnl),cls:x=>pnlClass(x.unrealized_net_pnl)}
  ],"No open Champion positions right now.");
  byId("overview-challenger-meta").textContent=strategies.length+" strategies · "+fmtNum(strategies.reduce((n,x)=>n+(val(x.trades)||0),0))+" closed trades";
  byId("overview-watch-meta").textContent=fmtNum(wa.candidate_count)+" candidates recorded · separate book";

  setMetrics("champion-kpis",[
    {label:"Virtual equity",value:fmtMoney(a.equity),note:"₹1 lakh initial virtual capital"},
    {label:"Realised net P&L",value:fmtMoney(a.realized_pnl),tone:pnlClass(a.realized_pnl),note:"Completed Champion trades"},
    {label:"Unrealised net P&L",value:fmtMoney(a.unrealized_net_pnl),tone:pnlClass(a.unrealized_net_pnl),note:"Open positions, after estimated costs"},
    {label:"Available virtual cash",value:fmtMoney(a.available_cash),note:"After reserving open position value"}
  ]);
  setRisk("champion",riskUsed,riskCap,riskLeft);
  statGrid("champion-guardrails",[
    {label:"Risk per trade",value:fmtMoney(a.risk_per_trade)},
    {label:"Daily loss limit",value:fmtMoney(a.max_daily_loss)},
    {label:"Max open positions",value:fmtNum(a.max_open_positions)},
    {label:"Current open exposure",value:fmtMoney(a.open_exposure)}
  ]);
  statGrid("champion-performance",[
    {label:"Closed trades",value:fmtNum(a.closed_trades)},
    {label:"Win rate",value:fmtPct(a.win_rate_pct)},
    {label:"Expectancy / trade",value:fmtMoney(a.expectancy_per_trade)},
    {label:"Profit factor",value:a.profit_factor==="infinite"?"∞":fmtNum(a.profit_factor)},
    {label:"Maximum drawdown",value:fmtMoney(a.max_drawdown)},
    {label:"Open planned risk",value:fmtMoney(a.open_planned_risk_inr)}
  ]);
  byId("champion-open-count").textContent=opens.length+" open";
  renderTable("champion-open-rows",opens,[
    {key:"symbol"},{render:sideCell},{value:x=>fmtNum(x.quantity)},{value:x=>fmtMoney(x.entry_fill_price)},
    {value:x=>fmtMoney(x.last_price)},{value:x=>fmtMoney(x.unrealized_net_pnl),cls:x=>pnlClass(x.unrealized_net_pnl)},
    {value:x=>fmtMoney(x.stop_loss)},{value:x=>fmtMoney(x.target_1)},{value:x=>fmtMoney(x.target_2)},
    {value:x=>fmtRrr(x.net_rr_t1)},{value:x=>fmtRrr(x.net_rr_t2)},
    {render:(td,x)=>{const button=document.createElement("button");button.type="button";button.className="btn-close";button.dataset.closeTrade=String(x.id);button.textContent="Close";button.setAttribute("aria-label","Manually close "+safeText(x.symbol)+" paper position");td.append(button);}}
  ],"No open Champion positions. A position will appear here when a qualifying simulated entry is accepted.");
  renderTable("champion-history-rows",closed,[
    {value:x=>fmtTime(x.exit_time)},{key:"symbol"},{key:"strategy"},{render:sideCell},{value:x=>fmtNum(x.quantity)},
    {value:x=>fmtMoney(x.entry_fill_price)},{value:x=>fmtMoney(x.exit_fill_price)},
    {value:x=>fmtMoney(x.transaction_charges)},{value:x=>fmtMoney(x.slippage_cost)},
    {value:x=>fmtMoney(x.net_pnl),cls:x=>pnlClass(x.net_pnl)},{key:"exit_reason",cls:()=>"wrap"}
  ],"No completed Champion trades yet. Open positions are shown in the table above.");

  const registry=s.strategy_registry||[];
  const registryByVersion=new Map(registry.map(x=>[x.strategy_version,x]));
  byId("challenger-count").textContent=strategies.length+" strategies";
  const cards=byId("challenger-strategy-cards");cards.replaceChildren();
  const descriptions={
    challenger_ema_9_21_v1:"EMA 9/21 crossover with VWAP and MACD confirmation.",
    challenger_vwap_momentum_v1:"VWAP crossover confirmed by EMA direction and MACD.",
    challenger_ema_pullback_v1:"Trend-aligned pullback toward EMA 21 with ATR-based levels."
  };
  strategies.forEach(st=>{
    const reg=registryByVersion.get(st.strategy_version)||{};
    const card=document.createElement("article");card.className="strategy-card";
    const top=document.createElement("div");top.className="strategy-top";
    const title=document.createElement("h3");title.textContent=safeText(st.strategy);
    const rawStatus=String(reg.status||"TESTING");
    const badge=document.createElement("span");badge.className="pill "+(rawStatus==="REVIEW_CANDIDATE"?"amber":"blue");
    badge.textContent=rawStatus==="REVIEW_CANDIDATE"?"Review candidate":rawStatus==="ACTIVE"?"Active":"Testing";
    top.append(title,badge);
    const desc=document.createElement("p");desc.className="desc";desc.textContent=descriptions[st.strategy_version]||"Experimental Challenger strategy.";
    const stats=document.createElement("div");stats.className="strategy-stats";
    const values=[
      ["Closed trades",fmtNum(st.trades)],
      ["Win rate",fmtPct(st.win_rate_pct)],
      ["Expectancy",fmtMoney(st.expectancy)],
      ["Net P&L",fmtMoney(st.net_pnl)]
    ];
    values.forEach(pair=>{const box=document.createElement("div");const l=document.createElement("span");l.textContent=pair[0];const v=document.createElement("strong");v.textContent=pair[1];if(pair[0]==="Net P&L")v.className=pnlClass(st.net_pnl);box.append(l,v);stats.append(box);});
    card.append(top,desc,stats);cards.append(card);
  });
  if(!strategies.length){const note=document.createElement("div");note.className="panel";note.textContent="Challenger strategy results will appear after the strategy lab processes archived completed candles.";cards.append(note);}
  byId("challenger-open-count").textContent=shadowOpen.length+" open";
  renderTable("challenger-open-rows",shadowOpen,[
    {key:"strategy_name"},{key:"symbol"},{render:sideCell},{value:x=>fmtNum(x.quantity)},
    {value:x=>fmtMoney(x.entry_fill_price)},{value:x=>fmtMoney(x.last_price)},
    {value:x=>fmtMoney(x.stop_loss)},{value:x=>fmtMoney(x.target_1)},{value:x=>fmtMoney(x.target_2)},
    {value:x=>fmtRrr(x.net_rr_t1)},{value:x=>fmtRrr(x.net_rr_t2)},
    {value:x=>fmtMoney(x.unrealized_net_pnl),cls:x=>pnlClass(x.unrealized_net_pnl)},
    {render:(td,x)=>statusCell(td,"Shadow paper","blue")}
  ],"No open Challenger positions at the moment.");
  renderTable("challenger-history-rows",shadowClosed,[
    {value:x=>fmtTime(x.exit_time)},{key:"strategy_name"},{key:"symbol"},{render:sideCell},{value:x=>fmtNum(x.quantity)},
    {value:x=>fmtMoney(x.entry_fill_price)},{value:x=>fmtMoney(x.exit_fill_price)},
    {value:x=>fmtMoney(x.transaction_charges)},{value:x=>fmtMoney(x.slippage_cost)},
    {value:x=>fmtMoney(x.net_pnl),cls:x=>pnlClass(x.net_pnl)},{key:"exit_reason",cls:()=>"wrap"}
  ],"No completed Challenger shadow trades yet. Keep the service running to collect prospective results.");
  renderTable("challenger-registry-rows",registry.filter(x=>String(x.track).toUpperCase()==="CHALLENGER"),[
    {key:"strategy_name"},
    {render:(td,x)=>{const st=String(x.status||"TESTING");statusCell(td,st==="REVIEW_CANDIDATE"?"Review candidate":st==="ACTIVE"?"Active":"Testing",st==="REVIEW_CANDIDATE"?"amber":"blue");}},
    {value:x=>x.description||"Still collecting paper observations.",cls:()=>"wrap"}
  ],"No strategy registry entries are available yet.");

  setMetrics("watch-kpis",[
    {label:"Watch realised net P&L",value:fmtMoney(wa.realized_pnl),tone:pnlClass(wa.realized_pnl),note:"Closed Watch trades only"},
    {label:"Watch unrealised net P&L",value:fmtMoney(wa.unrealized_net_pnl),tone:pnlClass(wa.unrealized_net_pnl),note:"Currently open Watch positions"},
    {label:"Open Watch positions",value:fmtNum(wa.open_positions),note:"Separate from Champion"},
    {label:"Closed Watch trades",value:fmtNum(wa.closed_trades),note:"Hypothetical results"},
    {label:"Candidates not tracked",value:fmtNum(wa.excluded_candidates),note:"The reason is shown in decision history"},
    {label:"Watch win rate",value:fmtPct(wa.win_rate_pct),note:"Based on closed Watch trades"}
  ]);
  const watchOpen=watch.open_positions||[],watchSignals=watch.recent_signals||[],watchClosed=watch.closed_trades||[];
  byId("watch-open-count").textContent=watchOpen.length+" open";
  renderTable("watch-open-rows",watchOpen,[
    {key:"symbol"},{render:sideCell},{value:x=>fmtNum(x.quantity)},{value:x=>fmtMoney(x.entry_fill_price)},
    {value:x=>fmtMoney(x.last_price)},{value:x=>fmtMoney(x.stop_loss)},
    {value:x=>fmtMoney(x.target_1)},{value:x=>fmtMoney(x.target_2)},
    {value:x=>fmtRrr(x.net_rr_t1)},{value:x=>fmtRrr(x.net_rr_t2)},
    {value:x=>fmtMoney(x.unrealized_net_pnl),cls:x=>pnlClass(x.unrealized_net_pnl)}
  ],"No Watch positions are open. This is normal when no eligible near-miss passes all safety checks.");
  renderTable("watch-audit-rows",watchSignals,[
    {value:x=>fmtTime(x.created_at)},{key:"symbol"},{render:sideCell},
    {render:(td,x)=>{const st=String(x.status||"");const label=st==="OPEN"?"Tracked · open":st==="CLOSED"?"Tracked · closed":st==="EXCLUDED"?"Not tracked":"Pending";statusCell(td,label,st==="EXCLUDED"?"amber":"blue");}},
    {value:x=>x.assessed_net_rr_t1==null?"—":fmtNum(x.assessed_net_rr_t1)+":1"},
    {value:x=>x.exclusion_reason||x.source_rejection_reason||"Eligible Watch candidate; outcome is being monitored.",cls:()=>"wrap"}
  ],"No Watch candidates recorded yet. Scanner near-misses will be listed here with their tracking decision.");
  renderTable("watch-history-rows",watchClosed,[
    {value:x=>fmtTime(x.exit_time)},{key:"symbol"},{key:"strategy"},{render:sideCell},{value:x=>fmtNum(x.quantity)},
    {value:x=>fmtMoney(x.entry_fill_price)},{value:x=>fmtMoney(x.exit_fill_price)},
    {value:x=>fmtMoney(x.transaction_charges)},{value:x=>fmtMoney(x.slippage_cost)},
    {value:x=>fmtMoney(x.net_pnl),cls:x=>pnlClass(x.net_pnl)},{key:"exit_reason",cls:()=>"wrap"}
  ],"No completed Watch trades yet.");

  const events=s.events||[];
  renderTable("activity-rows",events,[
    {value:x=>fmtTime(x.event_time)},{key:"event_type"},
    {render:(td,x)=>{const st=String(x.severity||"INFO").toUpperCase();statusCell(td,st,st==="ERROR"||st==="CRITICAL"?"red":st==="WARNING"?"amber":"blue");}},
    {key:"symbol"},{key:"message",cls:()=>"wrap"},
    {value:x=>JSON.stringify(x.details||{}),cls:()=>"wrap",title:x=>JSON.stringify(x.details||{})}
  ],"No system events yet. Scanner and paper-service activity will appear here.");
  const updated=s.updated_at?fmtTime(s.updated_at):"—";
  byId("updated").textContent="Last data refresh: "+updated;
  const status=byId("connection-status");
  status.className="status";status.innerHTML="<i></i><span>Dashboard connected</span>";
  if(s.research_only!==true||s.orders_sent_to_broker!==false){
    status.className="status offline";status.innerHTML="<i></i><span>Safety state needs review</span>";
    byId("api-error").style.display="block";
    byId("api-error").textContent="The API safety flags are not in the expected research-only state. Do not rely on this dashboard until the service state is checked.";
  } else {
    byId("api-error").style.display="none";
  }
}
async function refresh(){
  try{
    const response=await fetch("/api/state",{cache:"no-store"});
    if(!response.ok)throw new Error("HTTP "+response.status);
    render(await response.json());
  }catch(error){
    const status=byId("connection-status");status.className="status offline";status.innerHTML="<i></i><span>API unavailable</span>";
    byId("api-error").style.display="block";byId("api-error").textContent="Cannot read the paper-service API: "+error.message+". Check that the paper trading service is running.";
    byId("updated").textContent="Waiting for the local API…";
  }
}
refresh();setInterval(refresh,3000);
</script>
</body>
</html>"""


def make_handler(
    broker: PaperBroker,
    watch_shadow: WatchShadowPortfolio | None = None,
) -> type[BaseHTTPRequestHandler]:
    watch_shadow = watch_shadow or WatchShadowPortfolio(broker.db_path)

    class Handler(BaseHTTPRequestHandler):
        server_version = "TradingGPTPaperDesk"
        sys_version = ""

        def do_GET(self):
            route = urlparse(self.path).path
            if route == "/":
                body = DASHBOARD_HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if route == "/api/state":
                try:
                    state = broker.snapshot()
                    state["watch_shadow"] = watch_shadow.snapshot()
                    body = json.dumps(
                        state, ensure_ascii=False, default=str, allow_nan=False
                    ).encode("utf-8")
                    status = 200
                except Exception as exc:
                    body = json.dumps(
                        {"error": f"Dashboard state unavailable: {type(exc).__name__}"}
                    ).encode("utf-8")
                    status = 500
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if route == "/health":
                body = b'{"ok":true,"research_only":true,"orders_sent_to_broker":false}'
                status = 200
            else:
                body = b'{"error":"not found"}'
                status = 404
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            route = urlparse(self.path).path
            prefix = "/api/positions/"
            suffix = "/close"
            if not (route.startswith(prefix) and route.endswith(suffix)):
                body = b'{"error":"not found"}'
                self.send_response(404)
            else:
                raw_id = route[len(prefix):-len(suffix)]
                try:
                    if not raw_id.isdigit():
                        raise ValueError("Invalid paper position ID.")
                    result = broker.close_position(int(raw_id))
                    body = json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
                    self.send_response(200)
                except ValueError as exc:
                    body = json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8")
                    self.send_response(409)
                except Exception as exc:
                    body = json.dumps({"error": f"Manual paper exit failed: {type(exc).__name__}"}).encode("utf-8")
                    self.send_response(500)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            return

    return Handler


def make_server(
    broker: PaperBroker,
    host: str = "127.0.0.1",
    port: int = 8000,
    watch_shadow: WatchShadowPortfolio | None = None,
) -> ThreadingHTTPServer:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Dashboard must bind to localhost; remote interfaces are disabled.")
    server = ThreadingHTTPServer(
        (host, int(port)), make_handler(broker, watch_shadow)
    )
    server.daemon_threads = True
    return server


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Serve the local TradingGPT paper-trading dashboard."
    )
    parser.add_argument("--db-path", default=str(DEFAULT_DB))
    parser.add_argument(
        "--host", default="127.0.0.1", choices=("127.0.0.1", "localhost")
    )
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    broker = PaperBroker(args.db_path)
    server = make_server(broker, args.host, args.port)
    print(f"TradingGPT paper dashboard: http://{args.host}:{args.port}")
    print("Research-only mode. No broker orders are sent.")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
