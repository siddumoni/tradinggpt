# Shared-capital portfolio replay

## Purpose

The individual backtests each use their own capital budget. Adding their P&L does not model a single ₹20,000 account, because two symbols may signal at the same time and compete for the same cash.

This replay combines **one selected saved trade log per intended candidate stream** and applies one capital pool chronologically. It closes positions from prior simulated trades as their saved exit times are reached, reserves capital for active position notional plus a stop-cost buffer, scales quantity down to respect both available capital and the per-trade risk budget, blocks overlapping positions in the same symbol, and records any candidate it must reject. It recalculates P&L at the allocated quantity with the selected slippage and the configured transaction-charge estimate.

## PowerShell

```powershell
python -m src.portfolio_replay --trades reports/backtests/RELIANCE_vwap_trend_trades.csv reports/backtests/INFY_vwap_pullback_trades.csv --initial-capital 20000 --risk-per-trade 200 --slippage-bps 5 --max-concurrent-positions 3
```

A directory containing `*_trades.csv` files can also be passed. The tool writes a JSON summary and a `*_trade_details.csv` with accepted/rejected candidates, allocated quantity, available/reserved capital, and net P&L.

## Interpretation and limitations

- The simulation is a **portfolio allocation replay of saved per-symbol trades**, not a full portfolio backtest: it does not regenerate signals while capital is occupied or test alternative signal priority policies.
- For simultaneous entry timestamps, input file order and then row order decides priority. Keep that order fixed and report it when comparing runs.
- Feed one selected baseline log per intended candidate stream. Do not include overlapping logs from alternative slippage levels or regime-filter variants as if they were independent signals.
- The saved exit path is reused; positions are not marked to market between bars. The reported drawdown is based on realized trade outcomes and omits intratrade mark-to-market drawdown.
- Capital reservation is cash-only and reserves entry notional plus an estimated stop-cost buffer. It deliberately does not assume MIS leverage.
- The loss budget is an estimate. Price gaps and actual fills can exceed the planned risk; estimated charges/slippage differ from actual contract notes.
- This remains an offline advisory research tool. It does not place, change, or cancel orders, and its result is not proof of a tradable edge.

Use the [research protocol](RESEARCH_PROTOCOL.md) and freeze the candidate list before unseen-date testing.
