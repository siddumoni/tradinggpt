# TradingGPT — NSE Advisory-Only Intraday Research Assistant

TradingGPT is a Python research and advisory project for NSE equities. It combines market-data helpers, technical indicators, candidate intraday strategies, risk checks, news/context helpers, historical backtesting, and validation reports.

**Safety boundary:** the project is advisory-only. It must not place, modify, or cancel orders. Backtests and scanner signals are research outputs, not promises of profit or recommendations to trade.

## What is in the repository

- **Authentication and session checks:** Zerodha Kite Connect login/session helpers.
- **Market-data adapters:** Kite-related fetch helpers and a Yahoo Finance historical-data adapter.
- **Indicators and strategies:** EMA, RSI, MACD, ATR, session VWAP, VWAP trend/pullback, and opening-range logic.
- **Risk and costs:** risk assessment, cash-only notional sizing in the backtester, and estimated NSE intraday charges.
- **Research tools:** fixed-level trade simulator, multi-stock/slippage sensitivity runner, frozen-trade-set slippage repricing, shared-capital portfolio replay, prospective paper-signal journal/outcome evaluator, candle data-quality audit, and chronological validation.
- **Tests:** offline unit and regression tests. Tests should not require a live brokerage session.

The modules are separate components; a passing unit suite does not prove the strategy has a repeatable positive edge.

## Requirements

- Windows 11
- Python 3.13 (64-bit recommended)
- VS Code with the Python extension
- Optional Zerodha Kite Connect app credentials for authentication/API workflows

## Setup in PowerShell

Run from the cloned repository root:

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Add your Kite API credentials to `.env` only if you use the Kite login flow. Never commit `.env`, API secrets, access tokens, or screenshots containing them.

If PowerShell blocks activation, use this only in the current terminal session:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

## Authentication commands

```powershell
python -m src.main login
python -m src.main check
```

The login flow opens the broker login URL, then asks you to paste the short-lived request-token value from your registered redirect URL. The session check is intended to verify the cached session through a read-only profile request.

The token cache is plaintext under `.local\kite_token.json`; treat it like a password. The `.env` and `.local/` paths are excluded by `.gitignore`. If a secret is ever committed, rotate/revoke it—the later deletion of a file does not remove it from Git history.

## Run the test suite

```powershell
python -m pytest -q
python -m compileall -q src tests
```

Unit tests are expected to use dummy values and local data. Do not add tests that submit real orders or require live market access.

## Historical research workflow

### 1. Run a reproducible multi-stock baseline

```powershell
python -m src.multi_stock_validation --days 59 --risk-per-trade 200 --output-dir reports/backtests/phase9
```

The script caches the downloaded candle set and uses it for the scenarios in that run. If you intentionally want new data, pass `--refresh-data`; record that choice because refreshing changes the sample. The provider's returned coverage can be shorter than the requested calendar-day window.

### 2. Audit the cached candles

```powershell
python -m src.data_quality_audit --data-dir reports/backtests/phase9/candle_cache --days-label 59d --symbols RELIANCE,HDFCBANK,ICICIBANK,SBIN,INFY
```

Inspect both summary and per-session reports. Missing candles are not reconstructed. A session with a missing required bar or invalid OHLCV must not quietly enter the backtest.

### 3. Run a chronological holdout diagnostic

```powershell
python -m src.chronological_validation --data-dir reports/backtests/phase9/candle_cache --days-label 59d --candidates HDFCBANK:vwap_trend,INFY:vwap_pullback --train-fraction 0.70 --risk-per-trade 200
```

The runner requires at least 10 complete sessions in both chronological partitions and reports when the test sample has fewer than 30 trades (configurable with `--minimum-test-trades`). This threshold is a warning label, not proof of statistical significance or profitability.

**Important selection-bias rule:** if you chose a stock, strategy, or parameter after inspecting the same period's backtest results, a later split of those same dates is not an untouched out-of-sample test. Treat it as a diagnostic. Freeze the candidate list and settings, then test them prospectively on unseen sessions before considering real-money use.

Read [the research protocol](docs/RESEARCH_PROTOCOL.md) before comparing strategy results.

## Evaluate paper outcomes

After the journal has enough later 5-minute data, evaluate the saved signals with a next-bar-open hypothesis and conservative stop/target handling:

```powershell
python -m src.paper_outcomes --journal logs/paper_signals.csv --candles-dir data/paper_candles --output reports/paper_outcomes.csv
python -m src.portfolio_replay --trades reports/paper_outcomes_portfolio_candidates.csv --initial-capital 20000 --risk-per-trade 200 --slippage-bps 5 --max-concurrent-positions 3
```

The outcome report preserves pending/invalid/gap statuses and does not treat individual overlapping signals as portfolio results. Read [the paper outcome evaluator guide](docs/PAPER_OUTCOME_EVALUATOR.md) before interpreting the output.

