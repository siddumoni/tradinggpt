# Prospective paper signal journal

## Purpose

This journal preserves directional candidates from saved scanner snapshots so future paper evaluation can be based on signals recorded at the time, rather than reconstructed from hindsight. It is append-only and deduplicates by symbol, side, strategy and completed signal-candle timestamp.

It is **not a trade log**: entries in this CSV are observations, not fills. The module never places, modifies or cancels orders, and does not invent realised P&L or assume that the reference close was an executable fill.

## Select a market-data source

The scanner defaults to Yahoo Finance development data. For stock quotes and 5/15-minute candles from the authenticated, read-only Kite Connect API, first use the project's manual session flow and then select `--provider kite`:

```powershell
python -m src.main login
python -m src.main check
python -m src.advisory_engine --provider kite --symbols RELIANCE,HDFCBANK,INFY --paper-journal logs/paper_signals.csv
```

Kite symbol data is marked verified only when a positive quote and timestamp are present and the quote timestamp is within 15 minutes of the scan time. A stale/missing quote fails closed. The NIFTY regime context still uses unofficial Yahoo Finance index data, and the scanner does not verify MIS margin. Consequently this change does not approve trades; paper/journal use remains the safe mode.

## Capture during a scanner run

Optionally pass the journal path to the scanner. Each saved scan writes its JSON as before; directional candidates from that report are also appended to the journal:

```powershell
python -m src.advisory_engine --symbols RELIANCE,HDFCBANK,INFY --paper-journal logs/paper_signals.csv
```

Or import an existing report manually:

```powershell
python -m src.paper_journal --scan reports/phase6_scan_20261022_103000.json --journal logs/paper_signals.csv
```

Run it prospectively and archive the journal with its source scan reports. Do not backfill it by looking at historical charts and pretending those were live observations.

## Meaning of paper status

- `WATCH_ONLY_UNVERIFIED_DATA`: the scanner did not verify the feed as live exchange data.
- `WATCH_ONLY_UNVERIFIED_MARGIN`: margin verification is missing.
- `WATCH_ONLY_NOT_APPROVED`: another scanner gate kept the signal in watch status.
- `PAPER_ONLY_NOT_EXECUTED`: candidate passed the scanner's conditional gates but no order or fill was simulated.

The current scanner uses Yahoo Finance as an unofficial development feed and supplies no margin-per-share estimate. Therefore, current records should normally stay watch-only. That is intentional. A paper candidate is not permission to trade.

## Limitations and next milestone

This step records the observation only. A separate outcome evaluator is still required before win-rate, expectancy or drawdown can be calculated prospectively. That evaluator must specify a fill convention (e.g. first observable quote or next-bar open), reject stale/unverified data, use stop-first handling for ambiguous OHLC bars, and persist outcome timestamps. Use the existing research protocol; do not compare repeated observations of the same signal as independent trades.
