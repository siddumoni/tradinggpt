"""Persistent virtual broker for read-only, research-only NSE paper trading."""

from __future__ import annotations
import hashlib, json, logging, math, sqlite3
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo
from src.cost_calculator import estimate_intraday_round_trip_cost
from src.paper_allocator import rank_paper_candidates
from src.risk_engine import (
    RiskSettings,
    assess_trade,
    calculate_net_reward_risk,
    load_phase2_settings,
)
from src.exit_policy import ExitPolicy, evaluate_ohlc_exit, load_exit_policy

LOGGER = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
DEFAULT_DB = Path("data/paper_trading.sqlite3")
DEFAULT_CAPITAL_INR = 100_000.0
DEFAULT_RISK_PER_TRADE_INR = 500.0
DEFAULT_DAILY_LOSS_LIMIT_INR = 2_000.0
DEFAULT_MAX_OPEN_POSITIONS = 3
DEFAULT_SLIPPAGE_BPS = 5.0
DEFAULT_STOP_GAP_RISK_RESERVE_PCT = 0.10
PAPER_BASELINE_VERSION = "nse_champion_v2_foundation_candidate"
CHALLENGER_VERSION_NAMES = {
    "challenger_ema_9_21_v1": "EMA 9/21 momentum (pre-ATR-seed)",
    "challenger_vwap_momentum_v1": "VWAP + MACD momentum (pre-ATR-seed)",
    "challenger_ema_pullback_v1": "EMA trend pullback (pre-ATR-seed)",
    "challenger_ema_9_21_atr_seed_v2": "EMA 9/21 momentum (ATR-seeded)",
    "challenger_vwap_momentum_atr_seed_v2": "VWAP + MACD momentum (ATR-seeded)",
    "challenger_ema_pullback_atr_seed_v2": "EMA trend pullback (ATR-seeded)",
}
CHALLENGER_VERSION_TRACKS = {
    version: ("CHALLENGER_ARCHIVE" if version.endswith("_v1") else "SHADOW")
    for version in CHALLENGER_VERSION_NAMES
}
MAX_ENTRY_QUOTE_AGE_SECONDS = 120.0
# The configured time exit is sourced from config/exit_policy.yaml.


def to_ist(value: Any) -> datetime:
    stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return stamp.replace(tzinfo=IST) if stamp.tzinfo is None else stamp.astimezone(IST)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, allow_nan=False)


def _finite_positive(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a finite positive number") from exc
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} must be a finite positive number")
    return number


def _signal_key(candidate: dict[str, Any]) -> str:
    raw = "|".join(str(candidate.get(k, "")) for k in ("symbol", "side", "strategy", "signal_time"))
    return hashlib.sha256(raw.upper().encode("utf-8")).hexdigest()[:32]


