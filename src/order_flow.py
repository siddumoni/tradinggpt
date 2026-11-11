"""Research-only Kite WebSocket order-flow snapshot capture and analysis.

Kite WebSocket updates are snapshots, not a guaranteed exchange-by-exchange trade
tape. Aggressor-side volume and depth pressure from this module are estimates.
"""
from __future__ import annotations

import argparse
import gzip
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
import json
import logging
import math
from pathlib import Path
import queue
import threading
import time
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

import yaml

LOGGER = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "order_flow.yaml"


class OrderFlowError(ValueError):
    """Raised when order-flow input cannot be analysed safely."""


@dataclass(frozen=True)
class OrderFlowCaptureSettings:
    enabled: bool
    directory: Path
    queue_size: int = 50_000
    batch_size: int = 200
    flush_interval_seconds: float = 1.0


def _parse_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=IST)
    return parsed.astimezone(IST)


def _iso_datetime(value: Any) -> str | None:
    parsed = _parse_datetime(value)
    return parsed.isoformat() if parsed is not None else None


def _finite_float(value: Any, default: float | None = None) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return result if math.isfinite(result) else default


def _normalise_depth(levels: Any) -> list[dict[str, float | int]]:
    if not isinstance(levels, list):
        return []
    normalized: list[dict[str, float | int]] = []
    for level in levels[:5]:
        if not isinstance(level, Mapping):
            continue
        price = _finite_float(level.get("price"))
        quantity = _finite_float(level.get("quantity"))
        orders = _finite_float(level.get("orders"), 0.0)
        if price is None or quantity is None or price <= 0 or quantity < 0:
            continue
        normalized.append(
            {
                "price": price,
                "quantity": quantity,
                "orders": max(0, int(orders or 0)),
            }
        )
    return normalized


def normalise_kite_tick(
    symbol: str,
    tick: Mapping[str, Any],
    *,
    received_at: datetime | None = None,
) -> dict[str, Any] | None:
    """Convert one Kite FULL-mode tick into a compact, stable JSON-safe record."""
    clean_symbol = str(symbol).strip().upper()
    if not clean_symbol:
        return None
    receive_time = received_at or datetime.now(IST)
    if receive_time.tzinfo is None:
        receive_time = receive_time.replace(tzinfo=IST)
    else:
        receive_time = receive_time.astimezone(IST)

    exchange_time = _iso_datetime(tick.get("exchange_timestamp") or tick.get("timestamp"))
    last_trade_time = _iso_datetime(tick.get("last_trade_time"))
    event_time = exchange_time or last_trade_time or receive_time.isoformat()
    last_price = _finite_float(tick.get("last_price"))
    if last_price is None or last_price <= 0:
        return None

    quantity = _finite_float(
        tick.get("last_quantity", tick.get("last_traded_quantity")),
        0.0,
    )
    volume_traded = _finite_float(tick.get("volume_traded", tick.get("volume")))
    depth = tick.get("depth")
    depth = depth if isinstance(depth, Mapping) else {}
    buy = _normalise_depth(depth.get("buy"))
    sell = _normalise_depth(depth.get("sell"))
    best_bid = buy[0]["price"] if buy else None
    best_ask = sell[0]["price"] if sell else None

    return {
        "schema_version": 1,
        "symbol": clean_symbol,
        "instrument_token": int(tick["instrument_token"]) if tick.get("instrument_token") else None,
        "timestamp": event_time,
        "exchange_timestamp": exchange_time,
        "last_trade_time": last_trade_time,
        "received_at": receive_time.isoformat(),
        "last_price": last_price,
        "last_quantity": max(0.0, quantity or 0.0),
        "volume_traded": volume_traded,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "depth_buy": buy,
        "depth_sell": sell,
        "buy_depth_quantity": float(sum(float(level["quantity"]) for level in buy)),
        "sell_depth_quantity": float(sum(float(level["quantity"]) for level in sell)),
        "source": "KITE_WEBSOCKET_FULL_SNAPSHOT",
    }