## Prospective strategy forward test

To test the frozen candidates selected in the historical study using new Kite candles (without reconstructing old signals), run the separate prospective evaluator alongside the integrated paper-trading service in another terminal:

```powershell
python -m src.prospective_strategy_evaluator --candles-dir data/paper_candles_kite --candidates HDFCBANK:vwap_trend,INFY:vwap_pullback
```

The first launch seeds a per-symbol watermark at the current archive head and skips pre-start bars. Keep it running as new completed candles are appended. It uses the same baseline strategy callbacks, ₹20,000 individual-signal sizing, ₹500 risk budget, fixed 1.5 after-cost Target 2 net R:R gate (matching the historical single-target backtest), and 2/5/8 bps per-side slippage scenarios. Only signals rejected specifically by the net R:R gate get a separate research-only shadow outcome; other risk/data failures stay fail-closed. Reports are written to `reports/prospective_strategy_evaluation`. Individual signals may overlap, so their summed P&L is not portfolio P&L. Read [the forward evaluator guide](docs/PROSPECTIVE_STRATEGY_EVALUATOR.md) before interpreting results. This process has no order-placement path and does not change the integrated paper broker.

## NSE equity universe scanner

A separate, read-only market-wide scan mode can screen the available Kite NSE cash-equity instrument universe by fresh quotes and liquidity, then run the existing technical/risk/news pipeline on the highest-ranked symbols. It does **not** automatically add the whole exchange universe to the integrated paper account.

After authenticating with Kite, run:

```powershell
python -m src.main login
python -m src.main check
python -m src.advisory_engine --provider kite --universe nse_equity --universe-max-symbols 50 --output-dir reports --verbose
```

The scanner obtains the current NSE instrument dump (cached for the IST calendar day), requests full quotes in batches of at most 500, and selects fresh NSE EQ quotes using configurable price, current-day volume and turnover-proxy filters. The default deep-analysis limit is 50 stocks. Current turnover ranking uses `average_price × current-day volume`; it is a screening proxy, **not** an exchange-reported turnover value or an average-daily-volume calculation. Symbols with missing/stale/invalid quotes or failed filters are excluded and counted in the JSON report.

The selected symbols reuse their already-fetched quote snapshots. For each selected symbol, the scanner fetches current-session 5-minute and 15-minute candles and reuses the existing indicator, signal, risk, news and read-only margin checks. It skips the daily-history request in this fast intraday path. The report includes the universe filter settings, available instrument count, quote coverage, eligible count, exclusions and selected symbols.

Optional thresholds:

```powershell
python -m src.advisory_engine --provider kite --universe nse_equity --universe-max-symbols 75 --universe-min-price 50 --universe-max-price 10000 --universe-min-volume 10000 --universe-min-turnover 5000000 --universe-max-quote-age-minutes 15 --output-dir reports --verbose
```

This universe mode is deliberately separate from the existing three-symbol integrated paper service. It produces an advisory research report only; it does not place, modify or cancel orders, change Champion/Challenger/Watch-Shadow books, or relax the minimum after-cost R:R / risk gates. Review scan time, quote coverage, API errors and the report's exclusions before interpreting a result. A top-50 ranking is not a prediction of future returns or proof that the selected symbols are tradable.

See [the NSE universe scanner guide](docs/NSE_UNIVERSE_SCANNER.md) for the filtering methodology, Kite API pacing and operational limitations.

## Use authenticated Kite market data

The scanner now accepts `--provider kite` for read-only stock quotes and 5/15-minute candles from the authenticated Kite Connect API. Log in through the existing manual flow first:

```powershell
python -m src.main login
python -m src.main check
python -m src.advisory_engine --provider kite --symbols RELIANCE,HDFCBANK,INFY --paper-journal logs/paper_signals.csv --candle-archive-dir data/paper_candles
```

This option needs valid Kite credentials and a same-day session. A quote must include a positive last price and timestamp no more than 15 minutes old for the stock-data trust flag to pass. Missing/stale quotes fail closed. In Kite mode, the NIFTY 50 regime is fetched from Kite historical candles when the index instrument is present; Yahoo is not used as a fallback. For directional candidates, the scanner calls Kite's read-only order-margin calculator for a hypothetical one-share NSE MIS limit order. This does not submit an order. Missing, invalid or unavailable margin estimates fail closed. The report records the index source and each margin check. Use this for research/paper journaling, not live trade decisions.

## Automate paper scans on Windows

To scan automatically after each completed 5-minute candle, install the local scheduler and Windows Task Scheduler entry once:

```powershell
git pull origin main
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m src.main check
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install_paper_scan_task.ps1
```

