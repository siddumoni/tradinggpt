"""Optional Telegram notifications for TradingGPT paper-account events.

This module sends advisory notifications only. It has no broker integration and
never submits, modifies, or cancels orders. Delivery is opt-in and best-effort.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from html import escape
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

LOGGER = logging.getLogger("telegram_alerts")
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
        enabled: bool | None = None,
        token: str | None = None,
        chat_id: str | None = None,
        poll_interval: float = 2.0,
        timeout: float = 5.0,
        sender: Callable[[str], bool] | None = None,
    ) -> None:
        if poll_interval <= 0 or timeout <= 0:
            raise ValueError("poll_interval and timeout must be positive")
        self.snapshot_provider = snapshot_provider
        self.state_path = Path(state_path)
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

    def poll_once(self) -> int:
        """Process one broker snapshot; return the count of delivered alerts."""
        snapshot = self.snapshot_provider()
        events = snapshot.get("events", []) if isinstance(snapshot, dict) else []
        valid = [event for event in events if isinstance(event, dict) and _valid_event_id(event.get("id"))]
        if not valid:
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
        if self.sender is not None:
            try:
                return bool(self.sender(message))
            except Exception:
                LOGGER.exception("Injected Telegram sender failed; paper service will continue.")
                return False
        payload = json.dumps({"chat_id": self.chat_id, "text": message}).encode("utf-8")
        request = Request(
            f"https://api.telegram.org/bot{self.token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        for attempt in range(2):
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    result = json.loads(response.read().decode("utf-8"))
                if result.get("ok") is True:
                    return True
                LOGGER.warning("Telegram API rejected an advisory notification (attempt %s).", attempt + 1)
            except HTTPError as exc:
                LOGGER.warning("Telegram HTTP error %s while sending advisory notification.", exc.code)
                if exc.code < 500 and exc.code != 429:
                    return False
            except (URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
                LOGGER.warning("Telegram delivery attempt %s failed; paper service remains independent.", attempt + 1)
            if attempt == 0:
                time.sleep(0.25)
        return False

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
