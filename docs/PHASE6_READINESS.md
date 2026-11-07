# Phase 6 — evidence readiness and completion guide

## Objective

Phase 6 publishes a concise, evidence-led performance status in the repository README and `docs/RESULTS.md`. It does not tune strategies, relax risk gates, or enable live trading.

The statistics engine already exists in `src/performance_stats.py`. It:
- loads one or more daily paper-report trade exports or shared-capital replay details;
- requires explicit selection of a track when a report contains multiple tracks;
- requires an explicit strategy-version selector when the selected track contains multiple versions;
- evaluates accepted, completed trades only and reconciles gross P&L to side, prices and quantity;
- reprices the same accepted trades at 8 bps per-side slippage;
- calculates expectancy, a 95% session-block bootstrap interval, win rate, profit factor, realized drawdown, losing streak and concentration; and
- applies the thresholds in `config/promotion.yaml`.

## Current evidence status (2026-11-06)

The private research archive now contains daily reports for 2026-11-03 through 2026-11-06: four independent entry sessions. The current archived Champion version, `champion_v1`, has 16 closed trades across those four sessions. Challenger versions must remain separate: `challenger_ema_pullback_v1` has 32 closed trades across three sessions, `challenger_ema_9_21_v1` has 15 across three sessions, and `challenger_vwap_momentum_v1` has 9 across three sessions. None meets the minimum 20 sessions and 100 accepted, completed trades.

The 193 `CANDIDATE_OUTCOME` closes recorded on 2026-11-04 are overlapping single-candidate observations from one session, not shared-capital portfolio trades; they do not satisfy the evidence gate and must not be pooled with Champion or Challenger. `WATCH_SHADOW` is also a separate research track. Phase E merged on 2026-11-07, after the last archived session, so the frozen `nse_champion_v2` baseline has no prospective session in this archive yet. Keep `champion_v1` and v2 results separate.

A large number of candidate observations from one session does not create additional independent sessions. Do not combine CHAMPION, CHALLENGER, WATCH_SHADOW and CANDIDATE_OUTCOME into one result: they are separate research tracks with different selection and allocation rules. In particular, independent candidate outcomes can overlap and are not portfolio P&L.

Until a track meets the sample gate, retain the explicit **INSUFFICIENT PROSPECTIVE EVIDENCE** status. Do not replace it with a win rate, P&L headline, or strategy ranking.

## Evidence collection rules

1. Keep the paper service running on eligible market sessions and retain the end-of-day reports and required operational inputs.
2. Preserve the strategy configuration/version, account profile, cost assumptions and source report files for every session.
3. Do not backfill prospective observations from historical charts or silently reconstruct missing candles.
4. Count distinct complete entry dates as sessions; count only accepted, closed trades as completed trades.
5. Analyze each track and strategy version independently. Never add alternative challenger variants together or sum independent candidate observations as a portfolio.
6. Use shared-capital replay trade details for portfolio-level claims. Per-symbol backtests and independent signal outcomes are not shared-account results.
7. Keep the existing advisory-only boundary: no order placement, modification or cancellation.

## Completion thresholds

All of the following checks must pass for a track before it can be considered for the next research-validation stage:

| Check | Required value |
|---|---:|
| Accepted, completed trades | At least 100 |
| Independent entry sessions | At least 20 |
| Lower bound of 95% session-block expectancy interval at 8 bps/side | Greater than ₹0 |
| Profit factor at 8 bps/side | At least 1.0 |
| Maximum positive net P&L share from any one symbol or session | At most 30% |

These values are sourced from `config/promotion.yaml`. A failed or insufficient check is reported as `INCONCLUSIVE`, not as a failed trading strategy. Passing is research-stage eligibility only and is never live-trading authorization.

## Reproducible analysis

Run from the repository root after the sample gate is met. Example for the Champion track; replace the date list with every retained daily report in the frozen evaluation window:

```powershell
python -m src.performance_stats --trades data/paper_daily_YYYY-MM-DD_trades.csv data/paper_daily_YYYY-MM-DD_trades.csv --track CHAMPION --initial-capital 100000
```

For a different isolated paper track, use its exact track label and the correct initial capital. If multiple strategy versions exist within the selected track, add `--strategy-version VERSION`. Do not mix capital assumptions or track labels.

For portfolio-level claims, first run `src.portfolio_replay` on one frozen baseline trade log per candidate stream, then pass the resulting shared-capital trade-detail CSV to `src.performance_stats`. Record the exact input filenames and frozen evaluation window in the review notes.

The command always writes:
- `reports/performance/validation.json` — machine-readable statistics and gate checks; and
- `reports/performance/validation.md` — detailed research report.

It updates `docs/RESULTS.md` only when every configured promotion check passes. If the sample, paired-comparison, or any other gate is inconclusive, the existing public results status is left unchanged.

Do not run the command against an under-sized sample if the intent is to update the public results page. The current `docs/RESULTS.md` is deliberately a status statement until the evidence gate is met.

## Final Phase 6 checklist

- [ ] One frozen track and strategy version are selected before reviewing its evaluation outcomes.
- [ ] At least 100 accepted, closed trades span at least 20 independent sessions.
- [ ] Input completeness and data-quality exclusions are reviewed; no missing bars are synthesized.
- [ ] Shared-capital replay is used for any portfolio-level claim.
- [ ] Statistics are calculated at 8 bps per-side slippage and every configured promotion check is visible.
- [ ] README and `docs/RESULTS.md` state the sample window, track, sample size, limitations and whether each criterion passed.
- [ ] No trade-level data, credentials, Kite tokens, private account data or local runtime files are committed.
- [ ] CI passes on Ubuntu and Windows with Python 3.13.
- [ ] Advisory-only behavior remains unchanged.

A result may remain inconclusive. That is a valid research outcome; do not weaken thresholds or alter execution assumptions to force a positive result.