The task launches `src.paper_scan_scheduler` at user sign-in and starts it immediately when installed. It checks every five seconds, waits 10 seconds after the candle boundary for data publication, and scans on weekdays from 09:20 through 15:15 IST. It runs the existing Kite scanner with the paper signal journal, scan ledger, and Kite-only candle archive. A persistent state file prevents repeated attempts for the same candle; stale boundaries are skipped rather than replayed.

Keep the Windows user signed in and the PC awake. The task uses that user's cached Kite session and does not run while the computer is asleep or powered off. It does not place, modify, or cancel orders. For log monitoring and stop/uninstall instructions, see [the automatic scheduler guide](docs/PAPER_SCAN_SCHEDULER.md).

## Virtual paper broker and local dashboard

The virtual account is separate from Kite's brokerage account and uses no real-order API. It starts with ₹1,00,000 virtual capital, ₹500 planned risk per entry, a ₹2,000 daily loss limit, at most three open positions, no leverage, and 5 bps per-side slippage by default. The persistent SQLite ledger tracks simulated entry/exit prices, quantity, stop/targets, estimated fees, slippage, gross/net P&L, and strategy reasons.

Start the dashboard locally:

```powershell
python -m src.paper_dashboard --db-path data/paper_trading.sqlite3 --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000`. The dashboard binds to localhost only. The simulator accepts only fresh, verified Kite reports with approved conditional candidates, verified margin, valid price levels, sufficient virtual cash and passing risk/after-cost reward-to-risk checks. No actual Kite orders are submitted. Quote snapshots are included in saved Kite reports for the simulator; stale quotes are rejected.

The integrated service now runs the scheduler, live read-only Kite tick monitor, champion virtual broker, challenger shadow portfolios, dashboard and daily report generator in one process. After pulling these updates, rerun the Windows task installer once so Task Scheduler switches from the scan-only module to the integrated service.

## Run the integrated paper-trading service

The integrated service runs the candle scheduler, read-only Kite WebSocket monitor, champion virtual account, isolated challenger shadow portfolios, the watch-candidate shadow book, localhost dashboard and daily reports together.

Foreground command: python -m src.paper_trading_service --symbols RELIANCE,HDFCBANK,INFY

Open http://127.0.0.1:8000. After pulling updates, rerun the Windows task installer once so Task Scheduler starts src.paper_trading_service instead of the old scan-only module. Keep the Windows user signed in and the PC awake. If the Kite session expires, authenticate again using python -m src.main login.

Daily outputs are written to `reports/paper_daily_YYYY-MM-DD.md`, `.json` and `_trades.csv`. All three files distinguish the champion account, challenger strategy lab, and watch-candidate shadow book. The combined trade CSV includes the three tracks. Challenger and watch-shadow books never affect champion statistics and are never automatically promoted. The report also lists closed trades whose realised simulated loss exceeded their recorded per-trade risk budget.

### Watch-candidate shadow book

The integrated service also maintains a separate cash-only shadow book for directional `WATCH_CANDIDATE` signals rejected specifically by the scanner's minimum **net** reward-to-risk gate. It does not lower or bypass that gate for the champion account. A shadow entry is recorded only when the saved report is Kite-only, the no-order assertion is explicit, live data and margin are verified, company-specific risk is not HIGH, news checks are complete, the quote is fresh, and best bid/ask are available. Other watch candidates are shown in the dashboard's decision audit with their exclusion reason.

The shadow book uses the service's virtual capital/risk settings, a maximum-position limit, 5 bps adverse slippage per side, estimated transaction charges, stop-loss/target-2 exits, and a fresh-quote square-off at 15:15 IST. Its P&L and trade history remain completely separate from champion and challenger statistics. Outcomes are prospective observations—not evidence of a profitable edge and never real Kite orders. Existing saved reports are not replayed automatically; observations begin as new reports arrive after you update and restart the service.

### End-of-session report and upload checklist

At or after **15:20 IST** on a weekday, the service writes the daily report and prints an **UPLOAD CHECKLIST** in the console. When you stop the service with **Ctrl+C**, it refreshes the report and prints the checklist again, including when you stop it before market close. The checklist is also saved as `reports/paper_upload_checklist_YYYY-MM-DD.txt`. Allow the shutdown messages to finish before closing the terminal; do not use Task Manager/force-kill if you want the final report written.

During end-of-session finalization, challenger shadow entries are reconciled: a signal whose next-bar entry is at/after the 14:45 IST entry cutoff becomes `EXPIRED_NO_ENTRY`; an otherwise eligible signal whose next-bar candle is missing becomes `INCOMPLETE_DATA_GAP`. Neither status is treated as a filled trade, and no price is fabricated. The project's final modeled candle is timestamped 15:10 and completes at the configured 15:15 square-off boundary.

