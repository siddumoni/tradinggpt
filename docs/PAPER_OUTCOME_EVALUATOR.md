# Prospective paper outcome evaluator

## Purpose

This evaluator takes the observations recorded by `src.paper_journal` and evaluates only the candles after the signal. It is research-only and never places, modifies or cancels orders.

It uses the following fixed execution convention:

1. The signal candle must exist in the selected symbol's candle file, and its close must match the journal reference close within the configured 5-bps/₹0.02 tolerance.
2. The hypothetical entry is the **next 5-minute candle's open**. If that bar is absent, the outcome stays pending or is marked missing; it is never backfilled from a later open.
3. The next open must still sit between the signal's stop and targets. If price has gapped through a level, the entry is rejected.
4. Quantity is recalculated with the configured cash-only capital, risk budget, estimated charges, slippage and minimum net R:R gate. The tool does not use the journal's original quantity because the scanner may not have verified margin.
5. At exit, stop-loss takes priority if stop and target 2 are both touched in the same candle. A stop gap fills at the worse candle open; target gaps do not receive favorable price improvement.
6. The full quantity exits at target 2 or at the 15:10 candle close (the regular session's final 5-minute bar). For intrabar stop/target exits, the reported exit timestamp is the bar-end boundary, so the shared-capital replay does not release the cash earlier than the model can know the outcome.
7. A missing 5-minute candle before an exit makes the signal unscored. The evaluator does not guess what happened during the gap.

Target 1 is an entry viability gate; the simulated full exit is at target 2. This is a stated paper assumption, not a claim that an actual fill would have occurred.

## Prepare one candle file per symbol

The evaluator expects one 5-minute OHLCV CSV per symbol. Its timestamp column can be named `date`, `timestamp`, `datetime`, or `time`; required price columns are `open, high, low, close, volume`. Timestamps are interpreted in Asia/Kolkata if timezone-naive and converted to IST if timezone-aware.

A file named `INFY.csv` is preferred. A unique file such as `INFY_60d_5minute.csv` can be used if it is the only `INFY_*.csv` file in the directory. If there are multiple matching caches, the tool fails rather than guessing which version to use. Copy the explicitly chosen cache into a clean paper-candles directory with the simple symbol filename, for example:

```powershell
New-Item -ItemType Directory -Force data\paper_candles | Out-Null
Copy-Item cache\INFY_60d_5minute.csv data\paper_candles\INFY.csv
Copy-Item cache\HDFCBANK_60d_5minute.csv data\paper_candles\HDFCBANK.csv
```

Use the same provider and time convention as the scanner whenever possible. Keep a copy of the source candle files with the outcome report. The tool validates OHLCV values, duplicates, alignment and session gaps, but cannot independently certify a provider's licensing, completeness or exchange-liveness.

## Run the outcome evaluation

```powershell
python -m src.paper_outcomes --journal logs/paper_signals.csv --candles-dir data/paper_candles --output reports/paper_outcomes.csv
```

Outputs:
- `reports/paper_outcomes.csv`: one row per journal observation, including pending/rejected/unscored status, fill assumptions, estimated costs and net result where an outcome is defensible.
- `reports/paper_outcomes_portfolio_candidates.csv`: completed hypothetical trade rows in a schema accepted by the shared-capital replay.
- `reports/paper_outcomes_summary.json`: input and status counts only. It deliberately does not sum per-signal P&L as if overlapping signals were independent portfolio trades.

Then run a separate shared-capital replay on the completed candidates:

```powershell
python -m src.portfolio_replay --trades reports/paper_outcomes_portfolio_candidates.csv --initial-capital 20000 --risk-per-trade 200 --slippage-bps 5 --max-concurrent-positions 3
```

That replay enforces one cash pool, quantity caps and same-symbol overlap controls. It still reuses precomputed signals and does not regenerate signals when capital is busy, so it is an allocation replay rather than a full portfolio backtest.

## Interpretation safeguards

- The journal source flag `data_verified_live` is carried into outcomes. The current Yahoo-based scanner is unofficial and should remain watch-only; hypothetical results from it are research diagnostics, not live-trading evidence.
- The risk engine's cash-only sizing is used for hypothetical quantity; no MIS leverage or margin verification is assumed.
- A target/stop outcome is hypothetical. It does not model queue priority, spread, partial fills, price impact or exact intrabar sequencing.
- The report should be generated after the relevant session has closed and the candle cache has refreshed. Pending latest-day sessions can be evaluated again later.
- Repeated observations may overlap. Do not use individual-signal aggregate P&L, win rate or profit factor as an independent sample. Use the shared-capital replay for allocation constraints and later validate a frozen rule set on untouched dates.
- Neither a completed paper outcome nor a passing test suite establishes a repeatable profitable edge.
