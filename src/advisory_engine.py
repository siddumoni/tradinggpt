"""Phase 6 CLI: scan stocks, collect RSS headlines, and write advisory-only reports.

Example (Yahoo development data; not a verified live feed):
    python -m src.advisory_engine --symbols RELIANCE,HDFCBANK,ICICIBANK,SBIN,INFY

This command never places, modifies, or cancels orders.
"""
from __future__ import annotations

import argparse
import json
import logging
from math import isfinite
from datetime import datetime, time as dtime, timedelta
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from src.market_scanner import evaluate_symbol_bundle, rank_candidates
from src.news_events import DEFAULT_FEEDS, fetch_news, google_news_search_url
from src.risk_engine import load_phase2_settings
from src.paper_journal import append_scan_ledger, append_scan_report
from src.candle_archive import append_completed_candles
from src.live_entry_validation import inspect_executable_quote, revalidate_candidate_at_quote
from src.market_calendar import calendar_maintenance_status, market_day_status, market_session_message
from src.market_safety import (
    inspect_circuit_limits,
    load_market_safety_settings,
    load_surveillance_snapshot,
    surveillance_status_for_symbol,
)

IST = ZoneInfo("Asia/Kolkata")
LOGGER = logging.getLogger("nse_advisory_assistant")


def load_watchlist(path: Path) -> list[str]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    symbols = list(raw.get("nifty50", [])) + list(raw.get("additional_liquid_fo", []))
    return list(dict.fromkeys(str(x).strip().upper() for x in symbols if str(x).strip()))


def _fetch_nifty_15m(now: datetime) -> list[dict[str, Any]]:
    """Fetch NIFTY 50 index candles from Yahoo for research/regime context only."""
    try:
        import yfinance as yf
    except ImportError as exc:
        raise RuntimeError("yfinance is missing. Install project requirements before running the scanner.") from exc
    # Use recent sessions for indicator warm-up; the regime classifier needs more
    # than the handful of bars available shortly after the open.
    start = datetime.combine(now.date() - timedelta(days=7), dtime(9, 15), tzinfo=IST)
    frame = yf.Ticker("^NSEI").history(
        start=start.replace(tzinfo=None), end=(now + timedelta(minutes=1)).replace(tzinfo=None),
        interval="15m", auto_adjust=False, prepost=False, actions=False,
    )
    if frame is None or frame.empty:
        return []
    rows: list[dict[str, Any]] = []
    for idx, row in frame.iterrows():
        stamp = idx.to_pydatetime() if hasattr(idx, "to_pydatetime") else idx
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=IST)
        else:
            stamp = stamp.astimezone(IST)
        rows.append({"date": stamp, "open": float(row["Open"]), "high": float(row["High"]), "low": float(row["Low"]), "close": float(row["Close"]), "volume": int(row.get("Volume", 0) or 0)})
    return rows


def _load_feeds(path: Path | None) -> list[str]:
    if path is None or not path.exists():
        return list(DEFAULT_FEEDS)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    feeds = raw.get("feeds", [])
    return [str(url).strip() for url in feeds if str(url).strip()] or list(DEFAULT_FEEDS)


def _kite_quote_verification(quote: Any, now: datetime, max_age_minutes: float = 15.0) -> tuple[bool, str]:
    """Verify a fresh Kite quote snapshot before setting the stock-data trust flag.

    A valid session alone is not enough: the quote must include a positive last
    price and a recent timestamp. This checks data freshness, not margin or a
    profitable edge.
    """
    if not isinstance(quote, dict):
        return False, "Kite quote snapshot is missing; stock data cannot be marked verified."
    try:
        last_price = float(quote.get("last_price"))
        timestamp_value = quote.get("timestamp")
        if timestamp_value is None:
            timestamp_value = quote.get("last_trade_time")
        if isinstance(timestamp_value, datetime):
            quote_time = timestamp_value
        elif isinstance(timestamp_value, str) and timestamp_value.strip():
            quote_time = datetime.fromisoformat(timestamp_value.strip().replace("Z", "+00:00"))
        else:
            raise ValueError("quote timestamp is missing")
        if not (last_price > 0 and last_price < float("inf")):
            raise ValueError("last price is not a positive finite value")
        quote_time = quote_time.replace(tzinfo=IST) if quote_time.tzinfo is None else quote_time.astimezone(IST)
        current = now.replace(tzinfo=IST) if now.tzinfo is None else now.astimezone(IST)
        age_minutes = (current - quote_time).total_seconds() / 60.0
    except (TypeError, ValueError, OverflowError) as exc:
        return False, f"Kite quote failed basic freshness validation: {exc}."
    if age_minutes < -1.0:
        return False, f"Kite quote timestamp is in the future ({age_minutes:.1f} minutes); verification failed."
    if age_minutes > max_age_minutes:
        return False, f"Kite quote is stale ({age_minutes:.1f} minutes old); verification failed."
    return True, ""



