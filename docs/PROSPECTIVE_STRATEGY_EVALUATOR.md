# Prospective strategy forward evaluator

## Purpose

This is an isolated research process for the two strategy/symbol candidates selected from the historical study:

- `HDFCBANK:vwap_trend`
- `INFY:vwap_pullback`

It uses the same `make_signal_callback` logic from `src.strategy_backtest` that the historical baseline uses. It reads only the existing Kite candle archive and never places, modifies or cancels orders. It does not change the integrated paper broker, champion gates, or the configured 1.5 minimum after-cost net reward/risk rule.

## Start it prospectively

Start the normal paper-trading service first so it continues archiving completed Kite candles to `data/paper_candles_kite`. Open a separate PowerShell terminal at the repository root, activate the same virtual environment, then run:

```powershell
python -m src.prospective_strategy_evaluator --candles-dir data/paper_candles_kite --candidates HDFCBANK:vwap_trend,INFY:vwap_pullback
```

Keep the process running during market hours alongside the existing service. Stop it with Ctrl+C. The default poll interval is five seconds.

**First-start rule:** the first launch records the latest timestamp present in each existing archive as its watermark and skips every older candle. If an archive appears later, its current head is seeded and its existing rows are skipped too. Signals are generated only from newly appended candles after that watermark. This is deliberate: the tool does not reconstruct old signals and pretend they were observed prospectively. Keep `logs/prospective_strategy_evaluator_state.json`; deleting it causes another warm start and skips the archive head again.

The process accepts only archives whose `provider_mode` is consistently `kite`. Current-session bars must be consecutive five-minute candles starting at 09:15. Incomplete current sessions are skipped without fabricating bars. Past incomplete sessions are excluded before indicators are calculated. A newly discovered candle more than 120 seconds after its candle-end boundary is audited and skipped rather than replayed as a new signal.

## Session boundary and final candle

The project's configured paper session ends at **15:15 IST**, not 15:30. The final modeled five-minute candle is timestamped **15:10** and completes at **15:15**. The archive and scheduler intentionally stop at this boundary; candle start times after 15:10 are outside this evaluator's signal window. Therefore, a per-symbol evaluator watermark of 15:10 is expected when that final modeled candle has been archived and processed. The summary JSON records this session model explicitly so the timestamp is not mistaken for a missed 15:15–15:30 candle.

This is a project simulation boundary, not a statement that the NSE cash market itself closes at 15:15. Do not extend the evaluator to later candles without separately changing and validating the configured square-off and entry assumptions.

## What the source-verification flag means

`provider_mode=kite` is checked when the evaluator reads each archive, and mixed/non-Kite archives fail closed. This verifies the **candle archive's provider provenance**. It does not mean the evaluator has fetched a fresh quote for the same timestamp.

The journal's `data_verified_live` field intentionally remains **false** because the prospective evaluator consumes archived OHLCV candles only and does not perform the scanner's fresh-quote timestamp/last-price check. Do not change it to true merely because `provider_mode` is `kite`. The outcome summary exposes both facts separately: Kite archive provenance is enforced, while fresh-quote verification is not performed by this process.

## Frozen experiment settings

- Symbols and rules: HDFCBANK VWAP Trend; INFY VWAP Pullback.
- Indicator/signal callback: the existing baseline callback in `src.strategy_backtest`.
- Levels: 1 ATR stop, 2 ATR Target 1, 2.5 ATR Target 2.
- Starting capital for individual signal sizing: ₹20,000.
- Planned risk budget per signal: ₹500, including estimated stop costs and modeled slippage.
- Minimum after-cost Target 2 net R:R gate: 1.5, fixed. This matches the historical baseline backtest, whose single 2.5-ATR target is the scored target.
- Per-side slippage sensitivity: 2, 5, and 8 basis points.
- Entry at the next five-minute candle open; target 2 full exit; stop-first rule if stop and target share a candle; adverse stop-gap handling; regular-session square-off; no outcome scoring across missing bars.
- Costs are estimates from the project's cost calculator. This does not guarantee exact broker fills or current fees.

For each signal and slippage scenario, the result records the regular gate decision independently. When the only rejection is Target 2 net R:R below 1.5 and a valid positive quantity fits the existing risk/cash checks, a separate **research-only shadow outcome** is tracked. This does not approve the entry or alter any live/champion decision. Other gate rejections, invalid levels, gaps, missing data, and pending exits remain rejected or unscored.

## Output files

The default directory is `reports/prospective_strategy_evaluation`:

- `prospective_strategy_signals.csv`: append-only signal observations with a deterministic id, signal candle, levels, source, and reason.
- `prospective_strategy_audit.csv`: each processed candidate bar, including no-signal, data-quality, late-candle, and duplicate statuses.
- `prospective_strategy_outcomes.csv`: one row per signal and each slippage scenario, including gate decision, hypothetical fills, exit reason, charges, slippage, and net P&L.
- `prospective_strategy_outcomes_summary.json`: completed sample counts and metrics by symbol, strategy and slippage scenario.
- `logs/prospective_strategy_evaluator_state.json`: persistent per-symbol watermarks and pending-report state.

Pending, invalid, late, or data-gap rows are not wins/losses. The summary uses 30 completed signal outcomes per scenario as an initial review threshold; reaching it is not proof of statistical significance or profitability.

**Do not read the sum of individual signal P&Ls as portfolio P&L.** Signals can overlap and each signal is sized independently against the same notional capital. This evaluator reports signal-level diagnostics only. Before any account-level interpretation, build or run a separate shared-capital replay with non-overlapping accepted trades. Do not merge the outcome journal with the live paper-account ledger or promote either candidate automatically.

## Troubleshooting

- If logs say Kite-only archive required, confirm the integrated service is using `--provider kite` and is writing to `data/paper_candles_kite`.
- If no signal rows appear, check `prospective_strategy_audit.csv`. Many bars can correctly produce `NO_SIGNAL`; the strategy rules must not be loosened just to increase the count.
- If the first run creates no signal rows, that is expected: it only seeds watermarks. Leave it running for newly completed candles.
- Do not delete the signal journal or state file while the process is running.
- A watermark at 15:10 is the expected final modeled candle start; see the session-boundary section before treating it as a missing-candle defect.
- `data_verified_live=false` is intentional for this candle-only evaluator. It does not mean the Kite archive provider check failed.
