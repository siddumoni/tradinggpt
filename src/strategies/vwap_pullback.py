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
) -> StrategySignal | None:
    """Signal a pullback near VWAP that closes back in the trend direction.

    Requires at least two completed rows. This is a deliberately conservative
    heuristic; it does not estimate probability of profit.
    """
    required = {"open", "high", "low", "close", "vwap", "ema_9", "ema_21", "atr_14"}
    if (
        len(candles) < 2
        or not required.issubset(candles.columns)
        or not isinstance(candles.index, pd.DatetimeIndex)
        or candles.index.has_duplicates
        or not candles.index.is_monotonic_increasing
    ):
        return None

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
    if (
        previous_stamp.date() != current_stamp.date()
        or current_stamp - previous_stamp != pd.Timedelta(minutes=5)
    ):
        return None

    prev, row = candles.iloc[-2], candles.iloc[-1]
    if pd.isna(prev[list(required)]).any() or pd.isna(row[list(required)]).any():
        return None
    atr = float(row["atr_14"])
    vwap = float(row["vwap"])
    close = float(row["close"])
    if atr <= 0:
        return None
    near_vwap = abs(float(prev["close"]) - float(prev["vwap"])) <= max_distance_atr * atr

    if (
        market_regime != "BEARISH"
        and near_vwap
        and float(row["close"]) > float(row["open"])
        and close > vwap
        and float(row["ema_9"]) > float(row["ema_21"])
    ):
        return StrategySignal(
            symbol, "LONG", "vwap_pullback",
            f"Enter only after a completed 5-minute close above ₹{close:.2f}",
            f"Invalidate if price closes below VWAP ₹{vwap:.2f} or EMA21 ₹{float(row['ema_21']):.2f}",
            64 if market_regime == "BULLISH" else 54,
            f"Prior candle was within {max_distance_atr:.2f} ATR of VWAP and current candle reclaimed VWAP in an EMA uptrend; regime={market_regime}.",
        )
    if (
        market_regime != "BULLISH"
        and near_vwap
        and float(row["close"]) < float(row["open"])
        and close < vwap
        and float(row["ema_9"]) < float(row["ema_21"])
    ):
        return StrategySignal(
            symbol, "SHORT", "vwap_pullback",
            f"Enter only after a completed 5-minute close below ₹{close:.2f}",
            f"Invalidate if price closes above VWAP ₹{vwap:.2f} or EMA21 ₹{float(row['ema_21']):.2f}",
            64 if market_regime == "BEARISH" else 54,
            f"Prior candle was within {max_distance_atr:.2f} ATR of VWAP and current candle rejected VWAP in an EMA downtrend; regime={market_regime}.",
        )
    return None
