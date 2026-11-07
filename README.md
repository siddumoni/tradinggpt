# TradingGPT — NSE Stock Scanner & Paper-Trading Research

TradingGPT is a tool that watches NSE stocks, checks possible intraday trading setups, and tests them using a virtual account. It is built for research and learning—not for automatically trading real money.

> **Important:** TradingGPT is advisory-only. It can read market data from Zerodha Kite, but it does not place, change, or cancel real orders. Paper-trading results are simulated and do not guarantee profits.

## What it does

- Scans the NSE cash-equity market and selects liquid stocks for closer analysis.
- Checks market conditions, price and volume, previous-day support/resistance, surveillance status, news, and data quality.
- Looks for intraday setups using strategies such as VWAP, EMA and opening-range patterns.
- Rechecks the live bid/ask price before approving a setup. If the price, data, or risk checks fail, the setup is kept on a watchlist instead.
- Calculates position size, stop-loss, targets, estimated charges, slippage and risk/reward.
- Runs a **Champion** virtual portfolio and separate **Challenger** and **Watch** research portfolios.
- Provides a local dashboard, daily reports, trade history and optional Telegram alerts.
- Saves research reports to a separate private GitHub repository when automatic uploads are configured.

## How the project works

1. **Connect:** Sign in to Zerodha Kite. TradingGPT uses the session only to read market data and perform permitted checks.
2. **Scan:** On each completed 5-minute candle, it checks the NSE equity list and selects the most liquid eligible stocks.
3. **Find setups:** It studies price, volume and technical indicators to identify possible LONG or SHORT opportunities.
4. **Apply safety checks:** It checks data freshness, market eligibility, price levels, news, margin estimates and trading costs. It then verifies the executable bid/ask price again.
5. **Make a decision:** A setup either passes the rules, becomes watch-only, or is rejected. Failed checks do not get bypassed.
6. **Simulate:** Approved candidates can enter the Champion virtual account if cash, position, daily-loss and portfolio-risk limits allow it. Other research candidates are tracked separately.
7. **Monitor:** The dashboard shows decisions and virtual positions. The service tracks exits, stop-losses, targets and estimated costs without sending broker orders.
8. **Review:** At the end of the day, it creates reports and an upload checklist. Configured private GitHub uploads and optional Telegram alerts help with review.
9. **Validate:** Historical tests and new paper-trading sessions are analysed for after-cost returns, drawdown, consistency and sample size before any strategy is considered for promotion.

## Project status

- **Development:** Core scanner, safety checks, paper-trading engine, dashboard, reporting and research tools are implemented.
- **Current focus:** Prospective paper validation—collecting new sessions and completed trades to see whether the strategies perform consistently.
- **Not yet proven:** A working scanner or a successful test suite does not prove a profitable strategy. Do not use real money based only on backtests or a small sample of paper trades.

The next validation milestone requires at least **100 accepted, completed trades across 20 independent sessions**, a positive lower bound on the 95% expectancy confidence interval at **8 bps slippage per side**, a profit factor of at least **1.0**, and no single stock or session contributing more than **30% of positive net P&L**. See [Phase 6 readiness](docs/PHASE6_READINESS.md) for the full rules.

## Start here (Windows 11 / PowerShell)

Requirements: Python 3.13 (64-bit recommended), Git, and optional Zerodha Kite Connect credentials.

```powershell
git clone https://github.com/siddumoni/tradinggpt.git
cd tradinggpt
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Add Kite credentials to your local `.env` only if you use Kite. Never commit API keys, access tokens or the `.env` file.

Sign in and start the integrated paper-trading service:

```powershell
python -m src.main login
python -m src.main check
python -m src.paper_trading_service
```

Open the dashboard at **http://127.0.0.1:8000**. Keep the computer awake and the Windows user signed in while the service is running. For automatic weekday scans, see the [Windows scheduler guide](docs/PAPER_SCAN_SCHEDULER.md).

## Virtual account and safety

The default integrated paper account uses **₹1,00,000 virtual capital**, **₹500 planned risk per trade**, a **₹2,000 daily loss limit**, up to **3 open positions**, and estimated slippage and transaction charges. These are simulation settings, not money held at Zerodha. The service also has separate research and live-advisory profiles; every profile remains unable to place real orders.

The dashboard is local to your computer. The Champion account is kept separate from Challenger and Watch research results so experimental signals cannot improve or distort Champion performance.

## Research and project documentation

- [Delivery phases](docs/PHASES.md) — project milestones and progress.
- [Phase 6 readiness](docs/PHASE6_READINESS.md) — what must be proven before promotion.
- [Research protocol](docs/RESEARCH_PROTOCOL.md) — how to compare strategies fairly.
- [Market safety](docs/MARKET_SAFETY.md) and [NSE scanner guide](docs/NSE_UNIVERSE_SCANNER.md) — stock filters and safety checks.
- [Paper portfolio allocation](docs/PAPER_PORTFOLIO_ALLOCATION.md) — virtual capital and risk controls.
- [Results](docs/RESULTS.md) and [performance statistics](docs/PERFORMANCE_STATS.md) — how results are measured.
- [Private GitHub report uploads](docs/GITHUB_RESEARCH_UPLOAD.md) — daily research backup setup.
- [Telegram alerts](docs/TELEGRAM_ALERTS.md) — optional paper-trading notifications.

Run the automated checks before contributing:

```powershell
python -m pytest -q
```

**Bottom line:** TradingGPT scans, filters, evaluates, simulates and reports. The software is built to support disciplined research; only fresh, independent paper-trading evidence can show whether a strategy is worth taking further.