def load_order_flow_capture_settings(
    config_path: Path = DEFAULT_CONFIG,
) -> OrderFlowCaptureSettings:
    """Load and validate the optional tick-capture configuration."""
    try:
        raw = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise OrderFlowError(f"Could not read order-flow configuration: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise OrderFlowError("Order-flow configuration must be a YAML mapping")
    capture = raw.get("capture", {})
    if not isinstance(capture, Mapping):
        raise OrderFlowError("capture configuration must be a YAML mapping")

    enabled = bool(capture.get("enabled", False))
    directory = Path(str(capture.get("directory", ".local/order_flow")))
    if not directory.is_absolute():
        directory = PROJECT_ROOT / directory
    try:
        queue_size = int(capture.get("queue_size", 50_000))
        batch_size = int(capture.get("batch_size", 200))
        flush_interval = float(capture.get("flush_interval_seconds", 1.0))
    except (TypeError, ValueError) as exc:
        raise OrderFlowError("Capture queue and flush settings must be numeric") from exc
    if queue_size < 1 or batch_size < 1 or flush_interval <= 0:
        raise OrderFlowError("queue_size, batch_size and flush interval must be positive")
    return OrderFlowCaptureSettings(
        enabled=enabled,
        directory=directory,
        queue_size=queue_size,
        batch_size=batch_size,
        flush_interval_seconds=flush_interval,
    )


class KiteTickRecorder:
    """Bounded, asynchronous JSONL recorder that does not block the tick callback."""

    def __init__(self, settings: OrderFlowCaptureSettings):
        if settings.queue_size < 1 or settings.batch_size < 1:
            raise ValueError("queue_size and batch_size must be positive")
        if settings.flush_interval_seconds <= 0:
            raise ValueError("flush_interval_seconds must be positive")
        self.settings = settings
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=settings.queue_size)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._accepted = 0
        self._written = 0
        self._dropped = 0
        self._write_errors = 0
        self._last_error: str | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self.settings.directory.mkdir(parents=True, exist_ok=True)
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._writer_loop,
            name="tradinggpt-order-flow-writer",
            daemon=True,
        )
        self._thread.start()

    def submit(
        self,
        tick: Mapping[str, Any],
        *,
        symbol: str,
        received_at: datetime | None = None,
    ) -> bool:
        """Enqueue a normalized snapshot; return immediately if the queue is full."""
        if self._thread is None or not self._thread.is_alive() or self._stop_event.is_set():
            return False
        try:
            record = normalise_kite_tick(symbol, tick, received_at=received_at)
            if record is None:
                return False
            self._queue.put_nowait(record)
        except queue.Full:
            with self._lock:
                self._dropped += 1
            return False
        except Exception:
            # A research recorder must never break the existing paper-price monitor.
            LOGGER.exception("Could not enqueue Kite order-flow snapshot for %s", symbol)
            return False
        with self._lock:
            self._accepted += 1
        return True

    def _flush(self, batch: list[dict[str, Any]]) -> None:
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for record in batch:
            stamp = _parse_datetime(record["timestamp"])
            if stamp is None:
                with self._lock:
                    self._write_errors += 1
                    self._last_error = "record timestamp could not be parsed"
                continue
            grouped[(stamp.date().isoformat(), record["symbol"])].append(record)

        written = 0
        try:
            for (session_date, symbol), records in grouped.items():
                target = self.settings.directory / session_date / f"{symbol}.jsonl.gz"
                target.parent.mkdir(parents=True, exist_ok=True)
                with gzip.open(target, "at", encoding="utf-8", newline="\n") as handle:
                    for record in records:
                        handle.write(json.dumps(record, separators=(",", ":"), ensure_ascii=False))
                        handle.write("\n")
                        written += 1
        except OSError as exc:
            with self._lock:
                self._write_errors += len(batch)
                self._last_error = str(exc)
            LOGGER.exception("Could not persist Kite order-flow snapshots")
            return
        with self._lock:
            self._written += written

    def _writer_loop(self) -> None:
        batch: list[dict[str, Any]] = []
        last_flush = time.monotonic()
        while not self._stop_event.is_set() or not self._queue.empty():
            wait_for = max(0.05, self.settings.flush_interval_seconds - (time.monotonic() - last_flush))
            try:
                record = self._queue.get(timeout=wait_for)
            except queue.Empty:
                record = None
            if record is not None:
                batch.append(record)
                self._queue.task_done()
            elapsed = time.monotonic() - last_flush
            should_flush = (
                len(batch) >= self.settings.batch_size
                or (batch and elapsed >= self.settings.flush_interval_seconds)
                or (self._stop_event.is_set() and self._queue.empty() and batch)
            )
            if should_flush:
                self._flush(batch)
                batch = []
                last_flush = time.monotonic()
        if batch:
            self._flush(batch)

    def stop(self, timeout_seconds: float = 15.0) -> dict[str, Any]:
        """Stop accepting events, drain the queue, and return capture diagnostics."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=max(0.1, timeout_seconds))
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "accepted": self._accepted,
                "written": self._written,
                "dropped_queue_full": self._dropped,
                "write_errors": self._write_errors,
                "last_error": self._last_error,
                "pending": self._queue.qsize(),
                "writer_alive": bool(self._thread and self._thread.is_alive()),
            }


def read_order_flow_events(path: Path) -> list[dict[str, Any]]:
    """Read JSONL snapshots; malformed lines fail with their line number."""
    events: list[dict[str, Any]] = []
    try:
        opener = gzip.open if Path(path).suffix.lower() == ".gz" else open
        with opener(path, "rt", encoding="utf-8") as handle:
            for line_number, raw_line in enumerate(handle, start=1):
                if not raw_line.strip():
                    continue
                try:
                    value = json.loads(raw_line)
                except json.JSONDecodeError as exc:
                    raise OrderFlowError(f"Invalid JSON at line {line_number}: {exc}") from exc
                if not isinstance(value, dict):
                    raise OrderFlowError(f"Expected a JSON object at line {line_number}")
                events.append(value)
    except OSError as exc:
        raise OrderFlowError(f"Could not read order-flow file {path}: {exc}") from exc
    return events


def _depth_quantity(event: Mapping[str, Any], side: str, levels: int) -> float:
    values = event.get(f"depth_{side}")
    if isinstance(values, list):
        return sum(
            max(0.0, _finite_float(level.get("quantity"), 0.0) or 0.0)
            for level in values[:levels]
            if isinstance(level, Mapping)
        )
    total_key = "buy_depth_quantity" if side == "buy" else "sell_depth_quantity"
    total = _finite_float(event.get(total_key))
    return max(0.0, total or 0.0)


def analyze_order_flow(
    events: Iterable[Mapping[str, Any]],
    *,
    depth_levels: int = 5,
) -> dict[str, Any]:
    """Estimate aggressor volume and displayed-depth imbalance from snapshots.

    Quote-touch classification is preferred. For trades inside the spread, an
    uptick/downtick rule is used; unchanged prices inherit the last known tick
    direction. Repeated last-trade snapshots are de-duplicated by trade time,
    price and quantity. Snapshot feeds are not guaranteed to contain every print.
    """
    if depth_levels < 1:
        raise OrderFlowError("depth_levels must be positive")
    rows = list(events)
    if not rows:
        raise OrderFlowError("At least one order-flow event is required")

    parsed: list[tuple[datetime, Mapping[str, Any]]] = []
    for event in rows:
        stamp = _parse_datetime(event.get("timestamp") or event.get("exchange_timestamp"))
        if stamp is None:
            raise OrderFlowError("Every event must include a valid timestamp")
        parsed.append((stamp, event))
    parsed.sort(key=lambda item: item[0])

    symbols = {
        str(event.get("symbol", "")).strip().upper()
        for _, event in parsed
        if str(event.get("symbol", "")).strip()
    }
    if len(symbols) > 1:
        raise OrderFlowError(
            "Analyse one symbol at a time; do not mix trade and depth snapshots across symbols"
        )

    seen_trade_keys: set[tuple[str, str, float, float]] = set()
    previous_price: float | None = None
    previous_side: str | None = None
    buy_volume = 0.0
    sell_volume = 0.0
    unclassified_volume = 0.0
    trade_updates = 0
    duplicates = 0
    missing_trade_time = 0
    methods: Counter[str] = Counter()
    depth_imbalances: list[float] = []
    displayed_bid_quantities: list[float] = []
    displayed_ask_quantities: list[float] = []

    for _, event in parsed:
        bid_qty = _depth_quantity(event, "buy", depth_levels)
        ask_qty = _depth_quantity(event, "sell", depth_levels)
        displayed_bid_quantities.append(bid_qty)
        displayed_ask_quantities.append(ask_qty)
        if bid_qty + ask_qty > 0:
            depth_imbalances.append((bid_qty - ask_qty) / (bid_qty + ask_qty))

        price = _finite_float(event.get("last_price", event.get("price")))
        quantity = _finite_float(event.get("last_quantity", event.get("quantity")), 0.0)
        if price is None or price <= 0 or quantity is None or quantity <= 0:
            continue
        trade_time = _iso_datetime(event.get("last_trade_time"))
        if trade_time is None:
            missing_trade_time += 1
            continue
        symbol = str(event.get("symbol", "")).strip().upper()
        key = (symbol, trade_time, price, quantity)
        if key in seen_trade_keys:
            duplicates += 1
            continue
        seen_trade_keys.add(key)
        trade_updates += 1

        supplied_side = str(event.get("aggressor_side", "")).strip().upper()
        side: str | None = supplied_side if supplied_side in {"BUY", "SELL"} else None
        method = "PROVIDED_AGGRESSOR_SIDE" if side else ""

        bid = _finite_float(event.get("best_bid"))
        ask = _finite_float(event.get("best_ask"))
        if side is None and bid is not None and ask is not None and 0 < bid < ask:
            if price >= ask:
                side = "BUY"
                method = "ASK_TOUCH_ESTIMATE"
            elif price <= bid:
                side = "SELL"
                method = "BID_TOUCH_ESTIMATE"

        if side is None and previous_price is not None:
            if price > previous_price:
                side = "BUY"
                method = "UPTICK_ESTIMATE"
            elif price < previous_price:
                side = "SELL"
                method = "DOWNTICK_ESTIMATE"
            elif previous_side is not None:
                side = previous_side
                method = "ZERO_TICK_CARRY_ESTIMATE"

        if side == "BUY":
            buy_volume += quantity
            methods[method] += 1
            previous_side = side
        elif side == "SELL":
            sell_volume += quantity
            methods[method] += 1
            previous_side = side
        else:
            unclassified_volume += quantity
            methods["UNCLASSIFIED"] += 1
        previous_price = price

    classified_volume = buy_volume + sell_volume
    delta = buy_volume - sell_volume
    imbalance_pct = (delta / classified_volume * 100.0) if classified_volume else None
    latest_depth_imbalance = depth_imbalances[-1] if depth_imbalances else None
    result = {
        "data_quality": "ESTIMATED_KITE_WEBSOCKET_SNAPSHOT",
        "snapshot_count": len(parsed),
        "unique_trade_updates": trade_updates,
        "duplicate_trade_snapshots_skipped": duplicates,
        "trade_updates_missing_last_trade_time": missing_trade_time,
        "classified_buy_volume_estimate": buy_volume,
        "classified_sell_volume_estimate": sell_volume,
        "unclassified_volume": unclassified_volume,
        "estimated_delta": delta,
        "estimated_delta_pct_of_classified_volume": imbalance_pct,
        "depth_levels_used": depth_levels,
        "depth_snapshot_count": len(depth_imbalances),
        "mean_displayed_depth_imbalance": (
            sum(depth_imbalances) / len(depth_imbalances) if depth_imbalances else None
        ),
        "latest_displayed_depth_imbalance": latest_depth_imbalance,
        "latest_displayed_bid_quantity": (
            displayed_bid_quantities[-1] if displayed_bid_quantities else 0.0
        ),
        "latest_displayed_ask_quantity": (
            displayed_ask_quantities[-1] if displayed_ask_quantities else 0.0
        ),
        "classification_counts": dict(methods),
        "limitations": [
            "Kite WebSocket messages are market-data snapshots, not a guaranteed record of every exchange execution.",
            "Last-trade quantity can repeat across snapshots; duplicate timestamp/price/quantity keys are suppressed, but two indistinguishable same-second prints cannot be separated.",
            "Aggressor side is estimated from quote touches or the tick rule unless an explicit aggressor_side field is supplied.",
            "Displayed depth is resting order quantity and can be cancelled or changed; it is not executed buying or selling pressure.",
            "This module does not calculate true order-flow imbalance (OFI), iceberg/absorption events or exchange-level trade delta.",
        ],
    }
    if symbols:
        result["symbol"] = next(iter(symbols))
    if parsed:
        result["start_time"] = parsed[0][0].isoformat()
        result["end_time"] = parsed[-1][0].isoformat()
    return result


def _main() -> int:
    parser = argparse.ArgumentParser(
        description="Analyse estimated order flow from saved Kite WebSocket snapshots."
    )
    parser.add_argument("--events", required=True, type=Path, help="JSONL file from the order-flow recorder")
    parser.add_argument("--depth-levels", type=int, default=5)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    try:
        summary = analyze_order_flow(
            read_order_flow_events(args.events),
            depth_levels=args.depth_levels,
        )
        rendered = json.dumps(summary, indent=2, ensure_ascii=False)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered + "\n", encoding="utf-8")
        else:
            print(rendered)
    except (OSError, OrderFlowError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
