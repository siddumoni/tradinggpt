# Five-year Kite historical backtesting

The five-year research runner downloads NSE 5-minute OHLCV candles from Zerodha Kite, stores a local CSV cache plus provenance manifest, filters incomplete sessions, and invokes the repository's existing backtester without changing strategy logic.

## Run (Windows PowerShell)

    python -m src.main login
    python -m src.kite_five_year_backtest --years 5

Choose a smaller universe while validating:

    python -m src.kite_five_year_backtest --years 5 --symbols RELIANCE,HDFCBANK,INFY --strategies vwap_trend,vwap_pullback

Refresh the historical cache explicitly:

    python -m src.kite_five_year_backtest --years 5 --refresh-data

The runner displays two PowerShell progress indicators by default:
- Per-stock download progress, including completed 60-calendar-day Kite requests, candle rows received, elapsed time and an estimated time remaining for that symbol.
- Overall stock progress, including completed/total symbols, elapsed time and an estimated time remaining for the full run. Each stock also reports whether its candles were downloaded or loaded from cache.

Disable the console progress display when needed:

    python -m src.kite_five_year_backtest --years 5 --no-progress

ETA is an estimate based on completed chunks or completed stocks; Kite response times, retries, cache hits and backtest duration can change the actual remaining time. Cached symbols are reported as cache hits and skip historical downloading.

## Data and safeguards

- Uses Kite's authenticated, read-only historical-data endpoint and current NSE instrument dump to resolve cash-equity tokens.
- Requests 5-minute candles in sequential windows of at most 60 calendar days and uses the existing historical endpoint pacing.
- Saves returned candles and a manifest containing provider, token, interval, requested range, fetch time and row count. Cache reuse requires an exact manifest match.
- Does not manufacture missing candles. The complete-session validator excludes incomplete, duplicated or malformed sessions.
- Runs supported strategies at 5 and 8 bps slippage per side with configured charges and risk controls.
- Writes trade logs and CSV/JSON summaries under reports/backtests/kite_5y/. Do not commit local data or reports.

## Important limitations

Kite history availability depends on the instrument and broker retention. Five years is the requested window, not a guarantee every symbol has five full years of 5-minute candles. Check actual data_start/data_end and complete_sessions in each report. The current instrument dump is not a point-in-time universe, so delisted symbols and survivorship bias remain limitations. OHLCV cannot reconstruct historical bid/ask depth or true order-flow events; this workflow does not claim to backtest order-flow or volume-profile microstructure strategies.

This is research-only. It does not place, modify or cancel orders and does not modify frozen Champion v2 decision logic. Use chronological holdout/walk-forward testing and prospective paper validation before drawing conclusions.