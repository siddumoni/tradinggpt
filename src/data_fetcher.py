"""Read-only Kite Connect market-data client for Phase 3.

This module intentionally contains no order placement/modification/cancellation calls.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

import yaml

from src.auth import DEFAULT_TOKEN_CACHE, load_credentials, load_token_cache

LOGGER = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SETTINGS = PROJECT_ROOT / "config" / "settings.yaml"
DEFAULT_WATCHLIST = PROJECT_ROOT / "config" / "watchlist.yaml"
INSTRUMENT_CACHE = PROJECT_ROOT / ".local" / "nse_instruments.json"


class DataFetchError(RuntimeError):
    """Raised when market data cannot be fetched or validated."""


class MarketClosedError(DataFetchError):
    """Raised when a data run is attempted on a weekend or outside market hours."""


@dataclass(frozen=True)
class DataClientConfig:
    retries: int = 3
    retry_delay_seconds: float = 1.0
    quote_batch_size: int = 500
    quote_min_interval_seconds: float = 1.0
    historical_min_interval_seconds: float = 0.34


def load_yaml(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise DataFetchError(f"Could not read configuration file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise DataFetchError(f"Configuration file must contain a YAML mapping: {path}")
    return value


def is_weekend(day: date) -> bool:
    return day.weekday() >= 5


def is_regular_market_hours(now: datetime) -> bool:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    local = now.astimezone(IST)
    return (not is_weekend(local.date())) and dtime(9, 15) <= local.time().replace(tzinfo=None) <= dtime(15, 30)


def _retry(operation: Callable[[], Any], *, attempts: int, delay: float, label: str) -> Any:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except Exception as exc:  # Kite SDK has multiple exception classes for transient failures.
            last_error = exc
            if attempt == attempts:
                break
            wait = delay * attempt
            LOGGER.warning("%s failed (attempt %s/%s); retrying in %.1fs: %s", label, attempt, attempts, wait, type(exc).__name__)
            time.sleep(wait)
    raise DataFetchError(f"{label} failed after {attempts} attempts: {last_error}") from last_error


class KiteMarketData: 
    """Read-only market-data wrapper around an authenticated KiteConnect client."""

    def __init__(self, kite: Any, config: DataClientConfig | None = None):
        self.kite = kite
        self.config = config or DataClientConfig()
        if self.config.quote_batch_size < 1:
            raise ValueError("quote_batch_size must be positive")
        if self.config.quote_min_interval_seconds < 0:
            raise ValueError("quote_min_interval_seconds must be non-negative")
        if self.config.historical_min_interval_seconds < 0:
            raise ValueError("historical_min_interval_seconds must be non-negative")
        self._last_quote_request_at: float | None = None
        self._last_historical_request_at: float | None = None

    @classmethod
    def from_cached_session(cls, cache_path: Path = DEFAULT_TOKEN_CACHE, config: DataClientConfig | None = None) -> "KiteMarketData":
        api_key, _ = load_credentials()
        cached = load_token_cache(api_key, cache_path=cache_path)
        if cached is None:
            raise DataFetchError("No valid cached Kite token. Run: python -m src.main login")
        try:
            from kiteconnect import KiteConnect
        except ImportError as exc:
            raise DataFetchError("kiteconnect is missing. Run: python -m pip install -r requirements.txt") from exc
        kite = KiteConnect(api_key=api_key)
        kite.set_access_token(cached.access_token)
        return cls(kite, config=config)

    def instruments(self, cache_path: Path = INSTRUMENT_CACHE, now: datetime | None = None) -> list[dict[str, Any]]:
        """Return NSE instrument dump, caching one copy per IST calendar day."""
        current = (now or datetime.now(IST)).astimezone(IST)
        if cache_path.exists():
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                if cached.get("date") == current.date().isoformat() and isinstance(cached.get("instruments"), list):
                    return cached["instruments"]
            except (OSError, json.JSONDecodeError, AttributeError):
                LOGGER.warning("Instrument cache is invalid; downloading a fresh copy")
        rows = _retry(lambda: self.kite.instruments("NSE"), attempts=self.config.retries, delay=self.config.retry_delay_seconds, label="NSE instrument download")
        if not isinstance(rows, list) or not rows:
            raise DataFetchError("Kite returned an empty NSE instrument list")
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = cache_path.with_suffix(cache_path.suffix + ".tmp")
        temp_path.write_text(json.dumps({"date": current.date().isoformat(), "instruments": rows}, default=str), encoding="utf-8")
        temp_path.replace(cache_path)
        return rows

    @staticmethod
    def instrument_map(instruments: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        return {str(row.get("tradingsymbol", "")).upper(): row for row in instruments if row.get("tradingsymbol")}

    def resolve_watchlist(self, watchlist_path: Path = DEFAULT_WATCHLIST, instrument_rows: list[dict[str, Any]] | None = None) -> tuple[list[dict[str, Any]], list[str]]:
        config = load_yaml(watchlist_path)
        requested = list(dict.fromkeys([str(s).upper() for s in config.get("nifty50", []) + config.get("additional_liquid_fo", [])]))
        rows = instrument_rows if instrument_rows is not None else self.instruments()
        by_symbol = self.instrument_map(rows)
        resolved: list[dict[str, Any]] = []
        missing: list[str] = []
        for symbol in requested:
            row = by_symbol.get(symbol)
            if row and row.get("segment") == "NSE" and row.get("instrument_type") == "EQ":
                resolved.append(row)
            else:
                missing.append(symbol)
        return resolved, missing

    def ltp(self, symbols: Iterable[str]) -> dict[str, Any]:
        keys = [f"NSE:{symbol.upper()}" for symbol in dict.fromkeys(symbols)]
        if not keys:
            return {}
        result: dict[str, Any] = {}
        for offset in range(0, len(keys), self.config.quote_batch_size):
            batch = keys[offset:offset + self.config.quote_batch_size]
            data = _retry(lambda b=batch: self.kite.ltp(*b), attempts=self.config.retries, delay=self.config.retry_delay_seconds, label="LTP request")
            if isinstance(data, dict):
                result.update(data)
        return result

    def _wait_for_endpoint_slot(self, endpoint: str) -> None:
        """Apply Kite's documented per-endpoint request pacing in this client."""
        if endpoint == "quote":
            interval = self.config.quote_min_interval_seconds
            last = self._last_quote_request_at
        elif endpoint == "historical":
            interval = self.config.historical_min_interval_seconds
            last = self._last_historical_request_at
        else:
            raise ValueError(f"Unknown rate-limit endpoint: {endpoint}")

        if last is not None and interval > 0:
            elapsed = time.monotonic() - last
            wait = interval - elapsed
            if wait > 0:
                time.sleep(wait)

        stamp = time.monotonic()
        if endpoint == "quote":
            self._last_quote_request_at = stamp
        else:
            self._last_historical_request_at = stamp

    def quotes(self, symbols: Iterable[str]) -> dict[str, Any]:
        """Fetch full quotes in batches of at most 500, paced at one request/second."""
        keys = [f"NSE:{symbol.upper()}" for symbol in dict.fromkeys(symbols)]
        if not keys:
            return {}
        result: dict[str, Any] = {}
        batch_size = min(int(self.config.quote_batch_size), 500)
        for offset in range(0, len(keys), batch_size):
            batch = keys[offset:offset + batch_size]

            def request(batch_keys: list[str] = batch) -> Any:
                self._wait_for_endpoint_slot("quote")
                return self.kite.quote(*batch_keys)

            data = _retry(
                request,
                attempts=self.config.retries,
                delay=self.config.retry_delay_seconds,
                label="Quote request",
            )
            if isinstance(data, dict):
                result.update(data)
        return result

    def screen_liquid_nse_equities(
        self,
        instrument_rows: Iterable[dict[str, Any]],
        *,
        now: datetime | None = None,
        max_symbols: int = 50,
        min_price_inr: float = 50.0,
        max_price_inr: float = 10000.0,
        min_current_day_volume: int = 10000,
        min_current_day_turnover_inr: float = 5000000.0,
        max_quote_age_minutes: float = 15.0,
    ) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, Any]]:
        """Screen the full NSE EQ universe by fresh quotes, then rank by turnover proxy.

        The turnover proxy is average_price multiplied by current-day volume. It is
        a liquidity-ranking estimate, not an exchange-reported turnover field.
        """
        from math import isfinite

        if max_symbols < 1:
            raise ValueError("max_symbols must be at least 1")
        numeric_values = (min_price_inr, max_price_inr, min_current_day_turnover_inr, max_quote_age_minutes)
        if any(not isfinite(float(value)) for value in numeric_values):
            raise ValueError("Universe screen thresholds must be finite")
        if min_price_inr <= 0 or max_price_inr < min_price_inr:
            raise ValueError("Price bounds must be positive and max_price_inr >= min_price_inr")
        if min_current_day_volume < 0 or min_current_day_turnover_inr < 0 or max_quote_age_minutes <= 0:
            raise ValueError("Volume/turnover thresholds must be non-negative and quote age must be positive")

        current = now or datetime.now(IST)
        current = current.replace(tzinfo=IST) if current.tzinfo is None else current.astimezone(IST)

        equities: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in instrument_rows:
            symbol = str(row.get("tradingsymbol", "")).strip().upper()
            if (
                not symbol
                or symbol in seen
                or str(row.get("exchange", "NSE")).upper() != "NSE"
                or str(row.get("segment", "")).upper() != "NSE"
                or str(row.get("instrument_type", "")).upper() != "EQ"
                or not row.get("instrument_token")
            ):
                continue
            seen.add(symbol)
            equities.append(row)

        symbols = [str(row["tradingsymbol"]).strip().upper() for row in equities]
        quote_map = self.quotes(symbols)
        exclusions: dict[str, int] = {}
        ranked: list[tuple[float, float, str, dict[str, Any], dict[str, Any]]] = []

        def exclude(reason: str) -> None:
            exclusions[reason] = exclusions.get(reason, 0) + 1

        for instrument in equities:
            symbol = str(instrument["tradingsymbol"]).strip().upper()
            quote = quote_map.get(f"NSE:{symbol}")
            if not isinstance(quote, dict):
                exclude("QUOTE_MISSING")
                continue

            timestamp_value = quote.get("timestamp") or quote.get("last_trade_time")
            try:
                if isinstance(timestamp_value, datetime):
                    quote_time = timestamp_value
                elif isinstance(timestamp_value, str) and timestamp_value.strip():
                    quote_time = datetime.fromisoformat(timestamp_value.strip().replace("Z", "+00:00"))
                else:
                    raise ValueError("quote timestamp missing")
                quote_time = quote_time.replace(tzinfo=IST) if quote_time.tzinfo is None else quote_time.astimezone(IST)
                quote_age = (current - quote_time).total_seconds() / 60.0
                if quote_age < -1.0:
                    exclude("QUOTE_TIMESTAMP_IN_FUTURE")
                    continue
                if quote_age > max_quote_age_minutes:
                    exclude("QUOTE_STALE")
                    continue
            except (TypeError, ValueError, OverflowError):
                exclude("QUOTE_TIMESTAMP_INVALID")
                continue

            try:
                price = float(quote.get("last_price"))
                volume = int(quote.get("volume"))
                average_price = float(quote.get("average_price") or price)
            except (TypeError, ValueError, OverflowError):
                exclude("QUOTE_FIELDS_INVALID")
                continue
            if not (isfinite(price) and price > 0 and isfinite(average_price) and average_price > 0 and volume >= 0):
                exclude("QUOTE_FIELDS_INVALID")
                continue
            if price < min_price_inr or price > max_price_inr:
                exclude("PRICE_OUTSIDE_RANGE")
                continue
            if volume < min_current_day_volume:
                exclude("CURRENT_DAY_VOLUME_BELOW_MINIMUM")
                continue

            turnover_proxy = average_price * volume
            if turnover_proxy < min_current_day_turnover_inr:
                exclude("CURRENT_DAY_TURNOVER_BELOW_MINIMUM")
                continue
            ranked.append((turnover_proxy, float(volume), symbol, instrument, quote))

        ranked.sort(key=lambda item: (-item[0], -item[1], item[2]))
        selected_rows = ranked[:max_symbols]
        selected_instruments = [item[3] for item in selected_rows]
        selected_quotes = {item[2]: item[4] for item in selected_rows}
        summary = {
            "mode": "NSE_EQ_LIQUID_TOP",
            "instrument_universe": "Kite NSE instruments where exchange=NSE, segment=NSE, instrument_type=EQ",
            "instruments_total": len(equities),
            "quotes_requested": len(symbols),
            "quotes_received": sum(1 for symbol in symbols if f"NSE:{symbol}" in quote_map),
            "eligible_before_top_n": len(ranked),
            "selected_for_technical_analysis": len(selected_instruments),
            "selection_limit": max_symbols,
            "filters": {
                "min_price_inr": float(min_price_inr),
                "max_price_inr": float(max_price_inr),
                "min_current_day_volume": int(min_current_day_volume),
                "min_current_day_turnover_inr": float(min_current_day_turnover_inr),
                "max_quote_age_minutes": float(max_quote_age_minutes),
            },
            "exclusions": exclusions,
            "selection_metric": "average_price × current-day volume",
            "selection_metric_warning": "Turnover is a proxy for ranking only; it is not an exchange-reported turnover figure or an average-daily-volume statistic.",
            "selected_symbols": [item[2] for item in selected_rows],
        }
        return selected_instruments, selected_quotes, summary

    def historical_candles(self, instrument_token: int, from_dt: datetime, to_dt: datetime, interval: str) -> list[dict[str, Any]]:
        if from_dt.tzinfo is None or to_dt.tzinfo is None:
            raise ValueError("Historical-data datetimes must be timezone-aware")
        if from_dt > to_dt:
            raise ValueError("from_dt must be <= to_dt")
        allowed = {"5minute", "15minute", "day"}
        if interval not in allowed:
            raise ValueError(f"Unsupported interval {interval!r}; choose one of {sorted(allowed)}")
        def request() -> Any:
            self._wait_for_endpoint_slot("historical")
            return self.kite.historical_data(
                instrument_token, from_dt.astimezone(IST), to_dt.astimezone(IST), interval
            )

        candles = _retry(
            request,
            attempts=self.config.retries,
            delay=self.config.retry_delay_seconds,
            label=f"Historical {interval} candles",
        )
        if candles is None:
            return []
        if not isinstance(candles, list):
            raise DataFetchError(f"Kite returned an unexpected response for {interval} candles")
        return candles

    def fetch_symbol_bundle(
        self,
        instrument: dict[str, Any],
        now: datetime | None = None,
        *,
        quote_data: dict[str, Any] | None = None,
        include_daily_candles: bool = True,
    ) -> dict[str, Any]:
        """Fetch quote and intraday candles; daily history can be skipped for fast scans."""
        current = (now or datetime.now(IST)).astimezone(IST)
        if is_weekend(current.date()):
            raise MarketClosedError("Today is a weekend; no NSE cash-market session is expected.")
        token = instrument.get("instrument_token")
        symbol = str(instrument.get("tradingsymbol", "")).upper()
        if not token or not symbol:
            raise DataFetchError("Instrument row must include instrument_token and tradingsymbol")
        quote_key = f"NSE:{symbol}"
        resolved_quote = quote_data if quote_data is not None else self.quotes([symbol]).get(quote_key)
        start_today = datetime.combine(current.date(), dtime(9, 15), tzinfo=IST)
        daily_start = datetime.combine(current.date() - timedelta(days=60), dtime(0, 0), tzinfo=IST)
        return {
            "symbol": symbol,
            "instrument_token": int(token),
            "fetched_at": current.isoformat(),
            "quote": resolved_quote,
            "candles_5m": self.historical_candles(int(token), start_today, current, "5minute") if current.time() >= dtime(9, 15) else [],
            "candles_15m": self.historical_candles(int(token), start_today, current, "15minute") if current.time() >= dtime(9, 15) else [],
            "candles_daily_60d": (
                self.historical_candles(int(token), daily_start, current, "day")
                if include_daily_candles else []
            ),
        }



    def fetch_nifty50_15m_candles(
        self,
        instrument_rows: Iterable[dict[str, Any]],
        now: datetime | None = None,
        lookback_days: int = 7,
    ) -> list[dict[str, Any]]:
        """Fetch NIFTY 50 15-minute regime candles from Kite, not Yahoo."""
        current = (now or datetime.now(IST)).astimezone(IST)
        candidates = []
        for row in instrument_rows:
            trading_symbol = " ".join(str(row.get("tradingsymbol", "")).upper().split())
            name = " ".join(str(row.get("name", "")).upper().split())
            exchange = str(row.get("exchange", "NSE")).upper()
            if (
                (trading_symbol in {"NIFTY 50", "NIFTY50"} or name == "NIFTY 50")
                and exchange == "NSE"
                and row.get("instrument_token")
            ):
                candidates.append(row)
        if not candidates:
            raise DataFetchError(
                "NIFTY 50 index instrument was not found in the Kite NSE instrument dump; "
                "index regime unavailable and Yahoo fallback is disabled in Kite mode."
            )
        instrument = next(
            (row for row in candidates if str(row.get("segment", "")).upper() == "INDICES"),
            candidates[0],
        )
        start = datetime.combine(
            current.date() - timedelta(days=lookback_days), dtime(0, 0), tzinfo=IST
        )
        candles = self.historical_candles(
            int(instrument["instrument_token"]), start, current, "15minute"
        )
        if not candles:
            raise DataFetchError("Kite returned no NIFTY 50 15-minute candles for regime context")
        return candles

    def estimate_mis_margin_per_share(
        self,
        tradingsymbol: str,
        side: str,
        price: float,
    ) -> float:
        """Use Kite's read-only margin calculator for one hypothetical MIS share.

        This calls the margin-estimation API only; no order is submitted.
        Missing, malformed, zero, or negative estimates raise DataFetchError
        so the scanner can preserve its fail-closed policy.
        """
        symbol = str(tradingsymbol).strip().upper()
        direction = str(side).strip().upper()
        if not symbol or direction not in {"LONG", "SHORT"}:
            raise ValueError("tradingsymbol and side LONG/SHORT are required")
        try:
            unit_price = float(price)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("price must be a positive finite number") from exc
        if not (0 < unit_price < float("inf")):
            raise ValueError("price must be a positive finite number")

        params = [{
            "exchange": "NSE",
            "tradingsymbol": symbol,
            "transaction_type": "BUY" if direction == "LONG" else "SELL",
            "variety": "regular",
            "product": "MIS",
            "order_type": "LIMIT",
            "quantity": 1,
            "price": unit_price,
        }]
        result = _retry(
            lambda: self.kite.order_margins(params),
            attempts=self.config.retries,
            delay=self.config.retry_delay_seconds,
            label="MIS margin estimate",
        )
        rows = result
        if isinstance(result, dict):
            if isinstance(result.get("orders"), list):
                rows = result["orders"]
            elif isinstance(result.get("data"), list):
                rows = result["data"]
            else:
                rows = [result]
        if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
            raise DataFetchError("Kite margin calculator returned an unexpected response")

        try:
            amount = float(rows[0].get("total"))
        except (TypeError, ValueError, OverflowError):
            amount = 0.0
        if not (0 < amount < float("inf")):
            fields = ("span", "exposure", "option_premium", "additional", "bo", "cash", "var")
            components = []
            for field in fields:
                try:
                    value = float(rows[0].get(field, 0) or 0)
                except (TypeError, ValueError, OverflowError) as exc:
                    raise DataFetchError("Kite margin response contains invalid components") from exc
                if value < 0 or not (value < float("inf")):
                    raise DataFetchError("Kite margin response contains invalid components")
                components.append(value)
            amount = sum(components)
        if not (0 < amount < float("inf")):
            raise DataFetchError("Kite did not return a positive, usable MIS margin estimate")
        return amount

def market_session_message(now: datetime | None = None) -> str | None:
    """Return a graceful notice for weekends or outside normal NSE cash hours.

    Exchange holidays cannot be conclusively identified from weekday/time alone;
    empty historical candles should be treated as a no-data session, not a signal.
    """
    current = (now or datetime.now(IST)).astimezone(IST)
    if is_weekend(current.date()):
        return "NSE cash market is normally closed on weekends. No market-data scan was run."
    if current.time() < dtime(9, 15) or current.time() > dtime(15, 30):
        return "Outside normal NSE cash-market hours (09:15–15:30 IST). Historical daily data may still be available; intraday signals are not evaluated."
    return None
