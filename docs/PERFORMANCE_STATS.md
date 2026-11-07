# Statistical validation and promotion

Run a shared-capital replay first, then calculate portfolio-level statistics from its trade-detail CSV:

```powershell
python -m src.portfolio_replay --trades reports/backtests/BASELINE_trades.csv --output reports/backtests/portfolio_replay
python -m src.performance_stats --trades reports/backtests/portfolio_replay_trade_details.csv --initial-capital 20000

# Or analyze one isolated track from a paper_daily_*_trades.csv export
python -m src.performance_stats --trades data/paper_daily_2026-11-03_trades.csv data/paper_daily_2026-11-04_trades.csv data/paper_daily_2026-11-05_trades.csv --track CHAMPION --initial-capital 100000
```

You can pass multiple daily CSVs in one command; their columns are aligned before concatenation. For daily paper-report CSVs, pass `--track` (for example `CHAMPION`, `CHALLENGER`, `WATCH_SHADOW`, or `CANDIDATE_OUTCOME`). If a selected track contains multiple strategy versions, also pass `--strategy-version`; the loader refuses to pool isolated tracks or strategy versions accidentally. Only `CLOSED` rows are evaluated. The performance engine filters to accepted trades, validates gross P&L against side/entry/exit/quantity, retains transaction charges, and reprices the *same accepted trades* at 8 bps per side. It does not change selection, quantity, or risk gate at 8 bps.

For example, a challenger comparison must use the exact strategy version and the corresponding Champion export:

```powershell
python -m src.performance_stats --trades data/paper_daily_YYYY-MM-DD_trades.csv --track CHALLENGER --strategy-version challenger_ema_9_21_atr_seed_v2 --champion-trades data/paper_daily_YYYY-MM-DD_trades.csv --champion-strategy-version nse_champion_v2_foundation_candidate
```

The example date arguments are placeholders; pass every retained daily report in the same frozen evaluation window. Do not compare non-overlapping date sets or pool versions.

The 95% expectancy interval resamples complete entry sessions with replacement (10,000 draws, fixed seed 42). Metrics include expectancy, win rate, profit factor, realized close-to-close drawdown, maximum losing streak, MAE/MFE in R where the outcome evaluator recorded them, entry-hour buckets, and positive-P&L concentration by symbol and session.

For a `CHALLENGER` track, supply the matching Champion daily trade exports with `--champion-trades` and, if needed, `--champion-strategy-version`. The comparison pairs daily net P&L on common entry sessions at 8 bps/side, then bootstraps the session-level challenger-minus-Champion difference. The lower confidence bound uses Bonferroni adjustment across the configured family of three active challenger tracks (`config/promotion.yaml`). A challenger cannot pass without at least 20 common sessions, a positive paired mean, and a positive multiplicity-adjusted lower bound. If Champion comparison data is absent, the challenger decision remains `INCONCLUSIVE`.

The analysis always writes its private JSON/Markdown diagnostics. It updates `docs/RESULTS.md` only when every configured promotion check passes; an `INCONCLUSIVE` analysis leaves the existing public results status untouched.

Promotion thresholds live in `config/promotion.yaml`: at least 100 accepted trades across 20 sessions, positive lower 95% expectancy bound at 8 bps, profit factor at least 1.0, and no symbol/session contributing more than 30% of positive P&L. The decision is `PROMOTE` or `INCONCLUSIVE`; PROMOTE means eligible for the next research-validation stage only, never live trading.

Outputs: `reports/performance/validation.json`, `reports/performance/validation.md`, and sanitized aggregate `docs/RESULTS.md`. No trade-level rows, credentials, or account data are written to RESULTS.md.

Limitations: drawdown is realized-only, the session bootstrap does not remove serial dependence between adjacent sessions, and the 8 bps repricing does not re-run position sizing or acceptance. Missing MAE/MFE source fields are reported as unavailable, never estimated from incomplete data.
