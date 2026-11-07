"""Conservative intraday stop and execution-cost helpers."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import timedelta
from math import isfinite
import pandas as pd
from src.market_calendar import market_day_status

@dataclass(frozen=True)
class StopPlan:
    stop_loss: float
    reference: str
    distance_pct: float
    minimum_distance_applied: bool

def session_aware_atr(candles: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder ATR seeded from the prior session, without carrying overnight gaps.

    The first available session needs the configured number of bars to establish
    its seed. Once a session has a valid final ATR, the next session starts from
    that value and updates on its first completed candle. With 5-minute candles,
    ATR is therefore available at 09:20 while the first bar's true range remains
    strictly high-minus-low.
    """
    if not {"high", "low", "close"}.issubset(candles.columns):
        raise ValueError("Missing OHLC columns")
    if period < 1 or not isinstance(candles.index, pd.DatetimeIndex):
        raise ValueError("positive period and DatetimeIndex required")
    if candles.index.has_duplicates or not candles.index.is_monotonic_increasing:
        raise ValueError("timestamps must be unique and increasing")
    f = candles[["high", "low", "close"]].apply(pd.to_numeric, errors="coerce")
    if (
        f.isna().any().any()
        or not f.map(lambda x: isfinite(float(x))).all().all()
        or (f <= 0).any().any()
    ):
        raise ValueError("OHLC data must be finite and positive")

    session_keys = pd.Index(f.index.date)
    previous_close = f["close"].groupby(session_keys).shift(1)
    true_range = pd.concat(
        [
            f["high"] - f["low"],
            (f["high"] - previous_close).abs(),
            (f["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    result = pd.Series(float("nan"), index=f.index, dtype=float)
    previous_session_atr: float | None = None
    previous_session_date = None
    for session_date in session_keys.unique():
        session_status = market_day_status(session_date)["status"]
        if session_status in {
            "WEEKEND",
            "EXCHANGE_HOLIDAY",
            "SPECIAL_SESSION_NOT_SUPPORTED",
        }:
            previous_session_atr = None
            previous_session_date = session_date
            continue
        if previous_session_date is not None and previous_session_atr is not None:
            gap_day = previous_session_date + timedelta(days=1)
            while gap_day < session_date:
                status = market_day_status(gap_day)["status"]
                if status in {
                    "OPEN",
                    "CALENDAR_OUT_OF_COVERAGE",
                    "SPECIAL_SESSION_NOT_SUPPORTED",
                }:
                    previous_session_atr = None
                    break
                gap_day += timedelta(days=1)

        session_mask = session_keys == session_date
        session_true_range = true_range.loc[session_mask]
        if previous_session_atr is None:
            session_atr = session_true_range.ewm(
                alpha=1 / period, adjust=False, min_periods=period
            ).mean()
            result.loc[session_atr.index] = session_atr
        else:
            state = previous_session_atr
            values: list[float] = []
            for value in session_true_range:
                state = ((period - 1) * state + float(value)) / period
                values.append(state)
            result.loc[session_true_range.index] = values

        valid = result.loc[session_true_range.index].dropna()
        if not valid.empty:
            previous_session_atr = float(valid.iloc[-1])
        previous_session_date = session_date

    return result.rename(f"atr_session_{period}")

def build_structural_stop(*,side:str,entry:float,candles_5m:pd.DataFrame,candles_15m:pd.DataFrame,atr:float,minimum_distance_pct:float=0.25,swing_lookback:int=6)->StopPlan:
    """Choose nearest valid OR edge/VWAP/swing/15m ATR level, then enforce distance floor."""
    if side not in ("LONG","SHORT") or not all(isfinite(float(x)) for x in (entry,atr,minimum_distance_pct)) or entry<=0 or atr<=0 or minimum_distance_pct<=0: raise ValueError("Invalid side, entry, ATR, or minimum stop distance")
    if candles_5m.empty: raise ValueError("5-minute candles are required")
    latest=candles_5m.iloc[-1]; session=candles_5m.loc[candles_5m.index.date==candles_5m.index[-1].date()]
    opening=session.between_time("09:15","09:25"); candidates=[]
    if not opening.empty: candidates.append(("opening_range_edge",float(opening.low.min() if side=="LONG" else opening.high.max())))
    vwap=latest.get("vwap")
    if vwap is not None and isfinite(float(vwap)): candidates.append(("vwap",float(vwap)))
    recent=session.tail(max(2,swing_lookback))
    if len(recent)>=2: candidates.append(("recent_swing",float(recent.low.min() if side=="LONG" else recent.high.max())))
    atr15=float(candles_15m.iloc[-1].get("atr_session_14",float("nan"))) if not candles_15m.empty else float("nan")
    a=atr15 if isfinite(atr15) and atr15>0 else atr
    candidates.append(("session_atr",entry-a if side=="LONG" else entry+a))
    valid=[x for x in candidates if x[1]>0 and (x[1]<entry if side=="LONG" else x[1]>entry)]
    if not valid: raise ValueError("No valid structural stop exists on the adverse side of entry")
    name,structural=max(valid,key=lambda x:x[1]) if side=="LONG" else min(valid,key=lambda x:x[1])
    minimum=entry*minimum_distance_pct/100; close=(entry-structural)<minimum if side=="LONG" else (structural-entry)<minimum
    stop=min(structural,entry-minimum) if side=="LONG" else max(structural,entry+minimum)
    return StopPlan(round(stop,4),name if not close else name+"_minimum_distance_floor",abs(entry-stop)/entry*100,close)

def spread_slippage_bps_per_side(quote:dict|None,*,floor_bps_per_side:float=5.0)->tuple[float,str]:
    """Half quoted spread per side, never below configured 5 bps baseline."""
    if not isfinite(float(floor_bps_per_side)) or floor_bps_per_side<5: raise ValueError("slippage floor cannot be below 5 bps per side")
    if not isinstance(quote,dict): return float(floor_bps_per_side),"QUOTE_UNAVAILABLE_FLOOR_APPLIED"
    try: bid=float(quote["depth"]["buy"][0]["price"]); ask=float(quote["depth"]["sell"][0]["price"])
    except (KeyError,IndexError,TypeError,ValueError): return float(floor_bps_per_side),"QUOTE_DEPTH_UNAVAILABLE_FLOOR_APPLIED"
    if not isfinite(bid) or not isfinite(ask) or bid<=0 or ask<bid: return float(floor_bps_per_side),"QUOTE_DEPTH_INVALID_FLOOR_APPLIED"
    mid=(bid+ask)/2
    return max(float(floor_bps_per_side),(ask-bid)/2/mid*10000),"LIVE_SPREAD_PLUS_FLOOR"
