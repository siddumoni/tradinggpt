# End-of-day microstructure reports

TradingGPT generates two optional, research-only JSON summaries during daily finalisation:

- `daily/YYYY-MM-DD/volume_profile/volume_profile_summary.json`: per-symbol estimated POC, VAH, VAL, HVN and LVN from that session's completed 5-minute Kite candles.
- `daily/YYYY-MM-DD/order_flow/order_flow_summary.json`: per-symbol estimated buy/sell volume, delta, displayed-depth imbalance and snapshot-based top-of-book OFI from saved Kite WebSocket snapshots.

## Timing and data quality

Daily finalisation runs at 15:35 IST, after the regular NSE cash-market close. This allows the asynchronous Order Flow writer to settle before the reports are read.

**Volume Profile coverage limitation:** the existing candle archive intentionally keeps completed bars only through the 15:10 bar (ending at 15:15). The report therefore marks the profile as `PARTIAL` unless the archive contains all 75 regular-session bars through the 15:25 bar. It records each symbol's latest archived bar and warns when the final 15 minutes are absent. This preserves the existing scanner/archive cutoff rather than changing scanner behaviour.

Volume Profile requires the local daily NSE instrument cache at `.local/nse_instruments.json` for each symbol's tick size. Symbols without a valid tick size or usable session candles are listed as warnings and omitted from calculated profiles.

Order Flow reads `.local/order_flow/YYYY-MM-DD/*.jsonl.gz`. If capture was disabled, unavailable, or produced no usable events, the report explicitly says `NO_DATA`; it does not invent values.

Both reports are optional upload artifacts. Missing or invalid microstructure inputs never block the existing daily paper report or its upload. The private uploader still refuses public repositories, excludes credentials and the SQLite paper account database, and records file hashes in the daily manifest.

## Interpretation

These are research summaries only. Volume Profile uses uniform OHLC-range allocation and is approximate. Order Flow uses Kite snapshots, not a guaranteed complete exchange tape; buyer/seller classification and snapshot OFI are estimates. Neither report changes Champion entries, exits, stop-losses, targets, ranking, sizing, or risk controls.
