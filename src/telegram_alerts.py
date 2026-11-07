"""Optional Telegram notifications for TradingGPT paper-account events.

This module sends advisory notifications only. It has no broker integration and
never submits, modifies, or cancels orders. Delivery is opt-in and best-effort.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, time as dtime
from html import escape
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

LOGGER = logging.getLogger("telegram_alerts")
IST = ZoneInfo("Asia/Kolkata")
ALERT_TYPES = {"PAPER_ENTRY", "PAPER_EXIT", "RISK_OVERRUN"}


class TelegramEventNotifier:
    """Poll the paper broker's event feed and forward selected events to Telegram.

    Event IDs are persisted after successful delivery. Delivery is at-least-once:
    a process crash after Telegram accepts a message but before the cursor is saved
    can result in a duplicate notification, never a lost paper-account event.
    """

    def __init__(
        self,
        snapshot_provider: Callable[[], dict[str, Any]],
        *,
        state_path: Path = Path("logs/telegram_alert_state.json"),
        trade_plan_state_path: Path | None = None,
        enabled: bool | None = None,
        token: str | None = None,
        chat_id: str | None = None,
        poll_interval: float = 2.0,
        timeout: float = 5.0,
        sender: Callable[[str], bool] | None = None,
        quiet_hours_start: str | None = None,
        quiet_hours_end: str | None = None,
        max_per_minute: int | None = None,
        max_per_hour: int | None = None,
        clock: Callable[[], float] | None = None,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        if poll_interval <= 0 or timeout <= 0:
            raise ValueError("poll_interval and timeout must be positive")
        self.snapshot_provider = snapshot_provider
        self.state_path = Path(state_path)
        self.trade_plan_state_path = Path(
            trade_plan_state_path
            if trade_plan_state_path is not None
            else self.state_path.with_name("telegram_trade_plan_state.json")
        )
        # Load the same project-local .env file as the Kite authentication flow.
        try:
            from dotenv import load_dotenv
            load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env", override=False)
        except ImportError:
            pass
        self.enabled = _env_bool(os.getenv("TELEGRAM_ALERTS_ENABLED", "false")) if enabled is None else enabled
        self.token = token if token is not None else os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id if chat_id is not None else os.getenv("TELEGRAM_CHAT_ID", "")
        self.poll_interval = poll_interval
        self.timeout = timeout
        self.sender = sender
        self.quiet_hours_start = _parse_clock(
            quiet_hours_start or os.getenv("TELEGRAM_QUIET_HOURS_START", "22:00")
        )
        self.quiet_hours_end = _parse_clock(
            quiet_hours_end or os.getenv("TELEGRAM_QUIET_HOURS_END", "07:00")
        )
        self.max_per_minute = max_per_minute or _positive_env_int("TELEGRAM_MAX_ALERTS_PER_MINUTE", 5)
        self.max_per_hour = max_per_hour or _positive_env_int("TELEGRAM_MAX_ALERTS_PER_HOUR", 30)
        if self.max_per_minute < 1 or self.max_per_hour < self.max_per_minute:
            raise ValueError("Telegram rate limits must be positive and hourly >= per-minute")
        self._clock = clock or time.time
        self._now_provider = now_provider or (lambda: datetime.now(IST))
        self._trade_state = self._read_trade_plan_state()
        sent_ids = self._trade_state.get("sent_signal_ids", [])
        digest_dates = self._trade_state.get("digest_dates", [])
        sent_timestamps = self._trade_state.get("sent_timestamps", [])
        self._sent_signal_ids = {
            value for value in sent_ids if isinstance(value, str)
        } if isinstance(sent_ids, list) else set()
        self._digest_dates = {
            value for value in digest_dates if isinstance(value, str)
        } if isinstance(digest_dates, list) else set()
        self._sent_timestamps = [
            float(value) for value in sent_timestamps
            if _finite_timestamp(value)
        ] if isinstance(sent_timestamps, list) else []
        self._alert_lock = threading.Lock()
        self._cursor: int | None = self._read_cursor()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def configured(self) -> bool:
        return bool(self.token.strip() and self.chat_id.strip())

    def start(self) -> bool:
        """Start the daemon poller only when explicitly enabled and configured."""
        if not self.enabled:
            LOGGER.info("Telegram advisory alerts are disabled (opt-in).")
            return False
        if not self.configured:
            LOGGER.error("Telegram alerts enabled but bot token/chat ID is missing; notifications remain disabled.")
            return False
        if self._thread and self._thread.is_alive():
            return True
        self._stop.clear()
        if self._cursor is None:
            try:
                self.poll_once()  # Seed synchronously before the first scan can create new events.
            except Exception:
                LOGGER.exception("Could not seed Telegram event cursor; notifier will retry.")
        self._thread = threading.Thread(target=self._run, daemon=True, name="telegram-advisory-alerts")
        self._thread.start()
        LOGGER.info("Telegram advisory alert worker started.")
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=max(1.0, self.timeout + 1.0))

    def send_trade_plan(self, plan: dict[str, Any]) -> bool:
        """Deliver a validated conditional plan before the paper entry is recorded."""
        if not self.enabled:
            return True
        if not self.configured and self.sender is None:
            LOGGER.error("Trade-plan alert blocked: Telegram is enabled but not configured.")
            return False
        action = str(plan.get("action") or "").upper()
        if action not in {"CONDITIONAL_BUY", "CONDITIONAL_SHORT"}:
            return False
        if plan.get("data_verified_live") is not True or plan.get("margin_verified") is not True:
            return False
        signal_id = str(plan.get("signal_id") or _plan_fingerprint(plan))
        with self._alert_lock:
            if signal_id in self._sent_signal_ids:
                return True
        if not self._send(format_trade_plan(plan)):
            return False
        with self._alert_lock:
            self._sent_signal_ids.add(signal_id)
            self._write_trade_plan_state()
        return True

    def send_daily_digest(self, snapshot: dict[str, Any], report_date: str | None = None) -> bool:
        """Send one deduplicated EOD paper digest per NSE session date."""
        if not self.enabled:
            return True
        if not self.configured and self.sender is None:
            LOGGER.error("EOD digest blocked: Telegram is enabled but not configured.")
            return False
        current_time = self._now_provider()
        current_time = current_time.replace(tzinfo=IST) if current_time.tzinfo is None else current_time.astimezone(IST)
        date_key = report_date or current_time.date().isoformat()
        with self._alert_lock:
            if date_key in self._digest_dates:
                return True
        if not self._send(format_daily_digest(snapshot, date_key)):
            return False
        with self._alert_lock:
            self._digest_dates.add(date_key)
            self._write_trade_plan_state()
        return True

    def poll_once(self) -> int:
        """Process one broker snapshot; return the count of delivered alerts."""
        snapshot = self.snapshot_provider()
        events = snapshot.get("events", []) if isinstance(snapshot, dict) else []
        valid = [event for event in events if isinstance(event, dict) and _valid_event_id(event.get("id"))]
        if not valid:
            if self._cursor is None:
                # Persist a zero watermark when the database has no events yet,
                # so the first event created after startup is not mistaken for history.
                self._cursor = 0
                self._write_cursor(self._cursor)
            return 0
        ordered = sorted(valid, key=lambda item: int(item["id"]))
        if self._cursor is None:
            # First run intentionally seeds at the current head; it never floods
            # the user with historical paper events from before bot setup.
            self._cursor = max(int(event["id"]) for event in ordered)
            self._write_cursor(self._cursor)
            return 0

        delivered = 0
        for event in ordered:
            event_id = int(event["id"])
            if event_id <= self._cursor:
                continue
            event_type = str(event.get("event_type") or "").upper()
            if event_type in ALERT_TYPES:
                message = format_event(event)
                if not self._send(message):
                    # Do not advance beyond a failed delivery; retry on next poll.
                    break
                delivered += 1
            self._cursor = event_id
            self._write_cursor(self._cursor)
        return delivered

    def _send(self, message: str) -> bool:
        with self._alert_lock:
            now_epoch = self._clock()
            self._sent_timestamps = [
                stamp for stamp in self._sent_timestamps if 0 <= now_epoch - stamp < 3600
            ]
            if self._is_quiet_now():
                LOGGER.info("Telegram notification deferred by configured quiet hours.")
                return False
            per_minute = sum(now_epoch - stamp < 60 for stamp in self._sent_timestamps)
            if per_minute >= self.max_per_minute or len(self._sent_timestamps) >= self.max_per_hour:
                LOGGER.warning("Telegram notification deferred by configured rate limit.")
                return False

            if self.sender is not None:
                try:
                    success = bool(self.sender(message))
                except Exception:
                    LOGGER.exception("Injected Telegram sender failed; paper service will continue.")
                    success = False
            else:
                payload = json.dumps(
                    {"chat_id": self.chat_id, "text": message, "parse_mode": "HTML"}
                ).encode("utf-8")
                request = Request(
                    f"https://api.telegram.org/bot{self.token}/sendMessage",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                success = False
                for attempt in range(2):
                    try:
                        with urlopen(request, timeout=self.timeout) as response:
                            result = json.loads(response.read().decode("utf-8"))
                        if result.get("ok") is True:
                            success = True
                            break
                        LOGGER.warning(
                            "Telegram API rejected an advisory notification (attempt %s).",
                            attempt + 1,
                        )
                    except HTTPError as exc:
                        LOGGER.warning(
                            "Telegram HTTP error %s while sending advisory notification.", exc.code
                        )
                        if exc.code < 500 and exc.code != 429:
                            break
                    except (URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
                        LOGGER.warning(
                            "Telegram delivery attempt %s failed; paper service remains independent.",
                            attempt + 1,
                        )
                    if attempt == 0:
                        time.sleep(0.25)
            if success:
                self._sent_timestamps.append(now_epoch)
                self._write_trade_plan_state()
            return success

    def _is_quiet_now(self) -> bool:
        now = self._now_provider()
        now = now.replace(tzinfo=IST) if now.tzinfo is None else now.astimezone(IST)
        current = now.time().replace(tzinfo=None)
        start, end = self.quiet_hours_start, self.quiet_hours_end
        if start == end:
            return False
        if start < end:
            return start <= current < end
        return current >= start or current < end

    def _read_trade_plan_state(self) -> dict[str, Any]:
        if not self.trade_plan_state_path.exists():
            return {"sent_signal_ids": [], "sent_timestamps": [], "digest_dates": []}
        try:
            value = json.loads(self.trade_plan_state_path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("state must be an object")
            return value
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            LOGGER.warning("Telegram trade-plan state could not be read; deduplication will re-seed.")
            return {"sent_signal_ids": [], "sent_timestamps": [], "digest_dates": []}

    def _write_trade_plan_state(self) -> None:
        self.trade_plan_state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "sent_signal_ids": sorted(self._sent_signal_ids)[-5000:],
            "sent_timestamps": self._sent_timestamps[-100:],
            "digest_dates": sorted(self._digest_dates)[-60:],
        }
        temporary = self.trade_plan_state_path.with_suffix(
            self.trade_plan_state_path.suffix + ".tmp"
        )
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temporary.replace(self.trade_plan_state_path)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:
                LOGGER.exception("Telegram event polling failed; paper service will continue.")
            self._stop.wait(self.poll_interval)

    def _read_cursor(self) -> int | None:
        if not self.state_path.exists():
            return None
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            value = data.get("last_event_id") if isinstance(data, dict) else None
            return int(value) if _valid_event_id(value) else None
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            LOGGER.warning("Telegram alert cursor could not be read; it will be safely re-seeded.")
            return None

    def _write_cursor(self, event_id: int) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temporary.write_text(json.dumps({"last_event_id": event_id}, indent=2), encoding="utf-8")
        temporary.replace(self.state_path)


def _env_bool(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _valid_event_id(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _positive_env_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
        return value if value > 0 else default
    except (TypeError, ValueError):
        return default


def _parse_clock(value: str) -> dtime:
    try:
        hour, minute = (int(part) for part in value.split(":"))
        return dtime(hour, minute)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Invalid Telegram quiet-hours time: {value!r}") from exc


def _finite_timestamp(value: Any) -> bool:
    try:
        return float(value) >= 0
    except (TypeError, ValueError):
        return False


def _plan_fingerprint(plan: dict[str, Any]) -> str:
    raw = "|".join(
        str(plan.get(key) or "")
        for key in ("symbol", "side", "strategy", "strategy_version", "signal_time")
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _safe(value: Any) -> str:
    return escape(str(value if value is not None else "—"))


def _price(value: Any) -> str:
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return _safe(value)


def format_trade_plan(plan: dict[str, Any]) -> str:
    """Render a validated conditional plan as Telegram-safe HTML."""
    reasons = plan.get("reasons", [])
    reason_text = (
        " · ".join(_safe(value) for value in reasons[:3])
        if isinstance(reasons, list)
        else _safe(reasons)
    )
    return (
        "<b>🟡 CONDITIONAL TRADE PLAN · PAPER</b>\n"
        f"<b>Symbol:</b> {_safe(plan.get('symbol'))} · <b>Side:</b> {_safe(plan.get('side'))}\n"
        f"<b>Strategy:</b> {_safe(plan.get('strategy'))}\n"
        f"<b>Version:</b> {_safe(plan.get('strategy_version'))}\n"
        f"<b>Signal time:</b> {_safe(plan.get('signal_time'))}\n"
        f"<b>Signal reference:</b> ₹{_price(plan.get('reference_entry'))}\n"
        f"<b>Executable quote:</b> ₹{_price(plan.get('entry_reference_price'))}\n"
        f"<b>Indicative paper fill:</b> ₹{_price(plan.get('entry_fill_price'))}\n"
        f"<b>Stop-loss:</b> ₹{_price(plan.get('stop_loss'))}\n"
        f"<b>Target 1:</b> ₹{_price(plan.get('target_1'))} · <b>Target 2:</b> ₹{_price(plan.get('target_2'))}\n"
        f"<b>Quantity:</b> {_safe(plan.get('quantity'))}\n"
        f"<b>Net risk:</b> ₹{_price(plan.get('net_risk_inr'))} · <b>Risk budget:</b> ₹{_price(plan.get('risk_budget_inr'))}\n"
        f"<b>Net R:R:</b> T1 {_price(plan.get('net_rr_t1'))} · T2 {_price(plan.get('net_rr_t2'))}\n"
        f"<b>Estimated margin:</b> ₹{_price(plan.get('estimated_margin_inr'))}\n"
        f"<b>Quote age:</b> {_price(plan.get('quote_age_seconds'))} sec\n"
        f"<b>Market regime:</b> {_safe(plan.get('market_regime'))}\n"
        f"<b>Reason:</b> {reason_text}\n"
        "Paper risk and live quote checks passed at alert time; the quote is rechecked "
        "before the paper ledger records entry. No broker order is placed."
    )


def format_daily_digest(snapshot: dict[str, Any], report_date: str) -> str:
    """Render EOD metrics without pooling different strategy versions."""
    account = snapshot.get("account", {}) if isinstance(snapshot, dict) else {}
    lines = [
        f"<b>📊 EOD PAPER DIGEST · {_safe(report_date)}</b>",
        f"Account total today net P&amp;L (all versions): ₹{_price(account.get('today_net_pnl', 0))}",
        f"Account cumulative realized P&amp;L (all versions): ₹{_price(account.get('realized_pnl', 0))}",
        f"Unrealized net P&amp;L: ₹{_price(account.get('unrealized_net_pnl', 0))}",
        f"Open positions: {_safe(account.get('open_positions', 0))}/{_safe(account.get('max_open_positions', 0))}",
        f"Open planned risk: ₹{_price(account.get('open_planned_risk_inr', 0))}",
        f"Available portfolio risk: ₹{_price(account.get('available_portfolio_risk_inr', 0))}",
        "<b>Version-isolated cumulative results</b>",
    ]
    for row in snapshot.get("strategies", []) if isinstance(snapshot, dict) else []:
        if isinstance(row, dict) and row.get("track") in {"CHAMPION_CANDIDATE", "SHADOW"}:
            lines.append(
                f"{_safe(row.get('strategy_version'))}: {_safe(row.get('trades', 0))} closed · "
                f"net ₹{_price(row.get('net_pnl', 0))}"
            )
    lines.append("Research only — no broker orders were placed.")
    return "\n".join(lines)


def format_event(event: dict[str, Any]) -> str:
    """Render a plain-text, explicitly simulated alert from a persisted broker event."""
    event_type = str(event.get("event_type") or "").upper()
    symbol = escape(str(event.get("symbol") or "N/A"))
    details = event.get("details") if isinstance(event.get("details"), dict) else {}
    if event_type == "PAPER_ENTRY":
        side_match = re.search(r"Simulated (LONG|SHORT)", str(event.get("message") or ""), re.IGNORECASE)
        side = escape(str(details.get("side") or details.get("direction") or (side_match.group(1).upper() if side_match else "PAPER")))
        quantity = details.get("quantity", "—")
        entry = details.get("entry_fill_price", "—")
        stop = details.get("stop_loss", "—")
        target_1 = details.get("target_1", "—")
        target_2 = details.get("target_2", "—")
        return (
            "🟢 PAPER ENTRY (SIMULATED)\n"
            f"{symbol} · {side}\nQuantity: {quantity}\nEntry fill: ₹{entry}\n"
            f"Stop-loss: ₹{stop}\nTarget 1: ₹{target_1}\nTarget 2: ₹{target_2}\n"
            "Research alert only — no broker order was placed."
        )
    if event_type == "PAPER_EXIT":
        side_match = re.search(r"Simulated (LONG|SHORT)", str(event.get("message") or ""), re.IGNORECASE)
        side = escape(str(details.get("side") or (side_match.group(1).upper() if side_match else "PAPER")))
        pnl = details.get("net_pnl", "—")
        reason = escape(str(details.get("exit_reason") or "EXIT"))
        exit_price = details.get("exit_fill_price", "—")
        return (
            "🔴 PAPER EXIT (SIMULATED)\n"
            f"{symbol} · {side}\nExit fill: ₹{exit_price}\nReason: {reason}\n"
            f"Estimated net P&L: ₹{pnl}\nResearch alert only — no broker order was placed."
        )
    if event_type == "RISK_OVERRUN":
        return (
            "⚠️ PAPER RISK OVERRUN\n"
            f"{symbol}\n{escape(str(event.get('message') or 'A simulated loss exceeded planned risk.'))}\n"
            "Review the paper-trading log. No broker order was placed."
        )
    raise ValueError(f"Unsupported Telegram event type: {event_type}")
