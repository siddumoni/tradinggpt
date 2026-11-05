"""Shared, versioned exit policy for advisory and paper research paths.

This module contains no broker/order API. OHLC exits are deterministic and
conservative: stop wins an intrabar collision, stop gaps fill at the worse open,
and targets receive no favorable gap improvement.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from datetime import time
from pathlib import Path
from typing import Literal

import yaml

from src.strategies.signal_planner import plan_atr_levels

GateTarget = Literal["target_1", "target_2"]
T1Action = Literal["none"]


@dataclass(frozen=True)
class ExitPolicy:
    version: str
    stop_atr_multiple: float
    target_1_atr_multiple: float
    t1_action: T1Action
    target_2_atr_multiple: float
    time_exit_ist: str
    gate_target: GateTarget

    def __post_init__(self) -> None:
        if not self.version.strip():
            raise ValueError("Exit policy version is required")
        for name in ("stop_atr_multiple", "target_1_atr_multiple", "target_2_atr_multiple"):
            value = getattr(self, name)
            if not isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.target_1_atr_multiple > self.target_2_atr_multiple:
            raise ValueError("Target 1 ATR multiple cannot exceed Target 2")
        if self.t1_action != "none":
            raise ValueError("Only t1_action='none' is implemented; no partial-exit behavior is implied")
        if self.gate_target not in ("target_1", "target_2"):
            raise ValueError("gate_target must be target_1 or target_2")
        try:
            parsed_time = time.fromisoformat(self.time_exit_ist)
            if parsed_time.tzinfo is not None:
                raise ValueError("time_exit_ist must be a local IST wall-clock time without an offset")
        except ValueError as exc:
            raise ValueError("time_exit_ist must be HH:MM[:SS]") from exc


@dataclass(frozen=True)
class CandleExit:
    reference_price: float
    reason: Literal["STOP_LOSS", "TARGET_2", "TIME_SQUARE_OFF"]
    detail: str


def load_exit_policy(config_path: str | Path | None = None) -> ExitPolicy:
    path = Path(config_path) if config_path else Path(__file__).resolve().parents[1] / "config" / "exit_policy.yaml"
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return ExitPolicy(
            version=str(raw["version"]),
            stop_atr_multiple=float(raw["stop_atr_multiple"]),
            target_1_atr_multiple=float(raw["target_1_atr_multiple"]),
            t1_action=str(raw["t1_action"]),  # type: ignore[arg-type]
            target_2_atr_multiple=float(raw["target_2_atr_multiple"]),
            time_exit_ist=str(raw["time_exit_ist"]),
            gate_target=str(raw["gate_target"]),  # type: ignore[arg-type]
        )
    except (OSError, KeyError, TypeError, ValueError, yaml.YAMLError) as exc:
        raise ValueError(f"Could not load shared exit policy from {path}: {exc}") from exc


def plan_policy_levels(side: str, *, entry: float, atr: float, policy: ExitPolicy | None = None):
    selected = policy or load_exit_policy()
    return plan_atr_levels(
        side,
        entry=entry,
        atr=atr,
        stop_atr_multiple=selected.stop_atr_multiple,
        target_1_atr_multiple=selected.target_1_atr_multiple,
        target_2_atr_multiple=selected.target_2_atr_multiple,
    )


def evaluate_ohlc_exit(
    *,
    side: str,
    stop_loss: float,
    target_2: float,
    open_price: float,
    high: float,
    low: float,
    close: float,
    bar_end_time: time,
    policy: ExitPolicy | None = None,
) -> CandleExit | None:
    """Evaluate one completed candle with identical conservative exit rules."""
    selected = policy or load_exit_policy()
    if side not in ("LONG", "SHORT"):
        raise ValueError("side must be LONG or SHORT")
    values = (stop_loss, target_2, open_price, high, low, close)
    if any(not isfinite(value) or value <= 0 for value in values):
        raise ValueError("OHLC and exit levels must be positive")
    if high < max(open_price, close, low) or low > min(open_price, close, high):
        raise ValueError("Invalid OHLC candle")

    stop_hit = low <= stop_loss if side == "LONG" else high >= stop_loss
    target_hit = high >= target_2 if side == "LONG" else low <= target_2
    if stop_hit:
        price = min(stop_loss, open_price) if side == "LONG" else max(stop_loss, open_price)
        return CandleExit(price, "STOP_LOSS", "Stop-first OHLC handling; adverse stop gaps use the worse bar open.")
    if target_hit:
        return CandleExit(target_2, "TARGET_2", "Target 2 touched; no favorable gap improvement.")
    if bar_end_time.replace(tzinfo=None) >= time.fromisoformat(selected.time_exit_ist):
        return CandleExit(close, "TIME_SQUARE_OFF", "Closed at the configured time-exit candle close.")
    return None
