# Frozen trade-set slippage repricing

## Why this exists

The multi-stock scenario runner re-executes the strategy at every slippage value. Because its minimum-net-reward/risk gate is cost-aware, increasing slippage can change both the estimated cost **and** which trades are accepted. That is useful as a cost-aware trade-selection sensitivity test, but it does not isolate the impact of costs on one fixed trade set.

The frozen-trade-set repricer holds each saved trade's symbol, side, entry/exit timestamps, entry/exit prices and quantity fixed. It recomputes transaction-charge estimates from `config/settings.yaml`, calculates slippage at each requested per-side bps assumption, and reports net P&L without regenerating signals, resizing positions, or reapplying the minimum-net-R:R gate.

## PowerShell usage

For one saved baseline trade log:

```powershell
python -m src.slippage_repricing --trades reports/backtests/INFY_vwap_trend_trades.csv --slippage-bps 2,5,8
```

For multiple separately saved trade logs:

```powershell
python -m src.slippage_repricing --trades reports/backtests/INFY_vwap_trend_trades.csv reports/backtests/HDFCBANK_vwap_trend_trades.csv --slippage-bps 2,5,8 --output reports/backtests/frozen_slippage_summary.csv
```

A directory can also be supplied; the tool finds `*_trades.csv` files recursively and reprices each file as its own trade set:

```powershell
python -m src.slippage_repricing --trades reports/backtests/phase9 --slippage-bps 2,5,8
```

The tool writes a summary CSV and a sibling `*_trade_details.csv` containing per-trade costs and P&L. Summary rows are separated by source filename and slippage. **Do not add separate source files together as if they formed one portfolio**; many files may be alternative scenarios over overlapping dates.

## Guardrails and interpretation

- The source log must include symbol, side, entry/exit timestamps, entry/exit prices, quantity, and gross P&L.
- Gross P&L is recalculated from side, prices and quantity and must reconcile to the saved log (₹0.02 tolerance).
- All scenarios must retain exactly the same trade count within each source file.
- Transaction charges use the configured estimate; actual broker contract notes are authoritative.
- The report is a conditional cost diagnostic, not a new strategy backtest and not an untouched out-of-sample test.
- This research tool never submits, modifies, or cancels orders.
