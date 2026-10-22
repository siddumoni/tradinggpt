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

## Use authenticated Kite market data

The scanner now accepts `--provider kite` for read-only stock quotes and 5/15-minute candles from the authenticated Kite Connect API. Log in through the existing manual flow first:

```powershell
python -m src.main login
python -m src.main check
python -m src.advisory_engine --provider kite --symbols RELIANCE,HDFCBANK,INFY --paper-journal logs/paper_signals.csv --candle-archive-dir data/paper_candles
```

This option needs valid Kite credentials and a same-day session. A quote must include a positive last price and timestamp no more than 15 minutes old for the stock-data trust flag to pass. Missing/stale quotes fail closed. **The NIFTY 50 index regime still comes from unofficial Yahoo Finance data, and margin is not verified**, so conditional entry approval remains blocked. Use this for research/paper journaling, not live trade decisions.

## Capture prospective paper observations

To append directional candidates from each scan to an idempotent CSV journal (without pretending candidates are filled trades), add `--paper-journal`:

```powershell
python -m src.advisory_engine --symbols RELIANCE,HDFCBANK,INFY --paper-journal logs/paper_signals.csv
```

When `--paper-journal` is enabled, completed 5-minute candles are also archived automatically to `data/paper_candles` unless `--candle-archive-dir` is specified. The archive records `provider_mode` and refuses to mix Yahoo and Kite history for the same symbol. The journal records scanner timestamp, signal levels, provider-verification flags and a watch-only/paper-only status. The current Yahoo development feed and missing margin estimate are intentionally not trade-approved. The journal does not calculate outcomes yet. See [the paper signal journal guide](docs/PAPER_SIGNAL_JOURNAL.md).

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
