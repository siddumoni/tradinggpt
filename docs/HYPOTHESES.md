# Strategy hypotheses and version boundaries

This register is written before any new challenger observations are collected.
Do not pool outcomes across strategy versions, stop models, or cost assumptions.

## Structural-stop baseline alignment

- **Identifier:** `nse_structural_stop_research_v1`
- **Status:** Engineering alignment in progress; not a promoted champion.
- **Hypothesis:** Applying the live scanner's structural stop selection to historical
  backtests and forward candidate evaluation will remove the current stop-model
  mismatch and make stop-out rate, average stop distance, and after-cost net R:R
  comparisons meaningful across those paths.
- **Single change:** Stop selection only. Signal generation, targets, 1.5 minimum
  net R:R gate, 5 bps/side cost floor, entry timing, and exit execution rules stay
  unchanged.
- **Measurements:** Stop-out rate, average stop distance as a percentage of entry,
  accepted candidates, and net R:R at T2; report separately by strategy and
  stop-policy version.
- **Validation:** Replay identical archived candles and assert that the backtest,
  forward evaluator, live scanner, and paper broker consume the same stop level
  for the same signal context. No look-ahead: 15-minute aggregates are eligible
  only after all constituent 5-minute candles have closed.
- **Promotion:** This research label cannot be promoted. The eventual champion
  freeze as `nse_champion_v2` is deferred until foundation phases A, B, and E
  are complete, CI is green on Ubuntu and Windows, and the version boundary is
  recorded. Existing `champion_v1` evidence remains untouched.

## Plain 1 ATR stop control

- **Identifier:** `challenger_atr_1x_stop_v1`
- **Status:** `DORMANT_CAPACITY_BLOCKED`; not an active challenger.
- **Hypothesis:** A plain 1 ATR stop may produce a different stop-out rate and
  average stop distance from the structural-stop model, with a measurable effect
  on net R:R at T2 after costs.
- **Single change:** Replace only structural stop selection with a 1.0 ATR stop.
  Keep the signal, entry, targets, exit execution, position sizing, and cost model
  identical to the paired structural-stop observations.
- **Measurements:** Stop-out rate, average stop distance percentage, net R:R at
  T2, net expectancy, and trade count at 5 and 8 bps per side.
- **Promotion threshold:** The repository's full independent promotion gate:
  at least 100 closed trades and 20 sessions, positive lower bootstrap bound at
  8 bps/side, no symbol or session contributing more than 30% of profit, positive
  paired challenger-minus-champion result over common sessions, and multiplicity-
  adjusted confidence bound. No threshold is weakened.
- **Capacity rule:** Three challenger strategies are already registered. This
  control remains dormant until a slot is genuinely freed by evidence-based
  retirement. It must not be run as a fourth active challenger.

## Evidence and reporting rules

- Engineering changes must preserve signal, stop, and exit results for archived
  replay unless the change is explicitly classified as a strategy-rule change.
- Never backfill or modify archived market data.
- Data-error sessions are excluded and logged, not repaired synthetically.
- Do not publish performance claims or update `RESULTS.md` until the configured
  sample and promotion gates are independently satisfied.
