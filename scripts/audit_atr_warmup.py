"""Audit early-session setups newly made ATR-eligible by prior-session seeding.

Research only. Counts technical opening-range opportunities before NIFTY,
relative-volume, news, circuit, quote, margin, and after-cost gates. It never
places orders or modifies archived candles.
"""
from __future__ import annotations

import argparse
import csv
from datetime import time
import json
from pathlib import Path

import pandas as pd

from src.execution_model import session_aware_atr
from src.market_scanner import add_scanner_indicators, _stock_15m_regime
from src.paper_outcomes import load_candles
from src.strategies.opening_range import opening_range_signal

EARLY_SCAN_STARTS = {
    time(9, 55), time(10, 0), time(10, 5), time(10, 10), time(10, 15)
}


def legacy_session_reset_atr(candles: pd.DataFrame, period: int = 14) -> pd.Series:
    """Reproduce the previous per-session warm-up for comparison only."""
    previous_close = candles["close"].groupby(candles.index.date).shift(1)
    true_range = pd.concat(
        [
            candles["high"] - candles["low"],
            (candles["high"] - previous_close).abs(),
            (candles["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.groupby(candles.index.date).transform(
        lambda values: values.ewm(
            alpha=1 / period, adjust=False, min_periods=period
        ).mean()
    )


def complete_session_dates(candles: pd.DataFrame) -> set[object]:
    """Return sessions with the full 09:15–15:10 five-minute timestamp grid."""
    complete: set[object] = set()
    for day, group in candles.groupby(candles.index.date, sort=True):
        expected = pd.date_range(
            pd.Timestamp(day).tz_localize(candles.index.tz)
            + pd.Timedelta(hours=9, minutes=15),
            periods=72,
            freq="5min",
        )
        if group.index.equals(expected):
            complete.add(day)
    return complete


def _completed_15m_regime(session: pd.DataFrame, as_of: pd.Timestamp) -> str:
    """Build only fully completed 15m bars available at the 5m signal close."""
    bars = session[["open", "high", "low", "close", "volume"]].resample(
        "15min", origin="start_day", offset="15min", label="left", closed="left"
    ).agg(
        {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }
    ).dropna()
    bars = bars.loc[bars.index + pd.Timedelta(minutes=15) <= as_of]
    if bars.empty:
        return "UNKNOWN"
    return _stock_15m_regime(add_scanner_indicators(bars))


def audit_symbol(path: Path) -> list[dict]:
    candles = load_candles(path)
    if "provider_mode" in candles.columns:
        modes = {str(value).strip().lower() for value in candles["provider_mode"].dropna().unique()}
        if modes != {"kite"}:
            return []
    complete = complete_session_dates(candles)
    if len(complete) < 2:
        return []

    indicators = add_scanner_indicators(candles)
    seeded_atr = session_aware_atr(candles, 14)
    legacy_atr = legacy_session_reset_atr(candles, 14)
    symbol = path.stem.upper()
    output: list[dict] = []

    ordered_days = sorted(complete)
    for day_index, day in enumerate(ordered_days):
        if day_index == 0:
            continue
        session = candles.loc[candles.index.date == day].copy()
        if session.empty:
            continue
        prior_day = ordered_days[day_index - 1]
        if prior_day >= day:
            continue
        if pd.isna(seeded_atr.loc[session.index[0]]):
            continue

        seeded_slots = 0
        raw_signals = 0
        trend_aligned_signals = 0
        examples: list[str] = []
        for stamp in session.index:
            if stamp.time().replace(tzinfo=None) not in EARLY_SCAN_STARTS:
                continue
            new_atr = seeded_atr.loc[stamp]
            old_atr = legacy_atr.loc[stamp]
            if pd.isna(new_atr) or pd.notna(old_atr):
                continue
            seeded_slots += 1

            prefix = indicators.loc[indicators.index <= stamp]
            signal = opening_range_signal(
                symbol, prefix, market_regime="UNKNOWN"
            )
            if signal is None:
                continue
            raw_signals += 1

            as_of = stamp + pd.Timedelta(minutes=5)
            regime = _completed_15m_regime(session.loc[:stamp], as_of)
            if (
                (signal.side == "LONG" and regime == "BULLISH")
                or (signal.side == "SHORT" and regime == "BEARISH")
            ):
                trend_aligned_signals += 1
                examples.append(f"{stamp.strftime('%H:%M')}:{signal.side}")

        if seeded_slots:
            output.append(
                {
                    "date": str(day),
                    "symbol": symbol,
                    "previous_session": str(prior_day),
                    "newly_atr_eligible_early_scan_slots": seeded_slots,
                    "opening_range_signals_before_15m_filter": raw_signals,
                    "opening_range_signals_after_15m_stock_filter": trend_aligned_signals,
                    "examples": ";".join(examples),
                }
            )
    return output


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare prior-session ATR warm-up with the previous 14-bar reset."
    )
    parser.add_argument(
        "--candles-dir",
        type=Path,
        default=Path("data/paper_candles_kite"),
        help="Directory containing one multi-session Kite CSV per symbol.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/atr_warmup_audit.csv"),
    )
    args = parser.parse_args()
    if not args.candles_dir.is_dir():
        parser.error(f"candle directory does not exist: {args.candles_dir}")

    rows: list[dict] = []
    rejected_files: list[str] = []
    for path in sorted(args.candles_dir.glob("*.csv")):
        try:
            rows.extend(audit_symbol(path))
        except (ValueError, OSError, KeyError) as exc:
            rejected_files.append(f"{path.name}: {exc}")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    columns = [
        "date",
        "symbol",
        "previous_session",
        "newly_atr_eligible_early_scan_slots",
        "opening_range_signals_before_15m_filter",
        "opening_range_signals_after_15m_stock_filter",
        "examples",
    ]
    with args.output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)

    summary = {
        "symbol_sessions": len(rows),
        "newly_atr_eligible_early_scan_slots": sum(
            int(row["newly_atr_eligible_early_scan_slots"]) for row in rows
        ),
        "opening_range_signals_before_15m_filter": sum(
            int(row["opening_range_signals_before_15m_filter"]) for row in rows
        ),
        "opening_range_signals_after_15m_stock_filter": sum(
            int(row["opening_range_signals_after_15m_stock_filter"]) for row in rows
        ),
        "rejected_files": rejected_files,
        "output": str(args.output),
        "interpretation": (
            "Technical opportunities only; NIFTY direction, relative volume, news, "
            "circuit limits, live quote, margin, and after-cost risk gates are not applied."
        ),
    }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
