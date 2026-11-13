# Market safety, previous-session levels and eligibility metadata

## Previous-session level proximity

Before a directional setup reaches structural-stop and risk sizing, the scanner resolves the exact previous normal NSE cash session from the verified exchange calendar and requires that session's completed daily OHLC candle. Weekend, exchange-holiday and unsupported special-session dates are skipped. If the exact previous-session candle is missing, the scanner fails closed; it never substitutes an older available daily candle. The current-day daily candle, if the provider returns one while it is still forming, is excluded.

The configured clearance is `0.3 ATR` (`config/market_safety.yaml`). LONG setups check only previous-session high and pivot levels ahead of the entry. SHORT setups check only previous-session low and pivot levels ahead of the entry. Clearance is checked first at the completed-candle signal close and checked again after the final Kite quote refresh at the executable best ask (LONG) or best bid (SHORT). If the executable entry has moved within the 0.3 ATR buffer, the conditional candidate is demoted to `WATCH_CANDIDATE` with quantity zero. Levels behind the direction of travel do not block the trade.

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

Only HTTPS URLs on `nseindia.com` are accepted. The snapshot date must not be in the future and must be no more than three calendar days old. The file must be explicitly marked complete. Supported measure labels are `ASM`, `GSM`, `ESM`, `TRADE_TO_TRADE` and `PERIODIC_CALL_AUCTION`. A listed symbol is blocked from conditional entry. A symbol absent from a complete, current snapshot is reported as `NO_RESTRICTION_LISTED_IN_COMPLETE_SNAPSHOT`. A missing, stale, incomplete or invalid snapshot is reported as unavailable/unverified and is never described as clear. In line with the status-only requirement when surveillance data is available, an unavailable snapshot does not independently change the existing entry decision; a current snapshot that explicitly lists a restricted symbol blocks that setup.

Until the snapshot is maintained from NSE's latest official surveillance files, the scan report will show the status as `NOT_AVAILABLE` (or `STALE` / `INCOMPLETE` / `INVALID`). Do not mark `complete: true` unless the snapshot genuinely covers the full required universe.

### Build the local snapshot from NSE source files

The repository includes `scripts/refresh_nse_surveillance.py`. It does not scrape the NSE website or infer classifications from Kite instrument metadata. Download the current files directly from NSE and verify that all five cover the same trade date and the complete published lists:

| Input | Official NSE source | What it contributes |
|---|---|---|
| ASM CSV | https://www.nseindia.com/static/regulations/additional-surveillance-measure | ASM symbols and published stage |
| GSM CSV | https://www.nseindia.com/static/regulations/graded-surveillance-measure | GSM symbols and published stage |
| ESM CSV | https://www.nseindia.com/static/regulations/enhanced-surveillance-measure-esm | ESM symbols and published stage |
| UDiFF Common Bhavcopy Final ZIP | https://www.nseindia.com/all-reports | Trade-for-trade symbols whose equity series is BE/BZ/ST/SZ |
| Periodic Call Auction CSV | https://www.nseindia.com/market-data/stocks-in-call-auction | Periodic-call-auction symbols |

The All Reports page labels the daily UDiFF Common Bhavcopy Final ZIP and provides date-specific equity reports. Use the final bhavcopy for the chosen trade date, not an older cached file. The importer supports the current UDiFF `TckrSymb` / `SctySrs` fields and the legacy `SYMBOL` / `SERIES` fields for the T2T input.

Example (PowerShell):

```powershell
python -m scripts.refresh_nse_surveillance `
  --as-of 2026-11-13 `
  --asm-csv .local\nse-source\asm-latest.csv `
  --gsm-csv .local\nse-source\gsm-latest.csv `
  --esm-csv .local\nse-source\esm-latest.csv `
  --t2t-bhavcopy .local\nse-source\BhavCopy_NSE_CM_20261113_F_0000.csv.zip `
  --pca-csv .local\nse-source\List_sec_CA.csv `
  --confirm-complete
```

Only use `--confirm-complete` after personally verifying that every input is the latest official complete file for `--as-of`. The script validates all inputs, records filenames and SHA-256 hashes, merges duplicate classifications by symbol, validates the generated schema and atomically writes `.local/nse_surveillance.json`. Missing files, malformed headers or a failed schema validation leave the previous snapshot untouched. It refuses to create a complete snapshot when `--confirm-complete` is absent. Do not reuse an older PCA list or bhavcopy while labelling it with today's date.

The importer does not fetch files automatically because NSE download links can change and incomplete/stale downloads must not silently produce a clean snapshot. Until a verified complete set has been imported, `NOT_AVAILABLE` is the correct status.

## MIS margin status

`MIS_MARGIN_ESTIMATE_AVAILABLE` means Kite returned a positive value from the read-only `order_margins` API for a hypothetical one-share MIS LIMIT calculation. It is a margin estimate, not a guarantee of eventual order acceptance or exchange eligibility. Missing, invalid, stale-quote or failed refresh results are labelled `MIS_ELIGIBILITY_UNVERIFIED` and fail the existing margin gate. No order placement, modification, cancellation or GTT method is used.

## Calendar rollover

The scanner reports a maintenance warning when fewer than 30 calendar days remain in the verified NSE calendar coverage and marks coverage expired after the last verified date. Unknown dates continue to fail closed. As checked on 2026-11-07, NSE's official equity holiday page showed the calendar year 2026 only; no 2027 equity holiday list was available on that page. No 2027 dates have been guessed or added. Refresh `config/nse_equity_calendar.yaml` only when NSE publishes its 2027 equity schedule: https://www.nseindia.com/resources/exchange-communication-holidays?apiversion=v2

## Operational notes

- Cache files and the optional surveillance snapshot are under `.local/` and must never be committed.
- The daily and relative-volume caches are provider-specific to Kite and are not mixed with Yahoo data.
- `NOT_AVAILABLE` is an explicit status, not a synonym for unrestricted or eligible.
- When Phase E is merged, the paper broker's active baseline version becomes `nse_champion_v2` from the first supported normal NSE cash session. Existing `champion_v1` and `nse_champion_v2_foundation_candidate` trades remain archived, and daily trade exports mark them `CHAMPION_ARCHIVE`. The paper account's total realized P&L still includes its entire historical account ledger; use the separate `strategy_version_performance` breakdown and version-filtered CSV rows for strategy-specific analysis.

These checks are advisory/research filters only. The repository remains strictly advisory-only.