class PaperBroker:
    """SQLite-backed virtual account. It never submits real brokerage orders."""

    def __init__(
        self,
        db_path: Path = DEFAULT_DB,
        *,
        initial_capital: float = DEFAULT_CAPITAL_INR,
        risk_per_trade: float = DEFAULT_RISK_PER_TRADE_INR,
        max_daily_loss: float = DEFAULT_DAILY_LOSS_LIMIT_INR,
        max_open_positions: int = DEFAULT_MAX_OPEN_POSITIONS,
        slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
        stop_gap_risk_reserve_pct: float = DEFAULT_STOP_GAP_RISK_RESERVE_PCT,
        minimum_net_rr: float = 1.5,
        max_entry_quote_age_seconds: float = MAX_ENTRY_QUOTE_AGE_SECONDS,
        max_portfolio_risk_inr: float | None = None,
        exit_policy: ExitPolicy | None = None,
    ):
        for label, value in (
            ("initial_capital", initial_capital),
            ("risk_per_trade", risk_per_trade),
            ("max_daily_loss", max_daily_loss),
            ("minimum_net_rr", minimum_net_rr),
            ("max_entry_quote_age_seconds", max_entry_quote_age_seconds),
        ):
            if not math.isfinite(float(value)) or float(value) <= 0:
                raise ValueError(f"{label} must be finite and positive")
        if isinstance(max_open_positions, bool) or int(max_open_positions) < 1:
            raise ValueError("max_open_positions must be a positive integer")
        if not math.isfinite(slippage_bps) or not 0 <= slippage_bps <= 100:
            raise ValueError("slippage_bps must be between 0 and 100")
        if (
            not math.isfinite(float(stop_gap_risk_reserve_pct))
            or not 0 <= float(stop_gap_risk_reserve_pct) < 1
        ):
            raise ValueError("stop_gap_risk_reserve_pct must be in [0, 1)")
        self.db_path = Path(db_path)
        self.initial_capital = float(initial_capital)
        self.risk_per_trade = float(risk_per_trade)
        self.max_daily_loss = float(max_daily_loss)
        self.max_open_positions = int(max_open_positions)
        self.slippage_bps = float(slippage_bps)
        self.stop_gap_risk_reserve_pct = float(stop_gap_risk_reserve_pct)
        if float(minimum_net_rr) < 1.5:
            raise ValueError("minimum_net_rr must be at least 1.5")
        self.minimum_net_rr = float(minimum_net_rr)
        self.exit_policy = exit_policy or load_exit_policy()
        self.max_entry_quote_age_seconds = float(max_entry_quote_age_seconds)
        configured_portfolio_risk = (
            float(max_portfolio_risk_inr)
            if max_portfolio_risk_inr is not None
            else self.risk_per_trade * self.max_open_positions
        )
        if not math.isfinite(configured_portfolio_risk) or configured_portfolio_risk <= 0:
            raise ValueError("max_portfolio_risk_inr must be finite and positive")
        self.max_portfolio_risk_inr = configured_portfolio_risk
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path, timeout=15)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA busy_timeout=15000")
        return c

    def _initialize(self) -> None:
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
            CREATE TABLE IF NOT EXISTS account(id INTEGER PRIMARY KEY CHECK(id=1),starting_capital REAL NOT NULL,created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS trades(
              id INTEGER PRIMARY KEY AUTOINCREMENT,signal_id TEXT NOT NULL UNIQUE,strategy TEXT NOT NULL,
              strategy_version TEXT NOT NULL,symbol TEXT NOT NULL,side TEXT NOT NULL CHECK(side IN ('LONG','SHORT')),
              status TEXT NOT NULL CHECK(status IN ('OPEN','CLOSED')),signal_time TEXT NOT NULL,entry_time TEXT NOT NULL,
              signal_reference_entry REAL NOT NULL,entry_reference_price REAL NOT NULL,entry_fill_price REAL NOT NULL,
              stop_loss REAL NOT NULL,target_1 REAL NOT NULL,target_2 REAL NOT NULL,quantity INTEGER NOT NULL CHECK(quantity>0),
              risk_budget_inr REAL NOT NULL,position_value_inr REAL NOT NULL,planned_net_risk_inr REAL NOT NULL DEFAULT 0,
              market_regime TEXT NOT NULL,entry_reason TEXT NOT NULL,
              exit_time TEXT,exit_reference_price REAL,exit_fill_price REAL,exit_reason TEXT,exit_detail TEXT,gross_pnl REAL,
              brokerage REAL,stt REAL,exchange_charges REAL,sebi_charges REAL,gst REAL,stamp_duty REAL,ipft REAL,
              transaction_charges REAL,slippage_cost REAL,total_estimated_costs REAL,net_pnl REAL,created_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
            CREATE TABLE IF NOT EXISTS paper_universe_watchlist(
              id INTEGER PRIMARY KEY CHECK(id=1),symbols_json TEXT NOT NULL,universe_mode TEXT NOT NULL,
              scan_as_of TEXT NOT NULL,updated_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_trades_exit_time ON trades(exit_time);
            CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy,strategy_version);
            CREATE TABLE IF NOT EXISTS market_marks(symbol TEXT PRIMARY KEY,last_price REAL NOT NULL,bid REAL,ask REAL,market_time TEXT NOT NULL,received_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,event_time TEXT NOT NULL,event_type TEXT NOT NULL,
              severity TEXT NOT NULL,symbol TEXT,message TEXT NOT NULL,details_json TEXT NOT NULL,event_key TEXT UNIQUE);
            CREATE INDEX IF NOT EXISTS idx_events_time ON events(event_time DESC);
            CREATE TABLE IF NOT EXISTS strategy_registry(strategy_version TEXT PRIMARY KEY,strategy_name TEXT NOT NULL,
              track TEXT NOT NULL,status TEXT NOT NULL,description TEXT NOT NULL,created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS shadow_signals(
              signal_id TEXT PRIMARY KEY,strategy_version TEXT NOT NULL,strategy_name TEXT NOT NULL,symbol TEXT NOT NULL,
              side TEXT NOT NULL CHECK(side IN ('LONG','SHORT')),signal_time TEXT NOT NULL,signal_close REAL NOT NULL,
              stop_loss REAL NOT NULL,target_1 REAL NOT NULL,target_2 REAL NOT NULL,status TEXT NOT NULL,
              entry_time TEXT,entry_reference_price REAL,entry_fill_price REAL,quantity INTEGER,exit_time TEXT,
              exit_reference_price REAL,exit_fill_price REAL,exit_reason TEXT,exit_detail TEXT,gross_pnl REAL,brokerage REAL,
              stt REAL,exchange_charges REAL,sebi_charges REAL,gst REAL,stamp_duty REAL,ipft REAL,transaction_charges REAL,
              slippage_cost REAL,total_estimated_costs REAL,net_pnl REAL,reason TEXT NOT NULL,created_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_shadow_version_status ON shadow_signals(strategy_version,status);
            """)
            trade_columns = {row["name"] for row in db.execute("PRAGMA table_info(trades)")}
            if "planned_net_risk_inr" not in trade_columns:
                db.execute(
                    "ALTER TABLE trades ADD COLUMN planned_net_risk_inr REAL NOT NULL DEFAULT 0"
                )
            # Legacy open positions reserve their full per-trade budget conservatively.
            db.execute(
                "UPDATE trades SET planned_net_risk_inr=risk_budget_inr WHERE status='OPEN' AND planned_net_risk_inr=0"
            )
            now = datetime.now(IST).isoformat()
            db.execute(
                "INSERT OR IGNORE INTO account(id,starting_capital,created_at) VALUES(1,?,?)",
                (self.initial_capital, now),
            )
            defaults = [
                (
                    "champion_v1",
                    "Frozen pre-foundation champion",
                    "CHAMPION_ARCHIVE",
                    "FROZEN_VERSION",
                    "Historical evidence is preserved under this version; no new entries are assigned.",
                ),
                (
                    PAPER_BASELINE_VERSION,
                    "Structural-stop and prior-session ATR foundation candidate",
                    "CHAMPION_CANDIDATE",
                    "ACTIVE",
                    "Unpromoted research version. Freeze as nse_champion_v2 only after foundation phases A, B, and E are complete.",
                ),
                (
                    "challenger_ema_9_21_v1",
                    "EMA 9/21 momentum (pre-ATR-seed version)",
                    "CHALLENGER_ARCHIVE",
                    "SUPERSEDED_VERSION",
                    "Historical observations are preserved; do not pool with the ATR-seeded version.",
                ),
                (
                    "challenger_vwap_momentum_v1",
                    "VWAP + MACD momentum (pre-ATR-seed version)",
                    "CHALLENGER_ARCHIVE",
                    "SUPERSEDED_VERSION",
                    "Historical observations are preserved; do not pool with the ATR-seeded version.",
                ),
                (
                    "challenger_ema_pullback_v1",
                    "EMA trend pullback (pre-ATR-seed version)",
                    "CHALLENGER_ARCHIVE",
                    "SUPERSEDED_VERSION",
                    "Historical observations are preserved; do not pool with the ATR-seeded version.",
                ),
                (
                    "challenger_ema_9_21_atr_seed_v2",
                    "EMA 9/21 momentum with prior-session ATR seed",
                    "CHALLENGER",
                    "SHADOW",
                    "Research-only alternative; plain 1 ATR stop retained; separate version evidence.",
                ),
                (
                    "challenger_vwap_momentum_atr_seed_v2",
                    "VWAP + MACD momentum with prior-session ATR seed",
                    "CHALLENGER",
                    "SHADOW",
                    "Research-only alternative; plain 1 ATR stop retained; separate version evidence.",
                ),
                (
                    "challenger_ema_pullback_atr_seed_v2",
                    "EMA trend pullback with prior-session ATR seed",
                    "CHALLENGER",
                    "SHADOW",
                    "Research-only alternative; plain 1 ATR stop retained; separate version evidence.",
                ),
            ]
            for row in defaults:
                db.execute(
                    "INSERT OR IGNORE INTO strategy_registry(strategy_version,strategy_name,track,status,description,created_at) VALUES(?,?,?,?,?,?)",
                    (*row, now),
                )
            # Version changes supersede old labels; they are not performance-based
            # retirements and their historical rows remain untouched.
            db.execute(
                "UPDATE strategy_registry SET track='CHAMPION_ARCHIVE',status='FROZEN_VERSION',description='Historical evidence is preserved under this version; no new entries are assigned.' WHERE strategy_version='champion_v1'"
            )
            for old_version in (
                "challenger_ema_9_21_v1",
                "challenger_vwap_momentum_v1",
                "challenger_ema_pullback_v1",
            ):
                db.execute(
                    "UPDATE strategy_registry SET track='CHALLENGER_ARCHIVE',status='SUPERSEDED_VERSION',description='Historical observations are preserved; do not pool with the ATR-seeded version.' WHERE strategy_version=?",
                    (old_version,),
                )

    def _event(
        self,
        db,
        event_type,
        message,
        *,
        symbol=None,
        severity="INFO",
        details=None,
        event_key=None,
        at=None,
    ):
        t = at or datetime.now(IST)
        t = t.replace(tzinfo=IST) if t.tzinfo is None else t.astimezone(IST)
        db.execute(
            "INSERT OR IGNORE INTO events(event_time,event_type,severity,symbol,message,details_json,event_key) VALUES(?,?,?,?,?,?,?)",
            (t.isoformat(), event_type, severity, symbol, message, _json(details or {}), event_key),
        )

    def _account_values(self, db):
        account = db.execute("SELECT starting_capital FROM account WHERE id=1").fetchone()
        closed = db.execute(
            "SELECT COALESCE(SUM(net_pnl),0) pnl FROM trades WHERE status='CLOSED'"
        ).fetchone()
        return float(account["starting_capital"]), float(closed["pnl"])

    def _open_rows(self, db):
        return list(db.execute("SELECT * FROM trades WHERE status='OPEN' ORDER BY entry_time"))

    def monitored_symbols(self) -> list[str]:
        """Latest selected scan symbols plus all still-open Champion paper positions."""
        with self._connect() as db:
            row = db.execute(
                "SELECT symbols_json FROM paper_universe_watchlist WHERE id=1"
            ).fetchone()
            scanned = []
            if row:
                try:
                    values = json.loads(row["symbols_json"])
                    if isinstance(values, list):
                        scanned = [str(x).strip().upper() for x in values if str(x).strip()]
                except (TypeError, json.JSONDecodeError):
                    scanned = []
            opened = [
                str(row["symbol"]).strip().upper()
                for row in db.execute("SELECT DISTINCT symbol FROM trades WHERE status='OPEN'")
            ]
        return sorted(set(scanned) | set(opened))

    def _today_net_pnl(self, db, now):
        day = now.astimezone(IST).date().isoformat()
        closed = db.execute(
            "SELECT COALESCE(SUM(net_pnl),0) pnl FROM trades WHERE status='CLOSED' AND substr(exit_time,1,10)=?",
            (day,),
        ).fetchone()
        total = float(closed["pnl"])
        marks = {r["symbol"]: r for r in db.execute("SELECT * FROM market_marks")}
        _, rates = load_phase2_settings()
        for trade in self._open_rows(db):
            if not str(trade["entry_time"]).startswith(day):
                continue
            mark = marks.get(trade["symbol"])
            if not mark:
                continue
            side = trade["side"]
            qty = int(trade["quantity"])
            entry_ref = float(trade["entry_reference_price"])
            entry_fill = float(trade["entry_fill_price"])
            exit_ref = (
                float(mark["bid"] or mark["last_price"])
                if side == "LONG"
                else float(mark["ask"] or mark["last_price"])
            )
            exit_fill = (
                exit_ref * (1 - self.slippage_bps / 10000)
                if side == "LONG"
                else exit_ref * (1 + self.slippage_bps / 10000)
            )
            gross = ((exit_ref - entry_ref) if side == "LONG" else (entry_ref - exit_ref)) * qty
            fees = estimate_intraday_round_trip_cost(
                side=side, entry_price=entry_fill, exit_price=exit_fill, quantity=qty, rates=rates
            )
            slip = abs(entry_fill - entry_ref) * qty + abs(exit_fill - exit_ref) * qty
            total += gross - fees.total - slip
        return total

    def _record_rejection(self, db, candidate, report_as_of, reason):
        symbol = str(candidate.get("symbol") or "").strip().upper() or None
        key = hashlib.sha256(
            f"{report_as_of}|{symbol}|{_signal_key(candidate)}|{reason}".encode()
        ).hexdigest()[:32]
        self._event(
            db,
            "SIGNAL_REJECTED",
            reason,
            symbol=symbol,
            severity="WARNING",
            details={"candidate": candidate, "scan_as_of": report_as_of},
            event_key=key,
        )

    def process_scan_report(
        self,
        report_path: Path,
        before_entry_callback: Callable[[dict[str, Any]], bool] | None = None,
    ) -> dict[str, Any]:
        path = Path(report_path)
        try:
            report = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Could not read scan report {path}: {exc}") from exc
        if not isinstance(report, dict) or report.get("status") != "RESEARCH_SCAN_ONLY":
            raise ValueError("Paper broker requires a RESEARCH_SCAN_ONLY JSON report")
        if report.get("orders_placed_or_modified") is not False:
            raise ValueError("Paper broker rejected report without explicit no-order assertion")
        if str(report.get("data_provider_mode", "")).lower() != "kite":
            raise ValueError("Paper broker only accepts Kite provider reports")
        candidates = report.get("candidates", [])
        snapshots = report.get("market_snapshots", {})
        if not isinstance(candidates, list) or not isinstance(snapshots, dict):
            raise ValueError("Scan report candidates or market_snapshots schema is invalid")
        as_of = str(report.get("as_of") or "")
        if not as_of:
            raise ValueError("Scan report is missing as_of timestamp")
        scan_time = to_ist(as_of)
        opened = rejected = neutral = 0
        action_counts = {}
        allocation_order = []
        universe = (
            report.get("universe_selection")
            if isinstance(report.get("universe_selection"), dict)
            else {}
        )
        tracked_source = universe.get("selected_symbols")
        if not isinstance(tracked_source, list):
            tracked_source = report.get("symbols_requested", [])
        tracked_symbols = (
            sorted(
                set(
                    str(x).strip().upper()
                    for x in tracked_source
                    if isinstance(x, str) and x.strip()
                )
            )
            if isinstance(tracked_source, list)
            else []
        )
        universe_mode = str(universe.get("mode") or "watchlist").strip().lower()
        with self._connect() as db:
            if isinstance(tracked_source, list):
                db.execute(
                    """INSERT INTO paper_universe_watchlist(id,symbols_json,universe_mode,scan_as_of,updated_at)
                  VALUES(1,?,?,?,?) ON CONFLICT(id) DO UPDATE SET symbols_json=excluded.symbols_json,
                  universe_mode=excluded.universe_mode,scan_as_of=excluded.scan_as_of,updated_at=excluded.updated_at""",
                    (_json(tracked_symbols), universe_mode, as_of, datetime.now(IST).isoformat()),
                )
                self._event(
                    db,
                    "SCAN_UNIVERSE_UPDATED",
                    f"Paper market-data watchlist refreshed with {len(tracked_symbols)} selected symbols.",
                    details={
                        "universe_mode": universe_mode,
                        "symbols": tracked_symbols,
                        "scan_as_of": as_of,
                    },
                    event_key=f"universe:{as_of}",
                    at=scan_time,
                )
            directional = sum(
                1
                for c in candidates
                if isinstance(c, dict)
                and str(c.get("action") or "").upper() in {"CONDITIONAL_BUY", "CONDITIONAL_SHORT"}
            )
            self._event(
                db,
                "SCAN_PROCESSED",
                f"Processed scan report: {len(candidates)} candidates; conditional-entry actions={directional}.",
                details={
                    "report_path": str(path),
                    "provider": report.get("provider"),
                    "scan_as_of": as_of,
                },
                event_key=f"scan:{path.resolve()}:{as_of}",
                at=scan_time,
            )
            eligible = []
            for item in candidates:
                if not isinstance(item, dict):
                    rejected += 1
                    continue
                action = str(item.get("action") or "UNKNOWN").upper()
                action_counts[action] = action_counts.get(action, 0) + 1
                if action not in {"CONDITIONAL_BUY", "CONDITIONAL_SHORT"}:
                    neutral += 1
                    self._record_rejection(
                        db,
                        item,
                        as_of,
                        "No approved directional entry: scanner action is "
                        + (action or "UNKNOWN")
                        + ".",
                    )
                else:
                    eligible.append(item)

            ranked = rank_paper_candidates(eligible, snapshots)
            for ranked_item in ranked:
                item = ranked_item["candidate"]
                symbol = ranked_item["symbol"]
                audit = {key: value for key, value in ranked_item.items() if key != "candidate"}
                allocation_order.append(audit)
                self._event(
                    db,
                    "ALLOCATION_RANKED",
                    f"Paper allocation rank {ranked_item['allocation_rank']}: {symbol} scored {ranked_item['allocation_score']:.2f}/100.",
                    symbol=symbol or None,
                    details=audit,
                    event_key=f"allocation:{as_of}:{_signal_key(item)}",
                    at=scan_time,
                )
                try:
                    self._open_candidate(
                        db,
                        item,
                        snapshots.get(symbol),
                        allocation=ranked_item,
                        before_entry_callback=before_entry_callback,
                    )
                    opened += 1
                except _RejectedSignal as exc:
                    rejected += 1
                    self._record_rejection(db, item, as_of, str(exc))
        conditional_count = action_counts.get("CONDITIONAL_BUY", 0) + action_counts.get(
            "CONDITIONAL_SHORT", 0
        )
        watch_count = action_counts.get("WATCH_CANDIDATE", 0)
        no_trade_count = action_counts.get("NO_TRADE", 0)
        other_count = sum(
            count
            for action, count in action_counts.items()
            if action not in {"CONDITIONAL_BUY", "CONDITIONAL_SHORT", "WATCH_CANDIDATE", "NO_TRADE"}
        )
        return {
            "opened": opened,
            "rejected": rejected,
            "neutral": neutral,
            "candidate_count": len(candidates),
            "conditional_candidates": conditional_count,
            "watch_candidates": watch_count,
            "no_trade": no_trade_count,
            "other_actions": other_count,
            "ranked_candidates": len(ranked),
            "allocation_order": allocation_order,
            "allocation_method": "quality_first_greedy_with_shared_cash_and_risk_caps",
            "tracked_symbols": self.monitored_symbols(),
            "universe_mode": universe_mode,
        }

    def _open_candidate(
        self,
        db,
        candidate,
        snapshot,
        *,
        allocation: dict[str, Any] | None = None,
        before_entry_callback: Callable[[dict[str, Any]], bool] | None = None,
    ):
        symbol = str(candidate.get("symbol") or "").strip().upper()
        side = str(candidate.get("side") or "").upper()
        strategy = str(candidate.get("strategy") or "").strip()
        signal_time = str(candidate.get("signal_time") or "")
        signal_id = _signal_key(candidate)
        if not symbol or side not in {"LONG", "SHORT"} or not strategy or not signal_time:
            raise _RejectedSignal(
                "Candidate symbol, side, strategy or signal timestamp is missing."
            )
        if (side == "LONG" and str(candidate.get("action")).upper() != "CONDITIONAL_BUY") or (
            side == "SHORT" and str(candidate.get("action")).upper() != "CONDITIONAL_SHORT"
        ):
            raise _RejectedSignal("Candidate action and direction disagree.")
        if candidate.get("data_verified_live") is not True:
            raise _RejectedSignal("Live Kite quote/data verification did not pass.")
        if candidate.get("margin_verified") is not True:
            raise _RejectedSignal("Kite margin verification did not pass.")
        if not isinstance(snapshot, dict) or snapshot.get("verified_live") is not True:
            raise _RejectedSignal("Quote snapshot lacks explicit live-data verification.")
        if str(candidate.get("company_specific_risk", "")).upper() == "HIGH":
            raise _RejectedSignal("Company-specific risk is HIGH.")
        if not isinstance(snapshot, dict):
            raise _RejectedSignal("No market quote snapshot is available for a paper fill.")
        qtime = snapshot.get("timestamp") or snapshot.get("last_trade_time")
        if not qtime:
            raise _RejectedSignal("Quote timestamp is missing; fail-closed paper entry.")
        age = (datetime.now(IST) - to_ist(qtime)).total_seconds()
        if age < -2 or age > self.max_entry_quote_age_seconds:
            raise _RejectedSignal(f"Entry quote is stale or future-dated ({age:.1f}s old).")
        try:
            reference_entry = _finite_positive(candidate.get("reference_entry"), "reference_entry")
            stop = _finite_positive(candidate.get("stop_loss"), "stop_loss")
            t1 = _finite_positive(candidate.get("target_1"), "target_1")
            t2 = _finite_positive(candidate.get("target_2"), "target_2")
            last = _finite_positive(snapshot.get("last_price"), "quote.last_price")
        except ValueError as exc:
            raise _RejectedSignal(str(exc)) from exc
        try:
            bid = _finite_positive(snapshot.get("bid"), "quote.bid")
            ask = _finite_positive(snapshot.get("ask"), "quote.ask")
        except ValueError as exc:
            raise _RejectedSignal(f"Executable bid/ask is missing or invalid: {exc}") from exc
        if bid > ask:
            raise _RejectedSignal("Executable quote is crossed; paper entry rejected.")
        entry_reference = ask if side == "LONG" else bid
        entry_fill = (
            entry_reference * (1 + self.slippage_bps / 10000)
            if side == "LONG"
            else entry_reference * (1 - self.slippage_bps / 10000)
        )
        valid = stop < entry_fill < t1 <= t2 if side == "LONG" else t2 <= t1 < entry_fill < stop
        if not valid:
            raise _RejectedSignal(
                "Current bid/ask no longer lies inside strategy stop/target entry window."
            )
        if db.execute("SELECT 1 FROM trades WHERE signal_id=?", (signal_id,)).fetchone():
            raise _RejectedSignal("Duplicate signal already processed.")
        opens = self._open_rows(db)
        if len(opens) >= self.max_open_positions:
            raise _RejectedSignal(
                f"Maximum open paper positions ({self.max_open_positions}) reached."
            )
        if any(r["symbol"] == symbol for r in opens):
            raise _RejectedSignal("A paper position is already open for this symbol.")
        now = datetime.now(IST)
        day_net = self._today_net_pnl(db, now)
        if day_net <= -self.max_daily_loss:
            raise _RejectedSignal(f"Daily paper loss limit reached ({day_net:.2f} INR).")
        initial, realized = self._account_values(db)
        used = sum(float(r["position_value_inr"]) for r in opens)
        available = max(0.0, initial + realized - used)
        if available < entry_fill:
            raise _RejectedSignal("Insufficient virtual cash for one share; leverage is disabled.")
        existing_open_risk = sum(
            float(r["planned_net_risk_inr"] or r["risk_budget_inr"] or self.risk_per_trade)
            for r in opens
        )
        remaining_portfolio_risk = max(0.0, self.max_portfolio_risk_inr - existing_open_risk)
        if remaining_portfolio_risk <= 0:
            raise _RejectedSignal("Aggregate open portfolio-risk limit is fully allocated.")
        allocated_risk_budget = min(self.risk_per_trade, remaining_portfolio_risk)
        # Reserve 10% of the approved budget for adverse stop gaps beyond the
        # configured per-side slippage estimate. The budget itself remains unchanged.
        sizing_risk_budget = allocated_risk_budget * (1.0 - self.stop_gap_risk_reserve_pct)
        _, rates = load_phase2_settings()
        settings = RiskSettings(
            capital_inr=available,
            risk_per_trade_inr=sizing_risk_budget,
            minimum_net_risk_reward=self.minimum_net_rr,
            require_margin_verification=False,
            skip_if_margin_unavailable=False,
            slippage_bps_per_side=self.slippage_bps,
            entry_cutoff_ist="14:45",
            square_off_time_ist=self.exit_policy.time_exit_ist,
            minimum_net_rr_target=self.exit_policy.gate_target,
        )
        try:
            assessment = assess_trade(
                side=side,
                entry_price=entry_reference,
                stop_loss=stop,
                target_1=t1,
                target_2=t2,
                settings=settings,
                estimated_margin_per_share=entry_fill,
                charge_rates=rates,
            )
        except ValueError as exc:
            raise _RejectedSignal(f"Current quote failed the risk validator: {exc}") from exc
        if not assessment.accepted or assessment.quantity < 1:
            raise _RejectedSignal("Virtual risk/after-cost R:R gate: " + assessment.reason)
        if existing_open_risk + assessment.net_risk_inr > self.max_portfolio_risk_inr + 1e-6:
            raise _RejectedSignal("Aggregate open portfolio-risk limit would be exceeded.")
        reasons = candidate.get("reasons", [])
        entry_reason = (
            " ".join(str(x) for x in reasons) if isinstance(reasons, list) else str(reasons)
        )
        if before_entry_callback is not None:
            trade_plan = {
                "signal_id": signal_id,
                "action": str(candidate.get("action") or "").upper(),
                "symbol": symbol,
                "side": side,
                "strategy": strategy,
                "strategy_version": PAPER_BASELINE_VERSION,
                "signal_time": signal_time,
                "reference_entry": reference_entry,
                "entry_reference_price": entry_reference,
                "entry_fill_price": entry_fill,
                "stop_loss": stop,
                "target_1": t1,
                "target_2": t2,
                "quantity": int(assessment.quantity),
                "risk_budget_inr": allocated_risk_budget,
                "net_risk_inr": assessment.net_risk_inr,
                "net_reward_t1_inr": assessment.net_reward_t1_inr,
                "net_reward_t2_inr": assessment.net_reward_t2_inr,
                "net_rr_t1": assessment.net_rr_t1,
                "net_rr_t2": assessment.net_rr_t2,
                "estimated_margin_inr": assessment.estimated_margin_inr,
                "quote_age_seconds": age,
                "market_regime": str(candidate.get("market_regime") or "UNKNOWN"),
                "reasons": reasons if isinstance(reasons, list) else [entry_reason],
                "data_verified_live": candidate.get("data_verified_live"),
                "margin_verified": candidate.get("margin_verified"),
                "company_specific_risk": candidate.get("company_specific_risk"),
                "stop_reference": candidate.get("stop_reference"),
                "stop_distance_pct": candidate.get("stop_distance_pct"),
            }
            try:
                alert_delivered = before_entry_callback(trade_plan)
            except Exception:
                LOGGER.exception("Pre-entry trade-plan alert callback failed; paper entry withheld.")
                alert_delivered = False
            if alert_delivered is not True:
                raise _RejectedSignal(
                    "Pre-entry Telegram trade-plan alert was not delivered; paper entry withheld."
                )
        try:
            db.execute(
                """INSERT INTO trades(
              signal_id,strategy,strategy_version,symbol,side,status,signal_time,entry_time,signal_reference_entry,
              entry_reference_price,entry_fill_price,stop_loss,target_1,target_2,quantity,risk_budget_inr,
              position_value_inr,planned_net_risk_inr,market_regime,entry_reason,created_at)
              VALUES(?,?,?,?,?,'OPEN',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    signal_id,
                    strategy,
                    PAPER_BASELINE_VERSION,
                    symbol,
                    side,
                    signal_time,
                    now.isoformat(),
                    reference_entry,
                    entry_reference,
                    entry_fill,
                    stop,
                    t1,
                    t2,
                    int(assessment.quantity),
                    allocated_risk_budget,
                    float(assessment.quantity) * entry_fill,
                    assessment.net_risk_inr,
                    str(candidate.get("market_regime") or "UNKNOWN"),
                    entry_reason,
                    now.isoformat(),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise _RejectedSignal("Duplicate signal already processed.") from exc
        # Preserve the verified entry snapshot as the initial liquidation mark.
        # A later live tick will replace it; manual exits still require a fresh mark.
        received_at = datetime.now(IST)
        db.execute(
            """INSERT INTO market_marks(symbol,last_price,bid,ask,market_time,received_at)
          VALUES(?,?,?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET
          last_price=excluded.last_price,bid=excluded.bid,ask=excluded.ask,
          market_time=excluded.market_time,received_at=excluded.received_at""",
            (symbol, last, bid, ask, to_ist(qtime).isoformat(), received_at.isoformat()),
        )
        self._event(
            db,
            "PAPER_ENTRY",
            f"Simulated {side} entry: {assessment.quantity} {symbol} @ ₹{entry_fill:.2f}.",
            symbol=symbol,
            details={
                "signal_id": signal_id,
                "strategy": strategy,
                "strategy_version": PAPER_BASELINE_VERSION,
                "quantity": int(assessment.quantity),
                "entry_reference_price": entry_reference,
                "entry_fill_price": entry_fill,
                "signal_reference_entry": reference_entry,
                "stop_loss": stop,
                "target_1": t1,
                "target_2": t2,
                "entry_reason": entry_reason,
                "risk_budget_inr": allocated_risk_budget,
                "per_trade_risk_limit_inr": self.risk_per_trade,
                "allocated_risk_budget_inr": allocated_risk_budget,
                "sizing_risk_budget_inr": sizing_risk_budget,
                "stop_gap_risk_reserve_pct": self.stop_gap_risk_reserve_pct,
                "planned_net_risk_inr": assessment.net_risk_inr,
                "open_portfolio_risk_after_entry_inr": existing_open_risk + assessment.net_risk_inr,
                "max_portfolio_risk_inr": self.max_portfolio_risk_inr,
                "allocation_rank": (allocation or {}).get("allocation_rank"),
                "allocation_score": (allocation or {}).get("allocation_score"),
                "assessed_net_rr_t1": assessment.net_rr_t1,
            },
            event_key=f"entry:{signal_id}",
            at=now,
        )

    def update_market_price(
        self,
        symbol: str,
        last_price: float,
        *,
        bid: float | None = None,
        ask: float | None = None,
        market_time: datetime | None = None,
        received_at: datetime | None = None,
    ) -> int:
        """Store one market tick and trigger paper exits. No real orders are sent."""
        cleaned = str(symbol).strip().upper()
        last = _finite_positive(last_price, "last_price")

        def opt(v):
            try:
                p = float(v)
            except (TypeError, ValueError, OverflowError):
                return last
            return p if math.isfinite(p) and p > 0 else last

        bid_value, ask_value = opt(bid), opt(ask)
        qt = market_time or datetime.now(IST)
        qt = qt.replace(tzinfo=IST) if qt.tzinfo is None else qt.astimezone(IST)
        rec = received_at or datetime.now(IST)
        rec = rec.replace(tzinfo=IST) if rec.tzinfo is None else rec.astimezone(IST)
        local_now = datetime.now(IST)
        if (rec - local_now).total_seconds() > 2 or (qt - rec).total_seconds() > 2:
            raise ValueError("Market tick contains a future-dated receive or exchange timestamp.")
        closed = 0
        with self._connect() as db:
            db.execute(
                """INSERT INTO market_marks(symbol,last_price,bid,ask,market_time,received_at) VALUES(?,?,?,?,?,?)
              ON CONFLICT(symbol) DO UPDATE SET last_price=excluded.last_price,bid=excluded.bid,ask=excluded.ask,
              market_time=excluded.market_time,received_at=excluded.received_at""",
                (cleaned, last, bid_value, ask_value, qt.isoformat(), rec.isoformat()),
            )
            for trade in self._open_rows(db):
                if trade["symbol"] != cleaned:
                    continue
                side = trade["side"]
                stop_hit = (
                    last <= trade["stop_loss"] if side == "LONG" else last >= trade["stop_loss"]
                )
                target_hit = (
                    last >= trade["target_2"] if side == "LONG" else last <= trade["target_2"]
                )
                time_exit = qt.time().replace(tzinfo=None) >= dtime.fromisoformat(
                    self.exit_policy.time_exit_ist
                )
                if stop_hit:
                    exit_ref = (
                        min(float(trade["stop_loss"]), bid_value)
                        if side == "LONG"
                        else max(float(trade["stop_loss"]), ask_value)
                    )
                    reason, detail = (
                        "STOP_LOSS",
                        "Stop threshold crossed; adverse exit slippage applied.",
                    )
                elif target_hit:
                    exit_ref = float(trade["target_2"])
                    reason, detail = (
                        "TARGET_2",
                        "Target 2 crossed; no favorable gap improvement credited.",
                    )
                elif time_exit:
                    exit_ref = bid_value if side == "LONG" else ask_value
                    reason, detail = (
                        "TIME_SQUARE_OFF",
                        "Position squared off at/after 15:15 IST using latest quote.",
                    )
                else:
                    continue
                self._close_trade(db, trade, exit_ref, reason, detail, qt)
                closed += 1
        return closed

    def process_candle(
        self,
        symbol: str,
        candle_start: datetime | str,
        *,
        open_price: float,
        high: float,
        low: float,
        close: float,
        bar_minutes: int = 5,
    ) -> int:
        """Apply the shared completed-OHLC exit policy to one archived/streamed candle."""
        if isinstance(bar_minutes, bool) or int(bar_minutes) <= 0:
            raise ValueError("bar_minutes must be a positive integer")
        cleaned = str(symbol).strip().upper()
        if not cleaned:
            raise ValueError("symbol is required")
        start = to_ist(candle_start)
        end = start + timedelta(minutes=int(bar_minutes))
        closed = 0
        with self._connect() as db:
            for trade in self._open_rows(db):
                if trade["symbol"] != cleaned:
                    continue
                decision = evaluate_ohlc_exit(
                    side=trade["side"],
                    stop_loss=float(trade["stop_loss"]),
                    target_2=float(trade["target_2"]),
                    open_price=float(open_price),
                    high=float(high),
                    low=float(low),
                    close=float(close),
                    bar_end_time=end.time().replace(tzinfo=None),
                    policy=self.exit_policy,
                )
                if decision is None:
                    continue
                self._close_trade(
                    db, trade, decision.reference_price, decision.reason, decision.detail, end
                )
                closed += 1
        return closed

    def square_off_due_positions(
        self, now: datetime | None = None, *, max_quote_age_seconds: float = 60.0
    ) -> int:
        current = now or datetime.now(IST)
        current = current.replace(tzinfo=IST) if current.tzinfo is None else current.astimezone(IST)
        if current.time().replace(tzinfo=None) < dtime.fromisoformat(
            self.exit_policy.time_exit_ist
        ):
            return 0
        closed = 0
        with self._connect() as db:
            marks = {r["symbol"]: r for r in db.execute("SELECT * FROM market_marks")}
            for trade in self._open_rows(db):
                mark = marks.get(trade["symbol"])
                if mark is None:
                    self._event(
                        db,
                        "SQUARE_OFF_BLOCKED",
                        "No market mark; position left open instead of inventing an exit price.",
                        symbol=trade["symbol"],
                        severity="ERROR",
                        event_key=f"squareoff-missing:{trade['id']}:{current.date()}",
                        at=current,
                    )
                    continue
                mark_age = (current - to_ist(mark["received_at"])).total_seconds()
                if mark_age < -2 or mark_age > max_quote_age_seconds:
                    self._event(
                        db,
                        "SQUARE_OFF_BLOCKED",
                        "Latest market mark is stale or future-dated; position left open instead of inventing an exit price.",
                        symbol=trade["symbol"],
                        severity="ERROR",
                        event_key=f"squareoff-stale:{trade['id']}:{current.date()}",
                        at=current,
                    )
                    continue
                exit_ref = (
                    float(mark["bid"] or mark["last_price"])
                    if trade["side"] == "LONG"
                    else float(mark["ask"] or mark["last_price"])
                )
                self._close_trade(
                    db,
                    trade,
                    exit_ref,
                    "TIME_SQUARE_OFF",
                    "Time-based square-off using a recent cached quote.",
                    current,
                )
                closed += 1
        return closed

    def close_position(
        self, trade_id: int, *, max_quote_age_seconds: float = 120.0
    ) -> dict[str, Any]:
        """Manually close a Champion paper position using its latest fresh executable-side quote."""
        if isinstance(trade_id, bool) or int(trade_id) <= 0:
            raise ValueError("trade_id must be a positive integer")
        if not math.isfinite(float(max_quote_age_seconds)) or max_quote_age_seconds <= 0:
            raise ValueError("max_quote_age_seconds must be finite and positive")
        current = datetime.now(IST)
        with self._connect() as db:
            trade = db.execute("SELECT * FROM trades WHERE id=?", (int(trade_id),)).fetchone()
            if trade is None:
                raise ValueError("Paper position was not found.")
            if trade["status"] != "OPEN":
                raise ValueError("Paper position is already closed.")
            mark = db.execute(
                "SELECT * FROM market_marks WHERE symbol=?", (trade["symbol"],)
            ).fetchone()
            if mark is None:
                raise ValueError("No market quote is available; position remains open.")
            age = (current - to_ist(mark["received_at"])).total_seconds()
            if age < -2 or age > float(max_quote_age_seconds):
                raise ValueError(
                    "Latest market quote is stale or future-dated; position remains open."
                )
            side = str(trade["side"])
            quote_value = mark["bid"] if side == "LONG" else mark["ask"]
            if (
                quote_value is None
                or not math.isfinite(float(quote_value))
                or float(quote_value) <= 0
            ):
                raise ValueError("A valid bid/ask quote is unavailable; position remains open.")
            exit_reference = float(quote_value)
            detail = f"Manually closed from the local paper dashboard using the latest quote ({age:.1f}s old); estimated slippage and charges applied."
            self._close_trade(db, trade, exit_reference, "MANUAL_EXIT", detail, current)
            closed = db.execute(
                "SELECT exit_fill_price,net_pnl,transaction_charges,slippage_cost FROM trades WHERE id=?",
                (int(trade_id),),
            ).fetchone()
            return {
                "trade_id": int(trade_id),
                "symbol": trade["symbol"],
                "side": side,
                "quantity": int(trade["quantity"]),
                "exit_reference_price": exit_reference,
                "exit_fill_price": float(closed["exit_fill_price"]),
                "net_pnl": float(closed["net_pnl"]),
                "transaction_charges": float(closed["transaction_charges"]),
                "slippage_cost": float(closed["slippage_cost"]),
                "exit_reason": "MANUAL_EXIT",
            }

    def _close_trade(self, db, trade, exit_reference, exit_reason, exit_detail, at):
        if trade["status"] != "OPEN":
            return
        side = trade["side"]
        qty = int(trade["quantity"])
        entry_ref = float(trade["entry_reference_price"])
        entry_fill = float(trade["entry_fill_price"])
        exit_ref = _finite_positive(exit_reference, "exit_reference")
        exit_fill = (
            exit_ref * (1 - self.slippage_bps / 10000)
            if side == "LONG"
            else exit_ref * (1 + self.slippage_bps / 10000)
        )
        gross = ((exit_ref - entry_ref) if side == "LONG" else (entry_ref - exit_ref)) * qty
        _, rates = load_phase2_settings()
        fees = estimate_intraday_round_trip_cost(
            side=side, entry_price=entry_fill, exit_price=exit_fill, quantity=qty, rates=rates
        )
        slip = abs(entry_fill - entry_ref) * qty + abs(exit_fill - exit_ref) * qty
        total = fees.total + slip
        net = gross - total
        db.execute(
            """UPDATE trades SET status='CLOSED',exit_time=?,exit_reference_price=?,exit_fill_price=?,exit_reason=?,exit_detail=?,
          gross_pnl=?,brokerage=?,stt=?,exchange_charges=?,sebi_charges=?,gst=?,stamp_duty=?,ipft=?,transaction_charges=?,
          slippage_cost=?,total_estimated_costs=?,net_pnl=? WHERE id=? AND status='OPEN'""",
            (
                at.astimezone(IST).isoformat(),
                exit_ref,
                exit_fill,
                exit_reason,
                exit_detail,
                gross,
                fees.brokerage,
                fees.stt,
                fees.exchange_charges,
                fees.sebi_charges,
                fees.gst,
                fees.stamp_duty,
                fees.ipft,
                fees.total,
                slip,
                total,
                net,
                trade["id"],
            ),
        )
        self._event(
            db,
            "PAPER_EXIT",
            f"Simulated {side} exit: {qty} {trade['symbol']} @ ₹{exit_fill:.2f}; net P&L ₹{net:.2f}.",
            symbol=trade["symbol"],
            details={
                "trade_id": trade["id"],
                "signal_id": trade["signal_id"],
                "strategy": trade["strategy"],
                "quantity": qty,
                "entry_fill_price": entry_fill,
                "exit_fill_price": exit_fill,
                "exit_reason": exit_reason,
                "exit_detail": exit_detail,
                "gross_pnl": gross,
                "brokerage": fees.brokerage,
                "stt": fees.stt,
                "exchange_charges": fees.exchange_charges,
                "sebi_charges": fees.sebi_charges,
                "gst": fees.gst,
                "stamp_duty": fees.stamp_duty,
                "ipft": fees.ipft,
                "transaction_charges": fees.total,
                "slippage_cost": slip,
                "total_estimated_costs": total,
                "net_pnl": net,
                "entry_reason": trade["entry_reason"],
            },
            event_key=f"exit:{trade['signal_id']}",
            at=at,
        )
        planned_risk = abs(float(trade["risk_budget_inr"] or 0))
        if planned_risk and net < -planned_risk:
            self._event(
                db,
                "RISK_OVERRUN",
                f"Simulated net loss for {trade['symbol']} was ₹{abs(net):.2f}, exceeding planned per-trade risk ₹{planned_risk:.2f}; stop gaps/slippage can exceed the plan.",
                symbol=trade["symbol"],
                severity="WARNING",
                details={
                    "trade_id": trade["id"],
                    "signal_id": trade["signal_id"],
                    "planned_risk_inr": planned_risk,
                    "planned_net_risk_inr": float(trade["planned_net_risk_inr"] or 0),
                    "stop_loss": float(trade["stop_loss"]),
                    "stop_gap_loss_inr": (
                        max(0.0, float(trade["stop_loss"]) - exit_ref) * qty
                        if exit_reason == "STOP_LOSS" and side == "LONG"
                        else max(0.0, exit_ref - float(trade["stop_loss"])) * qty
                        if exit_reason == "STOP_LOSS" and side == "SHORT"
                        else 0.0
                    ),
                    "stop_gap_risk_reserve_pct": self.stop_gap_risk_reserve_pct,
                    "realized_net_loss_inr": abs(net),
                    "excess_loss_inr": abs(net) - planned_risk,
                    "exit_reason": exit_reason,
                    "exit_reference_price": exit_ref,
                    "exit_fill_price": exit_fill,
                    "slippage_cost": slip,
                    "total_estimated_costs": total,
                },
                event_key=f"risk-overrun:{trade['signal_id']}",
                at=at,
            )

    def record_research_observations(
        self, observations: list[dict[str, Any]], *, observed_at: datetime | None = None
    ) -> None:
        current = observed_at or datetime.now(IST)
        current = current.replace(tzinfo=IST) if current.tzinfo is None else current.astimezone(IST)
        with self._connect() as db:
            for item in observations:
                version = str(item.get("strategy_version") or "")
                symbol = str(item.get("symbol") or "").upper()
                action = str(item.get("action") or "NO_SIGNAL")
                candle = str(item.get("signal_time") or "")
                if not version or not symbol or not candle:
                    continue
                self._event(
                    db,
                    "CHALLENGER_OBSERVATION",
                    f"{item.get('strategy_name', version)}: {action} for {symbol}.",
                    symbol=symbol,
                    details=item,
                    event_key=f"research:{version}:{symbol}:{candle}",
                    at=current,
                )

    def snapshot(self) -> dict[str, Any]:
        with self._connect() as db:
            capital, realized = self._account_values(db)
            opens = self._open_rows(db)
            marks = {r["symbol"]: dict(r) for r in db.execute("SELECT * FROM market_marks")}
            positions = []
            unreal = 0.0
            exposure = 0.0
            _, rates = load_phase2_settings()
            open_planned_risk = sum(
                float(r["planned_net_risk_inr"] or r["risk_budget_inr"] or self.risk_per_trade)
                for r in opens
            )
            available_portfolio_risk = max(0.0, self.max_portfolio_risk_inr - open_planned_risk)
            for row in opens:
                trade = dict(row)
                mark = marks.get(trade["symbol"])
                last = float(mark["last_price"]) if mark else float(trade["entry_reference_price"])
                if mark and row["side"] == "LONG":
                    exit_ref = float(mark["bid"] or mark["last_price"])
                elif mark:
                    exit_ref = float(mark["ask"] or mark["last_price"])
                else:
                    exit_ref = float(trade["entry_reference_price"])
                entry_ref = float(row["entry_reference_price"])
                qty = int(row["quantity"])
                gross = (
                    (exit_ref - entry_ref) if row["side"] == "LONG" else (entry_ref - exit_ref)
                ) * qty
                exit_fill = (
                    exit_ref * (1 - self.slippage_bps / 10000)
                    if row["side"] == "LONG"
                    else exit_ref * (1 + self.slippage_bps / 10000)
                )
                fees = estimate_intraday_round_trip_cost(
                    side=row["side"],
                    entry_price=float(row["entry_fill_price"]),
                    exit_price=exit_fill,
                    quantity=qty,
                    rates=rates,
                )
                slip = (
                    abs(float(row["entry_fill_price"]) - entry_ref) * qty
                    + abs(exit_fill - exit_ref) * qty
                )
                net_mark = gross - fees.total - slip
                unreal += net_mark
                exposure += float(row["position_value_inr"])
                rr_metrics = calculate_net_reward_risk(
                    side=row["side"],
                    entry_price=float(row["entry_reference_price"]),
                    stop_loss=float(row["stop_loss"]),
                    target_1=float(row["target_1"]),
                    target_2=float(row["target_2"]),
                    quantity=qty,
                    slippage_bps_per_side=self.slippage_bps,
                    charge_rates=rates,
                )
                trade.update(
                    {
                        "last_price": last,
                        "unrealized_gross_pnl": gross,
                        "unrealized_net_pnl": net_mark,
                        "market_time": mark.get("market_time") if mark else None,
                        **rr_metrics,
                    }
                )
                positions.append(trade)
            closed = list(
                db.execute("SELECT * FROM trades WHERE status='CLOSED' ORDER BY exit_time DESC")
            )
            count = len(closed)
            wins = sum(1 for r in closed if float(r["net_pnl"] or 0) > 0)
            losses = sum(1 for r in closed if float(r["net_pnl"] or 0) < 0)
            gw = sum(float(r["net_pnl"] or 0) for r in closed if float(r["net_pnl"] or 0) > 0)
            gl = -sum(float(r["net_pnl"] or 0) for r in closed if float(r["net_pnl"] or 0) < 0)
            expectancy = sum(float(r["net_pnl"] or 0) for r in closed) / count if count else 0.0
            peak = equity = capital
            dd = 0.0
            for r in reversed(closed):
                equity += float(r["net_pnl"] or 0)
                peak = max(peak, equity)
                dd = max(dd, peak - equity)
            strategies = []
            for r in db.execute("""SELECT strategy,strategy_version,COUNT(*) trades,
              SUM(CASE WHEN net_pnl>0 THEN 1 ELSE 0 END) wins,COALESCE(SUM(net_pnl),0) net_pnl,
              COALESCE(AVG(net_pnl),0) expectancy FROM trades WHERE status='CLOSED' GROUP BY strategy,strategy_version ORDER BY net_pnl DESC"""):
                item = dict(r)
                item["track"] = (
                    "CHAMPION_CANDIDATE"
                    if item["strategy_version"] == PAPER_BASELINE_VERSION
                    else "CHAMPION_ARCHIVE"
                )
                item["win_rate_pct"] = (
                    100.0 * item["wins"] / item["trades"] if item["trades"] else 0.0
                )
                strategies.append(item)
            shadow_open = []
            shadow_closed = []
            for row in db.execute(
                "SELECT * FROM shadow_signals WHERE status='OPEN' ORDER BY entry_time DESC"
            ):
                item = dict(row)
                mark = marks.get(item["symbol"])
                last = float(mark["last_price"]) if mark else float(item["entry_reference_price"])
                if mark and item["side"] == "LONG":
                    exit_ref = float(mark["bid"] or mark["last_price"])
                elif mark:
                    exit_ref = float(mark["ask"] or mark["last_price"])
                else:
                    exit_ref = float(item["entry_reference_price"])
                qty = int(item["quantity"])
                entry_ref = float(item["entry_reference_price"])
                entry_fill = float(item["entry_fill_price"])
                gross = (
                    (exit_ref - entry_ref) if item["side"] == "LONG" else (entry_ref - exit_ref)
                ) * qty
                exit_fill = (
                    exit_ref * (1 - self.slippage_bps / 10000)
                    if item["side"] == "LONG"
                    else exit_ref * (1 + self.slippage_bps / 10000)
                )
                fees = estimate_intraday_round_trip_cost(
                    side=item["side"],
                    entry_price=entry_fill,
                    exit_price=exit_fill,
                    quantity=qty,
                    rates=rates,
                )
                slip = abs(entry_fill - entry_ref) * qty + abs(exit_fill - exit_ref) * qty
                rr_metrics = calculate_net_reward_risk(
                    side=item["side"],
                    entry_price=float(item["entry_reference_price"]),
                    stop_loss=float(item["stop_loss"]),
                    target_1=float(item["target_1"]),
                    target_2=float(item["target_2"]),
                    quantity=qty,
                    slippage_bps_per_side=self.slippage_bps,
                    charge_rates=rates,
                )
                item.update(
                    {
                        "last_price": last,
                        "unrealized_gross_pnl": gross,
                        "unrealized_net_pnl": gross - fees.total - slip,
                        "track": CHALLENGER_VERSION_TRACKS.get(item["strategy_version"], "SHADOW"),
                        "position_value_inr": entry_fill * qty,
                        "market_time": mark.get("market_time") if mark else None,
                        **rr_metrics,
                    }
                )
                shadow_open.append(item)
            for row in db.execute(
                "SELECT * FROM shadow_signals WHERE status='CLOSED' ORDER BY exit_time DESC LIMIT 100"
            ):
                item = dict(row)
                item["strategy"] = item["strategy_name"]
                item["track"] = CHALLENGER_VERSION_TRACKS.get(item["strategy_version"], "SHADOW")
                shadow_closed.append(item)
            for version, name in CHALLENGER_VERSION_NAMES.items():
                row = db.execute(
                    "SELECT COUNT(*) trades,SUM(CASE WHEN net_pnl>0 THEN 1 ELSE 0 END) wins,COALESCE(SUM(net_pnl),0) net_pnl,COALESCE(AVG(net_pnl),0) expectancy FROM shadow_signals WHERE strategy_version=? AND status='CLOSED'",
                    (version,),
                ).fetchone()
                shadow_count = int(row["trades"] or 0)
                strategies.append(
                    {
                        "strategy": name,
                        "strategy_version": version,
                        "track": CHALLENGER_VERSION_TRACKS[version],
                        "trades": shadow_count,
                        "wins": int(row["wins"] or 0),
                        "net_pnl": float(row["net_pnl"] or 0),
                        "expectancy": float(row["expectancy"] or 0),
                        "win_rate_pct": 100.0 * int(row["wins"] or 0) / shadow_count
                        if shadow_count
                        else 0.0,
                    }
                )
            events = [
                dict(r) for r in db.execute("SELECT * FROM events ORDER BY id DESC LIMIT 100")
            ]
            for item in events:
                try:
                    item["details"] = json.loads(item.pop("details_json"))
                except json.JSONDecodeError:
                    item["details"] = {}
            registry = [
                dict(r)
                for r in db.execute("SELECT * FROM strategy_registry ORDER BY track,strategy_name")
            ]
            account_row = db.execute("SELECT created_at FROM account WHERE id=1").fetchone()
            today = self._today_net_pnl(db, datetime.now(IST))
            return {
                "research_only": True,
                "orders_sent_to_broker": False,
                "account": {
                    "starting_capital": capital,
                    "realized_pnl": realized,
                    "unrealized_net_pnl": unreal,
                    "equity": capital + realized + unreal,
                    "available_cash": max(0.0, capital + realized - exposure),
                    "open_exposure": exposure,
                    "open_positions": len(positions),
                    "closed_trades": count,
                    "wins": wins,
                    "losses": losses,
                    "win_rate_pct": 100.0 * wins / count if count else 0.0,
                    "profit_factor": gw / gl if gl else (None if gw == 0 else "infinite"),
                    "expectancy_per_trade": expectancy,
                    "max_drawdown": dd,
                    "today_net_pnl": today,
                    "max_daily_loss": self.max_daily_loss,
                    "risk_per_trade": self.risk_per_trade,
                    "max_open_positions": self.max_open_positions,
                    "open_planned_risk_inr": open_planned_risk,
                    "max_portfolio_risk_inr": self.max_portfolio_risk_inr,
                    "available_portfolio_risk_inr": available_portfolio_risk,
                    "portfolio_risk_utilization_pct": (
                        100.0 * open_planned_risk / self.max_portfolio_risk_inr
                        if self.max_portfolio_risk_inr
                        else 0.0
                    ),
                    "created_at": account_row["created_at"],
                },
                "open_positions": positions,
                "shadow_open_positions": shadow_open,
                "closed_trades": [dict(r) for r in closed[:100]],
                "shadow_closed_trades": shadow_closed,
                "strategies": strategies,
                "strategy_registry": registry,
                "events": events,
                "updated_at": datetime.now(IST).isoformat(),
            }


class _RejectedSignal(Exception):
    pass
