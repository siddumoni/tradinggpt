# Repository hardening — backtest integrity and validation

This branch adds research safeguards and regression tests. It does not change strategy entry rules or claim that any candidate strategy is profitable.

## Changes

- Reject malformed/non-finite/non-positive OHLC values, inconsistent high/low relationships, and missing/non-finite/negative volume before indicators/backtesting.
- Reject duplicate timestamps instead of silently dropping one copy in the generic fixed-level backtester.
- Use consistent complete-session filtering for multi-stock validation: all 72 five-minute bars from 09:15 through 15:10 IST are required for a retained session; missing bars are never synthesized.
- Prevent VWAP pullback signals that accidentally pair yesterday's close with today's first candle or bridge an intraday gap.
- Require chronological validation to have at least 10 full sessions in both development and test partitions; allow candidate pairs to be specified on the command line and mark small test samples as inconclusive.
- Add explicit NumPy, pandas, and yfinance runtime dependencies.
- Update charge assumptions for the NSE IPFT estimate, including GST on IPFT; the rates remain configurable and must be verified against the applicable broker contract note.
- Update README, add a research protocol, and add Windows/Python 3.13 GitHub Actions test automation.

## Local verification

From PowerShell at the repository root:

```powershell
python -m pip install -r requirements.txt
python -m compileall -q src tests
python -m pytest -q
```

## Research cautions

- A unit-test pass means the tested code behavior is correct, not that a trading strategy will earn money.
- A candidate selected after reviewing overlapping historical results is not an untouched holdout candidate.
- Yahoo Finance intraday data may be incomplete or time-limited. Validate the exact saved candle files; do not manufacture bars.
- Costs are estimates and may differ from actual broker charges or number of executed orders.
- Planned risk per trade is not a cap on realised loss when gaps/slippage occur.
- The app remains advisory-only and must never submit, modify, or cancel orders.
