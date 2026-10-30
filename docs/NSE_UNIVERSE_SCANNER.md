# NSE Equity Universe Scanner

## Purpose

The NSE equity-universe mode is a read-only, intraday research scan. It screens the available instruments from the authenticated Kite NSE instrument dump, selects the most liquid names using a current-session quote snapshot, and then runs the existing TradingGPT technical and risk pipeline against those selected stocks.

It is intentionally separate from the integrated paper account. A universe scan does not add new instruments to the Champion, Challenger, or Watch-Shadow books and does not place, modify, or cancel a real Kite order.

## Run

Authenticate once for the current Kite session:

```powershell
python -m src.main login
python -m src.main check
```

Run a default top-50 scan:

```powershell
python -m src.advisory_engine --provider kite --universe nse_equity --universe-max-symbols 50 --output-dir reports --verbose
```

Example with explicit screening settings:

```powershell
python -m src.advisory_engine --provider kite --universe nse_equity --universe-max-symbols 75 --universe-min-price 50 --universe-max-price 10000 --universe-min-volume 10000 --universe-min-turnover 5000000 --universe-max-quote-age-minutes 15 --output-dir reports --verbose
```

The universe mode requires `--provider kite`. It does not silently fall back to Yahoo Finance or the manually maintained watchlist.

## Screening sequence

1. Read the Kite NSE instrument dump. The existing local cache is refreshed once per IST calendar day.
2. Retain unique records whose exchange is NSE, segment is NSE, instrument type is EQ, and instrument token is present. Index and ETF records are excluded.
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

## Output

The standard `phase6_scan_YYYYMMDD_HHMMSS.json` report contains:

- The universe selection mode and configured filters.
- Number of NSE EQ instruments identified.
- Quote count requested and received.
- Number eligible before the top-N cut and number selected.
- Exclusion counts by reason.
- Selected symbols and their individual scan results.
- Fetch errors, market/index context and the explicit advisory-only/no-order boundary.

A scan with no eligible instruments fails closed with a clear error; it does not manufacture candidates.

## API pacing and operational load

The adapter batches full quote requests at no more than 500 instruments per request, paces quote requests at one request per second, and paces historical candle requests at approximately three requests per second. A top-50 technical scan needs two intraday historical intervals per selected stock, plus index context. It can therefore take materially longer than a three-stock scan; leave sufficient time before the next five-minute candle and inspect timeout / fetch-error logs.

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
