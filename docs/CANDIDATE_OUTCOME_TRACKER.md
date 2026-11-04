# Independent Candidate Outcome Tracker

## Purpose and separation from the Watch-Shadow portfolio

The integrated paper service keeps an independent, candle-based outcome record for every eligible scanner `WATCH_CANDIDATE` whose only technical/risk rejection is the minimum after-cost Target 1 reward-to-risk gate.

This tracker is an **independent single-candidate experiment ledger**, not a portfolio. It deliberately does not apply the existing Watch-Shadow portfolio's three-position limit, same-symbol overlap limit, or shared-cash reservation. This reveals how eligible near-miss candidates behaved without allowing portfolio capacity to suppress observations.

That means signals can overlap in time, repeat a symbol, and each use the same fixed research capital assumption. Their P&L, win rate, expectancy and profit factor must not be summed or described as a tradable portfolio result. Use a separate shared-capital replay for allocation questions.

## Eligibility

A candidate is recorded when:
- the scan report asserts `RESEARCH_SCAN_ONLY`, the provider is Kite, and `orders_placed_or_modified=false`;
- the action is `WATCH_CANDIDATE` and a reason explicitly identifies the Target 1 net R:R below 1.50 gate;
- live quote data and read-only MIS margin verification both passed;
- company-specific risk is not HIGH and RSS/news checks are available;
- side, signal timestamp, reference close, stop and target levels are valid; and
- the next-bar entry timestamp remains before 14:45 IST.

Other watch candidates are retained as `EXCLUDED` with a reason, but are not counted as eligible outcome observations. Repeated scan snapshots are deduplicated by symbol, side, strategy and signal candle timestamp.

## Fixed hypothetical execution convention

The signal is not available merely because its source candle has closed. The tracker computes availability from the scanner report's `as_of`/`scan_completed_at` and the entry-validation quote timestamp. It enters only at the first five-minute candle open strictly after the later of signal-candle completion and scanner availability. Publication time is never backdated to the signal candle.

| Item | Assumption |
|---|---|
| Research capital per candidate | ₹1,00,000 |
| Planned risk per candidate | ₹500 |
| Entry | First five-minute candle open strictly after scanner publication/quote validation |
| Entry price | Adverse 5 bps slippage |
| Cash sizing | Independent cash-only sizing; no MIS leverage |
| Minimum after-cost Target 1 R:R | 0.01 for this diagnostic only |
| Exit | Stop, Target 2, or final 15:10 candle close |
| Costs | Estimated brokerage, statutory/exchange charges and slippage |
| Intrabar ambiguity | Stop takes priority over target |
| Stop gap | Worse of stop level or bar open |
| Target gap | No favorable price improvement |

The 0.01 entry gate is not a strategy-rule change. It permits the ledger to measure candidates which the normal 1.50 scanner gate declined, while still requiring a positive, defensible after-cost reward/risk calculation. The scanner, Champion account, Challenger rules and Watch-Shadow acceptance logic remain unchanged.

## Data integrity

The signal candle must exist in the provider-pure Kite archive, and its close must match the signal's saved reference close within the greater of ₹0.02 or 5 bps. The exact first eligible candle after scanner publication must exist; the evaluator never substitutes an earlier or later open. Candle continuity from the signal bar through the delayed entry bar is required.

After entry, every expected five-minute candle through the exit must be present. If a gap occurs, the outcome is marked `INCOMPLETE_DATA_GAP` with no P&L. Session finalization also requires the final regular-session 15:10 candle when no stop or Target 2 was reached earlier. A missing final bar leaves the candidate unscored; it is not force-closed at a stale quote.

The scanner receives an additional list of symbols with open/pending Challenger, Watch-Shadow or candidate outcomes. It refreshes their Kite candles even when the liquidity screener excludes them from the current top-N technical-analysis universe. These symbols are archived only; they are not added as fresh scanner candidates by this mechanism.

## Statuses

- `PENDING_ENTRY`: the signal was eligible but the exact next candle has not arrived yet.
- `OPEN`: a hypothetical next-bar entry was created and subsequent candles are still being evaluated.
- `CLOSED`: a stop, Target 2 or final 15:10 close was observed with continuous data.
- `INCOMPLETE_DATA_GAP`: the signal, entry or exit path is missing; no P&L is assigned.
- `EXPIRED_NO_ENTRY`: the next-bar entry time is outside the same-session cutoff.
- `EXCLUDED`: the candidate failed the eligibility or independent cash-sizing checks.
- `INVALIDATED_LEGACY_METHOD`: a result created by the older signal-candle-only entry timing or double-counted entry-slippage method; retained in an audit table but removed from performance statistics.

## Daily reports

Daily JSON, Markdown and combined trade CSV include independent candidate-outcome counts, availability timestamps and method versions under the `CANDIDATE_OUTCOME` track. On upgrade, prior method rows are preserved in `candidate_outcome_invalidations` and marked `INVALIDATED_LEGACY_METHOD`; they are not mixed with new outcomes. Read the status counts and data-gap count before interpreting any result. The daily output explicitly flags that overlapping candidate P&L is not portfolio P&L.

The tracker is research-only. It never places, modifies or cancels Kite orders and does not authorize live trading.