For the next review, upload the three daily report files plus the operational inputs listed as REQUIRED in the checklist:
- `logs/paper_daily_report_state.json`
- `logs/paper_scan_runs.csv`
- `logs/paper_signals.csv`
- `logs/paper_scan_scheduler_state.json`
- `logs/paper_trading_service.log`
- Each selected symbol's `data/paper_candles_kite/SYMBOL.csv` archive.

The SQLite paper-account database is listed as optional but can help when investigating full persistent trade/event history. **Never upload Kite API keys, access tokens, `.env` files, or browser profiles.** A missing file is explicitly marked in the checklist, so you can tell whether the file was not created or is merely optional.



## Capture prospective paper observations

To append directional candidates from each scan to the idempotent signal journal (without pretending candidates are filled trades), add `--paper-journal`:

```powershell
python -m src.advisory_engine --provider kite --symbols RELIANCE,HDFCBANK,INFY --paper-journal logs/paper_signals.csv --candle-archive-dir data/paper_candles_kite
```

When `--paper-journal` is enabled, completed 5-minute candles are archived to `data/paper_candles` by default unless `--candle-archive-dir` is specified. The scanner also appends a row to `logs/paper_scan_runs.csv` by default. This **scan-run ledger includes successful scans with zero directional signals**, plus candidate counts, quote-verification totals, margin-check pass/fail counts, fetch errors, and candle-archive health. Use `--scan-ledger PATH` to choose another path or enable the run ledger without a signal journal.

The signal journal remains selective: it stores only directional setups, so an empty signal journal can be normal when strategies return `NO_TRADE`. The separate ledger makes those no-trade runs auditable without manufacturing paper signals. The candle archive records `provider_mode` and refuses to mix Yahoo and Kite history for the same symbol. The outcome evaluator remains separate; see [the paper signal journal guide](docs/PAPER_SIGNAL_JOURNAL.md).

## Replay selected signals with shared capital

Per-symbol backtests do not model several stocks competing for one account balance. Use the shared-capital replay with **one selected baseline trade log per intended candidate stream**:

```powershell
python -m src.portfolio_replay --trades reports/backtests/RELIANCE_vwap_trend_trades.csv reports/backtests/INFY_vwap_pullback_trades.csv --initial-capital 20000 --risk-per-trade 200 --slippage-bps 5 --max-concurrent-positions 3
```

The replay records accepted/rejected candidates, allocated quantity, reserved capital, estimated costs and net P&L. It replays saved trades rather than regenerating signals, so it is an allocation diagnostic—not a full portfolio backtest. See [the portfolio replay guide](docs/PORTFOLIO_REPLAY.md).

## Isolate slippage cost from trade selection

The ordinary multi-stock sensitivity runner reruns the strategy for each slippage assumption; the minimum-net-R:R rule can therefore change the accepted trade set. To price the **same saved trades** at multiple slippage values, use the frozen-trade-set repricer:

```powershell
python -m src.slippage_repricing --trades reports/backtests/BASELINE_trades.csv --slippage-bps 2,5,8
```

It produces a summary CSV and a trade-level details CSV. Each source file is reported independently; alternative scenario logs often overlap and must not be summed as a portfolio. See [the frozen-trade repricing guide](docs/FROZEN_TRADE_SLIPPAGE_REPRICING.md).

## Current default backtest assumptions

- Starting capital: ₹20,000.
- Planned loss budget per trade: ₹200 by default; configurable to any finite positive amount.
- Minimum net reward/risk: 1.5 by default after estimated charges and configured slippage.
- Entry cutoff: 14:45 IST.
- Intended square-off: 15:15 IST.
- Maximum three entries per day in the fixed-level backtester.
- Cash-only notional sizing; the backtest does **not** assume MIS leverage.
- When a stop and target are both touched in the same OHLC bar, stop-loss is assumed first.
- Estimated costs and slippage are approximations; actual executed fills, gaps, order count, charges, and contract notes can differ. Realized loss can exceed the planned risk budget.

These are research-model assumptions, not guarantees about actual execution. Check `config/settings.yaml` and the backtest configuration before interpreting any report.

## Data and cost limitations

Yahoo Finance is an unofficial data source, is not endorsed by Yahoo, and is intended for personal use under its applicable terms. Its intraday history can be incomplete or limited, and it does not provide exchange-grade market depth. Do not treat a latest candle as a guaranteed live quote.

Transaction-charge rates are configurable estimates. Verify the current broker/exchange charges and applicable statutory levies before relying on net P&L estimates.

## Development principles

1. Preserve advisory-only behavior—no order placement, modification, or cancellation.
2. Never synthesize missing candles to make a backtest pass.
3. Keep all candidate selection and parameter tuning separate from final holdout evaluation.
4. Report net-of-cost metrics, sample size, drawdown, exclusions, and slippage sensitivity.
5. Prefer robust performance across unseen time periods and symbols over the highest in-sample P&L.
6. Do not promote a candidate to live trading on the basis of this tool's backtest alone.
