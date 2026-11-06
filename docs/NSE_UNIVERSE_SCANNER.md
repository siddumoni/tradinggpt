# NSE Equity Universe Scanner

## Purpose

The NSE equity-universe mode is a read-only, intraday research scan. It screens the available instruments from the authenticated Kite NSE instrument dump, selects the most liquid names using a current-session quote snapshot, and then runs the existing TradingGPT technical and risk pipeline against those selected stocks.

The standalone scanner command writes a research report and does not change a paper book. The integrated paper service also invokes this same NSE universe screen after each completed five-minute candle (default top 50), then sends the resulting conditional candidates to the Champion virtual broker for quality ranking and shared-capital allocation. Its WebSocket monitors the latest selected top-N symbols plus all open Champion and Watch-Shadow positions; it does not subscribe to every NSE instrument. No real Kite orders are placed.

## Run

Authenticate once for the current Kite session:

```powershell
python -m src.main login
python -m src.main check
```

Run a standalone top-50 advisory scan:

```powershell
python -m src.advisory_engine --provider kite --universe nse_equity --universe-max-symbols 50 --output-dir reports --verbose
```

Run the integrated paper service (market-wide scanning is the default):

```powershell
python -m src.paper_trading_service
```

To use a fixed watchlist instead, run `python -m src.paper_trading_service --universe watchlist --symbols RELIANCE,HDFCBANK,INFY`.

Example with explicit screening settings:

```powershell
python -m src.advisory_engine --provider kite --universe nse_equity --universe-max-symbols 75 --universe-min-price 50 --universe-max-price 10000 --universe-min-volume 10000 --universe-min-turnover 5000000 --universe-max-quote-age-minutes 15 --output-dir reports --verbose
```

The universe mode requires `--provider kite`. It does not silently fall back to Yahoo Finance or the manually maintained watchlist.

## Screening sequence

1. Read the Kite NSE instrument dump. The existing local cache is refreshed once per IST calendar day.
2. Retain unique NSE EQ records with an instrument token. Exclude explicit ETF instrument types, names containing the `ETF` token / `EXCHANGE TRADED FUND`, and common `BEES`-suffix ETF symbols. Kite's `instrument_type=EQ` alone does not distinguish every ETF from company shares, so this metadata/name filter is documented as a practical guard rather than a complete exchange security-master classification.
3. Request full quotes for the eligible instrument symbols in batches of up to 500 symbols. Requests are paced at the configured one-second quote interval.
4. Reject missing quotes, malformed timestamps, timestamps more than one minute in the future, and quotes older than the configured maximum quote age.
5. Apply the configured last-price, current-day volume, and turnover-proxy filters.
6. Rank the remaining symbols by `average_price × current-day volume` descending, with volume and symbol as deterministic tie-breakers.
7. Deeply analyse only the top `--universe-max-symbols` selected stocks.

Defaults:

| Filter | Default |
|---|---:|
| Maximum deep-analysis symbols | 50 |
| Minimum last price | ₹50 |
| Maximum last price | ₹10,000 |
| Minimum current-day volume | 10,000 shares |
| Minimum turnover proxy | ₹50,00,000 |
| Maximum quote age | 15 minutes |

The turnover calculation is a screening proxy only. It is not the exchange's official turnover field, an average-daily-volume measure, or a guarantee of liquidity at the intended order size. A single current-session snapshot can favour names with unusual intraday activity. Review the selected list and exclusion summary.

## Technical analysis and risk checks

For each selected symbol, the scanner reuses the quote snapshot from the bulk request and fetches the current session's 5-minute and 15-minute candles. It skips the separate 60-calendar-day daily history request in this intraday scan mode.

The existing technical indicators, strategy callbacks, market-regime context, news triage, quote-freshness validation, hypothetical one-share margin estimate, transaction-cost estimates, sizing, entry cutoff and after-cost net R:R gates remain in force. A missing/stale/invalid quote or failed data request is not converted into a directional signal.

The scanner may request read-only hypothetical MIS margin estimates for a directional candidate. This is not an order submission.

### Latest executable-entry validation

For a LONG candidate, the executable reference is the best ask; for a SHORT candidate, it is the best bid. The scanner requires both sides of the quote book, a valid timestamp, a non-crossed spread and a quote within the configured freshness window. It deliberately does not fall back to last traded price or the last candle close when executable depth is missing.

The strategy's candle-derived stop-loss and targets remain fixed. The risk engine then recalculates quantity, cash/margin constraints, estimated charges, slippage and net R:R at the executable entry. If the current entry has already crossed the original stop/target levels, the setup is invalidated. If after-cost net R:R falls below the configured minimum, or a margin/data/news gate fails, the action is downgraded to `WATCH_CANDIDATE`; risk thresholds are not relaxed.

