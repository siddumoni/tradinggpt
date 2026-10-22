"""Multi-stock sensitivity validation for advisory-only intraday strategies.

Downloads each symbol's 5-minute candles once, persists the exact dataset to CSV,
and reuses those saved candles for every strategy/slippage scenario. No orders are
submitted. Yahoo Finance is an unofficial source; validate candle coverage before
interpreting results.

Run from the repository root:
    python -m src.multi_stock_validation --days 30

The file should be placed at src/multi_stock_validation.py.
"""
from __future__ import annotations

import argparse
import logging
import math
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from src.backtest import BacktestConfig
from src.strategy_backtest import backtest_strategy, candles_to_frame, SUPPORTED_STRATEGIES
from src.yahoo_data_fetcher import YahooFinanceMarketData

LOGGER = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
DEFAULT_SYMBOLS = ("RELIANCE", "HDFCBANK", "ICICIBANK", "SBIN", "INFY")
DEFAULT_STRATEGIES = ("vwap_trend", "vwap_pullback")
DEFAULT_SLIPPAGE_BPS = (2.0, 5.0, 8.0)


def _load_or_fetch_candles(
    symbol: str,
    days: int,
    cache_dir: Path,
    *,
    refresh_data: bool = False,
) -> tuple[pd.DataFrame, Path, str]:
    """Load a saved OHLCV dataset or fetch and persist it once for reproducibility."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{symbol.upper()}_{days}d_5minute.csv"

    if cache_path.exists() and not refresh_data:
        frame = pd.read_csv(cache_path, parse_dates=["date"])
        if "date" not in frame.columns:
            raise RuntimeError(f"Saved candle cache lacks the date column: {cache_path}")
        if frame["date"].isna().any():
            invalid_count = int(frame["date"].isna().sum())
            raise RuntimeError(
                f"Saved candle cache has {invalid_count} invalid timestamp(s): {cache_path}"
            )
        frame = frame.set_index("date").sort_index()
        required = ("open", "high", "low", "close", "volume")
        missing = set(required).difference(frame.columns)
        if missing:
            raise RuntimeError(
                f"Saved candle cache is missing columns {sorted(missing)}: {cache_path}"
            )
        for column in required:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        if frame.empty:
            raise RuntimeError(f"Saved candle cache is empty/invalid: {cache_path}")
        # Keep malformed OHLCV rows for the session filter to reject and report;
        # dropping them here would conceal whether the provider supplied bad data.
        return frame, cache_path, "loaded_cache"

    now = datetime.now(IST)
    provider = YahooFinanceMarketData()
    raw = provider.historical_candles(
        symbol, now - timedelta(days=days), now, "5minute"
    )
    frame = candles_to_frame(raw)
    if frame.empty:
        raise RuntimeError(f"No historical candles returned for {symbol}; skipped this symbol.")

    to_save = frame.copy()
    to_save.index.name = "date"
    to_save.reset_index().to_csv(cache_path, index=False)
    return frame, cache_path, "downloaded_and_cached"



def filter_complete_sessions(frame: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Keep only complete, valid NSE 5-minute sessions (09:15–15:10 IST).

    Match the 72-bar window enforced by strategy_backtest.filter_incomplete_sessions.
    Reject sessions with missing/duplicate bars or invalid OHLCV; never fill gaps.
    """
    empty_quality = {
        "sessions_before_filter": 0, "sessions_kept": 0,
        "sessions_excluded": 0, "excluded_session_dates": "",
        "sessions_kept_with_gaps": 0, "missing_bars_total": 0,
        "session_filter_status": "empty",
    }
    if frame.empty:
        return frame.copy(), empty_quality

    required = ("open", "high", "low", "close", "volume")
    absent = set(required) - set(frame.columns)
    if absent:
        raise ValueError(f"Missing candle columns: {sorted(absent)}")

    clean = frame.copy()
    idx = pd.DatetimeIndex(pd.to_datetime(clean.index, errors="coerce"))
    valid_idx = ~idx.isna()
    clean = clean.loc[valid_idx].copy()
    idx = idx[valid_idx]
    if idx.tz is None:
        idx = idx.tz_localize(IST, ambiguous="NaT", nonexistent="NaT")
    else:
        idx = idx.tz_convert(IST)
    clean.index = idx
    clean = clean.loc[~clean.index.isna()].sort_index()
    if clean.empty:
        return clean, {**empty_quality, "session_filter_status": "no_valid_timestamps"}

    for column in required:
        clean[column] = pd.to_numeric(clean[column], errors="coerce")

    # Some feeds contain 15:15/15:20/15:25 bars. These are not part of the
    # strategy backtest's required window, so ignore them rather than marking
    # otherwise complete 09:15–15:10 sessions invalid.
    clock = clean.index.strftime("%H:%M")
    window = clean.loc[(clock >= "09:15") & (clock <= "15:10")].copy()
    expected = set(pd.date_range("09:15", "15:10", freq="5min").strftime("%H:%M"))

    kept_dates = []
    excluded = []
    missing_total = 0
    all_dates = sorted(set(clean.index.date))
    for day in all_dates:
        session = window.loc[window.index.date == day]
        actual = set(session.index.strftime("%H:%M"))
        missing = sorted(expected - actual)
        missing_total += len(missing)
        duplicate = session.index.has_duplicates
        prices = session[["open", "high", "low", "close"]]
        valid_prices = prices.notna().all(axis=1) & prices.apply(
            lambda col: col.map(math.isfinite)
        ).all(axis=1) & (prices > 0).all(axis=1)
        valid_ohlc = (
            (session["high"] >= session[["open", "close", "low"]].max(axis=1))
            & (session["low"] <= session[["open", "close", "high"]].min(axis=1))
        )
        volume = session["volume"]
        valid_volume = volume.notna() & volume.map(math.isfinite) & (volume >= 0)
        invalid_rows = int((~(valid_prices & valid_ohlc & valid_volume)).sum())
        if not duplicate and len(session) == 72 and not missing and invalid_rows == 0:
            kept_dates.append(day)
        else:
            reasons = []
            if duplicate:
                reasons.append("duplicate timestamps")
            if missing:
                reasons.append(f"{len(missing)} missing expected bars: {','.join(missing[:6])}")
            if len(session) != 72:
                reasons.append(f"{len(session)} bars instead of 72")
            if invalid_rows:
                reasons.append(f"{invalid_rows} invalid OHLCV rows")
            excluded.append(f"{day.isoformat()} ({'; '.join(reasons)})")

    keep = set(kept_dates)
    filtered = window.loc[[stamp.date() in keep for stamp in window.index]].copy()
    return filtered, {
        "sessions_before_filter": len(all_dates),
        "sessions_kept": len(kept_dates),
        "sessions_excluded": len(excluded),
        "excluded_session_dates": " | ".join(excluded),
        "sessions_kept_with_gaps": 0,
        "missing_bars_total": missing_total,
        "session_filter_status": "ok" if kept_dates else "no_usable_sessions",
    }

