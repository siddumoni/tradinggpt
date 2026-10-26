"""Trend pullback-to-VWAP strategy using completed candles."""
from __future__ import annotations

import pandas as pd

from .models import StrategySignal
from .market_regime import Regime


def vwap_pullback_signal(
    symbol: str,
    candles: pd.DataFrame,
    market_regime: Regime = "UNKNOWN",
    max_distance_atr: float = 0.35,
    diagnostics: list[str] | None = None,
) -> StrategySignal | None:
    """Signal a pullback near VWAP that closes back in the trend direction.

    Requires at least two completed rows. Diagnostics are informational only;
    they explain rejected setups without relaxing the strategy rules.
    """
    def reject(reason: str) -> None:
        if diagnostics is not None:
            diagnostics.append(f"VWAP pullback: {reason}")
        return None

    required = {"open", "high", "low", "close", "vwap", "ema_9", "ema_21", "atr_14"}
    if (
        len(candles) < 2
        or not required.issubset(candles.columns)
        or not isinstance(candles.index, pd.DatetimeIndex)
        or candles.index.has_duplicates
        or not candles.index.is_monotonic_increasing
    ):
        return reject("two completed candles, required columns, and a unique increasing timestamp index are required.")

    # A pullback must be formed by two consecutive candles in the same NSE
    # session. Without this guard, the prior day's closing candle can be paired
    # with today's opening candle after VWAP resets at the session boundary.
    previous_stamp = pd.Timestamp(candles.index[-2])
    current_stamp = pd.Timestamp(candles.index[-1])
    if previous_stamp.tzinfo is None:
        previous_stamp = previous_stamp.tz_localize("Asia/Kolkata")
    else:
        previous_stamp = previous_stamp.tz_convert("Asia/Kolkata")
    if current_stamp.tzinfo is None:
        current_stamp = current_stamp.tz_localize("Asia/Kolkata")
    else:
        current_stamp = current_stamp.tz_convert("Asia/Kolkata")
    if previous_stamp.date() != current_stamp.date():
        return reject("the two candles straddle different NSE sessions.")
    if current_stamp - previous_stamp != pd.Timedelta(minutes=5):
        return reject("the prior and current candles are not consecutive five-minute bars.")

    prev, row = candles.iloc[-2], candles.iloc[-1]
    if pd.isna(prev[list(required)]).any() or pd.isna(row[list(required)]).any():
        return reject("one or more required indicator values are not ready.")
    atr = float(row["atr_14"])
    vwap = float(row["vwap"])
    close = float(row["close"])
    open_price = float(row["open"])
    ema9 = float(row["ema_9"])
    ema21 = float(row["ema_21"])
    if atr <= 0:
        return reject(f"ATR14 is not positive ({atr:.4f}).")
    distance_atr = abs(float(prev["close"]) - float(prev["vwap"])) / atr
    near_vwap = distance_atr <= max_distance_atr
    bullish_candle = close > open_price
    bearish_candle = close < open_price
    above_vwap = close > vwap
    below_vwap = close < vwap
    ema_up = ema9 > ema21
    ema_down = ema9 < ema21

    if market_regime != "BEARISH" and near_vwap and bullish_candle and above_vwap and ema_up:
        return StrategySignal(
            symbol, "LONG", "vwap_pullback",
            f"Enter only after a completed 5-minute close above ₹{close:.2f}",
            f"Invalidate if price closes below VWAP ₹{vwap:.2f} or EMA21 ₹{ema21:.2f}",
            64 if market_regime == "BULLISH" else 54,
            f"Prior candle was within {max_distance_atr:.2f} ATR of VWAP and current candle reclaimed VWAP in an EMA uptrend; regime={market_regime}.",
        )
    if market_regime != "BULLISH" and near_vwap and bearish_candle and below_vwap and ema_down:
        return StrategySignal(
            symbol, "SHORT", "vwap_pullback",
            f"Enter only after a completed 5-minute close below ₹{close:.2f}",
            f"Invalidate if price closes above VWAP ₹{vwap:.2f} or EMA21 ₹{ema21:.2f}",
            64 if market_regime == "BEARISH" else 54,
            f"Prior candle was within {max_distance_atr:.2f} ATR of VWAP and current candle rejected VWAP in an EMA downtrend; regime={market_regime}.",
        )

    long_failures: list[str] = []
    if market_regime == "BEARISH":
        long_failures.append("blocked by BEARISH NIFTY regime")
    if not near_vwap:
        long_failures.append(f"prior close is {distance_atr:.2f} ATR from VWAP (limit {max_distance_atr:.2f})")
    if not bullish_candle:
        long_failures.append("latest candle did not close bullish")
    if not above_vwap:
        long_failures.append("latest close did not reclaim VWAP")
    if not ema_up:
        long_failures.append("EMA9 is not above EMA21")

    short_failures: list[str] = []
    if market_regime == "BULLISH":
        short_failures.append("blocked by BULLISH NIFTY regime")
    if not near_vwap:
        short_failures.append(f"prior close is {distance_atr:.2f} ATR from VWAP (limit {max_distance_atr:.2f})")
    if not bearish_candle:
        short_failures.append("latest candle did not close bearish")
    if not below_vwap:
        short_failures.append("latest close did not reject VWAP")
    if not ema_down:
        short_failures.append("EMA9 is not below EMA21")

    parts = []
    if long_failures:
        parts.append("LONG failed: " + ", ".join(long_failures))
    if short_failures:
        parts.append("SHORT failed: " + ", ".join(short_failures))
    return reject("no valid pullback entry; " + "; ".join(parts) + ".")
