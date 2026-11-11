# Intraday microstructure research reports

TradingGPT generates three optional, research-only JSON summaries during daily finalisation:

- `daily/YYYY-MM-DD/volume_profile/volume_profile_summary.json`: per-symbol estimated POC, VAH, VAL, HVN and LVN from completed 5-minute Kite candles.
- `daily/YYYY-MM-DD/order_flow/order_flow_summary.json`: per-symbol estimated buy/sell volume, delta, displayed-depth imbalance and snapshot-based top-of-book OFI.
- `daily/YYYY-MM-DD/microstructure_validation/microstructure_validation_summary.json`: a forward-return diagnostic for Order Flow, plus an explicit Volume Profile validation status.

## Timing and candle completeness

Daily finalisation runs at 15:35 IST, after the regular NSE cash-market close. Before Volume Profile generation, the report pipeline attempts an end-of-day refresh of incomplete, already-archived Kite symbols. It requests the current session's 5-minute candles and upserts only completed bars.

The archive accepts regular-session bar starts from 09:15 through 15:25. A profile is marked `READY` only when all 75 expected 5-minute bar timestamps are present. Merely having 75 rows or a late final timestamp is not sufficient. If Kite credentials, the instrument master, historical data, or a symbol's candle history is unavailable, the existing data is retained and the profile remains `PARTIAL` or `NO_DATA`; the main daily paper report is not blocked.

The refresh only touches symbols already present in the provider-pure Kite candle archive. It does not add new symbols to the scanner universe or modify Champion decisions.

Volume Profile requires the local NSE instrument cache at `.local/nse_instruments.json` for each symbol's tick size. Symbols without a valid tick size or usable session candles are listed as warnings and omitted from calculated profiles.

## Order Flow market-hours and quality

Order Flow capture is restricted to timestamps within the normal NSE cash session (09:15–15:30 IST). The analysis layer independently excludes out-of-session snapshots and reports the excluded count. It also rejects mixed-date inputs; analyse one symbol and one trading session at a time.

Order Flow reads `.local/order_flow/YYYY-MM-DD/*.jsonl.gz`. If capture was disabled, unavailable, or produced no usable in-session events, the report explicitly says `NO_DATA`; it does not invent values.

Kite WebSocket messages are snapshots, not a guaranteed exchange-by-exchange tape. Last-trade quantity can repeat, buyer/seller classification is estimated from quote touches or the tick rule, and displayed depth is resting liquidity rather than executed volume.

## Predictive validation

The Order Flow diagnostic groups in-session snapshots into 5-minute buckets, estimates the signed delta from each bucket, and compares that direction with the close of the immediately following completed 5-minute candle. Flat outcomes inside the configured 5-bps band are excluded from directional accuracy.

The diagnostic reports prediction count, accuracy, and independent session count. It remains `INSUFFICIENT_PROSPECTIVE_EVIDENCE` until at least 100 matched predictions span 20 independent sessions. Even after that threshold, `READY_FOR_REVIEW` is not a profitability claim, a promotion, or permission to trade.

Volume Profile predictive accuracy is explicitly `NOT_TESTED`. The current end-of-day profile uses the completed session; using those final POC/value-area levels to explain earlier price movements would introduce look-ahead bias. A separate point-in-time, time-sliced profile evaluator is required before this can be tested fairly.

## Interpretation and safety

These are research summaries only. Volume Profile distributes each completed candle's volume uniformly across its low-to-high price levels, so POC/VAH/VAL/HVN/LVN are estimates rather than exact traded volume at price. Order Flow uses Kite snapshots, not a guaranteed complete exchange tape.

Neither report nor the predictive diagnostic changes Champion entries, exits, stop-losses, targets, ranking, sizing, risk controls, or broker actions. No real orders are placed, modified, or cancelled. Optional microstructure failures never block the core paper report or its private upload.
