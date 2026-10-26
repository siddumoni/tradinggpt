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

Kite stock quotes and candles are used in `--provider kite` mode, and the NIFTY 50 regime context is also fetched through Kite without a Yahoo fallback. Quote verification requires a positive price and fresh timestamp; missing/stale quotes fail closed. For directional candidates, the scanner attempts Kite's read-only hypothetical MIS margin estimate, and missing, malformed or non-positive estimates fail closed. Passing these checks does not establish a profitable edge or approve live execution; paper-only review remains the safe mode.

## Capture during a scanner run

Optionally pass the journal path to the scanner. Each saved scan writes its JSON as before; directional candidates from that report are also appended to the journal:

```powershell
python -m src.advisory_engine --symbols RELIANCE,HDFCBANK,INFY --paper-journal logs/paper_signals.csv
```

Or import an existing report manually:

```powershell
python -m src.paper_journal --scan reports/phase6_scan_20261022_103000.json --journal logs/paper_signals.csv
```

When `--paper-journal` is enabled, the scanner also archives completed 5-minute candles under `data/paper_candles` by default. Override the location with `--candle-archive-dir`. Each per-symbol file is tagged with `provider_mode`; switching from Yahoo to Kite (or vice versa) in the same archive deliberately fails instead of mixing feeds. Keep the scan reports, journal, scan ledger, and candle archive together for auditability. When `--paper-journal` is provided, the scanner automatically writes `logs/paper_scan_runs.csv` unless `--scan-ledger` overrides it. The ledger includes every saved scan, even runs where every candidate is `NO_TRADE`; it does not add non-directional rows to the signal journal. Review the ledger to track scan counts, data status, margin verification attempts and fetch/archive errors. Run it prospectively and do not backfill by looking at historical charts and pretending those were live observations.

## Meaning of paper status

- `WATCH_ONLY_UNVERIFIED_DATA`: the scanner did not verify the feed as live exchange data.
- `WATCH_ONLY_UNVERIFIED_MARGIN`: margin verification is missing.
- `WATCH_ONLY_NOT_APPROVED`: another scanner gate kept the signal in watch status.
- `PAPER_ONLY_NOT_EXECUTED`: candidate passed the scanner's conditional gates but no order or fill was simulated.

With the Yahoo provider, stock data is unverified for live use and candidates remain watch-only. With the Kite provider, only a candidate whose live-data, margin, news and configured risk gates pass can be marked `PAPER_ONLY_NOT_EXECUTED`; this remains a paper observation, not permission to trade. The scan ledger logs runs even when no directional setup was produced.

## Limitations and next milestone

This step records the observation only. A separate outcome evaluator is still required before win-rate, expectancy or drawdown can be calculated prospectively. That evaluator must specify a fill convention (e.g. first observable quote or next-bar open), reject stale/unverified data, use stop-first handling for ambiguous OHLC bars, and persist outcome timestamps. Use the existing research protocol; do not compare repeated observations of the same signal as independent trades.
