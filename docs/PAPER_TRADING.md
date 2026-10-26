# Virtual paper broker and local dashboard

## Safety and scope

This module is a research-only simulator. It does not call Kite order placement, modification, or cancellation APIs. A virtual position is not a real fill, and fees/slippage are estimates. The broker rejects missing, stale or unverified quote inputs rather than inventing prices.

The default virtual account has ₹1,00,000 starting capital, ₹500 planned risk per trade, a ₹2,000 daily loss limit, a maximum of three simultaneous positions, no leverage, and five basis points of adverse slippage per side. These are configurable research defaults, not proven optimal risk settings. Account capital is written when the SQLite database is first created and is not reset by restarting the program.

## Dashboard

```powershell
python -m src.paper_dashboard --db-path data/paper_trading.sqlite3 --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000`. The API binds to a loopback interface and does not accept remote binding. Refresh happens every three seconds.

The dashboard shows virtual equity, realised/unrealised P&L, open exposure, virtual cash, positions, closed trades, fees, slippage, exits, strategy grouping and recent decision events. The research registry is seeded with the frozen champion and named challenger candidates; it does not automatically promote or rewrite any strategy.

## Integrated automation

The Windows scheduled task runs the integrated service rather than the scan-only loop. It starts the localhost dashboard, subscribes to read-only Kite WebSocket ticks to monitor virtual exits, runs the frozen champion scanner after completed five-minute candles, updates independent challenger shadow portfolios, and creates Markdown/JSON/CSV daily reports.

After pulling the new version, rerun the installer once so Task Scheduler replaces the old module target. The signed-in Windows user and the PC must remain active, and the daily Kite session must be valid. No real broker orders are submitted.

## Challenger strategy lab

Three alternatives are evaluated in isolated shadow books: EMA 9/21 crossover with VWAP/MACD confirmation, VWAP crossover with EMA/MACD confirmation, and EMA21 trend pullback. They use completed candles, next-bar-open entry assumptions, stop-first OHLC exits, cash-only sizing, estimated charges and adverse slippage. The existing scanner remains the frozen champion. A REVIEW_CANDIDATE label only invites out-of-sample review after a minimum sample and risk screens; strategies are never automatically promoted.

## Approved entry behavior

`PaperBroker.process_scan_report()` accepts only saved reports marked `RESEARCH_SCAN_ONLY`, explicitly asserting no orders were placed, from the Kite provider. It admits only `CONDITIONAL_BUY` / `CONDITIONAL_SHORT` candidates with verified live data and margin, a fresh quote timestamp, usable bid/ask, valid stop/target ordering, sufficient virtual cash, available position capacity and passing risk plus after-cost reward/risk checks. It does not trust a scanner's suggested quantity; it recomputes a cash-only quantity against the virtual account's risk budget.

Entry execution is modelled at the quote ask for LONG or bid for SHORT, with adverse entry slippage. Candidate IDs are deduplicated using symbol, direction, strategy and signal-candle timestamp. Existing open positions block another entry in the same symbol.

## Simulated exits and costs

The live quote monitor should call `update_market_price()` as ticks arrive. The broker triggers a stop or target-2 exit when market prices cross the configured threshold. It does not assume favorable target gap improvement, applies adverse exit slippage, and estimates brokerage, STT, exchange charges, SEBI charges, GST, stamp duty and IPFT using the repository's configurable cost model. P&L and fee fields are persisted to SQLite.

`TIME_SQUARE_OFF` is applied from a fresh mark at/after 15:15 IST. If the latest market quote is stale, the broker records a square-off block rather than fabricating an exit price.

## Persistence and auditability

- Database: `data/paper_trading.sqlite3` (SQLite; WAL mode).
- `trades`: every virtual entry, exit, quantity, price, reason, fee and net result.
- `market_marks`: latest trusted mark per symbol.
- `events`: scan processing, entries, rejected setups, exits and reasons.
- `strategy_registry`: frozen champion and named challenger candidates.

Back up the database before changing its schema. It is local research output and should not be committed if it contains personal trading activity. All real-order operations remain disabled.