def run_validation(
    symbols: tuple[str, ...] = DEFAULT_SYMBOLS,
    strategies: tuple[str, ...] = DEFAULT_STRATEGIES,
    days: int = 30,
    output_dir: str | Path = "reports/backtests",
    risk_per_trade: float = 200.0,
    minimum_net_risk_reward: float = 1.5,
    target_atr_multiple: float = 2.5,
    slippage_values: tuple[float, ...] = DEFAULT_SLIPPAGE_BPS,
    reject_nontrending_regimes: bool = False,
    refresh_data: bool = False,
) -> pd.DataFrame:
    """Run all strategy/slippage combinations against each symbol's saved candles."""
    if not 5 <= days <= 59:
        raise ValueError("days must be between 5 and 59")
    if not symbols or not strategies or not slippage_values:
        raise ValueError("symbols, strategies, and slippage_values must not be empty")
    invalid = set(strategies).difference(SUPPORTED_STRATEGIES)
    if invalid:
        raise ValueError(f"Unsupported strategies: {sorted(invalid)}")
    if not math.isfinite(risk_per_trade) or risk_per_trade <= 0:
        raise ValueError("risk_per_trade must be a finite positive INR amount")
    if minimum_net_risk_reward <= 0:
        raise ValueError("minimum_net_risk_reward must be positive")
    if target_atr_multiple <= 1.0 or target_atr_multiple > 10.0:
        raise ValueError("target_atr_multiple must be > 1.0 and <= 10.0")
    if any(bps < 0 or bps > 100 for bps in slippage_values):
        raise ValueError("slippage values must be between 0 and 100 bps per side")

    out = Path(output_dir)
    cache_dir = out / "candle_cache"
    out.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []

    for symbol in symbols:
        symbol = symbol.strip().upper()
        if not symbol:
            continue
        try:
            frame, cache_path, cache_status = _load_or_fetch_candles(
                symbol, days, cache_dir, refresh_data=refresh_data
            )
            bars_before_filter = int(len(frame))
            frame, quality = filter_complete_sessions(frame)
            LOGGER.info(
                "%s: %d/%d bars after complete-session filter; %d/%d sessions kept (%s)",
                symbol, len(frame), bars_before_filter,
                quality["sessions_kept"], quality["sessions_before_filter"], cache_status
            )
            if frame.empty:
                raise RuntimeError(
                    f"No usable sessions available for {symbol}. Each retained session "
                    "must contain all 72 valid 5-minute bars from 09:15 through 15:10 IST. "
                    "Check candle cache/source coverage."
                )
        except Exception as exc:  # continue the batch, but preserve the failure in the report
            LOGGER.exception("Could not load candles for %s", symbol)
            rows.append({
                "symbol": symbol, "risk_per_trade": risk_per_trade, "strategy": None, "slippage_bps_per_side": None,
                "bars": 0, "trades": 0, "win_rate_pct": None, "net_pnl": None,
                "profit_factor": None, "expectancy_per_trade": None,
                "max_drawdown": None, "executed_trade_estimated_costs": None,
                "ending_equity": None, "net_rr_rejections": None,
                "data_start": None, "data_end": None,
                "bars_before_filter": None, "sessions_before_filter": None,
                "sessions_kept": None, "sessions_excluded": None,
                "excluded_session_dates": None, "session_filter_status": "error",
                "reject_nontrending_regimes": reject_nontrending_regimes,
                "candle_cache_path": None, "candle_cache_status": "error",
                "error": str(exc),
            })
            continue

        # All strategy/slippage runs for this symbol use this exact in-memory frame.
        for strategy in strategies:
            for bps in slippage_values:
                # Run paired baseline and experimental filter variants on the same candles.
                for filter_enabled in (False, True):
                    try:
                        config = BacktestConfig(
                            risk_per_trade=risk_per_trade,
                            minimum_net_risk_reward=minimum_net_risk_reward,
                            slippage_bps_per_side=float(bps),
                        )
                        trades, summary = backtest_strategy(
                            symbol, frame, strategy, config=config,
                            target_atr_multiple=target_atr_multiple,
                            reject_nontrending_regimes=filter_enabled,
                        )
                        
                        # Export individual trades for this configuration.
                        if not trades.empty:
                            trade_log_dir = out / "trade_logs"
                            trade_log_dir.mkdir(parents=True, exist_ok=True)

                            regime_label = (
                                "filtered" if filter_enabled else "baseline"
                            )
                            trade_stamp = datetime.now().strftime(
                                "%Y%m%d_%H%M%S_%f"
                            )

                            
                            trade_log = trades.copy()

                            # Assign columns safely: some already exist
                            # in the backtest's trade DataFrame.
                            trade_log["symbol"] = symbol
                            trade_log["strategy"] = strategy
                            trade_log["risk_per_trade"] = risk_per_trade
                            trade_log["slippage_bps_per_side"] = float(bps)
                            trade_log["regime_filter"] = (
                                "filtered" if filter_enabled else "baseline"
                            )

                            trade_stamp = datetime.now().strftime(
                                "%Y%m%d_%H%M%S_%f"
                            )
                            trade_path = trade_log_dir / (
                                f"{symbol}_{strategy}_{bps}bps_"
                                f"{'filtered' if filter_enabled else 'baseline'}_"
                                f"{trade_stamp}_trades.csv"
                            )
                            trade_log.to_csv(trade_path, index=False)
                            print(f"Trade log: {trade_path}")



                        rows.append({
                        "symbol": symbol,
                        "risk_per_trade": risk_per_trade,
                        "strategy": strategy,
                        "slippage_bps_per_side": float(bps),
                        "bars": int(len(frame)),
                        "backtester_bars": summary.get("bars"),
                        "backtester_sessions_kept": summary.get("sessions_kept"),
                        "backtester_sessions_excluded": summary.get("sessions_excluded"),
                        "trades": int(summary.get("trades", len(trades))),
                        "win_rate_pct": summary.get("win_rate_pct"),
                        "net_pnl": summary.get("net_pnl"),
                        "profit_factor": summary.get("profit_factor"),
                        "expectancy_per_trade": summary.get("expectancy_per_trade"),
                        "max_drawdown": summary.get("max_drawdown"),
                        "executed_trade_estimated_costs": (
                            float(trades["estimated_costs"].astype(float).sum())
                            if not trades.empty and "estimated_costs" in trades.columns else 0.0
                        ),
                        "ending_equity": summary.get("ending_equity"),
                        "net_rr_rejections": (summary.get("rejections") or {}).get("net_rr_below_minimum"),
                        "data_start": str(frame.index.min()),
                        "data_end": str(frame.index.max()),
                        "bars_before_filter": bars_before_filter,
                        **quality,
                            "reject_nontrending_regimes": filter_enabled,
                            "candle_cache_path": str(cache_path),
                            "candle_cache_status": cache_status,
                            "error": None,
                        })
                    except Exception as exc:
                        LOGGER.exception("Scenario failed: %s %s %.1f bps filter=%s", symbol, strategy, bps, filter_enabled)
                        rows.append({
                            "symbol": symbol, "risk_per_trade": risk_per_trade, "strategy": strategy,
                            "slippage_bps_per_side": float(bps), "bars": int(len(frame)),
                            "trades": 0, "win_rate_pct": None, "net_pnl": None,
                            "profit_factor": None, "expectancy_per_trade": None,
                            "max_drawdown": None, "executed_trade_estimated_costs": None,
                            "ending_equity": None, "net_rr_rejections": None,
                            "data_start": str(frame.index.min()), "data_end": str(frame.index.max()),
                            "bars_before_filter": bars_before_filter,
                            **quality,
                            "reject_nontrending_regimes": filter_enabled,
                            "candle_cache_path": str(cache_path),
                            "candle_cache_status": cache_status, "error": str(exc),
                        })

    result = pd.DataFrame(rows)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = out / f"multi_stock_validation_{stamp}.csv"
    result.to_csv(report_path, index=False)
    print("\nMulti-stock strategy validation (same saved candles per symbol; baseline and filtered variants):")
    print(result.to_string(index=False))
    print(f"\nComparison CSV: {report_path}")
    print(f"Candle cache directory: {cache_dir}")
    print("Data quality: requires all 72 valid 5-minute bars from 09:15 through 15:10 IST; incomplete sessions are excluded.")
    print("Missing candles are not synthesized; missing_bars_total counts missing expected bars across inspected sessions.")
    print("Research only: historical results are not predictive; no orders were submitted.")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Multi-stock sensitivity backtest for advisory-only NSE strategies."
    )
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    parser.add_argument("--strategies", default=",".join(DEFAULT_STRATEGIES))
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--output-dir", default="reports/backtests")
    parser.add_argument("--risk-per-trade", type=float, default=200.0, help="Configurable positive planned loss budget per trade in INR; no arbitrary upper cap")
    parser.add_argument("--min-net-rr", type=float, default=1.5)
    parser.add_argument("--target-atr-multiple", type=float, default=2.5)
    parser.add_argument("--slippage-bps", default="2,5,8", help="Comma-separated per-side slippage scenarios")
    parser.add_argument("--refresh-data", action="store_true", help="Redownload candle data instead of using saved cache")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        symbols = tuple(item.strip().upper() for item in args.symbols.split(",") if item.strip())
        strategies = tuple(item.strip() for item in args.strategies.split(",") if item.strip())
        slippage_values = tuple(float(item.strip()) for item in args.slippage_bps.split(",") if item.strip())
    except ValueError as exc:
        parser.error(f"Invalid comma-separated option: {exc}")
    run_validation(
        symbols=symbols, strategies=strategies, days=args.days,
        output_dir=args.output_dir, risk_per_trade=args.risk_per_trade,
        minimum_net_risk_reward=args.min_net_rr,
        target_atr_multiple=args.target_atr_multiple,
        slippage_values=slippage_values,
        refresh_data=args.refresh_data,
    )


if __name__ == "__main__":
    main()
