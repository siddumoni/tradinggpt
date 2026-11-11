# Order Flow research capture and analysis

## What is collected

The paper service already subscribes to monitored NSE symbols in Kite FULL WebSocket mode. The optional sidecar recorder now stores a compact JSONL snapshot for each tick callback, including:

- last traded price, last traded quantity, last-trade timestamp and cumulative session volume when supplied by Kite;
- best bid and best ask;
- up to five displayed bid and ask depth levels, with price, quantity and order count;
- exchange timestamp and local receive timestamp.

By default capture is enabled in config/order_flow.yaml. Files are written under .local/order_flow/YYYY-MM-DD/SYMBOL.jsonl.gz, which is ignored by Git. Only the symbols already monitored by the paper service are recorded; the recorder does not subscribe to the entire NSE universe.

The callback only performs a bounded, non-blocking queue insertion. A separate daemon writer batches disk writes so disk I/O does not block the existing virtual-position price monitor. If the bounded queue fills, snapshots are dropped and counted. At shutdown, the service logs accepted, written, dropped and failed records.

The writer appends gzip-compressed JSONL. The reader detects compressed files by the `.gz` suffix and reads concatenated gzip members safely. This keeps capture disk usage materially lower during full-session recording.

## Run analysis

Run from the repository root in PowerShell after a paper-service session has collected data:

~~~powershell
python -m src.order_flow --events .local\order_flow\2026-11-11\SBIN.jsonl.gz --output reports\microstructure\SBIN-order-flow.json
~~~

The report includes estimated buy/sell volume, estimated delta, quote/tick-rule classification counts, and displayed top-five depth imbalance.

## Accuracy boundary

This is **not a complete exchange trade tape**. Kite WebSocket messages are snapshots, and the feed does not guarantee one message for every execution. The same last-trade fields can appear in multiple quote updates. TradingGPT de-duplicates by last-trade timestamp, price and quantity, but two indistinguishable prints in the same timestamp resolution cannot be separated.

Aggressor side is estimated by:
1. classifying a trade at/above the best ask as buy-initiated;
2. classifying a trade at/below the best bid as sell-initiated;
3. using the uptick/downtick rule for trades inside the spread; and
4. carrying the last direction for an unchanged-price update.

The output is therefore labelled ESTIMATED_KITE_WEBSOCKET_SNAPSHOT. The displayed-depth imbalance is the difference between resting bid and ask quantities divided by their sum. It describes visible queue size, not executed buying/selling pressure. It is not true order-flow imbalance (OFI), and it does not claim to detect iceberg orders, absorption or spoofing.

For reliable exchange-level trade delta and exact Volume Profile, a complete, timestamped trade-print feed with aggressor-side classification is required. The private research archive currently contains 5-minute OHLCV candles, not historical tick/depth records; it cannot be used to backtest this order-flow estimate retrospectively.

## Research-only and Champion isolation

Order-flow output is descriptive context only. It is not used by the scanner to create, rank, accept or reject candidates. It does not modify nse_champion_v2, stops, targets, risk sizing, paper-broker decisions, challenger allocation or any real broker order. No strategy version is changed.

Do not treat a positive estimated delta or bid-depth imbalance as a standalone LONG signal, or a negative value as a standalone SHORT signal. Preserve raw JSONL files, source quality, symbol, session and analysis parameters when running future prospective experiments.
