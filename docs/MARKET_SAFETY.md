# Market safety, previous-session levels and eligibility metadata

## Previous-session level proximity

Before a directional setup reaches structural-stop and risk sizing, the scanner reads the most recent completed daily candle strictly before the current IST session. The current-day daily candle, if the provider returns one while it is still forming, is excluded.

The configured clearance is `0.3 ATR` (`config/market_safety.yaml`). LONG setups check only previous-session high and pivot levels ahead of the entry. SHORT setups check only previous-session low and pivot levels ahead of the entry. If the nearest relevant level is no more than 0.3 ATR away, the setup is returned as `NO_TRADE`. Levels behind the direction of travel do not block the trade.

Missing or invalid previous-session daily data blocks verified-live setups; it is never replaced with a fabricated level. The previous-session date and per-level distance are included in the candidate's `entry_validation.previous_day_levels` object.

## Daily OHLC and relative-volume caches

Kite daily OHLC history is cached below `.local/daily_ohlc_history/`; 5-minute relative-volume history is cached below `.local/relative_volume_history/`. Both caches are partitioned by symbol, IST calendar date, instrument token and requested calendar lookback. They are written atomically and ignored by Git. A new IST date, changed instrument token or changed lookback triggers a refresh. The cache metadata is included in the scan report under `market_data_cache`.

Relative-volume history requests cover the configured 45 calendar days and end before the current session. The existing same-clock-time volume calculation still validates exact candle continuity, uses only prior dates, and requires the configured number of complete comparable sessions. Cache presence does not bypass any candle-quality checks.

## NSE surveillance snapshot

Kite's NSE instrument dump is not treated as a source of ASM/GSM/ESM surveillance classifications. The scanner accepts an optional, manually refreshed snapshot at `.local/nse_surveillance.json`; this file is local, ignored by Git, and is not fetched or synthesized from unrelated instrument data.

Example schema:

```json
{
  "schema_version": 1,
  "source": "NSE India",
  "source_url": "https://www.nseindia.com/reports/asm",
  "as_of": "YYYY-MM-DD",
  "complete": true,
  "symbols": {
    "ABC": [{"measure": "ASM", "stage": "Stage I"}],
    "XYZ": []
  }
}
```

Only HTTPS URLs on `nseindia.com` are accepted. The snapshot date must not be in the future and must be no more than three calendar days old. The file must be explicitly marked complete. Supported measure labels are `ASM`, `GSM`, `ESM`, `TRADE_TO_TRADE` and `PERIODIC_CALL_AUCTION`. A listed symbol is blocked from conditional entry. A symbol absent from a complete, current snapshot is reported as `NO_RESTRICTION_LISTED_IN_COMPLETE_SNAPSHOT`. A missing, stale, incomplete or invalid snapshot is reported as unavailable/unverified and is not treated as clear; verified-live candidates remain watch-only until a complete current snapshot is available.

Until the snapshot is maintained from NSE's latest official surveillance files, the scan report will show the status as `NOT_AVAILABLE` (or `STALE` / `INCOMPLETE` / `INVALID`). Do not mark `complete: true` unless the snapshot genuinely covers the full required universe.

## MIS margin status

`MIS_MARGIN_ESTIMATE_AVAILABLE` means Kite returned a positive value from the read-only `order_margins` API for a hypothetical one-share MIS LIMIT calculation. It is a margin estimate, not a guarantee of eventual order acceptance or exchange eligibility. Missing, invalid, stale-quote or failed refresh results are labelled `MIS_ELIGIBILITY_UNVERIFIED` and fail the existing margin gate. No order placement, modification, cancellation or GTT method is used.

## Calendar rollover

The scanner reports a maintenance warning when fewer than 30 calendar days remain in the verified NSE calendar coverage and marks coverage expired after the last verified date. Unknown dates continue to fail closed. As checked on 2026-11-07, NSE's official equity holiday page showed the calendar year 2026 only; no 2027 equity holiday list was available on that page. No 2027 dates have been guessed or added. Refresh `config/nse_equity_calendar.yaml` only when NSE publishes its 2027 equity schedule: https://www.nseindia.com/resources/exchange-communication-holidays?apiversion=v2

## Operational notes

- Cache files and the optional surveillance snapshot are under `.local/` and must never be committed.
- The daily and relative-volume caches are provider-specific to Kite and are not mixed with Yahoo data.
- `NOT_AVAILABLE` is an explicit status, not a synonym for unrestricted or eligible.
- These checks are advisory/research filters only. The repository remains strictly advisory-only.