The candidate's `entry_validation` section reports the original candle reference, executable entry, bid/ask, quote timestamp and age, spread, percentage price drift, validation status and risk breakdown. The breakdown includes gross/net risk and reward, target-specific net R:R, estimated margin, slippage and the line-item cost estimate at the stop and both targets. A passed validation remains a conditional research signal, not an order instruction.

## Output

The standard `phase6_scan_YYYYMMDD_HHMMSS.json` report contains:

- The universe selection mode and configured filters, including ETF-like instrument exclusions.
- Separate scan start/completion timestamps (`as_of` is the report completion time).
- Per-directional-candidate live executable-entry validation, quote timestamp/age and price drift.
- Gross/net risk, reward, estimated charges, slippage and target-specific after-cost R:R breakdowns.
- Number of NSE EQ instruments identified.
- Quote count requested and received.
- Number eligible before the top-N cut and number selected.
- Exclusion counts by reason.
- Selected symbols and their individual scan results.
- Fetch errors, market/index context and the explicit advisory-only/no-order boundary.

A scan with no eligible instruments fails closed with a clear error; it does not manufacture candidates.

## API pacing and operational load

The adapter batches full quote requests at no more than 500 instruments per request, paces quote requests at one request per second, historical candle requests at approximately three requests per second, and other read-only API calls (such as margin estimates) at approximately nine requests per second. The margin pacing is intentionally below the documented ten-request-per-second ceiling. A top-50 technical scan needs two intraday historical intervals per selected stock, plus index context. It can therefore take materially longer than a three-stock scan; leave sufficient time before the next five-minute candle and inspect timeout / fetch-error logs.

Only one instance of a broad scan should be launched at a time using the same Kite session. Do not use the universe mode in the existing per-five-minute integrated scheduler until its runtime, quote freshness, historical-call volume, and paper-book behaviour have been independently validated.

## Safety and research limitations

- Research/advisory only. No real Kite order endpoint is called.
- This feature does not widen the integrated paper-trading symbols or subscribe every NSE instrument to the WebSocket.
- It does not alter the Champion, Challenger, Watch-Shadow or prospective evaluator configurations.
- It does not relax margin, quote, risk, entry-cutoff or minimum net reward/risk checks.
- A scan result is a snapshot, not a standing recommendation. Quote freshness can change immediately after retrieval.
- Screening by current-day turnover proxy does not establish order-book depth or execution capacity.
- The top-N list is a liquidity ranking, not a forecast.
- A scan and its generated candidates do not demonstrate strategy profitability. Backtests, historical replays and forward observations require separate controls for look-ahead bias, survivorship bias, missing data, costs, slippage, overlapping signals and out-of-sample validation.


## Phase 4 market-session and microstructure safeguards

### NSE equity calendar

The intraday scanner uses `config/nse_equity_calendar.yaml` for the published 2026 NSE cash-equity holidays. The file records its source and verified coverage dates. Normal-session scans and the direct Kite bundle fetch both refuse weekends, listed holidays, unsupported special sessions, and dates outside the calendar's verified range. The current calendar is valid only from **2026-01-01 through 2026-12-31**; it deliberately fails closed beyond that date until the next official calendar is reviewed and added. Muhurat Trading is not enabled because its special timing is outside the normal 09:15–15:30 session model.

Update the calendar only from the official NSE equity-segment holiday circular/page. Do not assume a date is open simply because it is a weekday.

### Circuit-limit protection

Kite full quotes must include positive `lower_circuit_limit` and `upper_circuit_limit` values with a valid last price inside the band. Missing or invalid band data is rejected. A stock is blocked when its last price is within **0.5%** of either band. During final executable-entry refresh, the same check is repeated against the actual best ask for LONG or best bid for SHORT, not just LTP. This is a conservative paper/advisory filter; it does not guarantee an order could execute or that the band will remain unchanged.

### Time-of-day relative volume

Live Kite candidates must pass a same-clock-time cumulative-volume check before strategy/risk approval. Current-session 5-minute volume from 09:15 through the latest completed candle is compared with the **median** cumulative volume over up to the previous **20 complete sessions**, using exactly the same 5-minute time prefix. At least **10 valid prior sessions** and a minimum ratio of **1.0x** are required by default. Missing bars are never imputed; incomplete current sessions, insufficient historical sessions, malformed volume, or a below-threshold ratio fail closed.

The Kite adapter fetches a 45-calendar-day historical 5-minute window ending on the previous calendar day. Newly listed or thinly covered symbols may not have enough valid history and will remain watch/no-trade until the required sample exists. Relative volume is a participation filter, not a directional signal or proof of edge.

### MIS margin check

The existing MIS margin validation remains a read-only hypothetical one-share margin calculation through Kite's margin estimator. Missing or invalid estimates continue to fail closed. It does not place an order and is not a guarantee of broker eligibility or execution.
