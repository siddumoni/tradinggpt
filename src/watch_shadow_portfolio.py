"""Separate forward shadow book for directional candidates rejected only by the net-R:R gate.

This module is research-only: it reads saved Kite scan reports and market quotes,
records hypothetical fills/exits, and never calls any order-placement API.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import sqlite3
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.cost_calculator import estimate_intraday_round_trip_cost
from src.risk_engine import RiskSettings, assess_trade, load_phase2_settings

IST = ZoneInfo("Asia/Kolkata")
DEFAULT_DB = Path("data/paper_trading.sqlite3")
DEFAULT_CAPITAL_INR = 100_000.0
DEFAULT_RISK_PER_TRADE_INR = 500.0
DEFAULT_DAILY_LOSS_LIMIT_INR = 2_000.0
DEFAULT_MAX_OPEN_POSITIONS = 3
DEFAULT_SLIPPAGE_BPS = 5.0
MAX_QUOTE_AGE_SECONDS = 120.0
ENTRY_CUTOFF = dtime(14, 45)
SQUARE_OFF_TIME = dtime(15, 15)
LOGGER = logging.getLogger("paper_trading_service")


def _to_ist(value: Any) -> datetime:
    if isinstance(value, datetime):
        stamp = value
    else:
        stamp = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    return stamp.replace(tzinfo=IST) if stamp.tzinfo is None else stamp.astimezone(IST)


def _positive(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) and result > 0 else None


def _signal_id(candidate: dict[str, Any]) -> str:
    raw = "|".join(str(candidate.get(key, "")) for key in
                   ("symbol", "side", "strategy", "signal_time"))
    return hashlib.sha256(raw.upper().encode("utf-8")).hexdigest()[:32]


class WatchShadowPortfolio:
    """Track eligible watch candidates in an isolated cash-only virtual account."""

    def __init__(
        self,
        db_path: Path = DEFAULT_DB,
        *,
        initial_capital: float = DEFAULT_CAPITAL_INR,
        risk_per_trade: float = DEFAULT_RISK_PER_TRADE_INR,
        max_daily_loss: float = DEFAULT_DAILY_LOSS_LIMIT_INR,
        max_open_positions: int = DEFAULT_MAX_OPEN_POSITIONS,
        slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
        max_quote_age_seconds: float = MAX_QUOTE_AGE_SECONDS,
    ) -> None:
        for name, value in (
            ("initial_capital", initial_capital),
            ("risk_per_trade", risk_per_trade),
            ("max_daily_loss", max_daily_loss),
            ("max_quote_age_seconds", max_quote_age_seconds),
        ):
            if not math.isfinite(float(value)) or float(value) <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if isinstance(max_open_positions, bool) or int(max_open_positions) < 1:
            raise ValueError("max_open_positions must be a positive integer")
        if not math.isfinite(float(slippage_bps)) or not 0 <= float(slippage_bps) <= 100:
            raise ValueError("slippage_bps must be between 0 and 100")
        self.db_path = Path(db_path)
        self.initial_capital = float(initial_capital)
        self.risk_per_trade = float(risk_per_trade)
        self.max_daily_loss = float(max_daily_loss)
        self.max_open_positions = int(max_open_positions)
        self.slippage_bps = float(slippage_bps)
        self.max_quote_age_seconds = float(max_quote_age_seconds)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=15000")
        return db

    def _initialize(self) -> None:
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS watch_shadow_trades (
                    signal_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL CHECK(side IN ('LONG','SHORT')),
                    strategy TEXT NOT NULL,
                    signal_time TEXT NOT NULL,
                    scan_as_of TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('OPEN','CLOSED','EXCLUDED')),
                    entry_time TEXT,
                    entry_reference_price REAL,
                    entry_fill_price REAL,
                    stop_loss REAL,
                    target_1 REAL,
                    target_2 REAL,
                    quantity INTEGER NOT NULL DEFAULT 0,
                    position_value_inr REAL NOT NULL DEFAULT 0,
                    risk_budget_inr REAL NOT NULL,
                    assessed_net_rr_t1 REAL,
                    estimated_net_risk_inr REAL,
                    estimated_net_reward_t1_inr REAL,
                    source_rejection_reason TEXT NOT NULL,
                    exclusion_reason TEXT,
                    exit_time TEXT,
                    exit_reference_price REAL,
                    exit_fill_price REAL,
                    exit_reason TEXT,
                    exit_detail TEXT,
                    gross_pnl REAL,
                    brokerage REAL,
                    stt REAL,
                    exchange_charges REAL,
                    sebi_charges REAL,
                    gst REAL,
                    stamp_duty REAL,
                    ipft REAL,
                    transaction_charges REAL,
                    slippage_cost REAL,
                    total_estimated_costs REAL,
                    net_pnl REAL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_watch_shadow_status
                    ON watch_shadow_trades(status, entry_time);
                CREATE INDEX IF NOT EXISTS idx_watch_shadow_symbol
                    ON watch_shadow_trades(symbol, status);
                CREATE TABLE IF NOT EXISTS market_marks (
                    symbol TEXT PRIMARY KEY, last_price REAL NOT NULL, bid REAL, ask REAL,
                    market_time TEXT NOT NULL, received_at TEXT NOT NULL
                );
            """)

    def process_scan_report(self, report_path: Path) -> dict[str, int]:
        """Record each WATCH_CANDIDATE and open only net-R:R-only near misses."""
        path = Path(report_path)
        try:
            report = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Could not read scan report {path}: {exc}") from exc
        if not isinstance(report, dict) or report.get("status") != "RESEARCH_SCAN_ONLY":
            raise ValueError("Watch shadow book requires a RESEARCH_SCAN_ONLY JSON report")
        if report.get("orders_placed_or_modified") is not False:
            raise ValueError("Watch shadow book rejected report without explicit no-order assertion")
        if str(report.get("data_provider_mode", "")).lower() != "kite":
            raise ValueError("Watch shadow book only accepts Kite provider reports")
        candidates, snapshots = report.get("candidates", []), report.get("market_snapshots", {})
        if not isinstance(candidates, list) or not isinstance(snapshots, dict):
            raise ValueError("Scan report candidates or market_snapshots schema is invalid")
        as_of = str(report.get("as_of") or "")
        if not as_of:
            raise ValueError("Scan report is missing as_of timestamp")
        scan_time = _to_ist(as_of)
        counts = {"seen": 0, "opened": 0, "excluded": 0, "duplicates": 0}
        with self._connect() as db:
            for item in candidates:
                if not isinstance(item, dict) or str(item.get("action", "")).upper() != "WATCH_CANDIDATE":
                    continue
                counts["seen"] += 1
                signal_id = _signal_id(item)
                if db.execute("SELECT 1 FROM watch_shadow_trades WHERE signal_id=?", (signal_id,)).fetchone():
                    counts["duplicates"] += 1
                    continue
                exclusion = self._eligibility_failure(item, scan_time)
                quote = snapshots.get(str(item.get("symbol", "")).upper())
                quote_time = None
                entry_reference = entry_fill = None
                if not exclusion:
                    exclusion, quote_time, entry_reference, entry_fill = self._quote_entry(quote, str(item.get("side", "")).upper())
                if not exclusion:
                    exclusion = self._account_gate(db, str(item.get("symbol", "")).upper(), scan_time)
                if not exclusion:
                    exclusion = self._levels_failure(item, str(item.get("side", "")).upper(), entry_fill)
                if exclusion:
                    self._insert_excluded(db, item, signal_id, as_of, exclusion)
                    counts["excluded"] += 1
                    continue
                assert quote_time is not None and entry_reference is not None and entry_fill is not None
                opened = self._open(db, item, signal_id, as_of, quote_time, entry_reference, entry_fill)
                if opened:
                    counts["opened"] += 1
                else:
                    counts["excluded"] += 1
        return counts

    @staticmethod
    def _eligibility_failure(candidate: dict[str, Any], scan_time: datetime) -> str | None:
        reasons = candidate.get("reasons")
        reasons = reasons if isinstance(reasons, list) else []
        rr_reason = next((str(reason) for reason in reasons
                          if "target 1 net r:r is below" in str(reason).lower()), None)
        if not rr_reason:
            return "Not a minimum net-R:R-only rejection; kept out of this shadow track."
        if candidate.get("data_verified_live") is not True:
            return "Live data verification did not pass."
        if candidate.get("margin_verified") is not True:
            return "Read-only margin verification did not pass."
        if str(candidate.get("company_specific_risk", "")).upper() == "HIGH":
            return "High company-specific news risk; no hypothetical entry recorded."
        if str(candidate.get("news_status", "")).upper() in {"", "NOT_CHECKED", "UNAVAILABLE"}:
            return "News/event checks are incomplete; no hypothetical entry recorded."
        if scan_time.time().replace(tzinfo=None) >= ENTRY_CUTOFF:
            return "Scan was at or after the 14:45 IST no-new-entry cutoff."
        return None

    def _quote_entry(self, quote: Any, side: str):
        if side not in {"LONG", "SHORT"}:
            return "Candidate side is missing or invalid.", None, None, None
        if not isinstance(quote, dict) or quote.get("verified_live") is not True:
            return "Quote snapshot is not explicitly verified as live.", None, None, None
        raw_time = quote.get("timestamp") or quote.get("last_trade_time")
        if not raw_time:
            return "Quote timestamp is missing.", None, None, None
        try:
            quote_time = _to_ist(raw_time)
        except (TypeError, ValueError, OverflowError):
            return "Quote timestamp is invalid.", None, None, None
        age = (datetime.now(IST) - quote_time).total_seconds()
        if age < -2 or age > self.max_quote_age_seconds:
            return f"Quote is stale or future-dated ({age:.1f}s old).", None, None, None
        last = _positive(quote.get("last_price"))
        bid = _positive(quote.get("bid"))
        ask = _positive(quote.get("ask"))
        if last is None:
            return "Quote has no valid last price.", None, None, None
        if bid is None or ask is None or bid > ask:
            return "Verified best bid/ask is missing or crossed; no shadow fill fabricated.", None, None, None
        reference = ask if side == "LONG" else bid
        fill = reference * (1 + self.slippage_bps / 10_000) if side == "LONG" else reference * (1 - self.slippage_bps / 10_000)
        return None, quote_time, reference, fill

    def _account_gate(self, db: sqlite3.Connection, symbol: str, scan_time: datetime) -> str | None:
        opened = list(db.execute("SELECT * FROM watch_shadow_trades WHERE status='OPEN'"))
        if len(opened) >= self.max_open_positions:
            return f"Watch-shadow open-position limit ({self.max_open_positions}) reached."
        if any(str(row["symbol"]).upper() == symbol for row in opened):
            return "A watch-shadow position is already open for this symbol."
        day = scan_time.date().isoformat()
        realised = db.execute(
            "SELECT COALESCE(SUM(net_pnl),0) FROM watch_shadow_trades WHERE status='CLOSED' AND substr(exit_time,1,10)=?",
            (day,),
        ).fetchone()[0]
        if float(realised or 0) <= -self.max_daily_loss:
            return f"Watch-shadow daily loss limit reached ({float(realised):.2f} INR)."
        reserved = sum(float(row["position_value_inr"] or 0) for row in opened)
        closed_pnl = db.execute("SELECT COALESCE(SUM(net_pnl),0) FROM watch_shadow_trades WHERE status='CLOSED'").fetchone()[0]
        if self.initial_capital + float(closed_pnl or 0) - reserved <= 0:
            return "Watch-shadow virtual cash is exhausted."
        return None

    @staticmethod
    def _levels_failure(candidate: dict[str, Any], side: str, entry_fill: float | None) -> str | None:
        stop, t1, t2 = (_positive(candidate.get(key)) for key in ("stop_loss", "target_1", "target_2"))
        if entry_fill is None or stop is None or t1 is None or t2 is None:
            return "Candidate has missing/invalid stop or target levels."
        valid = stop < entry_fill < t1 <= t2 if side == "LONG" else t2 <= t1 < entry_fill < stop
        return None if valid else "Current quote invalidated the saved stop/target entry window."

    def _insert_excluded(self, db: sqlite3.Connection, candidate: dict[str, Any],
                         signal_id: str, as_of: str, reason: str) -> None:
        now = datetime.now(IST).isoformat()
        symbol = str(candidate.get("symbol") or "UNKNOWN").strip().upper()
        side = str(candidate.get("side") or "").upper()
        if side not in {"LONG", "SHORT"}:
            side = "LONG"
        def level(key: str):
            return _positive(candidate.get(key))
        source = next((str(r) for r in candidate.get("reasons", [])
                       if "target 1 net r:r is below" in str(r).lower()), "WATCH_CANDIDATE")
        db.execute("""
            INSERT INTO watch_shadow_trades(
                signal_id,symbol,side,strategy,signal_time,scan_as_of,status,stop_loss,target_1,target_2,
                quantity,risk_budget_inr,source_rejection_reason,exclusion_reason,created_at,updated_at)
            VALUES(?,?,?,?,?,?,'EXCLUDED',?,?,?,0,?,?,?, ?,?)
        """, (signal_id, symbol, side, str(candidate.get("strategy") or "unknown"),
              str(candidate.get("signal_time") or as_of), as_of, level("stop_loss"), level("target_1"),
              level("target_2"), self.risk_per_trade, source, reason, now, now))

    def _open(self, db: sqlite3.Connection, candidate: dict[str, Any], signal_id: str, as_of: str,
              quote_time: datetime, entry_reference: float, entry_fill: float) -> bool:
        symbol = str(candidate.get("symbol") or "").strip().upper()
        side = str(candidate.get("side") or "").upper()
        stop, t1, t2 = (float(candidate[key]) for key in ("stop_loss", "target_1", "target_2"))
        open_rows = list(db.execute("SELECT * FROM watch_shadow_trades WHERE status='OPEN'"))
        reserved = sum(float(row["position_value_inr"] or 0) for row in open_rows)
        closed_pnl = float(db.execute("SELECT COALESCE(SUM(net_pnl),0) FROM watch_shadow_trades WHERE status='CLOSED'").fetchone()[0] or 0)
        available = max(0.0, self.initial_capital + closed_pnl - reserved)
        base, rates = load_phase2_settings()
        settings = RiskSettings(
            capital_inr=available,
            risk_per_trade_inr=self.risk_per_trade,
            minimum_net_risk_reward=1.5,
            require_margin_verification=False,
            skip_if_margin_unavailable=False,
            slippage_bps_per_side=self.slippage_bps,
            entry_cutoff_ist=base.entry_cutoff_ist,
            square_off_time_ist=base.square_off_time_ist,
        )
        assessment = assess_trade(
            side=side, entry_price=entry_reference, stop_loss=stop, target_1=t1, target_2=t2,
            settings=settings, estimated_margin_per_share=entry_reference, charge_rates=rates,
        )
        qty = int(assessment.quantity)
        if qty < 1:
            self._insert_excluded(db, candidate, signal_id, as_of,
                                  "No cash-only quantity fits the shadow risk budget after estimated costs.")
            return False
        # The candidate already failed only the minimum R:R gate. Keep it in the
        # shadow book even when assessment.accepted is False solely for that same gate.
        now = datetime.now(IST).isoformat()
        position_value = entry_fill * qty
        source = next((str(r) for r in candidate.get("reasons", [])
                       if "target 1 net r:r is below" in str(r).lower()), "Target 1 net R:R gate")
        db.execute("""
            INSERT INTO watch_shadow_trades(
              signal_id,symbol,side,strategy,signal_time,scan_as_of,status,entry_time,
              entry_reference_price,entry_fill_price,stop_loss,target_1,target_2,quantity,
              position_value_inr,risk_budget_inr,assessed_net_rr_t1,estimated_net_risk_inr,
              estimated_net_reward_t1_inr,source_rejection_reason,exclusion_reason,created_at,updated_at)
            VALUES(?,?,?,?,?,?,'OPEN',?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?)
        """, (signal_id, symbol, side, str(candidate.get("strategy") or "unknown"),
              str(candidate.get("signal_time") or as_of), as_of, quote_time.isoformat(),
              entry_reference, entry_fill, stop, t1, t2, qty, position_value, self.risk_per_trade,
              assessment.net_rr_t1, assessment.net_risk_inr, assessment.net_reward_t1_inr,
              source, now, now))
        return True

    def update_market_price(self, symbol: str, last_price: float, *, bid: float | None = None,
                            ask: float | None = None, market_time: datetime | None = None,
                            received_at: datetime | None = None) -> int:
        """Update the mark and close watch-shadow positions on stop, target 2, or square-off."""
        cleaned = str(symbol).strip().upper()
        last = _positive(last_price)
        if not cleaned or last is None:
            raise ValueError("symbol and a positive finite last_price are required")
        bid_value, ask_value = _positive(bid) or last, _positive(ask) or last
        qt = _to_ist(market_time or datetime.now(IST))
        rec = _to_ist(received_at or datetime.now(IST))
        closed = 0
        with self._connect() as db:
            db.execute("""INSERT INTO market_marks(symbol,last_price,bid,ask,market_time,received_at)
                VALUES(?,?,?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET last_price=excluded.last_price,
                bid=excluded.bid,ask=excluded.ask,market_time=excluded.market_time,received_at=excluded.received_at""",
                (cleaned, last, bid_value, ask_value, qt.isoformat(), rec.isoformat()))
            rows = list(db.execute("SELECT * FROM watch_shadow_trades WHERE status='OPEN' AND symbol=?", (cleaned,)))
            for row in rows:
                side = row["side"]
                stop_hit = last <= float(row["stop_loss"]) if side == "LONG" else last >= float(row["stop_loss"])
                target_hit = last >= float(row["target_2"]) if side == "LONG" else last <= float(row["target_2"])
                time_exit = qt.time().replace(tzinfo=None) >= SQUARE_OFF_TIME
                if stop_hit:
                    stop = float(row["stop_loss"])
                    exit_ref = min(stop, bid_value) if side == "LONG" else max(stop, ask_value)
                    reason, detail = "STOP_LOSS", "Stop crossed; conservative adverse quote and exit slippage applied."
                elif target_hit:
                    exit_ref = float(row["target_2"])
                    reason, detail = "TARGET_2", "Target 2 crossed; no favourable gap improvement credited."
                elif time_exit:
                    exit_ref = bid_value if side == "LONG" else ask_value
                    reason, detail = "TIME_SQUARE_OFF", "Position squared off at/after 15:15 IST using the latest quote."
                else:
                    continue
                self._close(db, row, exit_ref, reason, detail, qt)
                closed += 1
        return closed

    def square_off_due_positions(self, now: datetime | None = None, *, max_quote_age_seconds: float = 60.0) -> int:
        current = _to_ist(now or datetime.now(IST))
        if current.time().replace(tzinfo=None) < SQUARE_OFF_TIME:
            return 0
        closed = 0
        with self._connect() as db:
            marks = {row["symbol"]: row for row in db.execute("SELECT * FROM market_marks")}
            rows = list(db.execute("SELECT * FROM watch_shadow_trades WHERE status='OPEN'"))
            for row in rows:
                mark = marks.get(row["symbol"])
                if mark is None:
                    continue
                age = (current - _to_ist(mark["received_at"])).total_seconds()
                if age < -2 or age > max_quote_age_seconds:
                    continue
                exit_ref = float(mark["bid"] or mark["last_price"]) if row["side"] == "LONG" else float(mark["ask"] or mark["last_price"])
                self._close(db, row, exit_ref, "TIME_SQUARE_OFF",
                            "Time-based square-off using a recent cached market quote.", current)
                closed += 1
        return closed

    def _close(self, db: sqlite3.Connection, row: sqlite3.Row, exit_reference: float,
               exit_reason: str, detail: str, at: datetime) -> None:
        side = row["side"]
        qty = int(row["quantity"])
        entry_ref, entry_fill = float(row["entry_reference_price"]), float(row["entry_fill_price"])
        exit_fill = exit_reference * (1 - self.slippage_bps / 10_000) if side == "LONG" else exit_reference * (1 + self.slippage_bps / 10_000)
        gross = ((exit_reference - entry_ref) if side == "LONG" else (entry_ref - exit_reference)) * qty
        _, rates = load_phase2_settings()
        fees = estimate_intraday_round_trip_cost(side=side, entry_price=entry_fill, exit_price=exit_fill, quantity=qty, rates=rates)
        slip = abs(entry_fill - entry_ref) * qty + abs(exit_fill - exit_reference) * qty
        total = fees.total + slip
        net = gross - total
        now = datetime.now(IST).isoformat()
        db.execute("""
            UPDATE watch_shadow_trades SET status='CLOSED',exit_time=?,exit_reference_price=?,exit_fill_price=?,
              exit_reason=?,exit_detail=?,gross_pnl=?,brokerage=?,stt=?,exchange_charges=?,sebi_charges=?,
              gst=?,stamp_duty=?,ipft=?,transaction_charges=?,slippage_cost=?,total_estimated_costs=?,net_pnl=?,updated_at=?
            WHERE signal_id=? AND status='OPEN'
        """, (at.isoformat(), exit_reference, exit_fill, exit_reason, detail, gross, fees.brokerage, fees.stt,
              fees.exchange_charges, fees.sebi_charges, fees.gst, fees.stamp_duty, fees.ipft, fees.total,
              slip, total, net, now, row["signal_id"]))
        planned_risk = abs(float(row["risk_budget_inr"] or 0))
        if planned_risk and net < -planned_risk:
            LOGGER.warning(
                "WATCH_SHADOW_RISK_OVERRUN symbol=%s side=%s net_loss=₹%.2f planned_risk=₹%.2f "
                "excess=₹%.2f exit_reason=%s exit_reference=%.4f exit_fill=%.4f; simulated stop/slippage "
                "can exceed planned risk.",
                row["symbol"], side, abs(net), planned_risk, abs(net) - planned_risk,
                exit_reason, exit_reference, exit_fill,
            )

    def snapshot(self) -> dict[str, Any]:
        with self._connect() as db:
            opened = list(db.execute("SELECT * FROM watch_shadow_trades WHERE status='OPEN' ORDER BY entry_time DESC"))
            closed = list(db.execute("SELECT * FROM watch_shadow_trades WHERE status='CLOSED' ORDER BY exit_time DESC LIMIT 100"))
            signals = list(db.execute("SELECT * FROM watch_shadow_trades ORDER BY created_at DESC LIMIT 100"))
            marks = {row["symbol"]: dict(row) for row in db.execute("SELECT * FROM market_marks")}
            all_closed = list(db.execute("SELECT net_pnl FROM watch_shadow_trades WHERE status='CLOSED'"))
            realised = sum(float(row["net_pnl"] or 0) for row in all_closed)
            unrealised = 0.0
            exposure = 0.0
            positions = []
            _, rates = load_phase2_settings()
            for row in opened:
                item = dict(row)
                mark = marks.get(row["symbol"])
                last = float(mark["last_price"]) if mark else float(row["entry_reference_price"])
                exit_ref = (float(mark.get("bid") or mark["last_price"]) if row["side"] == "LONG"
                            else float(mark.get("ask") or mark["last_price"])) if mark else float(row["entry_reference_price"])
                qty = int(row["quantity"])
                gross = ((exit_ref - float(row["entry_reference_price"])) if row["side"] == "LONG"
                         else (float(row["entry_reference_price"]) - exit_ref)) * qty
                exit_fill = exit_ref * (1 - self.slippage_bps / 10_000) if row["side"] == "LONG" else exit_ref * (1 + self.slippage_bps / 10_000)
                fees = estimate_intraday_round_trip_cost(side=row["side"], entry_price=float(row["entry_fill_price"]),
                                                          exit_price=exit_fill, quantity=qty, rates=rates)
                slippage = abs(float(row["entry_fill_price"]) - float(row["entry_reference_price"])) * qty + abs(exit_fill - exit_ref) * qty
                item.update({"last_price": last, "unrealized_gross_pnl": gross,
                             "unrealized_net_pnl": gross - fees.total - slippage,
                             "market_time": mark.get("market_time") if mark else None})
                unrealised += float(item["unrealized_net_pnl"])
                exposure += float(row["position_value_inr"] or 0)
                positions.append(item)
            wins = sum(1 for row in all_closed if float(row["net_pnl"] or 0) > 0)
            losses = sum(1 for row in all_closed if float(row["net_pnl"] or 0) < 0)
            initial = self.initial_capital
            summary = {
                "starting_capital": initial, "realized_pnl": realised, "unrealized_net_pnl": unrealised,
                "equity": initial + realised + unrealised,
                "available_cash": max(0.0, initial + realised - exposure),
                "open_exposure": exposure, "open_positions": len(opened), "closed_trades": len(all_closed),
                "win_rate_pct": (wins / len(all_closed) * 100) if all_closed else 0.0,
                "excluded_candidates": int(db.execute("SELECT COUNT(*) FROM watch_shadow_trades WHERE status='EXCLUDED'").fetchone()[0]),
                "candidate_count": int(db.execute("SELECT COUNT(*) FROM watch_shadow_trades").fetchone()[0]),
                "risk_per_trade": self.risk_per_trade, "max_daily_loss": self.max_daily_loss,
                "max_open_positions": self.max_open_positions,
            }
        return {
            "account": summary,
            "open_positions": positions,
            "closed_trades": [dict(row) for row in closed],
            "recent_signals": [dict(row) for row in signals],
            "updated_at": datetime.now(IST).isoformat(),
            "research_only": True,
            "orders_sent_to_broker": False,
        }
