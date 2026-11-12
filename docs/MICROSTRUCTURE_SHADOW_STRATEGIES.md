# Volume Profile + Order Flow shadow strategies

## Purpose

TradingGPT now evaluates three microstructure-inspired strategies in independent paper/shadow books. They do **not** send orders to a broker and do not change nse_champion_v2 signals, ranking, sizing, entries, exits, or risk controls.

The design follows three ideas:

1. **Auction/value context:** use Point of Control (POC), Value Area High (VAH), and Value Area Low (VAL) to describe where the session has accepted trading.
2. **Order-book context:** use estimated trade-side delta, displayed-depth imbalance, and snapshot-to-snapshot top-of-book OFI as confirmation, never as a standalone signal.
3. **Price confirmation:** require candle structure, VWAP/EMA alignment and relative volume before recording a candidate.

## Strategies

| Version | Setup | Confirmation |
|---|---|---|
| micro_vp_value_rejection_ofi_v1 | Price probes a prior-only VAL/VAH and closes back inside value | Reversal candle, same-bar estimated delta and at least one positive book-pressure vote |
| micro_vp_value_acceptance_breakout_v1 | Completed candle closes beyond prior-only VAH/VAL | Strong close location, minimum candle body vs ATR, relative volume, delta and book-pressure confirmation |
| micro_orderflow_price_continuation_v1 | Directional price continuation | Same-bar delta plus VWAP, EMA 9/21, candle body and relative-volume alignment |

### Point-in-time profile

For a signal at a completed five-minute candle, the profile is built from the **earlier completed candles of that same session only**. The signal candle itself is excluded from the profile. This prevents the tested bar from moving its own POC/VAH/VAL levels.

A minimum of 12 preceding five-minute bars is required before a profile signal can be generated. The current end-of-day profile remains a descriptive report and is not reused as an earlier-in-session signal.

### Order Flow input

The recorder preserves the canonical daily compressed feed at:

.local/order_flow/YYYY-MM-DD/SYMBOL.jsonl.gz

It also writes a lookup index for each five-minute bucket:

.local/order_flow_bars/YYYY-MM-DD/SYMBOL/HHMM.jsonl.gz

The strategy reads only the bucket aligned to the signal candle. A missing or sparse bucket causes no signal; it does not borrow later or neighbouring-session data.

Minimum default quality gates are 15 snapshots and 5 distinct trade updates per bucket. Delta is estimated from quote-touch/tick-rule classification. Depth imbalance and OFI are derived from periodic snapshots.

## Important data limitations

- The available Kite stream is a snapshot feed, not a complete exchange execution tape or a full sequence of every order-book event.
- Last-trade quantity may be repeated across snapshots; the existing recorder/analysis de-duplicates observable timestamp/price/quantity keys, but indistinguishable same-second executions cannot always be separated.
- Aggressor side is estimated from quote touches or the tick rule unless a trusted explicit aggressor-side field exists.
- Displayed depth is resting liquidity. Orders can be cancelled before execution.
- Snapshot OFI is incomplete when intermediate book events are not observed.
- Current OHLCV Volume Profile allocates each five-minute candle's volume uniformly across the price ticks between its low and high. This is an **approximation**, not true volume traded at every price. Exact POC/VAH/VAL requires a complete, correctly timestamped trade-print feed with quantity and price.
- The profile is a current-session developing profile. It is not a prior-day composite profile or a market-profile/time-at-price (TPO) chart.

## Execution and evaluation

All signals are inserted into the existing shadow_signals table and evaluated independently by the existing Challenger paper ledger:

- Signal is observed only after its candle is complete.
- Entry is simulated at the first eligible subsequent five-minute candle open after publication time.
- Adverse slippage and current transaction-charge estimates are applied.
- The existing risk sizing and after-cost net reward/risk gates apply.
- OHLC exits use the existing conservative stop-first rule; data gaps remain unscored.
- Each strategy version is tracked separately. Do not sum independent strategy P&Ls as one portfolio result unless a separate shared-capital portfolio replay is run.

## Tuning and promotion policy

The YAML thresholds are in config/microstructure_strategies.yaml. Change them as controlled experiments, not after looking at the same session's outcomes.

For every scan session, record the strategy version, parameter/config hash, profile levels, flow-quality counts, entry/exit assumptions, rejected candidates, data gaps, charges, slippage and net P&L. Evaluate by session and market regime, not by pooled snapshot count alone.

Keep the strategy in shadow status until it has:
- at least 20 independent prospective sessions and a substantial number of closed outcomes;
- positive net results after charges and realistic slippage;
- acceptable drawdown and stable results across symbols and market regimes;
- out-of-sample / walk-forward evidence and comparison to simple baselines;
- enough data quality to rule out obvious missing-bar and stale-tick artefacts.

Passing these checks makes a strategy a review candidate only. It does not automatically promote it to Champion or enable broker execution.

## References

- [TradingView — Volume Profile basics](https://www.tradingview.com/support/solutions/43000502040-volume-profile-indicators-basic-concepts/) (POC, value area, VAH and VAL definitions; their volume classification should not be mistaken for verified aggressor-side tape).
- [Cont, Kukanov & Stoikov (2014) — The Price Impact of Order Book Events](https://academic.oup.com/jfec/article/12/1/47/816163) (research on short-horizon price impact and order-flow imbalance; results are not a profitability claim for this NSE/Kite implementation).
- [NSE — Real-time data products](https://www.nseindia.com/market-data/real-time-data-subscription) (NSE describes access levels for market data; higher-granularity exchange data is distinct from periodic broker snapshots).
- [Kite Connect — WebSocket streaming](https://kite.trade/docs/connect/v3/websocket/) (broker stream and market-depth payload reference).
- [Public Volume Profile strategy research/backtest example](https://github.com/Quantreo/Volume-Profile-Trading-Strategy) (used only as inspiration for testing multiple hypotheses with controls and costs, not for claimed profitability).

## Operational interpretation

A report saying SHADOW_ENABLED_NOT_VALIDATED means the signal engine is connected and recording hypothetical outcomes. It does **not** mean the strategy is proven profitable, and it does not authorize live orders.
