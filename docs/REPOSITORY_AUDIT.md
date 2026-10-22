# TradingGPT repository audit and research roadmap

Audit date: 2026-10-22

## Executive assessment

TradingGPT has a useful modular foundation for an NSE intraday **advisory-only** project: normalized market-data adapters, technical indicators, three candidate strategies, risk checks, configurable cost estimates, an OHLCV backtester, multi-stock sensitivity runs, chronological validation, a scanner, RSS headline triage, and unit tests.

The project is **not yet ready for live trade recommendations or a claim of profitable edge**. The biggest blocker is evidence quality: the available Yahoo 5-minute history covered only about 39 sessions in the supplied run, the candidate ideas have been inspected and compared on that sample, and the per-stock backtests are not a combined portfolio simulation.

This audit prioritizes trustworthy input, execution, and evaluation before strategy tuning.

## Findings and changes on the research branch

### Addressed in this branch

- Reject invalid/non-finite/non-positive OHLC, inconsistent high/low values, missing/non-finite/negative volume, and duplicate timestamps instead of silently deleting them in the backtester, indicators, session-filter path, and scanner.
- Preserve malformed historical OHLCV rows long enough for session-quality logic to identify the affected session rather than silently dropping them during normalisation/cache reads.
- Require complete 72-bar 5-minute sessions from 09:15 through 15:10 IST for the fixed-level strategy research workflow. Incomplete sessions are excluded; candles are never fabricated.
- Prevent VWAP pullback logic from pairing yesterday's close with today's first candle or spanning a missing 5-minute bar.
- Use current realised equity—not initial capital on every trade—to cap cash-only position notional after earlier closed trades.
- Validate risk/cost settings and charge-model inputs, including finite values, integer positive quantity, and non-negative finite charge rates.
- Load a configured 14:45 IST no-new-entry cutoff into the advisory risk settings and enforce it in scanner evaluation.
- Allow chronological candidates to be specified explicitly, require at least 10 complete sessions in both partitions, and label insufficient test samples/possible selection bias instead of treating a pass as proof of profitability.
- Add regression tests, explicit runtime dependencies, a Windows/Python 3.13 CI workflow, current workflow action versions, a refreshed README, and a research protocol.

### Still open / intentionally not promoted to live use

1. **No demonstrated repeatable strategy edge.** Some stock/strategy cells look positive, while others lose money. Positive results on 3–20 trades are inconclusive. Do not select a winner from the highest historical P&L alone.
2. **Selection bias is material.** If a strategy or stock was chosen after inspecting the same sample, splitting those dates afterward does not create an untouched holdout. Freeze the candidate/parameters and evaluate on later unseen sessions or new data.
3. **Short history.** The supplied report uses about 39 sessions. A robust intraday strategy evaluation should cover varied market regimes and more observations. Yahoo 5-minute history often has a short retention window; use an approved, sufficiently long historical source and preserve its data terms.
4. **No portfolio-level simulation yet.** Per-stock runs don't model simultaneous signals competing for the same ₹20,000 capital, overlapping exposures, sector concentration, or portfolio drawdown. A multi-symbol portfolio engine is a separate research task.
5. **Execution estimates are still approximations.** OHLC bars cannot reveal precise intrabar order; stop-first is deliberately conservative when stop and target are both touched. Slippage is an assumption, and actual fills/charges/order splits can differ. A planned risk budget is not a cap on realised loss after gaps.
6. **Live advisory is deliberately fail-closed.** The scanner uses unverified Yahoo research data and currently supplies no validated margin-per-share estimate. It should not emit an approved entry until a verified exchange/broker data path, quote freshness/spread checks, and a reliable margin check exist. This branch does not add order placement.
7. **News checks are triage only.** RSS keyword matching can miss events or misclassify a headline; a no-match means no match in the supplied feeds, not that no material news exists. Original exchange/company sources and publication times should be checked.
8. **Configuration consistency needs an integration pass.** Cost/risk settings should be loaded from the same configuration path by every CLI research runner, rather than depending on Python defaults. Verify this end-to-end before interpreting reported net P&L.

## Research protocol recommended for the next milestone

### Phase A — Integrity baseline
1. Sync this branch, install requirements.txt, compile, and run all tests.
2. Run the data-quality audit against every saved candle cache.
3. Re-run the same fixed multi-stock/slippage scenarios, preserving all reports and cache metadata.
4. Confirm every report's session counts equal the actual sessions passed to its backtester and reconcile every trade's gross P&L, charges, slippage, net P&L, and equity.

### Phase B — Honest strategy evaluation
1. Pre-register a very small candidate list and exact settings before viewing a new evaluation window.
2. Use an approved data source with enough history to cover trend, volatile, and sideways regimes; preserve source, retrieval time, timezone, exact candles, and data exclusions.
3. Use sequential/walk-forward periods by session/date. Never random-shuffle intraday bars.
4. Report net expectancy, profit factor, drawdown, trade count, losing streaks, exit reasons, slippage sensitivity, and performance by date/regime. Include the number of rejected signals.
5. Compare against simple baselines and test for look-ahead, same-bar ambiguity, session boundary leaks, survivorship/universe selection effects, and parameter overfitting.
6. Do not promote a strategy on a small sample. Continue forward paper tracking with frozen rules after a promising unseen-sample result.

### Phase C — Portfolio and advisory readiness
1. Model total cash, one or more simultaneous positions, per-symbol liquidity/spread filters, total portfolio risk, and daily loss stops.
2. Integrate an authorised verified market-data adapter, stale-quote checks, and margin/available-cash data without placing orders.
3. Compare the scanner's reference entry to a realistic executable quote, including spread and latency assumptions.
4. Keep all order submission disabled. Live execution is explicitly out of scope for this repository branch.

## How to verify this branch locally

~~~powershell
python -m pip install -r requirements.txt
python -m compileall -q src tests
python -m pytest -q
~~~

The GitHub Actions Windows workflow runs the compile and unit-test commands automatically.

## Final decision rule

The objective is **not** to force a profitable result. It is to identify whether a pre-specified strategy has a repeatable positive net expectancy on unseen data with acceptable drawdown and realistic costs. If the result is weak or under-sampled, report it as inconclusive and do not trade on it.
