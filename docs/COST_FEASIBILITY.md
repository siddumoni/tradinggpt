# Cost feasibility from the prospective signal journal

The append-only `logs/paper_signals.csv` journal records scanner reference entry, stop, Target 1, Target 2 and quantity. Run the audit directly against that journal; it derives the signal date from `signal_time` and recalculates Target 2 net reward/risk using the shared Zerodha NSE intraday charge model.

```powershell
python -m src.cost_feasibility logs/paper_signals.csv --output reports/cost_feasibility_detail.csv --summary-output reports/cost_feasibility_by_day_symbol.csv --profile research
```

The detailed output reports stop distance, estimated round-trip cost, net T2 R:R and the unchanged 1.5 gate at the 5 bps/side floor and at 8 bps/side. A wider observed bid/ask spread raises the assumed slippage; it never lowers either floor. The summary groups the auditable observations by signal date and symbol and reports pass counts/rates independently at both cost levels.

A journal row with zero, missing or invalid quantity is retained as `UNSCORABLE_NONPOSITIVE_OR_INVALID_QUANTITY`. It is excluded from the pass-rate denominator, not silently assigned one share. Invalid price ordering or malformed price data fails the audit rather than being repaired. This audit measures whether the saved signal's levels clear the estimated cost gate; it does not assert a fill, realized outcome, portfolio return or tradable opportunity.

The source journal is not rewritten. The audit outputs are derived files under `reports/` and must not be committed. Use only genuine prospective scanner journals; do not backfill from historical charts or edit source rows.