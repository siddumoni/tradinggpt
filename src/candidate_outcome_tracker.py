"""Independent, candle-based outcomes for eligible watch candidates.

This is an outcome research ledger, not a portfolio: every eligible near-miss
signal is simulated independently, without watch-shadow position, same-symbol,
or cash-reservation limits. Overlapping outcomes must not be summed as portfolio P&L.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from src.cost_calculator import estimate_intraday_round_trip_cost
from src.paper_outcomes import load_candles
from src.risk_engine import RiskSettings, assess_trade, load_phase2_settings
from src.research_timing import next_five_minute_bar_after

IST = ZoneInfo("Asia/Kolkata")
OUTCOME_CAPITAL_INR = 100_000.0
OUTCOME_RISK_INR = 500.0
SLIPPAGE_BPS = 5.0
MIN_ENTRY_RR = 0.01
ENTRY_CUTOFF = dtime(14, 45)
LAST_BAR = dtime(15, 10)
RR_REASON_PATTERN = re.compile(r"rejected:\s*target\s*[12]\s+net\s+r:r\s+is\s+below\s+1\.50", re.IGNORECASE)
METHOD_VERSION = "availability_aware_v3"
LEGACY_INVALIDATION_REASON = (
    "Superseded: the legacy evaluator entered from the signal candle without waiting for "
    "scanner publication and also double-counted entry slippage in risk sizing."
)


def _to_ist(value: Any) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    if pd.isna(stamp):
        raise ValueError("timestamp is missing")
    return stamp.tz_localize(IST) if stamp.tzinfo is None else stamp.tz_convert(IST)


def _positive(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _candidate_id(candidate: dict[str, Any]) -> str:
    raw_signal_time = str(candidate.get("signal_time") or "").strip()
    try:
        canonical_signal_time = _to_ist(raw_signal_time).isoformat() if raw_signal_time else ""
    except (TypeError, ValueError, OverflowError):
        canonical_signal_time = raw_signal_time
    parts = (
        METHOD_VERSION,
        str(candidate.get("symbol") or "").strip().upper(),
        str(candidate.get("side") or "").strip().upper(),
        str(candidate.get("strategy") or "").strip().lower(),
        canonical_signal_time,
    )
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]


def _is_net_rr_rejection(value: Any) -> bool:
    return bool(RR_REASON_PATTERN.search(str(value or "")))


def _reason(candidate: dict[str, Any]) -> str:
    reasons = candidate.get("reasons")
    if not isinstance(reasons, list):
        return ""
    return next((str(value) for value in reasons if _is_net_rr_rejection(value)), "")


class CandidateOutcomeTracker:
    """Track every eligible R:R-only near miss independently using subsequent candles."""

    def __init__(self, db_path: Path, *, capital_inr: float = OUTCOME_CAPITAL_INR,
                 risk_per_trade_inr: float = OUTCOME_RISK_INR,
                 slippage_bps: float = SLIPPAGE_BPS) -> None:
        for name, value in (("capital_inr", capital_inr), ("risk_per_trade_inr", risk_per_trade_inr)):
            if not math.isfinite(float(value)) or float(value) <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if not math.isfinite(float(slippage_bps)) or not 0 <= float(slippage_bps) <= 100:
            raise ValueError("slippage_bps must be between 0 and 100")
        self.db_path = Path(db_path)
        self.capital_inr = float(capital_inr)
        self.risk_per_trade_inr = float(risk_per_trade_inr)
        self.slippage_bps = float(slippage_bps)
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
                CREATE TABLE IF NOT EXISTS candidate_outcomes (
                    candidate_id TEXT PRIMARY KEY, symbol TEXT NOT NULL,
                    side TEXT NOT NULL CHECK(side IN ('LONG','SHORT')), strategy TEXT NOT NULL,
                    signal_time TEXT NOT NULL, scan_as_of TEXT NOT NULL, signal_available_at TEXT,
                    signal_close REAL,
                    stop_loss REAL, target_1 REAL, target_2 REAL, market_regime TEXT,
                    news_status TEXT, news_risk TEXT, status TEXT NOT NULL,
                    entry_time TEXT, entry_reference_price REAL, entry_fill_price REAL,
                    quantity INTEGER NOT NULL DEFAULT 0, risk_budget_inr REAL NOT NULL,
                    assessed_net_rr_t1 REAL, estimated_net_risk_inr REAL,
                    estimated_net_reward_t1_inr REAL, exit_time TEXT, exit_reference_price REAL,
                    exit_fill_price REAL, exit_reason TEXT, exit_detail TEXT, gross_pnl REAL,
                    brokerage REAL, stt REAL, exchange_charges REAL, sebi_charges REAL,
                    gst REAL, stamp_duty REAL, ipft REAL, transaction_charges REAL,
                    slippage_cost REAL, total_estimated_costs REAL, net_pnl REAL,
                    source_rejection_reason TEXT NOT NULL, exclusion_reason TEXT,
                    method_version TEXT NOT NULL DEFAULT 'availability_aware_v3',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_candidate_outcome_status
                    ON candidate_outcomes(status, symbol, entry_time);
                CREATE INDEX IF NOT EXISTS idx_candidate_outcome_signal
                    ON candidate_outcomes(signal_time, strategy);
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(candidate_outcomes)")}
            if "signal_available_at" not in columns:
                db.execute("ALTER TABLE candidate_outcomes ADD COLUMN signal_available_at TEXT")
            if "method_version" not in columns:
                db.execute("ALTER TABLE candidate_outcomes ADD COLUMN method_version TEXT NOT NULL DEFAULT 'legacy_v1'")
            db.execute("""
                CREATE TABLE IF NOT EXISTS candidate_outcome_invalidations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    candidate_id TEXT NOT NULL,
                    method_version TEXT NOT NULL,
                    previous_status TEXT NOT NULL,
                    invalidated_at TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    previous_record_json TEXT NOT NULL
                )
            """)
            legacy = list(db.execute(
                "SELECT * FROM candidate_outcomes WHERE method_version<>? "
                "AND status IN ('PENDING_ENTRY','OPEN','CLOSED','INCOMPLETE_DATA_GAP','EXPIRED_NO_ENTRY')",
                (METHOD_VERSION,),
            ))
            invalidated_at = datetime.now(IST).isoformat()
            for row in legacy:
                record = dict(row)
                db.execute("""
                    INSERT INTO candidate_outcome_invalidations(
                      candidate_id,method_version,previous_status,invalidated_at,reason,previous_record_json
                    ) VALUES(?,?,?,?,?,?)
                """, (row["candidate_id"], row["method_version"], row["status"],
                      invalidated_at, LEGACY_INVALIDATION_REASON,
                      json.dumps(record, ensure_ascii=False, default=str, allow_nan=False)))
                db.execute("""
                    UPDATE candidate_outcomes SET status='INVALIDATED_LEGACY_METHOD',
                      gross_pnl=NULL,brokerage=NULL,stt=NULL,exchange_charges=NULL,sebi_charges=NULL,
                      gst=NULL,stamp_duty=NULL,ipft=NULL,transaction_charges=NULL,slippage_cost=NULL,
                      total_estimated_costs=NULL,net_pnl=NULL,updated_at=? WHERE candidate_id=?
                """, (invalidated_at, row["candidate_id"]))
            db.execute("CREATE INDEX IF NOT EXISTS idx_candidate_outcome_method ON candidate_outcomes(method_version,status)")

    @staticmethod
    def _eligibility_failure(candidate: dict[str, Any], scan_time: pd.Timestamp,
                             available_at: pd.Timestamp | None) -> str | None:
        if not _reason(candidate):
            return "Not a minimum net-R:R-only rejection."
        validation = candidate.get("entry_validation")
        if not isinstance(validation, dict) or validation.get("valid") is not True:
            return "Scanner live-entry validation did not pass."
        if str(validation.get("status") or "").upper() != "REJECTED_NET_RR":
            return "Scanner rejection was not exclusively the minimum net-R:R gate."
        if not _is_net_rr_rejection(validation.get("reason")):
            return "Scanner entry-validation reason does not confirm the minimum net-R:R gate."
        if not validation.get("quote_timestamp"):
            return "Scanner entry-validation quote timestamp is missing."
        if available_at is None:
            return "A valid scanner publication/quote timestamp is unavailable."
        if candidate.get("data_verified_live") is not True:
            return "Live data verification did not pass."
        if candidate.get("margin_verified") is not True:
            return "Read-only margin verification did not pass."
        if str(candidate.get("company_specific_risk") or "").upper() == "HIGH":
            return "High company-specific news risk."
        if str(candidate.get("news_status") or "").upper() in {"", "NOT_CHECKED", "UNAVAILABLE"}:
            return "News/event checks are incomplete."
        side = str(candidate.get("side") or "").upper()
        if side not in {"LONG", "SHORT"}:
            return "Candidate side is missing or invalid."
        signal_value = candidate.get("signal_time")
        if not signal_value:
            return "Candidate signal timestamp is missing or invalid."
        try:
            signal_time = _to_ist(signal_value)
        except (TypeError, ValueError, OverflowError):
            return "Candidate signal timestamp is missing or invalid."
        if signal_time.date() != scan_time.date():
            return "Candidate signal does not belong to the scan session."
        entry_time = next_five_minute_bar_after(signal_time, available_at)
        if entry_time.date() != signal_time.date() or entry_time.time().replace(tzinfo=None) >= ENTRY_CUTOFF:
            return "First candle after scanner publication is at or after the 14:45 IST cutoff."
        entry, stop, target_1, target_2 = (
            _positive(candidate.get(key))
            for key in ("reference_entry", "stop_loss", "target_1", "target_2")
        )
        if None in (entry, stop, target_1, target_2):
            return "Candidate has missing or invalid price levels."
        if side == "LONG" and not stop < entry < target_1 <= target_2:
            return "Saved long-side stop/target levels are inconsistent."
        if side == "SHORT" and not target_2 <= target_1 < entry < stop:
            return "Saved short-side stop/target levels are inconsistent."
        return None

    def process_scan_report(self, report_path: Path) -> dict[str, int]:
        """Persist watch candidates, deduplicated by symbol/side/strategy/signal bar."""
        path = Path(report_path)
        try:
            report = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Could not read scan report {path}: {exc}") from exc
        if not isinstance(report, dict) or report.get("status") != "RESEARCH_SCAN_ONLY":
            raise ValueError("Candidate outcomes require a RESEARCH_SCAN_ONLY JSON report")
        if report.get("orders_placed_or_modified") is not False:
            raise ValueError("Candidate outcomes require an explicit no-order assertion")
        if str(report.get("data_provider_mode") or "").lower() != "kite":
            raise ValueError("Candidate outcomes only accept Kite provider reports")
        candidates = report.get("candidates")
        if not isinstance(candidates, list):
            raise ValueError("Scan report candidates schema is invalid")
        as_of = str(report.get("as_of") or "")
        if not as_of:
            raise ValueError("Scan report is missing as_of timestamp")
        scan_time = _to_ist(as_of)
        scan_completed_raw = report.get("scan_completed_at") or as_of
        try:
            scan_completed = _to_ist(scan_completed_raw)
        except (TypeError, ValueError, OverflowError):
            scan_completed = scan_time
        counts = {"seen": 0, "eligible": 0, "excluded": 0, "duplicates": 0}
        now = datetime.now(IST).isoformat()
        with self._connect() as db:
            for item in candidates:
                if not isinstance(item, dict) or str(item.get("action") or "").upper() != "WATCH_CANDIDATE":
                    continue
                counts["seen"] += 1
                candidate_id = _candidate_id(item)
                if db.execute("SELECT 1 FROM candidate_outcomes WHERE candidate_id=?", (candidate_id,)).fetchone():
                    counts["duplicates"] += 1
                    continue
                rejection = _reason(item)
                validation = item.get("entry_validation") if isinstance(item.get("entry_validation"), dict) else {}
                quote_time_raw = validation.get("quote_timestamp")
                available_at = None
                try:
                    available_candidates = [scan_time, scan_completed]
                    if quote_time_raw:
                        available_candidates.append(_to_ist(quote_time_raw))
                    available_at = max(available_candidates)
                except (TypeError, ValueError, OverflowError):
                    available_at = None
                failure = self._eligibility_failure(item, scan_time, available_at)
                side = str(item.get("side") or "LONG").strip().upper()
                if side not in {"LONG", "SHORT"}:
                    side = "LONG"
                values = (
                    candidate_id, str(item.get("symbol") or "UNKNOWN").strip().upper(), side,
                    str(item.get("strategy") or "unknown"), str(item.get("signal_time") or as_of),
                    as_of, available_at.isoformat() if available_at is not None else None,
                    _positive(item.get("reference_entry")), _positive(item.get("stop_loss")),
                    _positive(item.get("target_1")), _positive(item.get("target_2")),
                    str(item.get("market_regime") or ""), str(item.get("news_status") or ""),
                    str(item.get("news_risk") or ""), "EXCLUDED" if failure else "PENDING_ENTRY",
                    self.risk_per_trade_inr, rejection or "WATCH_CANDIDATE", failure,
                    METHOD_VERSION, now, now,
                )
                db.execute("""
                    INSERT INTO candidate_outcomes(
                      candidate_id,symbol,side,strategy,signal_time,scan_as_of,signal_available_at,signal_close,
                      stop_loss,target_1,target_2,market_regime,news_status,news_risk,status,
                      risk_budget_inr,source_rejection_reason,exclusion_reason,method_version,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, values)
                counts["excluded" if failure else "eligible"] += 1
        return counts

    def _set_status(self, db: sqlite3.Connection, candidate_id: str, status: str,
                    reason: str) -> None:
        db.execute(
            "UPDATE candidate_outcomes SET status=?,exclusion_reason=?,updated_at=? WHERE candidate_id=?",
            (status, reason, datetime.now(IST).isoformat(), candidate_id),
        )

    def _enter(self, db: sqlite3.Connection, row: sqlite3.Row, frame: pd.DataFrame,
               *, finalize_session: bool) -> None:
        signal_time = _to_ist(row["signal_time"])
        available_at = _to_ist(row["signal_available_at"])
        entry_time = next_five_minute_bar_after(signal_time, available_at)
        if entry_time.date() != signal_time.date() or entry_time.time().replace(tzinfo=None) >= ENTRY_CUTOFF:
            self._set_status(db, row["candidate_id"], "EXPIRED_NO_ENTRY",
                             "Next-bar entry is outside the same-session entry window.")
            return
        if signal_time not in frame.index:
            if (not frame.empty and frame.index[-1] > signal_time) or finalize_session:
                self._set_status(db, row["candidate_id"], "INCOMPLETE_DATA_GAP",
                                 "Signal candle is missing from the Kite archive; no entry was fabricated.")
            return
        observed_close = float(frame.loc[signal_time]["close"])
        expected_close = float(row["signal_close"])
        tolerance = max(0.02, expected_close * 5.0 / 10_000.0)
        if abs(observed_close - expected_close) > tolerance:
            self._set_status(
                db, row["candidate_id"], "EXCLUDED",
                f"Signal reference close {expected_close:.4f} differs from archived close "
                f"{observed_close:.4f} by more than tolerance {tolerance:.4f}.",
            )
            return
        index = frame.index.get_indexer([entry_time])[0]
        if index < 0:
            if (not frame.empty and frame.index[-1] >= entry_time) or finalize_session:
                self._set_status(db, row["candidate_id"], "INCOMPLETE_DATA_GAP",
                                 "First five-minute candle after scanner publication is missing; no later open was substituted.")
            return

        expected_path = pd.date_range(signal_time, entry_time, freq="5min")
        observed_path = frame.index[(frame.index >= signal_time) & (frame.index <= entry_time)]
        if not observed_path.equals(expected_path):
            self._set_status(db, row["candidate_id"], "INCOMPLETE_DATA_GAP",
                             "Candle continuity between signal and publication-aware entry is incomplete; no P&L assigned.")
            return
        entry_reference = float(frame.iloc[index]["open"])
        side = str(row["side"])
        if side not in {"LONG", "SHORT"}:
            self._set_status(db, row["candidate_id"], "EXCLUDED",
                             f"Unsupported candidate side: {side!r}.")
            return
        entry_fill = entry_reference * (1 + self.slippage_bps / 10_000) if side == "LONG" else entry_reference * (1 - self.slippage_bps / 10_000)
        stop, target_1, target_2 = float(row["stop_loss"]), float(row["target_1"]), float(row["target_2"])
        if side == "LONG":
            reference_valid = stop < entry_reference < target_1 <= target_2
            fill_valid = stop < entry_fill < target_1 <= target_2
        else:
            reference_valid = target_2 <= target_1 < entry_reference < stop
            fill_valid = target_2 <= target_1 < entry_fill < stop
        if not reference_valid:
            self._set_status(db, row["candidate_id"], "EXCLUDED",
                             "Reference next-bar open invalidated the saved stop/target price levels.")
            return
        if not fill_valid:
            self._set_status(db, row["candidate_id"], "EXCLUDED",
                             "Slippage-adjusted executable fill invalidated the saved stop/target price levels.")
            return

        base, rates = load_phase2_settings()
        settings = RiskSettings(
            capital_inr=self.capital_inr, risk_per_trade_inr=self.risk_per_trade_inr,
            minimum_net_risk_reward=MIN_ENTRY_RR, require_margin_verification=False,
            skip_if_margin_unavailable=False, slippage_bps_per_side=self.slippage_bps,
            entry_cutoff_ist=base.entry_cutoff_ist, square_off_time_ist=base.square_off_time_ist,
            minimum_net_rr_target=base.minimum_net_rr_target,
        )
        try:
            assessment = assess_trade(
                side=side, entry_price=entry_reference, stop_loss=stop, target_1=target_1,
                target_2=target_2, settings=settings, estimated_margin_per_share=entry_fill,
                charge_rates=rates,
            )
        except ValueError as exc:
            self._set_status(
                db, row["candidate_id"], "EXCLUDED",
                f"Risk assessment rejected the publication-aware entry: {exc}",
            )
            return
        if not assessment.accepted or int(assessment.quantity) < 1:
            self._set_status(db, row["candidate_id"], "EXCLUDED",
                             "Independent cash-only sizing failed: " + str(assessment.reason))
            return
        db.execute("""
            UPDATE candidate_outcomes SET status='OPEN',entry_time=?,entry_reference_price=?,
              entry_fill_price=?,quantity=?,assessed_net_rr_t1=?,estimated_net_risk_inr=?,
              estimated_net_reward_t1_inr=?,exclusion_reason=NULL,updated_at=? WHERE candidate_id=?
        """, (entry_time.isoformat(), entry_reference, entry_fill, int(assessment.quantity),
              float(assessment.net_rr_t1), float(assessment.net_risk_inr),
              float(assessment.net_reward_t1_inr), datetime.now(IST).isoformat(), row["candidate_id"]))

    def _close(self, db: sqlite3.Connection, row: sqlite3.Row, exit_reference: float,
               reason: str, detail: str, exit_time: pd.Timestamp) -> None:
        side, quantity = str(row["side"]), int(row["quantity"])
        entry_reference, entry_fill = float(row["entry_reference_price"]), float(row["entry_fill_price"])
        exit_fill = exit_reference * (1 - self.slippage_bps / 10_000) if side == "LONG" else exit_reference * (1 + self.slippage_bps / 10_000)
        gross = ((exit_reference - entry_reference) if side == "LONG" else (entry_reference - exit_reference)) * quantity
        _, rates = load_phase2_settings()
        fees = estimate_intraday_round_trip_cost(
            side=side, entry_price=entry_fill, exit_price=exit_fill, quantity=quantity, rates=rates,
        )
        slippage = abs(entry_fill - entry_reference) * quantity + abs(exit_fill - exit_reference) * quantity
        total_costs = fees.total + slippage
        db.execute("""
            UPDATE candidate_outcomes SET status='CLOSED',exit_time=?,exit_reference_price=?,
              exit_fill_price=?,exit_reason=?,exit_detail=?,gross_pnl=?,brokerage=?,stt=?,
              exchange_charges=?,sebi_charges=?,gst=?,stamp_duty=?,ipft=?,transaction_charges=?,
              slippage_cost=?,total_estimated_costs=?,net_pnl=?,updated_at=? WHERE candidate_id=?
        """, (exit_time.isoformat(), exit_reference, exit_fill, reason, detail, gross,
              fees.brokerage, fees.stt, fees.exchange_charges, fees.sebi_charges, fees.gst,
              fees.stamp_duty, fees.ipft, fees.total, slippage, total_costs, gross - total_costs,
              datetime.now(IST).isoformat(), row["candidate_id"]))

    def _monitor(self, db: sqlite3.Connection, row: sqlite3.Row, frame: pd.DataFrame,
                 *, finalize_session: bool) -> None:
        entry_time = _to_ist(row["entry_time"])
        session = frame.loc[frame.index.date == entry_time.date()]
        bars = session.loc[entry_time:]
        if bars.empty or bars.index[0] != entry_time:
            if finalize_session:
                last = session.index[-1].isoformat() if not session.empty else "unavailable"
                self._set_status(db, row["candidate_id"], "INCOMPLETE_DATA_GAP",
                                 f"Session finalized without the exact entry candle; last archived candle={last}.")
            return
        previous = None
        for stamp, bar in bars.iterrows():
            if previous is not None and stamp != previous + pd.Timedelta(minutes=5):
                self._set_status(db, row["candidate_id"], "INCOMPLETE_DATA_GAP",
                                 f"Missing five-minute candle after {previous.isoformat()}; outcome unscored.")
                return
            op, high, low = float(bar["open"]), float(bar["high"]), float(bar["low"])
            side, stop, target_2 = str(row["side"]), float(row["stop_loss"]), float(row["target_2"])
            stop_hit = low <= stop if side == "LONG" else high >= stop
            target_hit = high >= target_2 if side == "LONG" else low <= target_2
            exit_time = stamp + pd.Timedelta(minutes=5)
            if stop_hit:
                exit_reference = min(stop, op) if side == "LONG" else max(stop, op)
                self._close(db, row, exit_reference, "STOP_LOSS",
                            "Stop-first OHLC handling; adverse stop gaps use the worse bar open.", exit_time)
                return
            if target_hit:
                self._close(db, row, target_2, "TARGET_2",
                            "Target 2 touched; no favorable gap improvement.", exit_time)
                return
            if stamp.time().replace(tzinfo=None) == LAST_BAR:
                self._close(db, row, float(bar["close"]), "TIME_SQUARE_OFF",
                            "Closed at the final regular-session five-minute candle close.", exit_time)
                return
            previous = stamp
        if finalize_session:
            last = bars.index[-1].isoformat() if not bars.empty else "unavailable"
            self._set_status(db, row["candidate_id"], "INCOMPLETE_DATA_GAP",
                             f"Session finalized without a defensible exit; last archived candle={last}; "
                             "continuous coverage through the 15:10 candle is required. No P&L assigned.")

    def process_archive(self, archive_dir: Path, *, now: datetime | pd.Timestamp | None = None,
                        finalize_session: bool = False) -> dict[str, int]:
        """Advance pending entries and open outcomes using Kite candles, never guessed bars."""
        with self._connect() as db:
            incomplete_before = int(db.execute(
                "SELECT COUNT(*) FROM candidate_outcomes WHERE status='INCOMPLETE_DATA_GAP'"
            ).fetchone()[0])
            active = list(db.execute(
                "SELECT DISTINCT symbol FROM candidate_outcomes WHERE status IN ('PENDING_ENTRY','OPEN')"
            ))
            processed = 0
            for item in active:
                symbol = str(item["symbol"]).upper()
                path = Path(archive_dir) / f"{symbol}.csv"
                if not path.is_file():
                    continue
                try:
                    frame = load_candles(path)
                except (OSError, ValueError, pd.errors.ParserError):
                    continue
                modes = ({str(value).strip().lower() for value in frame["provider_mode"].dropna().unique()}
                         if "provider_mode" in frame.columns else set())
                if modes != {"kite"}:
                    continue
                rows = list(db.execute(
                    "SELECT * FROM candidate_outcomes WHERE symbol=? AND status IN ('PENDING_ENTRY','OPEN')",
                    (symbol,),
                ))
                for row in rows:
                    if row["status"] == "PENDING_ENTRY":
                        self._enter(db, row, frame, finalize_session=finalize_session)
                    row = db.execute("SELECT * FROM candidate_outcomes WHERE candidate_id=?",
                                     (row["candidate_id"],)).fetchone()
                    if row is not None and row["status"] == "OPEN":
                        self._monitor(db, row, frame, finalize_session=finalize_session)
                    processed += 1

            if finalize_session:
                pending = list(db.execute(
                    "SELECT candidate_id,signal_time,signal_available_at FROM candidate_outcomes WHERE status='PENDING_ENTRY'"
                ))
                for row in pending:
                    entry_time = next_five_minute_bar_after(
                        _to_ist(row["signal_time"]), _to_ist(row["signal_available_at"])
                    )
                    status = "EXPIRED_NO_ENTRY" if entry_time.time().replace(tzinfo=None) >= ENTRY_CUTOFF else "INCOMPLETE_DATA_GAP"
                    reason = ("Next-bar entry is outside the same-session cutoff; no entry simulated."
                              if status == "EXPIRED_NO_ENTRY" else
                              "Session finalized before the exact next-bar candle was archived; no entry fabricated.")
                    self._set_status(db, row["candidate_id"], status, reason)
                opened = list(db.execute("SELECT candidate_id FROM candidate_outcomes WHERE status='OPEN'"))
                for row in opened:
                    self._set_status(db, row["candidate_id"], "INCOMPLETE_DATA_GAP",
                                     "Session finalized without a valid target, stop, or final-bar exit; no P&L assigned.")
            incomplete = int(db.execute(
                "SELECT COUNT(*) FROM candidate_outcomes WHERE status='INCOMPLETE_DATA_GAP'"
            ).fetchone()[0]) - incomplete_before
            counts = {
                "symbols": len(active), "observations_processed": processed,
                "open_or_pending": int(db.execute(
                    "SELECT COUNT(*) FROM candidate_outcomes WHERE status IN ('OPEN','PENDING_ENTRY')"
                ).fetchone()[0]),
                "incomplete_finalized": int(incomplete),
            }
        return counts

    def open_symbols(self) -> list[str]:
        """Symbols whose eligible outcomes still need subsequent five-minute candles."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT DISTINCT symbol FROM candidate_outcomes WHERE status IN ('OPEN','PENDING_ENTRY')"
            )
            return sorted({str(row["symbol"]).strip().upper() for row in rows if row["symbol"]})

    def snapshot(self, report_date: str | None = None) -> dict[str, Any]:
        with self._connect() as db:
            if report_date:
                rows = [dict(row) for row in db.execute(
                    "SELECT * FROM candidate_outcomes WHERE substr(signal_time,1,10)=? "
                    "OR substr(exit_time,1,10)=? ORDER BY COALESCE(exit_time,signal_time)",
                    (report_date, report_date),
                )]
            else:
                rows = [dict(row) for row in db.execute(
                    "SELECT * FROM candidate_outcomes ORDER BY COALESCE(exit_time,signal_time)"
                )]
        eligible = [row for row in rows if row["status"] not in {"EXCLUDED", "INVALIDATED_LEGACY_METHOD"}]
        closed = [row for row in eligible if row["status"] == "CLOSED"]
        wins = [row for row in closed if float(row.get("net_pnl") or 0) > 0]
        losses = [row for row in closed if float(row.get("net_pnl") or 0) < 0]
        status_counts: dict[str, int] = {}
        strategies: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            status_counts[row["status"]] = status_counts.get(row["status"], 0) + 1
            if row["status"] not in {"EXCLUDED", "INVALIDATED_LEGACY_METHOD"}:
                strategies.setdefault(str(row.get("strategy") or "unknown"), []).append(row)
        strategy_summary = []
        for strategy, items in sorted(strategies.items()):
            outcomes = [item for item in items if item["status"] == "CLOSED"]
            positive = [item for item in outcomes if float(item.get("net_pnl") or 0) > 0]
            negative = [item for item in outcomes if float(item.get("net_pnl") or 0) < 0]
            strategy_summary.append({
                "strategy": strategy, "eligible_observations": len(items),
                "closed_outcomes": len(outcomes), "wins": len(positive), "losses": len(negative),
                "win_rate_pct": (len(positive) / len(outcomes) * 100) if outcomes else None,
            })
        return {
            "eligible_observations": len(eligible), "closed_outcomes": len(closed),
            "wins": len(wins), "losses": len(losses),
            "incomplete_data_gaps": status_counts.get("INCOMPLETE_DATA_GAP", 0),
            "pending_entries": status_counts.get("PENDING_ENTRY", 0),
            "open_outcomes": status_counts.get("OPEN", 0),
            "expired_no_entry": status_counts.get("EXPIRED_NO_ENTRY", 0),
            "excluded": status_counts.get("EXCLUDED", 0),
            "invalidated_legacy_method": status_counts.get("INVALIDATED_LEGACY_METHOD", 0),
            "status_counts": status_counts, "strategy_summary": strategy_summary,
            "observations": rows, "research_only": True,
            "independent_single_candidate_model": True,
            "aggregate_pnl_is_not_portfolio_pnl": True, "orders_sent_to_broker": False,
        }
