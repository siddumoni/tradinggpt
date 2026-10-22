"""Phase 6 CLI: scan stocks, collect RSS headlines, and write advisory-only reports.

Example (Yahoo development data; not a verified live feed):
    python -m src.advisory_engine --symbols RELIANCE,HDFCBANK,ICICIBANK,SBIN,INFY

This command never places, modifies, or cancels orders.
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from src.market_scanner import evaluate_symbol_bundle, rank_candidates
from src.news_events import DEFAULT_FEEDS, fetch_news, google_news_search_url
from src.risk_engine import load_phase2_settings
from src.paper_journal import append_scan_report

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


def run_scan(
    *, symbols: list[str], now: datetime | None = None, max_symbols: int | None = None,
    watchlist_path: Path | None = None, news_feeds_path: Path | None = None,
    output_dir: Path | None = None, data_provider: str = "yahoo",
) -> dict[str, Any]:
    current = now or datetime.now(IST)
    current = current.astimezone(IST) if current.tzinfo else current.replace(tzinfo=IST)
    if current.weekday() >= 5 or current.time() < dtime(9, 15) or current.time() > dtime(15, 30):
        return {"status": "MARKET_CLOSED", "as_of": current.isoformat(), "message": "NSE cash-market hours are normally 09:15–15:30 IST on weekdays. No intraday scan was run.", "candidates": [], "news": {"items": [], "errors": []}}

    provider_name = str(data_provider).strip().lower()
    if provider_name not in {"yahoo", "kite"}:
        raise ValueError("data_provider must be 'yahoo' or 'kite'")
    selected = list(dict.fromkeys(s.strip().upper() for s in symbols if s.strip()))
    if max_symbols is not None:
        selected = selected[:max_symbols]
    if not selected and watchlist_path:
        selected = load_watchlist(watchlist_path)
        if max_symbols is not None:
            selected = selected[:max_symbols]
    if not selected:
        raise ValueError("No symbols supplied and no usable watchlist was found")

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
            from src.data_fetcher import KiteMarketData
            provider = KiteMarketData.from_cached_session()
            instrument_rows = provider.instruments(now=current)
            instruments_by_symbol = {
                str(row.get("tradingsymbol", "")).upper(): row
                for row in instrument_rows
                if row.get("segment") == "NSE" and row.get("instrument_type") == "EQ"
                and row.get("tradingsymbol")
            }
            stock_provider_label = "Zerodha Kite Connect authenticated NSE equity API; quote freshness checked"
        except Exception as exc:
            raise RuntimeError(
                "Could not initialise the read-only Kite data provider. Run 'python -m src.main login' "
                f"and check credentials/session, instrument access and connectivity. Details: {type(exc).__name__}: {exc}"
            ) from exc

    news_result = fetch_news(_load_feeds(news_feeds_path), timeout_seconds=5.0)
    # Headlines are triage inputs only. Per-symbol company RSS queries are added for
    # the most promising technical candidates in a later iteration after this scan.
    try:
        index_15m = _fetch_nifty_15m(current)
    except Exception as exc:
        LOGGER.warning("Could not fetch NIFTY index candles: %s", exc)
        index_15m = []
        news_result.setdefault("errors", []).append(f"NIFTY index data unavailable: {exc}")

    risk_settings, charge_rates = load_phase2_settings()
    bundles: dict[str, dict[str, Any]] = {}
    fetch_errors: list[str] = []
    for symbol in selected:
        try:
            if provider_name == "kite":
                instrument = instruments_by_symbol.get(symbol)
                if instrument is None:
                    fetch_errors.append(f"{symbol}: not found as an NSE equity in the current Kite instrument list")
                    continue
                bundle = provider.fetch_symbol_bundle(instrument, now=current)
                verified, quote_warning = _kite_quote_verification(bundle.get("quote"), current)
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

    general_news = list(news_result.get("items", []))
    candidates = [
        evaluate_symbol_bundle(
            symbol, bundle, index_15m_candles=index_15m,
            news_items=general_news, margin_per_share=None,
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
                margin_per_share=None, now=current, settings=risk_settings,
                charge_rates=charge_rates,
            ))
        candidates = revised

    ranked = rank_candidates(candidates)
    report = {
        "status": "RESEARCH_SCAN_ONLY",
        "as_of": current.isoformat(),
        "provider": stock_provider_label,
        "stock_data_provider": stock_provider_label,
        "index_regime_provider": "Yahoo Finance unofficial NIFTY 50 index history; not verified live exchange data",
        "data_provider_mode": provider_name,
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
        "symbols_requested": selected,
        "symbols_evaluated": len(candidates),
        "fetch_errors": fetch_errors,
        "news": news_result,
        "candidates": [item.to_dict() for item in ranked],
        "limitations": [
            ("Yahoo Finance stock data is unofficial and may be delayed or incomplete; it is not verified for live decisions."
             if provider_name == "yahoo" else
             "Kite stock quote freshness is checked against its timestamp, but index-regime context still uses unofficial Yahoo Finance data."),
            "Margin was not verified, so the risk engine is fail-closed and no candidate can be approved for entry.",
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
    parser.add_argument("--max-symbols", type=int, default=10, help="Maximum symbols to scan (default: 10 to limit unofficial feed requests)")
    parser.add_argument("--watchlist", type=Path, default=Path("config/watchlist.yaml"))
    parser.add_argument("--news-feeds", type=Path, default=Path("config/news_feeds.yaml"))
    parser.add_argument("--output-dir", type=Path, default=Path("reports"))
    parser.add_argument("--provider", choices=("yahoo", "kite"), default="yahoo", help="Market-data source; kite uses the authenticated read-only Kite Connect API and cached daily session")
    parser.add_argument("--paper-journal", type=Path, default=None, help="Optional CSV path to append directional scan observations for prospective paper review (never places orders)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")
    symbols = [x.strip().upper() for x in args.symbols.split(",") if x.strip()] if args.symbols else load_watchlist(args.watchlist)
    report = run_scan(symbols=symbols, max_symbols=args.max_symbols, watchlist_path=args.watchlist, news_feeds_path=args.news_feeds, output_dir=args.output_dir, data_provider=args.provider)
    if args.paper_journal is not None and report.get("report_path"):
        journal_result = append_scan_report(Path(report["report_path"]), args.paper_journal)
        print(f"Paper journal: {journal_result['journal_path']} (new events: {journal_result['new_events']}, duplicates skipped: {journal_result['duplicates_skipped']})")
    print(f"Status: {report['status']}")
    print(f"As of: {report['as_of']}")
    if report.get("report_path"):
        print(f"Report: {report['report_path']}")
    if report.get("message"):
        print(report["message"])
    for item in report.get("candidates", []):
        print(f"{item['symbol']:<14} {item['action']:<18} side={item['side'] or '-':<5} confidence={item['confidence']:>2} strategy={item['strategy'] or '-'}")
    print("No orders were placed, modified, or cancelled.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
