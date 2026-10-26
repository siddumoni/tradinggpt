"""Opening-range breakout strategy using completed 5-minute candles."""
from __future__ import annotations

import pandas as pd

from .models import StrategySignal
from .market_regime import Regime


_OPENING_TIMES = ("09:15", "09:20", "09:25")


def opening_range_signal(
    symbol: str,
    candles: pd.DataFrame,
    market_regime: Regime = "UNKNOWN",
    volume_multiple: float = 1.2,
    diagnostics: list[str] | None = None,
) -> StrategySignal | None:
    """Signal an opening-range breakout only from completed candles.

    Optional diagnostics receive one concise rejection reason when no signal is
    returned. Diagnostics explain existing rules; they do not relax any rule.
    """
    def reject(reason: str) -> None:
        if diagnostics is not None:
            diagnostics.append(f"Opening range: {reason}")
        return None

    required = {"open", "high", "low", "close", "volume"}
    if len(candles) < 4 or not required.issubset(candles.columns):
        return reject("at least four candles with OHLCV columns are required.")
    if not isinstance(candles.index, pd.DatetimeIndex):
        return reject("timestamps are not a DatetimeIndex.")
    if candles.index.has_duplicates:
        return reject("duplicate candle timestamps were found.")

    df = candles.sort_index()
    dates = pd.Index(df.index.date)
    today = dates[-1]
    session = df.loc[dates == today]
    if len(session) < 4:
        return reject("fewer than four completed candles are available in the current session.")

    # The strategy is defined for NSE regular-session timestamps in IST.
    # Do not silently treat a partial/misaligned feed as a valid opening range.
    session_index = pd.DatetimeIndex(session.index)
    if session_index.tz is not None:
        session_index = session_index.tz_convert("Asia/Kolkata")
    opening_times = tuple(ts.strftime("%H:%M") for ts in session_index[:3])
    if opening_times != _OPENING_TIMES:
        return reject("the first three session candles are not the 09:15, 09:20, and 09:25 IST opening bars.")

    # Ensure the first three opening bars are consecutive five-minute bars.
    if any(
        (session_index[i + 1] - session_index[i]) != pd.Timedelta(minutes=5)
        for i in range(2)
    ):
        return reject("the opening-range candles are not consecutive five-minute bars.")

    # Reject malformed opening bars rather than computing a range from them.
    opening = session.iloc[:3]
    opening_ohlcv = opening[list(required)].apply(pd.to_numeric, errors="coerce")
    if opening_ohlcv.isna().any().any():
        return reject("one or more opening-range OHLCV values are missing or non-numeric.")
    if (opening_ohlcv[["open", "high", "low", "close"]] <= 0).any().any():
        return reject("one or more opening-range prices are not positive.")
    if (opening_ohlcv["volume"] < 0).any():
        return reject("opening-range volume is negative.")
    if (
        (opening_ohlcv["high"] < opening_ohlcv[["open", "low", "close"]].max(axis=1))
        | (opening_ohlcv["low"] > opening_ohlcv[["open", "high", "close"]].min(axis=1))
    ).any():
        return reject("opening-range OHLC values are inconsistent.")

    latest = session.iloc[-1]
    latest_values = pd.to_numeric(latest[list(required)], errors="coerce")
    if latest_values.isna().any():
        return reject("latest OHLCV values are missing or non-numeric.")
    if float(latest_values["volume"]) < 0:
        return reject("latest candle volume is negative.")
    if any(float(latest_values[k]) <= 0 for k in ("open", "high", "low", "close")):
        return reject("latest candle contains a non-positive price.")

    # Only allow a signal after the opening range has completed at 09:30.
    latest_time = session_index[-1].strftime("%H:%M")
    if latest_time < "09:30":
        return reject("the opening range has not completed by 09:30 IST.")

    range_high = float(opening["high"].max())
    range_low = float(opening["low"].min())
    prior_volume = pd.to_numeric(session["volume"].iloc[:-1].tail(20), errors="coerce").dropna()
    baseline = float(prior_volume.mean()) if not prior_volume.empty else 0.0
    latest_volume = float(latest_values["volume"])
    if baseline <= 0:
        return reject("a positive prior-volume baseline is unavailable.")
    if latest_volume < baseline * volume_multiple:
        return reject(
            f"relative-volume confirmation failed (latest {latest_volume:.0f}; "
            f"required at least {baseline * volume_multiple:.0f})."
        )

    close = float(latest_values["close"])
    if close > range_high:
        if market_regime == "BEARISH":
            return reject("price broke above the opening range, but the BEARISH NIFTY regime blocks a long signal.")
        return StrategySignal(
            symbol=symbol, side="LONG", strategy="opening_range_breakout",
            trigger=f"Enter only after a completed 5-minute close above ₹{range_high:.2f}",
            invalidation=f"Setup invalid if price closes back below ₹{range_high:.2f}",
            confidence=65 if market_regime == "BULLISH" else 55,
            reason=f"Close ₹{close:.2f} broke opening-range high ₹{range_high:.2f} with volume confirmation; regime={market_regime}.",
        )
    if close < range_low:
        if market_regime == "BULLISH":
            return reject("price broke below the opening range, but the BULLISH NIFTY regime blocks a short signal.")
        return StrategySignal(
            symbol=symbol, side="SHORT", strategy="opening_range_breakdown",
            trigger=f"Enter only after a completed 5-minute close below ₹{range_low:.2f}",
            invalidation=f"Setup invalid if price closes back above ₹{range_low:.2f}",
            confidence=65 if market_regime == "BEARISH" else 55,
            reason=f"Close ₹{close:.2f} broke opening-range low ₹{range_low:.2f} with volume confirmation; regime={market_regime}.",
        )
    return reject(
        f"latest close ₹{close:.2f} remains inside the opening range "
        f"(₹{range_low:.2f}–₹{range_high:.2f})."
    )