def _paper_market_snapshot(bundle: dict[str, Any]) -> dict[str, Any]:
    """Expose only quote fields needed by the localhost paper simulator."""
    quote = bundle.get("quote")
    if not isinstance(quote, dict):
        return {}
    depth = quote.get("depth") or {}
    buys = depth.get("buy", []) if isinstance(depth, dict) else []
    sells = depth.get("sell", []) if isinstance(depth, dict) else []
    def best(rows: Any) -> float | None:
        if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
            return None
        try: price = float(rows[0].get("price"))
        except (TypeError, ValueError, OverflowError): return None
        return price if isfinite(price) and price > 0 else None
    return {"last_price": quote.get("last_price"), "timestamp": quote.get("timestamp"),
        "last_trade_time": quote.get("last_trade_time"), "bid": best(buys), "ask": best(sells),
        "lower_circuit_limit": quote.get("lower_circuit_limit"),
        "upper_circuit_limit": quote.get("upper_circuit_limit"),
        "verified_live": bundle.get("verified_live_data") is True}


def run_scan(
    *, symbols: list[str], now: datetime | None = None, max_symbols: int | None = None,
    watchlist_path: Path | None = None, news_feeds_path: Path | None = None,
    output_dir: Path | None = None, data_provider: str = "yahoo",
    candle_archive_dir: Path | None = None,
    paper_capital_inr: float | None = None,
    paper_risk_per_trade_inr: float | None = None,
    paper_minimum_net_rr: float | None = None,
    account_profile: str = "research",
    paper_slippage_bps_per_side: float | None = None,
    universe_mode: str = "watchlist",
    universe_max_symbols: int = 50,
    universe_min_price_inr: float = 50.0,
    universe_max_price_inr: float = 10000.0,
    universe_min_current_day_volume: int = 10000,
    universe_min_current_day_turnover_inr: float = 5000000.0,
    universe_max_quote_age_minutes: float = 15.0,
    paper_entry_max_quote_age_minutes: float = 2.0,
    archive_symbols: list[str] | None = None,
) -> dict[str, Any]:
    current = now or datetime.now(IST)
    current = current.astimezone(IST) if current.tzinfo else current.replace(tzinfo=IST)
    calendar_maintenance = calendar_maintenance_status(current.date())
    if calendar_maintenance["warning"]:
        LOGGER.warning("NSE calendar maintenance: %s", calendar_maintenance["message"])
    session_message = market_session_message(current)
    if session_message:
        return {
            "status": "MARKET_CLOSED",
            "as_of": current.isoformat(),
            "account_profile": account_profile,
            "message": session_message,
            "market_calendar": market_day_status(current.date()),
            "calendar_maintenance": calendar_maintenance,
            "candidates": [],
            "news": {"items": [], "errors": []},
            "advisory_only": True,
            "orders_placed_or_modified": False,
        }

    market_safety_settings = load_market_safety_settings()
    surveillance_snapshot = load_surveillance_snapshot(as_of=current)
    if surveillance_snapshot["status"] != "VALID":
        LOGGER.warning("NSE surveillance status is %s: %s", surveillance_snapshot["status"], surveillance_snapshot["reason"])
    provider_name = str(data_provider).strip().lower()
    if provider_name not in {"yahoo", "kite"}:
        raise ValueError("data_provider must be 'yahoo' or 'kite'")
    if account_profile == "live-advisory" and provider_name != "kite":
        raise ValueError("The live-advisory profile requires the authenticated read-only Kite provider")
    mode = str(universe_mode).strip().lower()
    if mode not in {"watchlist", "nse_equity"}:
        raise ValueError("universe_mode must be 'watchlist' or 'nse_equity'")
    if mode == "nse_equity" and provider_name != "kite":
        raise ValueError("The nse_equity universe requires --provider kite and fresh Kite quotes")
    if universe_max_symbols < 1:
        raise ValueError("universe_max_symbols must be at least 1")
    if not isfinite(float(paper_entry_max_quote_age_minutes)) or paper_entry_max_quote_age_minutes <= 0:
        raise ValueError("paper_entry_max_quote_age_minutes must be finite and positive")

    selected = (
        []
        if mode == "nse_equity"
        else list(dict.fromkeys(s.strip().upper() for s in symbols if s.strip()))
    )
    if mode == "watchlist":
        if max_symbols is not None:
            selected = selected[:max_symbols]
        if not selected and watchlist_path:
            selected = load_watchlist(watchlist_path)
            if max_symbols is not None:
                selected = selected[:max_symbols]
        if not selected:
            raise ValueError("No symbols supplied and no usable watchlist was found")

    universe_selection: dict[str, Any] = {"mode": mode}
    universe_quotes: dict[str, dict[str, Any]] = {}

    instruments_by_symbol: dict[str, dict[str, Any]] = {}
    if provider_name == "yahoo":
        try:
            from src.yahoo_data_fetcher import YahooFinanceMarketData
        except ImportError as exc:
            raise RuntimeError("Yahoo provider module is missing from src/yahoo_data_fetcher.py.") from exc
        provider: Any = YahooFinanceMarketData()
        stock_provider_label = "Yahoo Finance unofficial; not verified live exchange data"
    else:
        try:
            from src.data_fetcher import KiteMarketData, is_nse_cash_equity_instrument
            provider = KiteMarketData.from_cached_session()
            instrument_rows = provider.instruments(now=current)
            instruments_by_symbol = {
                str(row.get("tradingsymbol", "")).upper(): row
                for row in instrument_rows
                if is_nse_cash_equity_instrument(row)
                and row.get("tradingsymbol")
            }
            if mode == "nse_equity":
                selected_rows, universe_quotes, universe_selection = provider.screen_liquid_nse_equities(
                    instrument_rows,
                    now=current if now is not None else None,
                    max_symbols=universe_max_symbols,
                    min_price_inr=universe_min_price_inr,
                    max_price_inr=universe_max_price_inr,
                    min_current_day_volume=universe_min_current_day_volume,
                    min_current_day_turnover_inr=universe_min_current_day_turnover_inr,
                    max_quote_age_minutes=universe_max_quote_age_minutes,
                    circuit_limit_buffer_pct=market_safety_settings.circuit_near_band_buffer_pct,
                )
                selected = [
                    str(row.get("tradingsymbol", "")).strip().upper()
                    for row in selected_rows
                ]
            else:
                universe_selection.update({
                    "instrument_universe": "configured symbols/watchlist resolved against Kite NSE EQ instruments",
                    "selected_symbols": selected,
                })
            stock_provider_label = "Zerodha Kite Connect authenticated NSE equity API; quote freshness checked"
        except Exception as exc:
            raise RuntimeError(
                "Could not initialise the read-only Kite data provider. Run 'python -m src.main login' "
                f"and check credentials/session, instrument access and connectivity. Details: {type(exc).__name__}: {exc}"
            ) from exc

    news_result = fetch_news(_load_feeds(news_feeds_path), timeout_seconds=5.0)
    # Headlines are triage inputs only. Per-symbol company RSS queries are added for
    # the most promising technical candidates in a later iteration after this scan.
    index_provider_label = "Yahoo Finance unofficial NIFTY 50 index history; not verified live exchange data"
    if provider_name == "kite":
        index_provider_label = "Zerodha Kite Connect NIFTY 50 index historical candles"
        try:
            index_15m = provider.fetch_nifty50_15m_candles(instrument_rows, now=current)
        except Exception as exc:
            LOGGER.warning("Could not fetch NIFTY index candles from Kite: %s", exc)
            index_15m = []
            index_provider_label = "Unavailable: Kite NIFTY 50 index candles; Yahoo fallback disabled"
            news_result.setdefault("errors", []).append(f"Kite NIFTY 50 index data unavailable: {exc}")
    else:
        try:
            index_15m = _fetch_nifty_15m(current)
        except Exception as exc:
            LOGGER.warning("Could not fetch NIFTY index candles: %s", exc)
            index_15m = []
            news_result.setdefault("errors", []).append(f"NIFTY index data unavailable: {exc}")

    risk_settings, charge_rates = load_phase2_settings(profile=account_profile)
    overrides = {}
    if paper_capital_inr is not None: overrides["capital_inr"] = float(paper_capital_inr)
    if paper_risk_per_trade_inr is not None: overrides["risk_per_trade_inr"] = float(paper_risk_per_trade_inr)
    if paper_minimum_net_rr is not None:
        minimum_rr_override = float(paper_minimum_net_rr)
        if not isfinite(minimum_rr_override) or minimum_rr_override < 1.5:
            raise ValueError("Minimum after-cost net R:R override cannot be below 1.5")
        overrides["minimum_net_risk_reward"] = minimum_rr_override
    if any(not isfinite(value) or value <= 0 for value in overrides.values()):
        raise ValueError("Paper capital, risk and net R:R overrides must be finite and positive")
    if paper_slippage_bps_per_side is not None:
        slippage_override = float(paper_slippage_bps_per_side)
        if not isfinite(slippage_override) or not 5 <= slippage_override <= 100:
            raise ValueError("Paper slippage override must be finite and between 5 and 100 bps per side")
        overrides["slippage_bps_per_side"] = slippage_override
    if overrides: risk_settings = replace(risk_settings, **overrides)
    bundles: dict[str, dict[str, Any]] = {}
    fetch_errors: list[str] = []
    for symbol in selected:
        try:
            if provider_name == "kite":
                instrument = instruments_by_symbol.get(symbol)
                if instrument is None:
                    fetch_errors.append(f"{symbol}: not found as an NSE equity in the current Kite instrument list")
                    continue
                if mode == "nse_equity":
                    bundle = provider.fetch_symbol_bundle(
                        instrument,
                        now=current,
                        quote_data=universe_quotes.get(symbol),
                        include_daily_candles=True,
                        relative_volume_lookback_days=market_safety_settings.relative_volume_lookback_calendar_days,
                    )
                else:
                    bundle = provider.fetch_symbol_bundle(
                        instrument,
                        now=current,
                        relative_volume_lookback_days=market_safety_settings.relative_volume_lookback_calendar_days,
                    )
                # In universe mode, the bulk quote is reused rather than issuing
                # another per-symbol quote call. For live scans, verify freshness
                # against the observation time immediately after the bulk quote request.
                # An explicitly injected 'now' remains deterministic for tests/replays.
                quote_check_time = datetime.now(IST) if now is None else current
                verified, quote_warning = _kite_quote_verification(
                    bundle.get("quote"), quote_check_time
                )
                bundle["provider"] = stock_provider_label
                bundle["verified_live_data"] = verified
                bundle["data_quality_warning"] = quote_warning
            else:
                bundle = provider.fetch_symbol_bundle(symbol, now=current)
                bundle["provider"] = stock_provider_label
                bundle["verified_live_data"] = False
            bundles[symbol] = bundle
        except Exception as exc:
            fetch_errors.append(f"{symbol}: {type(exc).__name__}: {exc}")
            LOGGER.warning("Skipping %s after data error: %s", symbol, exc)

    for symbol, bundle in bundles.items():
        bundle["surveillance"] = surveillance_status_for_symbol(symbol, surveillance_snapshot)
        bundle.setdefault("mis_eligibility", {
            "status": "NOT_CHECKED",
            "eligible": None,
            "interpretation": "No MIS margin calculation has been attempted.",
        })

    archive_results: list[dict[str, Any]] = []
    archive_errors: list[str] = []
    if archive_symbols and provider_name != "kite":
        raise ValueError("archive_symbols require the authenticated Kite provider")
    if candle_archive_dir is not None:
        for symbol, bundle in bundles.items():
            try:
                archived = append_completed_candles(
                    symbol, bundle.get("candles_5m", []), candle_archive_dir,
                    now=current, provider_mode=provider_name,
                )
                archive_results.append(asdict(archived))
            except Exception as exc:
                message = f"{symbol}: {type(exc).__name__}: {exc}"
                archive_errors.append(message)
                LOGGER.warning("Could not archive paper candles for %s: %s", symbol, message)

        # Retain candle history for active research signals even when the
        # liquidity screener drops a symbol from the current technical universe.
        extra_archive_symbols = sorted({
            str(value).strip().upper() for value in (archive_symbols or [])
            if str(value).strip()
        } - set(bundles))
        for symbol in extra_archive_symbols:
            instrument = instruments_by_symbol.get(symbol)
            if instrument is None:
                message = f"{symbol}: open research symbol is not in the current NSE EQ instrument list"
                archive_errors.append(message)
                LOGGER.warning("Could not refresh candle archive for %s: %s", symbol, message)
                continue
            try:
                archive_bundle = provider.fetch_symbol_bundle(
                    instrument, now=current, include_daily_candles=False,
                    include_relative_volume_history=False,
                )
                archived = append_completed_candles(
                    symbol, archive_bundle.get("candles_5m", []), candle_archive_dir,
                    now=current, provider_mode="kite",
                )
                archive_results.append(asdict(archived))
            except Exception as exc:
                message = f"{symbol}: {type(exc).__name__}: {exc}"
                archive_errors.append(message)
                LOGGER.warning("Could not refresh candle archive for open research symbol %s: %s", symbol, message)

    general_news = list(news_result.get("items", []))
    candidates = [
        evaluate_symbol_bundle(
            symbol, bundle, index_15m_candles=index_15m,
            news_items=general_news, margin_per_share=None,
            now=current, settings=risk_settings, charge_rates=charge_rates,
        )
        for symbol, bundle in bundles.items()
    ]

    # Margin estimation is read-only: this requests a hypothetical one-share
    # MIS limit-order margin calculation and never submits an order.
    margin_per_share_by_symbol: dict[str, float] = {}
    margin_checks: list[dict[str, Any]] = []
    if provider_name == "kite":
        margin_validation_time = datetime.now(IST) if now is None else current
        for candidate in candidates:
            if not candidate.side or not candidate.reference_entry:
                continue
            try:
                quote_bundle = bundles.get(candidate.symbol, {})
                if not quote_bundle.get("verified_live_data"):
                    raise ValueError("Kite quote did not pass live freshness verification.")
                executable_quote = inspect_executable_quote(
                    quote_bundle.get("quote"),
                    candidate.side,
                    now=margin_validation_time,
                    max_age_minutes=universe_max_quote_age_minutes,
                )
                if not executable_quote.get("valid"):
                    raise ValueError(str(executable_quote.get("reason") or "Executable bid/ask is unavailable."))
                margin_entry_price = float(executable_quote["executable_entry_price"])
                raw_margin_value = provider.estimate_mis_margin_per_share(
                    candidate.symbol, candidate.side, margin_entry_price
                )
                try:
                    margin_value = float(raw_margin_value)
                except (TypeError, ValueError, OverflowError) as exc:
                    raise ValueError(
                        "Kite MIS margin estimate must be a finite positive number."
                    ) from exc
                if not isfinite(margin_value) or margin_value <= 0:
                    raise ValueError(
                        "Kite MIS margin estimate must be a finite positive number."
                    )
                margin_per_share_by_symbol[candidate.symbol] = margin_value
                mis_status = {
                    "status": "MIS_MARGIN_ESTIMATE_AVAILABLE",
                    "eligible": True,
                    "verified": True,
                    "product": "MIS",
                    "margin_per_share_inr": round(margin_value, 4),
                    "method": "Read-only Kite order_margins; hypothetical one-share LIMIT calculation",
                    "interpretation": "Kite returned a positive MIS margin estimate; this is not a guarantee of eventual order acceptance.",
                }
                quote_bundle["mis_eligibility"] = mis_status
                margin_checks.append({
                    "symbol": candidate.symbol,
                    "side": candidate.side,
                    "status": "MIS_MARGIN_ESTIMATE_AVAILABLE",
                    "verified": True,
                    "margin_per_share_inr": round(margin_value, 4),
                    "entry_price_used": round(margin_entry_price, 4),
                    "entry_price_source": executable_quote["entry_price_source"],
                })
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                mis_status = {
                    "status": "MIS_ELIGIBILITY_UNVERIFIED",
                    "eligible": None,
                    "verified": False,
                    "product": "MIS",
                    "reason": message,
                    "method": "Read-only Kite order_margins; hypothetical one-share LIMIT calculation",
                }
                quote_bundle["mis_eligibility"] = mis_status
                margin_checks.append({
                    "symbol": candidate.symbol, "side": candidate.side,
                    "status": "MIS_ELIGIBILITY_UNVERIFIED",
                    "verified": False, "reason": message,
                })
                LOGGER.warning("MIS margin estimate unavailable for %s: %s", candidate.symbol, message)

        # Re-evaluate only with explicit positive results from Kite's calculator.
        candidates = [
            evaluate_symbol_bundle(
                symbol, bundle, index_15m_candles=index_15m,
                news_items=general_news,
                margin_per_share=margin_per_share_by_symbol.get(symbol),
                now=current, settings=risk_settings, charge_rates=charge_rates,
            )
            for symbol, bundle in bundles.items()
        ]

    # After the first technical pass, fetch company-specific RSS only for symbols
    # that actually produced a directional setup. This limits requests to the
    # small candidate set instead of querying every watchlist constituent.
    company_news_by_symbol: dict[str, list[dict[str, Any]]] = {}
    preliminary = [c for c in rank_candidates(candidates) if c.side is not None][:5]
    for candidate in preliminary:
        try:
            company_query = f'"{candidate.symbol}" stock NSE results earnings order'
            company_result = fetch_news(
                [google_news_search_url(company_query)], timeout_seconds=3.0, per_feed_limit=12
            )
            company_news_by_symbol[candidate.symbol] = company_result.get("items", [])
            news_result.setdefault("errors", []).extend(company_result.get("errors", []))
        except Exception as exc:
            company_news_by_symbol[candidate.symbol] = []
            news_result.setdefault("errors", []).append(f"Company news {candidate.symbol}: {exc}")

    if company_news_by_symbol:
        news_result["company_news_checked"] = sorted(company_news_by_symbol)
        news_result["items"] = general_news + [
            item for items in company_news_by_symbol.values() for item in items
        ]
        revised = []
        for candidate in candidates:
            if candidate.symbol not in company_news_by_symbol:
                revised.append(candidate)
                continue
            revised.append(evaluate_symbol_bundle(
                candidate.symbol, bundles[candidate.symbol], index_15m_candles=index_15m,
                news_items=general_news + company_news_by_symbol[candidate.symbol],
                margin_per_share=margin_per_share_by_symbol.get(candidate.symbol), now=current, settings=risk_settings,
                charge_rates=charge_rates,
            ))
        candidates = revised

    # Refresh directional candidate quotes after company-news triage. Market-wide
    # scans take longer than watchlist scans, so the initial bulk quote is not used
    # as the final executable paper-entry reference.
    entry_quote_refresh_checks: list[dict[str, Any]] = []
    entry_quote_refresh_errors: list[str] = []
    if provider_name == "kite":
        directional_symbols = list(dict.fromkeys(c.symbol for c in candidates if c.side is not None))
        refreshed_quotes: dict[str, Any] = {}
        if directional_symbols:
            try:
                refreshed_quotes = provider.quotes(directional_symbols)
            except Exception as exc:
                entry_quote_refresh_errors.append(f"{type(exc).__name__}: {exc}")
                LOGGER.warning("Final executable quote refresh failed: %s", exc)

        refresh_time = datetime.now(IST) if now is None else current
        for candidate in candidates:
            if candidate.side is None:
                continue
            bundle = bundles.get(candidate.symbol, {})
            latest_quote = refreshed_quotes.get(f"NSE:{candidate.symbol}")
            if not isinstance(latest_quote, dict):
                bundle["quote"] = None
                bundle["verified_live_data"] = False
                bundle["data_quality_warning"] = "Final executable quote refresh returned no quote."
                bundle["mis_eligibility"] = {
                    "status": "MIS_ELIGIBILITY_UNVERIFIED", "eligible": None, "verified": False,
                    "product": "MIS", "reason": "Final executable quote refresh returned no quote.",
                }
                margin_per_share_by_symbol.pop(candidate.symbol, None)
                entry_quote_refresh_checks.append({"symbol": candidate.symbol, "verified": False,
                    "reason": "Final executable quote refresh returned no quote."})
                continue

            bundle["quote"] = latest_quote
            quote_verified, quote_warning = _kite_quote_verification(
                latest_quote, refresh_time, max_age_minutes=paper_entry_max_quote_age_minutes
            )
            bundle["verified_live_data"] = quote_verified
            bundle["data_quality_warning"] = quote_warning
            audit: dict[str, Any] = {
                "symbol": candidate.symbol, "verified": quote_verified,
                "quote_timestamp": latest_quote.get("timestamp") or latest_quote.get("last_trade_time"),
                "reason": quote_warning or "",
            }
            if not quote_verified:
                bundle["mis_eligibility"] = {
                    "status": "MIS_ELIGIBILITY_UNVERIFIED", "eligible": None, "verified": False,
                    "product": "MIS", "reason": quote_warning or "Final executable quote failed verification.",
                }
                margin_per_share_by_symbol.pop(candidate.symbol, None)
                entry_quote_refresh_checks.append(audit)
                continue

            circuit_check = inspect_circuit_limits(
                latest_quote,
                buffer_pct=market_safety_settings.circuit_near_band_buffer_pct,
            )
            audit["circuit_limits"] = circuit_check
            if not circuit_check.get("eligible"):
                bundle["verified_live_data"] = False
                bundle["data_quality_warning"] = (
                    "Final quote failed the circuit-limit safety gate: "
                    + str(circuit_check.get("reason") or circuit_check.get("status"))
                )
                bundle["mis_eligibility"] = {
                    "status": "MIS_ELIGIBILITY_UNVERIFIED", "eligible": None, "verified": False,
                    "product": "MIS", "reason": bundle["data_quality_warning"],
                }
                bundle["mis_eligibility"] = {
                    "status": "MIS_ELIGIBILITY_UNVERIFIED", "eligible": None, "verified": False,
                    "product": "MIS", "reason": bundle["data_quality_warning"],
                }
                margin_per_share_by_symbol.pop(candidate.symbol, None)
                audit["verified"] = False
                audit["reason"] = bundle["data_quality_warning"]
                entry_quote_refresh_checks.append(audit)
                continue

            executable = inspect_executable_quote(
                latest_quote, candidate.side, now=refresh_time,
                max_age_minutes=paper_entry_max_quote_age_minutes,
            )
            audit.update({"bid": executable.get("bid"), "ask": executable.get("ask"),
                          "executable_quote_valid": executable.get("valid") is True})
            if not executable.get("valid"):
                bundle["mis_eligibility"] = {
                    "status": "MIS_ELIGIBILITY_UNVERIFIED", "eligible": None, "verified": False,
                    "product": "MIS", "reason": executable.get("reason") or "Executable quote validation failed.",
                }
                margin_per_share_by_symbol.pop(candidate.symbol, None)
                audit["reason"] = executable.get("reason")
                entry_quote_refresh_checks.append(audit)
                continue

            executable_side_circuit = inspect_circuit_limits(
                {
                    **latest_quote,
                    "last_price": executable["executable_entry_price"],
                },
                buffer_pct=market_safety_settings.circuit_near_band_buffer_pct,
            )
            audit["executable_side_circuit_limits"] = executable_side_circuit
            if not executable_side_circuit.get("eligible"):
                bundle["verified_live_data"] = False
                bundle["data_quality_warning"] = (
                    "Executable bid/ask is at or near an exchange circuit band: "
                    + str(executable_side_circuit.get("reason") or executable_side_circuit.get("status"))
                )
                margin_per_share_by_symbol.pop(candidate.symbol, None)
                audit["verified"] = False
                audit["reason"] = bundle["data_quality_warning"]
                entry_quote_refresh_checks.append(audit)
                continue

            try:
                margin_value = float(provider.estimate_mis_margin_per_share(
                    candidate.symbol, candidate.side, float(executable["executable_entry_price"])
                ))
                if not isfinite(margin_value) or margin_value <= 0:
                    raise ValueError("Kite returned a non-positive or non-finite MIS margin estimate.")
                margin_per_share_by_symbol[candidate.symbol] = margin_value
                mis_status = {
                    "status": "MIS_MARGIN_ESTIMATE_AVAILABLE",
                    "eligible": True,
                    "verified": True,
                    "product": "MIS",
                    "margin_per_share_inr": round(margin_value, 4),
                    "method": "Read-only Kite order_margins; hypothetical one-share LIMIT calculation",
                    "interpretation": "Kite returned a positive MIS margin estimate; this is not a guarantee of eventual order acceptance.",
                }
                mis_eligibility_by_symbol[candidate.symbol] = mis_status
                bundle["mis_eligibility"] = mis_status
                audit.update({"margin_verified": True, "mis_eligibility": mis_status, "margin_per_share_inr": round(margin_value, 4),
                              "entry_price_used": round(float(executable["executable_entry_price"]), 4)})
            except Exception as exc:
                margin_per_share_by_symbol.pop(candidate.symbol, None)
                mis_status = {
                    "status": "MIS_ELIGIBILITY_UNVERIFIED",
                    "eligible": None,
                    "verified": False,
                    "product": "MIS",
                    "reason": f"MIS margin refresh failed: {type(exc).__name__}: {exc}",
                    "method": "Read-only Kite order_margins; hypothetical one-share LIMIT calculation",
                }
                mis_eligibility_by_symbol[candidate.symbol] = mis_status
                bundle["mis_eligibility"] = mis_status
                audit.update({"margin_verified": False, "mis_eligibility": mis_status,
                              "reason": mis_status["reason"]})
            entry_quote_refresh_checks.append(audit)

        # Timestamp the final validation after quote and margin refresh.
        entry_validation_time = datetime.now(IST) if now is None else current
        candidates = [
            revalidate_candidate_at_quote(
                candidate,
                bundles.get(candidate.symbol, {}).get("quote"),
                margin_per_share=margin_per_share_by_symbol.get(candidate.symbol),
                settings=risk_settings,
                charge_rates=charge_rates,
                quote_verified=(
                    bundles.get(candidate.symbol, {}).get("verified_live_data") is True
                    and _kite_quote_verification(
                        bundles.get(candidate.symbol, {}).get("quote"), entry_validation_time,
                        max_age_minutes=paper_entry_max_quote_age_minutes,
                    )[0]
                ),
                now=entry_validation_time,
                max_quote_age_minutes=paper_entry_max_quote_age_minutes,
            )
            for candidate in candidates
        ]
    else:
        entry_validation_time = datetime.now(IST) if now is None else current

    ranked = rank_candidates(candidates)
    scan_completed_at = entry_validation_time.isoformat()
    entry_status_counts: dict[str, int] = {}
    for candidate in candidates:
        validation_status = candidate.entry_validation.get("status", "NOT_APPLICABLE")
        entry_status_counts[validation_status] = entry_status_counts.get(validation_status, 0) + 1
    report = {
        "status": "RESEARCH_SCAN_ONLY",
        "as_of": scan_completed_at,
        "scan_started_at": current.isoformat(),
        "scan_completed_at": scan_completed_at,
        "as_of_semantics": "Report completion time; each candidate retains its own quote timestamp.",
        "provider": stock_provider_label,
        "stock_data_provider": stock_provider_label,
        "index_regime_provider": index_provider_label,
        "margin_checks": margin_checks,
        "entry_validation_summary": entry_status_counts,
        "data_provider_mode": provider_name,
        "candle_archive": ({
            "directory": str(candle_archive_dir),
            "results": archive_results,
            "errors": archive_errors,
            "provider_pure": True,
            "additional_symbols_requested": sorted({str(x).strip().upper() for x in (archive_symbols or []) if str(x).strip()}),
        } if candle_archive_dir is not None else {"enabled": False}),
        "advisory_only": True,
        "orders_placed_or_modified": False,
        "risk_settings": {
            "capital_inr": risk_settings.capital_inr,
            "risk_per_trade_inr": risk_settings.risk_per_trade_inr,
            "minimum_net_risk_reward": risk_settings.minimum_net_risk_reward,
            "slippage_bps_per_side": risk_settings.slippage_bps_per_side,
            "entry_cutoff_ist": risk_settings.entry_cutoff_ist,
            "square_off_time_ist": risk_settings.square_off_time_ist,
            "require_margin_verification": risk_settings.require_margin_verification,
        },
        "universe_selection": universe_selection,
        "account_profile": account_profile,
        "market_calendar": market_day_status(current.date()),
        "calendar_maintenance": calendar_maintenance,
        "surveillance_snapshot": {
            "status": surveillance_snapshot.get("status"),
            "source": surveillance_snapshot.get("source"),
            "source_url": surveillance_snapshot.get("source_url"),
            "as_of": surveillance_snapshot.get("as_of"),
            "reason": surveillance_snapshot.get("reason"),
            "max_age_calendar_days": 3,
            "not_instrument_dump": True,
        },
        "market_safety_settings": asdict(market_safety_settings),
        "symbols_requested": selected,
        "entry_quote_refresh": entry_quote_refresh_checks,
        "entry_quote_refresh_errors": entry_quote_refresh_errors,
        "paper_entry_max_quote_age_minutes": paper_entry_max_quote_age_minutes,
        "symbols_evaluated": len(candidates),
        "fetch_errors": fetch_errors,
        "news": news_result,
        "candidates": [item.to_dict() for item in ranked],
        "market_snapshots": ({symbol: _paper_market_snapshot(bundle) for symbol, bundle in bundles.items() if _paper_market_snapshot(bundle)} if provider_name == "kite" else {}),
        "limitations": [
            ("Yahoo Finance stock data is unofficial and may be delayed or incomplete; it is not verified for live decisions."
             if provider_name == "yahoo" else
             "Kite stock quote freshness and stock candles are checked; the NIFTY 50 index candles are also requested from Kite with no Yahoo fallback."),
            ("Kite's read-only MIS margin calculator is attempted for directional candidates; missing or invalid results remain fail-closed."
             if provider_name == "kite" else
             "The Yahoo-only path does not verify Kite MIS margin; candidates remain fail-closed."),
            "RSS keyword triage is not a sentiment model and does not replace original company filings or human verification.",
            "This report is advisory-only and never submits, modifies, or cancels orders.",
        ],
    }
    directory = output_dir or Path("reports")
    directory.mkdir(parents=True, exist_ok=True)
    filename = f"phase6_scan_{current.strftime('%Y%m%d_%H%M%S')}.json"
    (directory / filename).write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    report["report_path"] = str(directory / filename)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="NSE advisory-only market scanner (research data mode)")
    parser.add_argument("--symbols", help="Comma-separated NSE symbols, e.g. RELIANCE,HDFCBANK,INFY")
    parser.add_argument("--universe", choices=("watchlist", "nse_equity"), default="watchlist",
                        help="watchlist scans configured symbols; nse_equity screens available Kite NSE cash equities by fresh quote/liquidity")
    parser.add_argument("--universe-max-symbols", type=int, default=50,
                        help="Maximum liquid NSE equities to deeply analyse in nse_equity mode (default: 50)")
    parser.add_argument("--universe-min-price", type=float, default=50.0)
    parser.add_argument("--universe-max-price", type=float, default=10000.0)
    parser.add_argument("--universe-min-volume", type=int, default=10000,
                        help="Minimum current-day traded volume from the Kite quote")
    parser.add_argument("--universe-min-turnover", type=float, default=5000000.0,
                        help="Minimum average_price × current-day volume proxy in INR")
    parser.add_argument("--universe-max-quote-age-minutes", type=float, default=15.0)
    parser.add_argument("--paper-entry-max-quote-age-minutes", type=float, default=2.0,
                        help="Maximum age for the final refreshed executable entry quote")
    parser.add_argument("--max-symbols", type=int, default=10, help="Maximum configured symbols in watchlist mode (default: 10)")
    parser.add_argument("--watchlist", type=Path, default=Path("config/watchlist.yaml"))
    parser.add_argument("--news-feeds", type=Path, default=Path("config/news_feeds.yaml"))
    parser.add_argument("--output-dir", type=Path, default=Path("reports"))
    parser.add_argument("--provider", choices=("yahoo", "kite"), default="yahoo", help="Market-data source; kite uses the authenticated read-only Kite Connect API and cached daily session")
    parser.add_argument("--paper-journal", type=Path, default=None, help="Optional CSV path to append directional scan observations for prospective paper review (never places orders)")
    parser.add_argument("--scan-ledger", type=Path, default=None, help="Optional append-only CSV of scan health and candidate counts; defaults to logs/paper_scan_runs.csv when --paper-journal is enabled")
    parser.add_argument("--candle-archive-dir", type=Path, default=None, help="Optional directory for a provider-pure archive of completed 5-minute candles; enabled automatically at data/paper_candles when --paper-journal is set")
    parser.add_argument("--archive-symbols", default="", help="Additional Kite NSE EQ symbols with open/pending research outcomes; archived but not added to the scan universe")
    parser.add_argument("--profile", choices=("research", "paper", "live-advisory"), default="research", help="Named account/risk profile; CLI capital/risk flags override it")
    parser.add_argument("--account-capital", type=float, default=None, help="Optional capital override for the selected profile")
    parser.add_argument("--risk-per-trade", type=float, default=None, help="Optional virtual account planned risk per trade")
    parser.add_argument("--minimum-net-rr", type=float, default=None, help="Optional minimum after-cost reward/risk gate")
    parser.add_argument("--slippage-bps-per-side", type=float, default=None, help="Optional slippage assumption override (5-100 bps per side)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")
    if args.universe == "nse_equity" and args.provider != "kite":
        parser.error("--universe nse_equity requires --provider kite")
    if args.universe_max_symbols < 1:
        parser.error("--universe-max-symbols must be at least 1")
    symbols = (
        []
        if args.universe == "nse_equity"
        else ([x.strip().upper() for x in args.symbols.split(",") if x.strip()] if args.symbols else load_watchlist(args.watchlist))
    )
    candle_archive_dir = args.candle_archive_dir
    if args.paper_journal is not None and candle_archive_dir is None:
        candle_archive_dir = Path("data/paper_candles")
    report = run_scan(
        symbols=symbols, max_symbols=args.max_symbols, watchlist_path=args.watchlist,
        news_feeds_path=args.news_feeds, output_dir=args.output_dir,
        data_provider=args.provider, candle_archive_dir=candle_archive_dir,
        paper_capital_inr=args.account_capital, paper_risk_per_trade_inr=args.risk_per_trade,
        paper_minimum_net_rr=args.minimum_net_rr,
        account_profile=args.profile,
        paper_slippage_bps_per_side=args.slippage_bps_per_side,
        universe_mode=args.universe,
        universe_max_symbols=args.universe_max_symbols,
        universe_min_price_inr=args.universe_min_price,
        universe_max_price_inr=args.universe_max_price,
        universe_min_current_day_volume=args.universe_min_volume,
        universe_min_current_day_turnover_inr=args.universe_min_turnover,
        universe_max_quote_age_minutes=args.universe_max_quote_age_minutes,
        paper_entry_max_quote_age_minutes=args.paper_entry_max_quote_age_minutes,
        archive_symbols=[x.strip().upper() for x in args.archive_symbols.split(",") if x.strip()],
    )
    journal_result = None
    if args.paper_journal is not None and report.get("report_path"):
        journal_result = append_scan_report(Path(report["report_path"]), args.paper_journal)
        print(f"Paper journal: {journal_result['journal_path']} (new events: {journal_result['new_events']}, duplicates skipped: {journal_result['duplicates_skipped']})")
    scan_ledger_path = args.scan_ledger
    if scan_ledger_path is None and args.paper_journal is not None:
        scan_ledger_path = Path("logs/paper_scan_runs.csv")
    if scan_ledger_path is not None and report.get("report_path"):
        ledger_result = append_scan_ledger(
            Path(report["report_path"]),
            scan_ledger_path,
            signal_journal_result=journal_result,
        )
        print(
            f"Paper scan ledger: {ledger_result['ledger_path']} "
            f"(new runs: {ledger_result['new_runs']}, "
            f"directional candidates: {ledger_result['directional_candidates']}, "
            f"no-trade candidates: {ledger_result['no_trade_candidates']}, "
            f"margin checks: {ledger_result['margin_checks_attempted']}, "
            f"fetch errors: {ledger_result['fetch_error_count']})"
        )
    print(f"Status: {report['status']}")
    print(f"As of: {report['as_of']}")
    if report.get("report_path"):
        print(f"Report: {report['report_path']}")
    if report.get("message"):
        print(report["message"])
    for item in report.get("candidates", []):
        validation = item.get("entry_validation") or {}
        executable_entry = validation.get("executable_entry_price")
        entry_text = f" entry=INR {float(executable_entry):.2f}" if executable_entry is not None else ""
        validation_text = f" live={validation.get('status')}" if validation.get("status") else ""
        print(f"{item['symbol']:<14} {item['action']:<18} side={item['side'] or '-':<5} confidence={item['confidence']:>2} strategy={item['strategy'] or '-'}{entry_text}{validation_text}")
    print("No orders were placed, modified, or cancelled.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
