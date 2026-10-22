"""Append completed 5-minute candles to a provider-specific paper-research archive."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import pandas as pd

from src.market_scanner import candles_to_frame, completed_candles

IST = ZoneInfo("Asia/Kolkata")
OHLCV = ("open", "high", "low", "close", "volume")
SESSION_START = dtime(9, 15)
LAST_BAR_START = dtime(15, 10)


@dataclass(frozen=True)
class CandleArchiveResult:
    symbol: str
    path: str
    provider_mode: str
    rows_observed: int
    rows_written: int
    total_rows: int


def append_completed_candles(
    symbol: str,
    candles: Iterable[dict[str, Any]] | pd.DataFrame | None,
    archive_dir: Path,
    *,
    now: datetime,
    provider_mode: str,
) -> CandleArchiveResult:
    """Upsert only completed regular-session candles and keep one provider per file.

    The archive is intentionally provider-pure. Switching between Yahoo and Kite
    for a symbol raises an error rather than silently mixing price histories.
    Existing rows at a repeated timestamp are refreshed from the newest pull.
    """
    cleaned_symbol = str(symbol).strip().upper()
    mode = str(provider_mode).strip().lower()
    if not cleaned_symbol or not cleaned_symbol.replace("-", "").isalnum():
        raise ValueError("symbol must be a non-empty NSE-style ticker")
    if mode not in {"yahoo", "kite"}:
        raise ValueError("provider_mode must be 'yahoo' or 'kite'")
    if not isinstance(now, datetime):
        raise ValueError("now must be a datetime")
    current = now.replace(tzinfo=IST) if now.tzinfo is None else now.astimezone(IST)

    raw = candles_to_frame(candles)
    complete = completed_candles(raw, 5, current)
    if not complete.empty:
        complete = complete.loc[
            [
                SESSION_START <= stamp.time().replace(tzinfo=None) <= LAST_BAR_START
                for stamp in complete.index
            ]
        ]
    path = Path(archive_dir) / f"{cleaned_symbol}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists():
        try:
            raw_existing = pd.read_csv(path)
        except (OSError, pd.errors.ParserError) as exc:
            raise ValueError(f"Could not read existing candle archive {path}: {exc}") from exc
        if "provider_mode" not in raw_existing.columns:
            raise ValueError(
                f"Existing candle file {path} has no provider_mode metadata; move it out of the archive "
                "or place it in a separate, explicitly managed input directory."
            )
        existing_modes = {
            str(value).strip().lower()
            for value in raw_existing["provider_mode"].dropna().unique()
        }
        if existing_modes and existing_modes != {mode}:
            raise ValueError(
                f"Provider mismatch for {cleaned_symbol}: archive has {sorted(existing_modes)}, "
                f"new scan uses {mode}. Keep separate archives for each provider."
            )
        existing = candles_to_frame(raw_existing)
    else:
        existing = pd.DataFrame(columns=list(OHLCV))
        existing.index = pd.DatetimeIndex([], tz=IST, name="timestamp")

    rows_observed = int(len(complete))
    if rows_observed:
        if existing.empty:
            merged = complete.copy()
        else:
            merged = pd.concat([existing, complete], axis=0).sort_index()
        merged = merged.loc[~merged.index.duplicated(keep="last")].sort_index()
    else:
        merged = existing.copy()

    merged.index.name = "date"
    save = merged.reset_index()
    save["provider_mode"] = mode
    save.to_csv(path, index=False, date_format="%Y-%m-%dT%H:%M:%S%z")
    return CandleArchiveResult(
        symbol=cleaned_symbol,
        path=str(path),
        provider_mode=mode,
        rows_observed=rows_observed,
        rows_written=rows_observed,
        total_rows=int(len(merged)),
    )
