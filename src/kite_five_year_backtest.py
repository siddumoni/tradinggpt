"""Five-year NSE intraday backtests using Zerodha Kite historical candles.

Research-only: this module reads Kite market data and runs the existing frozen
strategy backtester. It never submits, modifies, or cancels broker orders.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, time, timedelta
from pathlib import Path
from time import perf_counter
from typing import Any, Callable
from zoneinfo import ZoneInfo

import pandas as pd

from src.backtest import BacktestConfig
from src.data_fetcher import DataFetchError, KiteMarketData, is_nse_cash_equity_instrument
from src.multi_stock_validation import filter_complete_sessions
from src.risk_engine import load_phase2_settings
from src.strategy_backtest import SUPPORTED_STRATEGIES, backtest_strategy

LOGGER = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
DEFAULT_YEARS = 5
MAX_KITE_INTRADAY_CHUNK_DAYS = 60
DEFAULT_SYMBOLS = ("RELIANCE", "HDFCBANK", "ICICIBANK", "SBIN", "INFY")
DEFAULT_STRATEGIES = ("vwap_trend", "vwap_pullback")
PROGRESS_LINE_WIDTH = 180


def _format_duration(seconds: float) -> str:
    """Format elapsed seconds as a readable H:MM:SS duration."""
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:d}:{minutes:02d}:{seconds:02d}"


def _progress_bar(completed: int, total: int, width: int = 24) -> str:
    """Return a dependency-free ASCII progress bar for Windows PowerShell."""
    if total <= 0:
        return "[" + ("-" * width) + "]"
    fraction = min(1.0, max(0.0, completed / total))
    filled = int(width * fraction)
    return f"[{'#' * filled}{'-' * (width - filled)}]"


def _write_progress_line(message: str, *, enabled: bool = True) -> None:
    """Rewrite one console line without adding thousands of progress log rows."""
    if not enabled:
        return
    sys.stdout.write("\r" + message[:PROGRESS_LINE_WIDTH].ljust(PROGRESS_LINE_WIDTH))
    sys.stdout.flush()


def _overall_progress_message(
    completed: int,
    total: int,
    started_at: float,
    last_symbol: str,
    last_status: str,
) -> str:
    elapsed = perf_counter() - started_at
    average_per_stock = elapsed / completed if completed else 0.0
    eta = average_per_stock * max(0, total - completed) if completed else 0.0
    percent = (100.0 * completed / total) if total else 100.0
    return (
        f"Stocks {_progress_bar(completed, total)} {completed}/{total} "
        f"({percent:5.1f}%) | elapsed {_format_duration(elapsed)} "
        f"| ETA {_format_duration(eta)} | last {last_symbol}: {last_status}"
    )


def _as_ist(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Backtest range datetimes must be timezone-aware")
    return value.astimezone(IST)


def iter_kite_chunks(start: datetime, end: datetime, max_days: int = MAX_KITE_INTRADAY_CHUNK_DAYS):
    """Yield non-overlapping inclusive date windows no longer than max_days."""
    start, end = _as_ist(start), _as_ist(end)
    if start > end:
        raise ValueError("start must be <= end")
    if max_days < 1 or max_days > MAX_KITE_INTRADAY_CHUNK_DAYS:
        raise ValueError(f"max_days must be between 1 and {MAX_KITE_INTRADAY_CHUNK_DAYS}")
    cursor = start
    while cursor <= end:
        chunk_end = min(end, cursor + timedelta(days=max_days) - timedelta(seconds=1))
        yield cursor, chunk_end
        cursor = chunk_end + timedelta(seconds=1)


def _normalise_candles(rows: list[dict[str, Any]]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    frame = pd.DataFrame(rows)
    if "date" not in frame:
        raise DataFetchError("Kite candle response is missing its date field")
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    if frame["date"].isna().any():
        raise DataFetchError("Kite candle response contains invalid timestamps")
    if frame["date"].dt.tz is None:
        frame["date"] = frame["date"].dt.tz_localize(IST)
    else:
        frame["date"] = frame["date"].dt.tz_convert(IST)
    required = ["open", "high", "low", "close", "volume"]
    missing = set(required).difference(frame.columns)
    if missing:
        raise DataFetchError(f"Kite candle response is missing fields: {sorted(missing)}")
    for column in required:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.set_index("date").sort_index()
    if frame.index.has_duplicates:
        duplicated = frame.loc[frame.index.duplicated(keep=False)]
        if duplicated.groupby(level=0)[required].nunique(dropna=False).gt(1).any().any():
            raise DataFetchError("Kite returned conflicting duplicate candle timestamps")
        frame = frame.loc[~frame.index.duplicated(keep="first")]
    return frame[required]


def fetch_five_year_candles(
    market_data: KiteMarketData,
    instrument_token: int,
    start: datetime,
    end: datetime,
    *,
    progress_callback: Callable[[int, int, int, float, datetime, datetime], None] | None = None,
) -> pd.DataFrame:
    """Download 5-minute candles in chunks and report per-stock download progress."""
    chunks = list(iter_kite_chunks(start, end))
    pieces = []
    rows_downloaded = 0
    download_started = perf_counter()
    for chunk_number, (chunk_start, chunk_end) in enumerate(chunks, start=1):
        rows = market_data.historical_candles(
            int(instrument_token), chunk_start, chunk_end, "5minute"
        )
        if rows:
            normalised = _normalise_candles(rows)
            pieces.append(normalised)
            rows_downloaded += len(normalised)
        if progress_callback is not None:
            progress_callback(
                chunk_number,
                len(chunks),
                rows_downloaded,
                perf_counter() - download_started,
                chunk_start,
                chunk_end,
            )
    if not pieces:
        raise DataFetchError(f"Kite returned no 5-minute candles for instrument token {instrument_token}")
    return _normalise_candles(
        pd.concat(pieces).reset_index().to_dict("records")
    )


def load_or_fetch_symbol(
    market_data: KiteMarketData,
    instrument: dict[str, Any],
    start: datetime,
    end: datetime,
    cache_dir: Path,
    *,
    refresh: bool = False,
    progress_callback: Callable[[int, int, int, float, datetime, datetime], None] | None = None,
) -> tuple[pd.DataFrame, Path, str]:
    symbol = str(instrument["tradingsymbol"]).upper()
    token = int(instrument["instrument_token"])
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{symbol}_5y_5minute.csv"
    manifest_path = cache_dir / f"{symbol}_5y_5minute.manifest.json"
    expected = {
        "schema_version": 1,
        "provider": "zerodha_kite",
        "symbol": symbol,
        "instrument_token": token,
        "interval": "5minute",
        "requested_start": _as_ist(start).isoformat(),
        "requested_end": _as_ist(end).isoformat(),
    }
    if cache_path.exists() and manifest_path.exists() and not refresh:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if all(manifest.get(key) == value for key, value in expected.items()):
                cached = pd.read_csv(cache_path, parse_dates=["date"])
                cached = cached.set_index("date")
                cached.index = pd.DatetimeIndex(cached.index)
                if cached.index.tz is None:
                    cached.index = cached.index.tz_localize(IST)
                else:
                    cached.index = cached.index.tz_convert(IST)
                return cached.sort_index(), cache_path, "cache_hit"
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            LOGGER.warning("Ignoring invalid Kite cache for %s", symbol)
    frame = fetch_five_year_candles(
        market_data, token, start, end, progress_callback=progress_callback
    )
    saved = frame.copy()
    saved.index.name = "date"
    temp_csv = cache_path.with_suffix(".csv.tmp")
    temp_manifest = manifest_path.with_suffix(".json.tmp")
    saved.reset_index().to_csv(temp_csv, index=False)
    manifest = {
        **expected,
        "fetched_at": datetime.now(IST).isoformat(),
        "rows": int(len(frame)),
        "data_start": frame.index.min().isoformat(),
        "data_end": frame.index.max().isoformat(),
    }
    temp_manifest.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    temp_csv.replace(cache_path)
    temp_manifest.replace(manifest_path)
    return frame, cache_path, "downloaded"


def run_backtests(
    symbols: tuple[str, ...] = DEFAULT_SYMBOLS,
    strategies: tuple[str, ...] = DEFAULT_STRATEGIES,
    years: int = DEFAULT_YEARS,
    output_dir: str | Path = "reports/backtests/kite_5y",
    *,
    refresh_data: bool = False,
    market_data: KiteMarketData | None = None,
    now: datetime | None = None,
    progress: bool = True,
) -> pd.DataFrame:
    if years < 1 or years > 5:
        raise ValueError("years must be between 1 and 5 for this five-year research workflow")
    if not symbols or not strategies:
        raise ValueError("symbols and strategies must not be empty")
    invalid = set(strategies).difference(SUPPORTED_STRATEGIES)
    if invalid:
        raise ValueError(f"Unsupported strategies: {sorted(invalid)}")
    configured, charges = load_phase2_settings()
    current = _as_ist(now or datetime.now(IST))
    # Exclude today's potentially incomplete session; session filter also removes
    # exchange holidays, partial first/last days, missing bars and malformed candles.
    end = datetime.combine(current.date() - timedelta(days=1), time(23, 59, 59), tzinfo=IST)
    try:
        start_date = end.date().replace(year=end.year - years)
    except ValueError:
        # A 29-Feb end date maps to 28-Feb in a non-leap start year.
        start_date = end.date().replace(year=end.year - years, day=28)
    start = datetime.combine(start_date, time(0, 0), tzinfo=IST)
    client = market_data or KiteMarketData.from_cached_session()
    instruments = client.instruments()
    by_symbol = {
        str(row.get("tradingsymbol", "")).upper(): row
        for row in instruments if is_nse_cash_equity_instrument(row)
    }
    out = Path(output_dir)
    cache_dir = out / "candle_cache"
    out.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    overall_started = perf_counter()
    completed_symbols = 0
    total_symbols = len(symbols)

    def update_overall(symbol: str, status: str) -> None:
        if progress:
            _write_progress_line(
                _overall_progress_message(
                    completed_symbols, total_symbols, overall_started, symbol, status
                ),
                enabled=True,
            )

    for symbol_number, raw_symbol in enumerate(symbols, start=1):
        symbol = raw_symbol.strip().upper()
        stock_started = perf_counter()
        if progress:
            print()
            print(f"[{symbol_number:02d}/{total_symbols:02d}] {symbol}: starting")

        instrument = by_symbol.get(symbol)
        if instrument is None:
            error = "Not found as an eligible NSE cash-equity instrument in current Kite instrument dump"
            rows.append({"symbol": symbol, "error": error, "cache_status": "not_found"})
            if progress:
                print(f"  {symbol}: SKIPPED - {error}")
            completed_symbols += 1
            update_overall(symbol, "SKIPPED")
            continue

        def show_download_progress(
            chunk_number: int,
            chunk_total: int,
            row_count: int,
            elapsed: float,
            chunk_start: datetime,
            chunk_end: datetime,
        ) -> None:
            average_chunk_seconds = elapsed / chunk_number if chunk_number else 0.0
            remaining_seconds = average_chunk_seconds * max(0, chunk_total - chunk_number)
            percent = 100.0 * chunk_number / chunk_total if chunk_total else 100.0
            message = (
                f"  {symbol} download {_progress_bar(chunk_number, chunk_total, 18)} "
                f"{chunk_number}/{chunk_total} ({percent:5.1f}%) | "
                f"{row_count:,} candles | elapsed {_format_duration(elapsed)} "
                f"| ETA {_format_duration(remaining_seconds)} | "
                f"{chunk_start:%Y-%m-%d} to {chunk_end:%Y-%m-%d}"
            )
            _write_progress_line(message, enabled=progress)

        try:
            raw, cache_path, cache_status = load_or_fetch_symbol(
                client,
                instrument,
                start,
                end,
                cache_dir,
                refresh=refresh_data,
                progress_callback=show_download_progress if progress else None,
            )
            if progress:
                if cache_status == "cache_hit":
                    print(
                        f"  {symbol}: CACHE HIT - {len(raw):,} candles loaded; "
                        "historical download skipped"
                    )
                else:
                    print()
                    print(f"  {symbol}: DOWNLOAD COMPLETE - {len(raw):,} candles saved")
            frame, quality = filter_complete_sessions(raw)
            if frame.empty:
                raise DataFetchError(
                    "No complete, valid 5-minute NSE sessions remain after quality filtering"
                )
            if progress:
                print(
                    f"  {symbol}: DATA CHECK - {quality['sessions_kept']:,} complete sessions, "
                    f"{quality['sessions_excluded']:,} excluded, "
                    f"{quality['missing_bars_total']:,} missing bars"
                )
        except Exception as exc:
            if progress:
                print()
                print(f"  {symbol}: ERROR - {exc}")
            LOGGER.exception("Kite history failed for %s", symbol)
            rows.append({"symbol": symbol, "error": str(exc), "cache_status": "error"})
            completed_symbols += 1
            update_overall(symbol, "ERROR")
            continue

        strategy_total = len(strategies) * 2
        strategy_number = 0
        for strategy in strategies:
            for slippage in (5.0, 8.0):
                strategy_number += 1
                if progress:
                    _write_progress_line(
                        f"  {symbol} backtest {strategy_number}/{strategy_total}: "
                        f"{strategy} at {slippage:g} bps per side",
                        enabled=True,
                    )
                try:
                    config = BacktestConfig(
                        initial_capital=configured.capital_inr,
                        risk_per_trade=configured.risk_per_trade_inr,
                        minimum_net_risk_reward=configured.minimum_net_risk_reward,
                        slippage_bps_per_side=slippage,
                        entry_cutoff=configured.entry_cutoff_ist,
                        square_off_time=configured.square_off_time_ist,
                        charge_rates=charges,
                    )
                    trades, summary = backtest_strategy(symbol, frame, strategy, config=config)
                    rows.append({
                        **summary,
                        "symbol": symbol,
                        "strategy": strategy,
                        "data_provider": "zerodha_kite",
                        "data_interval": "5minute",
                        "requested_years": years,
                        "slippage_bps_per_side": slippage,
                        "data_start": frame.index.min().isoformat(),
                        "data_end": frame.index.max().isoformat(),
                        "complete_sessions": quality["sessions_kept"],
                        "excluded_sessions": quality["sessions_excluded"],
                        "missing_bars_total": quality["missing_bars_total"],
                        "cache_path": str(cache_path),
                        "cache_status": cache_status,
                        "trades_csv": None,
                        "error": None,
                    })
                    if not trades.empty:
                        trade_dir = out / "trade_logs"
                        trade_dir.mkdir(parents=True, exist_ok=True)
                        trade_path = trade_dir / f"{symbol}_{strategy}_{int(slippage)}bps_trades.csv"
                        trades.to_csv(trade_path, index=False)
                        rows[-1]["trades_csv"] = str(trade_path)
                except Exception as exc:
                    LOGGER.exception(
                        "Backtest failed for %s %s at %.1f bps", symbol, strategy, slippage
                    )
                    rows.append({
                        "symbol": symbol,
                        "strategy": strategy,
                        "slippage_bps_per_side": slippage,
                        "data_provider": "zerodha_kite",
                        "error": str(exc),
                    })
        stock_elapsed = perf_counter() - stock_started
        if progress:
            print()
            print(
                f"  {symbol}: FINISHED in {_format_duration(stock_elapsed)} "
                f"(download status: {cache_status})"
            )
        completed_symbols += 1
        update_overall(symbol, "DONE")

    if progress:
        print()
    result = pd.DataFrame(rows)
    stamp = datetime.now(IST).strftime("%Y%m%d_%H%M%S")
    report = out / f"kite_five_year_backtest_{stamp}.csv"
    result.to_csv(report, index=False)
    (out / f"kite_five_year_backtest_{stamp}.json").write_text(
        result.to_json(orient="records", indent=2, date_format="iso"), encoding="utf-8"
    )
    print(result.to_string(index=False))
    print(f"Report: {report}")
    print("Research only: no broker orders were placed. Backtest results are not a promise of future profitability.")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Five-year NSE 5-minute backtests using Zerodha Kite historical data.")
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    parser.add_argument("--strategies", default=",".join(DEFAULT_STRATEGIES))
    parser.add_argument("--years", type=int, default=5)
    parser.add_argument("--output-dir", default="reports/backtests/kite_5y")
    parser.add_argument("--refresh-data", action="store_true")
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="Disable the PowerShell progress bars and ETA display",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_backtests(
        symbols=tuple(x.strip().upper() for x in args.symbols.split(",") if x.strip()),
        strategies=tuple(x.strip() for x in args.strategies.split(",") if x.strip()),
        years=args.years,
        output_dir=args.output_dir,
        refresh_data=args.refresh_data,
        progress=not args.no_progress,
    )


if __name__ == "__main__":
    main()
