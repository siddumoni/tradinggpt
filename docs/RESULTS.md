# TradingGPT performance results

**Status: NOT YET EVALUATED.** No validated shared-capital trade-detail report has been processed by `src.performance_stats` on this branch. Do not infer performance from scanner candidates, incomplete observations, or isolated per-symbol backtests.

After a completed shared-capital replay, run:

```powershell
python -m src.performance_stats --trades reports/backtests/portfolio_replay_trade_details.csv --initial-capital 20000
```

The command replaces this file with sanitized aggregate statistics and a research-stage decision. See [PERFORMANCE_STATS.md](PERFORMANCE_STATS.md) for methodology and limitations.
