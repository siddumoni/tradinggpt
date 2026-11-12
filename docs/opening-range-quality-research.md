# Opening Range Breakout: entry safety and research diagnostics

## November 12, 2026 findings

The Champion paper ledger recorded nine opening-range-breakout trades, all closed by stop-loss, for net estimated P&L of -INR 2,181.88. The trades included repeated same-symbol attempts in KMEW and AVANTIFEED. A critical entry-validation gap was identified in HAL: its opening-range high was INR 5,009.00, while the executable entry fill was INR 5,004.10. Existing validation checked only whether the quote remained between stop and target; it did not re-check the original breakout boundary.

## Safety correction

ORB candidates now carry a machine-readable strategy trigger with the opening-range boundary and required direction. At the final executable quote, the advisory validator requires a long breakout entry to remain strictly above the original high and a short breakdown entry to remain strictly below the original low. Missing or inconsistent trigger metadata fails closed. A retraced setup is demoted to watch-only.

## Research-only diagnostics

Every ORB signal now reports breakout extension as a multiple of opening-range width, directional candle close location, candle body as a fraction of candle range, and whether an earlier breakout in the same session failed before the current attempt.

Initial review thresholds (0.50 range-width extension, 0.65 directional close location, 0.30 body fraction) are diagnostic labels only. They do not block Champion signals. They must be evaluated on multiple sessions and unseen data before any strategy gate is enabled.

## Limitations

November 12 is one session and cannot establish a robust edge. Candidate-outcome rows are overlapping signal-level experiments, not a portfolio. Estimated charges/slippage are modelled, not broker-confirmed. Champion behavior must not be tuned to this one day's outcomes.
