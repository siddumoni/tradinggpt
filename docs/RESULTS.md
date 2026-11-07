# TradingGPT performance results

**Status: INSUFFICIENT PROSPECTIVE EVIDENCE — NOT YET EVALUATED.**

As of 2026-11-07, the private research archive contains daily paper reports for 2026-11-03 through 2026-11-06: four independent sessions. The archived `champion_v1` track has 16 closed trades across four sessions. Challenger strategy versions remain separate and each is below the configured minimum of 20 sessions and 100 accepted, completed trades. The frozen `nse_champion_v2` baseline has no prospective session in this archive because Phase E merged after the November 6 session. The 193 `CANDIDATE_OUTCOME` closes recorded on November 4 are overlapping single-candidate observations from one session, not shared-capital portfolio trades. No qualifying shared-capital trade-detail report has been processed by `src.performance_stats`.

This is a sample-size limitation, not evidence that a strategy is profitable or unprofitable. Scanner candidates, pending entries, incomplete observations, watch-candidate outcome rows from a single session, and isolated per-symbol backtests must not be presented as portfolio performance.

## Phase 6 evidence gate

The public results summary will be refreshed only after a frozen, clearly identified research track has at least:

- 100 accepted, completed trades;
- 20 independent entry sessions;
- a positive lower bound on the 95% session-block bootstrap expectancy interval after 8 bps per-side slippage;
- profit factor of at least 1.0 at 8 bps per side; and
- no symbol or session contributing more than 30% of positive net P&L.

The thresholds are defined in `config/promotion.yaml`. Passing them means only that a candidate may advance to the next research-validation stage; it never authorizes live trading.

See [the Phase 6 readiness guide](PHASE6_READINESS.md) and [the statistical validation methodology](PERFORMANCE_STATS.md).

No performance figures are published until the evidence gate is met.
