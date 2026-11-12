"""Point-in-time Volume Profile + estimated Order Flow shadow strategies.

Signals use only completed 5-minute candles and snapshots from the same completed
bar. OHLCV Volume Profile and Kite aggressor-side/book metrics are estimates.
Outputs are routed only to the independent challenger shadow ledger.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import pandas as pd
import yaml

from src.exit_policy import load_exit_policy, plan_policy_levels
from src.market_scanner import add_scanner_indicators
from src.order_flow import OrderFlowError, analyze_order_flow
from src.volume_profile import VolumeProfileError, build_volume_profile

IST = ZoneInfo("Asia/Kolkata")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "microstructure_strategies.yaml"
DEFAULT_TICK_SIZE_FILE = PROJECT_ROOT / ".local" / "nse_instruments.json"

DEFINITIONS = {
    "micro_vp_value_rejection_ofi_v1": "Volume Profile value-edge rejection with estimated Order Flow confirmation",
    "micro_vp_value_acceptance_breakout_v1": "Volume Profile value-area acceptance breakout with estimated Order Flow confirmation",
    "micro_orderflow_price_continuation_v1": "Estimated Order Flow delta with candle, VWAP, EMA and relative-volume confirmation",
}


@dataclass(frozen=True)
class MicrostructureSettings:
    enabled: bool = True
    minimum_profile_bars: int = 12
    minimum_bucket_snapshots: int = 15
    minimum_trade_updates: int = 5
    value_area_pct: float = 0.70
    value_rejection_delta_pct: float = 8.0
    breakout_delta_pct: float = 12.0
    continuation_delta_pct: float = 15.0
    minimum_relative_volume: float = 1.20
    minimum_body_atr: float = 0.35
    value_edge_buffer_atr: float = 0.15
    breakout_buffer_atr: float = 0.10
    stop_buffer_atr: float = 0.15
    minimum_book_imbalance: float = 0.03


def load_microstructure_settings(path: Path | None = None) -> MicrostructureSettings:
    """Load tunable research thresholds; invalid settings fail closed."""
    selected = Path(path) if path else DEFAULT_CONFIG
    try:
        raw = yaml.safe_load(selected.read_text(encoding="utf-8")) or {}
    except FileNotFoundError as exc:
        if path is not None:
            raise ValueError(f"Microstructure config file does not exist: {selected}") from exc
        return MicrostructureSettings()
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"Could not load microstructure strategy settings: {exc}") from exc
    block = raw.get("microstructure", raw)
    if not isinstance(block, Mapping):
        raise ValueError("microstructure config must be a YAML mapping")
    defaults = MicrostructureSettings()
    values: dict[str, Any] = {}
    for name in defaults.__dataclass_fields__:
        value = block.get(name, getattr(defaults, name))
        if name == "enabled":
            values[name] = bool(value)
        elif name in {"minimum_profile_bars", "minimum_bucket_snapshots", "minimum_trade_updates"}:
            values[name] = int(value)
        else:
            values[name] = float(value)
    settings = MicrostructureSettings(**values)
    for name in (
        "minimum_profile_bars",
        "minimum_bucket_snapshots",
        "minimum_trade_updates",
    ):
        if getattr(settings, name) < 1:
            raise ValueError(f"{name} must be positive")
    for name in (
        "value_area_pct",
        "value_rejection_delta_pct",
        "breakout_delta_pct",
        "continuation_delta_pct",
        "minimum_relative_volume",
        "minimum_body_atr",
        "value_edge_buffer_atr",
        "breakout_buffer_atr",
        "stop_buffer_atr",
        "minimum_book_imbalance",
    ):
        value = float(getattr(settings, name))
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"{name} must be finite and non-negative")
    if not 0 < settings.value_area_pct <= 1:
        raise ValueError("value_area_pct must be in (0, 1]")
    return settings


def load_tick_size_lookup(path: Path | None = None) -> dict[str, float]:
    """Read tick sizes from the cached Kite instrument master."""
    selected = Path(path) if path else DEFAULT_TICK_SIZE_FILE
    try:
        payload = json.loads(selected.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    rows = payload.get("instruments") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return {}
    result: dict[str, float] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("tradingsymbol") or "").strip().upper()
        try:
            tick = float(row.get("tick_size"))
        except (TypeError, ValueError, OverflowError):
            continue
        if symbol and math.isfinite(tick) and tick > 0:
            result[symbol] = tick
    return result


def _stamp(value: Any) -> datetime:
    parsed = value.to_pydatetime() if hasattr(value, "to_pydatetime") else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=IST) if parsed.tzinfo is None else parsed.astimezone(IST)


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _signal_id(version: str, symbol: str, side: str, stamp: str) -> str:
    raw = "|".join((version, symbol.upper(), side.upper(), stamp))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _profile_rows(frame: pd.DataFrame) -> list[dict[str, Any]]:
    return [
        {
            "date": stamp.isoformat(),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row["volume"]),
        }
        for stamp, row in frame.iterrows()
    ]


def _flow_metrics(
    events: list[Mapping[str, Any]],
    *,
    symbol: str,
    bar_start: datetime,
    bar_end: datetime,
    settings: MicrostructureSettings,
) -> dict[str, Any] | None:
    """Require adequate same-bar observations; never borrow later-session flow."""
    bucket: list[Mapping[str, Any]] = []
    for event in events:
        raw_stamp = event.get("timestamp") or event.get("exchange_timestamp")
        try:
            stamp = _stamp(raw_stamp)
        except (TypeError, ValueError, OverflowError):
            continue
        event_symbol = str(event.get("symbol") or "").strip().upper()
        if event_symbol == symbol.upper() and bar_start <= stamp < bar_end:
            bucket.append(event)
    if len(bucket) < settings.minimum_bucket_snapshots:
        return None
    try:
        result = analyze_order_flow(bucket, depth_levels=5)
    except (OrderFlowError, ValueError):
        return None
    if (
        int(result.get("unique_trade_updates") or 0) < settings.minimum_trade_updates
        or result.get("estimated_delta_pct_of_classified_volume") is None
        or not math.isfinite(float(result.get("estimated_delta_pct_of_classified_volume")))
    ):
        return None
    return result


def generate_microstructure_signals(
    symbol: str,
    candles: pd.DataFrame,
    events: list[Mapping[str, Any]],
    *,
    tick_size: float,
    settings: MicrostructureSettings | None = None,
) -> list[dict[str, Any]]:
    """Create point-in-time shadow candidates from the latest completed Kite bar.

    The profile is built only from bars preceding the signal bar. Order-flow
    snapshots are restricted to the signal bar's own five-minute interval.
    """
    cfg = settings or load_microstructure_settings()
    if not cfg.enabled or candles.empty or len(candles) < 35:
        return []
    if not math.isfinite(float(tick_size)) or tick_size <= 0:
        return []
    try:
        frame = add_scanner_indicators(candles)
    except (ValueError, KeyError, TypeError):
        return []
    if len(frame) < 35:
        return []

    current_stamp = _stamp(frame.index[-1])
    if current_stamp.hour < 9 or (current_stamp.hour == 9 and current_stamp.minute < 15):
        return []
    if current_stamp.hour > 15 or (current_stamp.hour == 15 and current_stamp.minute > 25):
        return []

    session = frame.loc[frame.index.date == current_stamp.date()]
    if len(session) < cfg.minimum_profile_bars + 1:
        return []
    prior_session = session.iloc[:-1]
    if len(prior_session) < cfg.minimum_profile_bars:
        return []

    current = frame.iloc[-1]
    previous = frame.iloc[-2]
    needed = ("open", "high", "low", "close", "volume", "atr_14", "vwap", "ema_9", "ema_21")
    if any(name not in frame.columns for name in needed):
        return []
    try:
        values = {name: float(current[name]) for name in needed}
        prev_close = float(previous["close"])
        prior_volumes = pd.to_numeric(prior_session["volume"], errors="coerce").dropna()
    except (TypeError, ValueError, KeyError):
        return []
    if not all(math.isfinite(value) for value in values.values()) or values["atr_14"] <= 0:
        return []
    if not math.isfinite(prev_close) or prior_volumes.empty:
        return []

    median_volume = float(prior_volumes.tail(20).median())
    if not math.isfinite(median_volume) or median_volume <= 0:
        return []
    relative_volume = values["volume"] / median_volume
    candle_range = max(values["high"] - values["low"], float(tick_size))
    close_location = (values["close"] - values["low"]) / candle_range
    close_location = min(1.0, max(0.0, close_location))
    body_atr = abs(values["close"] - values["open"]) / values["atr_14"]

    bar_end = current_stamp + timedelta(minutes=5)
    flow = _flow_metrics(
        events,
        symbol=symbol,
        bar_start=current_stamp,
        bar_end=bar_end,
        settings=cfg,
    )
    if flow is None:
        return []
    delta_pct = float(flow["estimated_delta_pct_of_classified_volume"])
    depth_imbalance = _finite(flow.get("mean_displayed_depth_imbalance"))
    mean_ofi = _finite(flow.get("mean_snapshot_ofi"))
    long_book_votes = (
        (depth_imbalance is not None and depth_imbalance >= cfg.minimum_book_imbalance),
        (mean_ofi is not None and mean_ofi > 0),
    )
    short_book_votes = (
        (depth_imbalance is not None and depth_imbalance <= -cfg.minimum_book_imbalance),
        (mean_ofi is not None and mean_ofi < 0),
    )
    has_book_vote = any(value is not None for value in (depth_imbalance, mean_ofi))
    book_long = has_book_vote and any(long_book_votes) and not any(short_book_votes)
    book_short = has_book_vote and any(short_book_votes) and not any(long_book_votes)
    config_hash = hashlib.sha256(
        json.dumps(asdict(cfg), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    try:
        profile = build_volume_profile(
            _profile_rows(prior_session),
            tick_size=tick_size,
            value_area_pct=cfg.value_area_pct,
            as_of=current_stamp,
            bar_minutes=5,
            symbol=symbol,
        )
    except (VolumeProfileError, ValueError, TypeError):
        return []
    area = profile.get("value_area")
    poc_data = profile.get("poc")
    if not isinstance(area, dict) or not isinstance(poc_data, dict):
        return []
    val = _finite(area.get("low"))
    vah = _finite(area.get("high"))
    poc = _finite(poc_data.get("price"))
    if val is None or vah is None or poc is None or not val < poc < vah:
        return []

    atr = values["atr_14"]
    edge_buffer = max(float(tick_size) * 2, atr * cfg.value_edge_buffer_atr)
    breakout_buffer = max(float(tick_size) * 2, atr * cfg.breakout_buffer_atr)
    stop_buffer = max(float(tick_size) * 2, atr * cfg.stop_buffer_atr)
    long_setups: list[tuple[str, str, float, float, float]] = []
    short_setups: list[tuple[str, str, float, float, float]] = []

    # Value-edge rejection: test VAL/VAH, close back into value, and require
    # same-bar estimated trade delta plus at least one book-pressure vote.
    if (
        values["low"] <= val + edge_buffer
        and values["close"] >= val + 0.05 * atr
        and values["close"] > values["open"]
        and close_location >= 0.65
        and delta_pct >= cfg.value_rejection_delta_pct
        and book_long
        and relative_volume >= 0.90
        and values["close"] < poc < vah
    ):
        stop = min(values["low"], val) - stop_buffer
        long_setups.append((
            "micro_vp_value_rejection_ofi_v1",
            f"VAL rejection: prior-only estimated VAL={val:.4f}, POC={poc:.4f}, VAH={vah:.4f}; same-bar estimated delta={delta_pct:.1f}%, depth imbalance={depth_imbalance}, volume ratio={relative_volume:.2f}x.",
            stop, max(poc, values["close"] + 0.50 * atr), max(vah, values["close"] + 2.00 * atr),
        ))
    if (
        values["high"] >= vah - edge_buffer
        and values["close"] <= vah - 0.05 * atr
        and values["close"] < values["open"]
        and close_location <= 0.35
        and delta_pct <= -cfg.value_rejection_delta_pct
        and book_short
        and relative_volume >= 0.90
        and val < poc < values["close"]
    ):
        stop = max(values["high"], vah) + stop_buffer
        short_setups.append((
            "micro_vp_value_rejection_ofi_v1",
            f"VAH rejection: prior-only estimated VAL={val:.4f}, POC={poc:.4f}, VAH={vah:.4f}; same-bar estimated delta={delta_pct:.1f}%, depth imbalance={depth_imbalance}, volume ratio={relative_volume:.2f}x.",
            stop, min(poc, values["close"] - 0.50 * atr), min(val, values["close"] - 2.00 * atr),
        ))

    # Acceptance breakout: require a close beyond the prior profile, a strong
    # candle, relative volume, and order-flow/book confirmation.
    if (
        prev_close <= vah
        and values["close"] > vah + breakout_buffer
        and values["close"] > values["open"]
        and close_location >= 0.75
        and body_atr >= cfg.minimum_body_atr
        and relative_volume >= cfg.minimum_relative_volume
        and delta_pct >= cfg.breakout_delta_pct
        and book_long
    ):
        try:
            levels = plan_policy_levels("LONG", entry=values["close"], atr=atr, policy=load_exit_policy())
            long_setups.append((
                "micro_vp_value_acceptance_breakout_v1",
                f"Accepted above prior-only estimated VAH={vah:.4f}; same-bar estimated delta={delta_pct:.1f}%, volume ratio={relative_volume:.2f}x, depth imbalance={depth_imbalance}.",
                float(levels.stop_loss), float(levels.target_1), float(levels.target_2),
            ))
        except (ValueError, TypeError):
            pass
    if (
        prev_close >= val
        and values["close"] < val - breakout_buffer
        and values["close"] < values["open"]
        and close_location <= 0.25
        and body_atr >= cfg.minimum_body_atr
        and relative_volume >= cfg.minimum_relative_volume
        and delta_pct <= -cfg.breakout_delta_pct
        and book_short
    ):
        try:
            levels = plan_policy_levels("SHORT", entry=values["close"], atr=atr, policy=load_exit_policy())
            short_setups.append((
                "micro_vp_value_acceptance_breakout_v1",
                f"Accepted below prior-only estimated VAL={val:.4f}; same-bar estimated delta={delta_pct:.1f}%, volume ratio={relative_volume:.2f}x, depth imbalance={depth_imbalance}.",
                float(levels.stop_loss), float(levels.target_1), float(levels.target_2),
            ))
        except (ValueError, TypeError):
            pass

    # Delta continuation is not a delta-only signal: price structure, VWAP,
    # EMA alignment, candle strength and relative volume must agree as well.
    if (
        values["close"] > values["open"]
        and values["close"] > values["vwap"]
        and values["ema_9"] > values["ema_21"]
        and close_location >= 0.75
        and body_atr >= cfg.minimum_body_atr
        and relative_volume >= cfg.minimum_relative_volume
        and delta_pct >= cfg.continuation_delta_pct
    ):
        try:
            levels = plan_policy_levels("LONG", entry=values["close"], atr=atr, policy=load_exit_policy())
            long_setups.append((
                "micro_orderflow_price_continuation_v1",
                f"Price/EMA/VWAP bullish with same-bar estimated delta={delta_pct:.1f}% and volume ratio={relative_volume:.2f}x; OFI is snapshot-estimated.",
                float(levels.stop_loss), float(levels.target_1), float(levels.target_2),
            ))
        except (ValueError, TypeError):
            pass
    if (
        values["close"] < values["open"]
        and values["close"] < values["vwap"]
        and values["ema_9"] < values["ema_21"]
        and close_location <= 0.25
        and body_atr >= cfg.minimum_body_atr
        and relative_volume >= cfg.minimum_relative_volume
        and delta_pct <= -cfg.continuation_delta_pct
    ):
        try:
            levels = plan_policy_levels("SHORT", entry=values["close"], atr=atr, policy=load_exit_policy())
            short_setups.append((
                "micro_orderflow_price_continuation_v1",
                f"Price/EMA/VWAP bearish with same-bar estimated delta={delta_pct:.1f}% and volume ratio={relative_volume:.2f}x; OFI is snapshot-estimated.",
                float(levels.stop_loss), float(levels.target_1), float(levels.target_2),
            ))
        except (ValueError, TypeError):
            pass

    stamp = current_stamp.isoformat()
    output: list[dict[str, Any]] = []
    for side, setups in (("LONG", long_setups), ("SHORT", short_setups)):
        for version, reason, stop, target_1, target_2 in setups:
            entry = values["close"]
            if side == "LONG" and not stop < entry < target_1 <= target_2:
                continue
            if side == "SHORT" and not target_2 <= target_1 < entry < stop:
                continue
            output.append({
                "signal_id": _signal_id(version, symbol, side, stamp),
                "strategy_version": version,
                "strategy_name": DEFINITIONS[version],
                "symbol": symbol.upper(),
                "side": side,
                "signal_time": stamp,
                "signal_close": entry,
                "stop_loss": float(stop),
                "target_1": float(target_1),
                "target_2": float(target_2),
                "reason": (
                    reason
                    + " Profile method=UNIFORM_OHLC_RANGE_ESTIMATE (approximate, not exact volume-at-price). "
                    + f"Order-flow source={flow.get('data_quality')}; snapshots={flow.get('snapshot_count')}, "
                    + f"unique trade updates={flow.get('unique_trade_updates')}; no guaranteed full exchange tape; "
                    + f"config_sha256={config_hash}."
                ),
            })
    return output
