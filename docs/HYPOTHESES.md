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


## Prior-session ATR warm-up

- **Identifier:** `nse_atr_prior_session_seed_v1`
- **Status:** Foundation implementation; not a promoted strategy version.
- **Hypothesis:** Seeding the current session's Wilder ATR from the previous
  session's final valid ATR, while using high-minus-low for the first bar, will
  make the ATR input valid from the 09:20 close when prior-session history is
  available. This should expose otherwise-hidden early-session setups without
  allowing the overnight price gap to inflate true range.
- **Single change:** ATR initialization only. EMA, RSI, MACD, VWAP, entry filters,
  structural-stop selection, target distances, costs, and exit rules are not
  changed by this hypothesis.
- **Measurements:** Count sessions with a valid prior-session seed; count 5-minute
  bars and strategy signals that were previously ATR-unavailable but become
  evaluable; split by symbol and session. Report actual accepted/approved setups
  separately from merely ATR-eligible bars.
- **Data rule:** Use only the immediately previous normal NSE session's final
  valid ATR and completed current-session candles. The live scanner requires a
  complete prior 09:15–15:10 candle grid; a missing intervening trading session,
  special session, or incomplete prior session disables the seed. If unavailable,
  retain the original 14-bar warm-up; never synthesize a seed or repair candles.
- **Version boundary:** Results using this initialization must be labelled
  separately from older ATR-reset evidence. The eventual `nse_champion_v2`
  freeze remains deferred until A, B, and E are all complete.

- **Audit command:** `python scripts/audit_atr_warmup.py --candles-dir data/paper_candles_kite`.
  The report compares the previous 14-bar session reset with the new seeded ATR,
  and counts opening-range signals that also pass the completed 15-minute stock
  trend filter in the five potentially affected scan slots (10:00–10:20 IST).
  It deliberately labels these as technical opportunities, not approved trades.
