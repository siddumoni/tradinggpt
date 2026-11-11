"""Research-only intraday Volume Profile calculations.

Candle inputs are distributed uniformly over the price levels touched by each
OHLCV bar. This is an explicit estimate, not exact traded volume at price.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
import json
import math
from pathlib import Path
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
_MAX_LEVELS_PER_CANDLE = 20_000


class VolumeProfileError(ValueError):
    """Raised when input data cannot produce a defensible profile."""


def _timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value or "").strip()
        if not raw:
            raise VolumeProfileError("Every candle must include a timestamp")
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise VolumeProfileError(f"Invalid candle timestamp: {raw}") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=IST)
    return parsed.astimezone(IST)


def _positive_tick_size(value: float) -> Decimal:
    try:
        tick = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise VolumeProfileError("tick_size must be a positive finite number") from exc
    if not tick.is_finite() or tick <= 0:
        raise VolumeProfileError("tick_size must be a positive finite number")
    return tick


def _price_level(price: float, tick_size: Decimal) -> int:
    try:
        value = Decimal(str(price))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise VolumeProfileError("Prices must be finite numbers") from exc
    if not value.is_finite() or value <= 0:
        raise VolumeProfileError("Prices must be positive finite numbers")
    return int((value / tick_size).to_integral_value(rounding=ROUND_FLOOR))


def _price_from_level(level: int, tick_size: Decimal) -> float:
    return float(Decimal(level) * tick_size)


def _aligned_price_level(price: float, tick_size: Decimal) -> int:
    level = _price_level(price, tick_size)
    aligned_price = _price_from_level(level, tick_size)
    if not math.isclose(price, aligned_price, rel_tol=0.0, abs_tol=float(tick_size) * 1e-6):
        raise VolumeProfileError(
            f"OHLC price {price} is not aligned to instrument tick size {tick_size}"
        )
    return level


def _summarize(
    volumes: Mapping[int, float],
    *,
    tick_size: Decimal,
    session_date: str,
    method: str,
    exact_volume_at_price: bool,
    source_rows: int,
    value_area_pct: float,
    symbol: str | None,
    bars_excluded_incomplete: int = 0,
) -> dict[str, Any]:
    if not 0 < value_area_pct <= 1:
        raise VolumeProfileError("value_area_pct must be greater than 0 and at most 1")
    if not volumes:
        raise VolumeProfileError("No usable price-volume observations were supplied")

    first_level = min(volumes)
    last_level = max(volumes)
    dense = [float(volumes.get(level, 0.0)) for level in range(first_level, last_level + 1)]
    if not all(math.isfinite(value) and value >= 0 for value in dense):
        raise VolumeProfileError("Allocated volume must be finite and non-negative")
    total_volume = float(sum(dense))
    bins = [
        {
            "price": _price_from_level(first_level + offset, tick_size),
            "volume": volume,
            "volume_pct": (volume / total_volume * 100.0) if total_volume > 0 else 0.0,
        }
        for offset, volume in enumerate(dense)
    ]

    poc_offset: int | None = None
    value_low_offset: int | None = None
    value_high_offset: int | None = None
    if total_volume > 0:
        # bins are ascending; max() keeps the lower price on an exact tie.
        poc_offset = max(range(len(dense)), key=lambda offset: dense[offset])
        value_low_offset = poc_offset
        value_high_offset = poc_offset
        accumulated = dense[poc_offset]
        target = total_volume * value_area_pct
        while accumulated + 1e-12 < target:
            lower = value_low_offset - 1 if value_low_offset > 0 else None
            upper = value_high_offset + 1 if value_high_offset + 1 < len(dense) else None
            if lower is None and upper is None:
                break
            if lower is None:
                chosen = upper
            elif upper is None:
                chosen = lower
            elif dense[lower] >= dense[upper]:
                # Deterministic tie break: expand to the lower-price bin.
                chosen = lower
            else:
                chosen = upper
            assert chosen is not None
            accumulated += dense[chosen]
            value_low_offset = min(value_low_offset, chosen)
            value_high_offset = max(value_high_offset, chosen)

    for offset, item in enumerate(bins):
        item["inside_value_area"] = (
            value_low_offset is not None
            and value_high_offset is not None
            and value_low_offset <= offset <= value_high_offset
        )

    high_nodes: list[float] = []
    low_nodes: list[float] = []
    for offset in range(1, len(dense) - 1):
        left, current, right = dense[offset - 1], dense[offset], dense[offset + 1]
        if current > 0 and current >= left and current >= right:
            if current >= 1.5 * max(left, right, 1e-12):
                high_nodes.append(_price_from_level(first_level + offset, tick_size))
        if current >= 0 and current <= left and current <= right:
            if current <= 0.5 * min(left, right):
                low_nodes.append(_price_from_level(first_level + offset, tick_size))

    result: dict[str, Any] = {
        "session_date": session_date,
        "method": method,
        "exact_volume_at_price": exact_volume_at_price,
        "approximate": not exact_volume_at_price,
        "tick_size": float(tick_size),
        "value_area_pct": value_area_pct,
        "source_rows": source_rows,
        "bars_excluded_incomplete": bars_excluded_incomplete,
        "bin_count": len(bins),
        "total_volume": total_volume,
        "poc": None,
        "value_area": None,
        "high_volume_nodes": high_nodes,
        "low_volume_nodes": low_nodes,
        "bins": bins,
        "limitations": (
            "OHLCV candles do not reveal the actual traded volume at each price. "
            "Candle volume is split equally across every tick level from low to high; "
            "POC, VAH, VAL, HVNs and LVNs are therefore estimates, not exchange tape facts."
            if not exact_volume_at_price
            else (
                "The profile is exact only if the supplied trade-print feed is complete, "
                "correctly timestamped and contains every execution for this session."
            )
        ),
    }
    if symbol:
        result["symbol"] = symbol.upper()
    if poc_offset is not None and value_low_offset is not None and value_high_offset is not None:
        result["poc"] = {
            "price": _price_from_level(first_level + poc_offset, tick_size),
            "volume": dense[poc_offset],
        }
        result["value_area"] = {
            "low": _price_from_level(first_level + value_low_offset, tick_size),
            "high": _price_from_level(first_level + value_high_offset, tick_size),
            "volume": float(sum(dense[value_low_offset : value_high_offset + 1])),
            "coverage_pct": (
                sum(dense[value_low_offset : value_high_offset + 1]) / total_volume * 100.0
            ),
        }
    return result


def build_volume_profile(
    candles: Iterable[Mapping[str, Any]],
    *,
    tick_size: float,
    value_area_pct: float = 0.70,
    as_of: datetime | None = None,
    bar_minutes: int = 5,
    symbol: str | None = None,
) -> dict[str, Any]:
    """Build an estimated session profile from completed OHLCV candles.

    Each candle's volume is split evenly over the tick levels touched between
    low and high, inclusive. This avoids pretending the typical price was the
    only traded price, but the uniform allocation remains an approximation.
    """
    tick = _positive_tick_size(tick_size)
    if bar_minutes < 1:
        raise VolumeProfileError("bar_minutes must be positive")
    if not 0 < value_area_pct <= 1:
        raise VolumeProfileError("value_area_pct must be greater than 0 and at most 1")
    cutoff = _timestamp(as_of) if as_of is not None else None

    rows = list(candles)
    if not rows:
        raise VolumeProfileError("At least one candle is required")

    parsed_rows: list[tuple[datetime, float, float, float, float, float]] = []
    seen_timestamps: set[datetime] = set()
    session_dates: set[str] = set()
    excluded = 0
    for row in rows:
        stamp_value = row.get("date", row.get("timestamp", row.get("time")))
        stamp = _timestamp(stamp_value)
        if stamp in seen_timestamps:
            raise VolumeProfileError(f"Duplicate candle timestamp: {stamp.isoformat()}")
        seen_timestamps.add(stamp)
        session_dates.add(stamp.date().isoformat())
        try:
            open_price = float(row["open"])
            high = float(row["high"])
            low = float(row["low"])
            close = float(row["close"])
            volume = float(row["volume"])
        except (KeyError, TypeError, ValueError) as exc:
            raise VolumeProfileError("Candles must contain numeric OHLCV fields") from exc
        values = (open_price, high, low, close, volume)
        if not all(math.isfinite(value) for value in values):
            raise VolumeProfileError("Candle OHLCV values must be finite")
        if min(open_price, high, low, close) <= 0 or volume < 0:
            raise VolumeProfileError("OHLC prices must be positive and volume non-negative")
        if high < max(open_price, close, low) or low > min(open_price, close, high):
            raise VolumeProfileError("Invalid OHLC range: high/low must contain open and close")
        session_minute = stamp.hour * 60 + stamp.minute
        offset_from_open = session_minute - (9 * 60 + 15)
        if (
            offset_from_open < 0
            or offset_from_open + bar_minutes > 15 * 60 + 30 - (9 * 60 + 15)
            or offset_from_open % bar_minutes != 0
            or stamp.second != 0
            or stamp.microsecond != 0
        ):
            raise VolumeProfileError(
                "Candle timestamps must be aligned to the regular NSE session grid starting 09:15 IST"
            )
        for price in (open_price, high, low, close):
            _aligned_price_level(price, tick)
        if cutoff is not None and stamp.timestamp() + bar_minutes * 60 > cutoff.timestamp():
            excluded += 1
            continue
        parsed_rows.append((stamp, open_price, high, low, close, volume))

    if len(session_dates) != 1:
        raise VolumeProfileError("Build one profile per regular trading session; split dates first")
    if not parsed_rows:
        raise VolumeProfileError("No completed candles remain at the supplied as_of time")

    parsed_rows.sort(key=lambda item: item[0])
    volumes: dict[int, float] = {}
    for _, _, high, low, _, volume in parsed_rows:
        low_level = _aligned_price_level(low, tick)
        high_level = _aligned_price_level(high, tick)
        level_count = high_level - low_level + 1
        if level_count < 1 or level_count > _MAX_LEVELS_PER_CANDLE:
            raise VolumeProfileError(
                f"Candle range spans {level_count} price levels; refusing an unsafe allocation"
            )
        per_level = volume / level_count
        for level in range(low_level, high_level + 1):
            volumes[level] = volumes.get(level, 0.0) + per_level

    return _summarize(
        volumes,
        tick_size=tick,
        session_date=next(iter(session_dates)),
        method="UNIFORM_OHLC_RANGE_ESTIMATE",
        exact_volume_at_price=False,
        source_rows=len(parsed_rows),
        value_area_pct=value_area_pct,
        symbol=symbol,
        bars_excluded_incomplete=excluded,
    )


def build_trade_volume_profile(
    trades: Iterable[Mapping[str, Any]],
    *,
    tick_size: float,
    value_area_pct: float = 0.70,
    complete_trade_feed_verified: bool = False,
    symbol: str | None = None,
) -> dict[str, Any]:
    """Bucket trade prints by price; exactness requires caller-verified completeness."""
    tick = _positive_tick_size(tick_size)
    if not 0 < value_area_pct <= 1:
        raise VolumeProfileError("value_area_pct must be greater than 0 and at most 1")
    rows = list(trades)
    if not rows:
        raise VolumeProfileError("At least one trade print is required")

    volumes: dict[int, float] = {}
    session_dates: set[str] = set()
    for row in rows:
        try:
            price = float(row["price"])
            quantity = float(row.get("quantity", row.get("volume")))
        except (KeyError, TypeError, ValueError) as exc:
            raise VolumeProfileError("Trade prints require numeric price and quantity") from exc
        if not math.isfinite(price) or price <= 0:
            raise VolumeProfileError("Trade prices must be positive and finite")
        if not math.isfinite(quantity) or quantity < 0:
            raise VolumeProfileError("Trade quantities must be finite and non-negative")
        level = _aligned_price_level(price, tick)
        stamp_value = row.get("date", row.get("timestamp", row.get("time")))
        if stamp_value is not None:
            session_dates.add(_timestamp(stamp_value).date().isoformat())
        volumes[level] = volumes.get(level, 0.0) + quantity

    if len(session_dates) > 1:
        raise VolumeProfileError("Build one trade profile per session; split dates first")
    quality = bool(complete_trade_feed_verified)
    return _summarize(
        volumes,
        tick_size=tick,
        session_date=next(iter(session_dates)) if session_dates else "UNSPECIFIED",
        method="TRADE_PRINTS_AT_PRICE",
        exact_volume_at_price=quality,
        source_rows=len(rows),
        value_area_pct=value_area_pct,
        symbol=symbol,
    )


def _main() -> int:
    parser = argparse.ArgumentParser(
        description="Create a research-only Volume Profile from one session of OHLCV candles."
    )
    parser.add_argument("--candles", required=True, type=Path, help="CSV with date/open/high/low/close/volume")
    parser.add_argument("--tick-size", required=True, type=float, help="Instrument tick size in INR")
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--as-of", default=None, help="Optional ISO timestamp; excludes unfinished bars")
    parser.add_argument("--bar-minutes", type=int, default=5)
    parser.add_argument("--value-area-pct", type=float, default=0.70)
    parser.add_argument("--output", type=Path, default=None, help="Optional JSON output path")
    args = parser.parse_args()
    try:
        with args.candles.open("r", encoding="utf-8", newline="") as handle:
            candles = list(csv.DictReader(handle))
        as_of = _timestamp(args.as_of) if args.as_of else None
        profile = build_volume_profile(
            candles,
            tick_size=args.tick_size,
            value_area_pct=args.value_area_pct,
            as_of=as_of,
            bar_minutes=args.bar_minutes,
            symbol=args.symbol,
        )
        rendered = json.dumps(profile, indent=2, ensure_ascii=False)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered + "\n", encoding="utf-8")
        else:
            print(rendered)
    except (OSError, VolumeProfileError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
