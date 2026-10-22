# Intraday Strategy Research Protocol

This protocol is a control against backtest overfitting. The objective is not to find the most profitable-looking historical scenario; it is to test whether a fixed rule set shows a repeatable net-of-cost edge on unseen data.

## 1. Freeze the experiment

Before viewing the evaluation period's results, record:
- symbols and strategy names;
- entry, stop, target, cutoff and square-off rules;
- capital, risk budget, maximum entries per day and cash-only sizing;
- costs and per-side slippage scenarios;
- source provider, candle interval, cache files, and date range;
- the metric used to evaluate the candidate.

Changing any rule after observing the test results makes that test period part of the development data. Do not relabel it as an untouched out-of-sample result.

## 2. Validate input data before calculating performance

For each symbol:
- normalize timestamps to Asia/Kolkata and candle-start semantics;
- reject duplicate timestamps, non-finite or non-positive OHLC, invalid high/low relationships, missing volume and negative volume;
- require all expected 5-minute candles in the strategy's defined window;
- list excluded sessions and missing timestamps; never synthesize candles;
- record the exact cache path, first/last timestamp, and whether it was reused or refreshed.

If a provider has a timestamp convention different from the project's assumption, resolve and test it explicitly before comparing results.

## 3. Keep execution assumptions conservative

Signals must use only candles completed at the time of the signal. Entry is the next bar's open. Where OHLC cannot disambiguate whether the stop or target came first, assume the stop first. Model gap-through-stop behavior conservatively, include brokerage/statutory estimates, and show slippage separately.

The planned-risk budget is not a hard cap on realized loss. Gaps and real fills can exceed it. Compare results at multiple plausible slippage rates and examine whether the accepted-trade set changes as costs rise.

## 4. Report a complete result, not only P&L

Every run should preserve:
- number of evaluated candidates and executed trades;
- net P&L after estimated charges/slippage and gross P&L;
- expectancy per trade, profit factor, win rate, and maximum drawdown;
- losing-trade distribution and exit reasons;
- number of complete/incomplete sessions and invalid rows;
- number of risk/reward rejections and cash-notional rejections;
- performance by symbol, strategy, time segment, and slippage assumption.

A high profit factor based on a handful of trades is inconclusive. A user-set minimum trade count is a review threshold, not a statistical proof.

## 5. Use chronological, preferably rolling validation

Do not randomly shuffle intraday candles. Split by complete session/date, not individual rows. Keep the future test dates completely outside strategy selection and parameter tuning. Use several sequential folds where data is adequate, or conduct a prospective paper-trading period with frozen rules.

If a candidate was selected after reviewing the full period (including the later portion), that later portion is contaminated for that candidate. A chronological split of the same reviewed dataset does not remove that selection bias.

## 6. Criteria for a candidate to advance

Do not automatically select the row with the highest net P&L. Before reviewing results, define acceptance criteria appropriate to the strategy and capital, including:
- positive net expectancy after conservative costs on unseen sessions;
- acceptable drawdown and losing streaks;
- sufficient number of trades and representation across different days/regimes;
- no single symbol/day/trade dominating total profit;
- results not collapsing under plausible higher slippage;
- stable behavior across more than one out-of-sample period;
- clean data-quality status and complete cost reconciliation.

Failure or insufficient sample is a valid result. Do not weaken risk rules or lower costs solely to turn an unprofitable report positive.

## 7. Live-trading boundary

This repository remains advisory-only. Passing the research protocol does not prove profitability and is not authorization to enable automated orders. Confirm results in prospective paper trading, reconcile intended signals against realistic fills and charges, and review broker/exchange rules before any separate decision about live use.
