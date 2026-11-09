import json

from datetime import datetime
from zoneinfo import ZoneInfo

from src import telegram_alerts
from src.telegram_alerts import TelegramEventNotifier, format_daily_digest, format_event, format_trade_plan
from src.paper_broker import PAPER_BASELINE_VERSION, PaperBroker

IST = ZoneInfo("Asia/Kolkata")


def test_first_poll_seeds_cursor_without_sending_historical_events(tmp_path):
    sent = []
    notifier = TelegramEventNotifier(
        lambda: {"events": [{"id": 1, "event_type": "PAPER_ENTRY"}]},
        state_path=tmp_path / "cursor.json",
        enabled=True, token="token", chat_id="chat", sender=lambda msg: sent.append(msg) or True,
    )
    assert notifier.poll_once() == 0
    assert sent == []
    assert json.loads((tmp_path / "cursor.json").read_text()) == {"last_event_id": 1}


def test_new_paper_entry_is_sent_once_and_cursor_persisted(tmp_path):
    sent = []
    events = [
        {"id": 1, "event_type": "SCAN_PROCESSED"},
        {"id": 2, "event_type": "PAPER_ENTRY", "symbol": "RELIANCE",
         "details": {"quantity": 10, "entry_fill_price": 100, "stop_loss": 98, "target_1": 103, "target_2": 104}},
    ]
    notifier = TelegramEventNotifier(
        lambda: {"events": events}, state_path=tmp_path / "cursor.json",
        enabled=True, token="token", chat_id="chat", sender=lambda msg: sent.append(msg) or True,
        now_provider=lambda: datetime(2026, 11, 6, 12, 0, tzinfo=IST),
    )
    notifier._cursor = 1
    assert notifier.poll_once() == 1
    assert len(sent) == 1
    assert "PAPER ENTRY (SIMULATED)" in sent[0]
    assert "RELIANCE" in sent[0]
    assert "no broker order was placed" in sent[0]
    assert json.loads((tmp_path / "cursor.json").read_text()) == {"last_event_id": 2}
    assert notifier.poll_once() == 0


def test_failed_delivery_does_not_advance_cursor_and_is_retried(tmp_path):
    attempts = []
    events = [{"id": 5, "event_type": "PAPER_EXIT", "symbol": "INFY", "details": {"net_pnl": -25}}]
    notifier = TelegramEventNotifier(
        lambda: {"events": events}, state_path=tmp_path / "cursor.json",
        enabled=True, token="token", chat_id="chat",
        sender=lambda msg: (attempts.append(msg), len(attempts) > 1)[1],
        now_provider=lambda: datetime(2026, 11, 6, 12, 0, tzinfo=IST),
    )
    notifier._cursor = 4
    assert notifier.poll_once() == 0
    assert notifier._cursor == 4
    assert notifier.poll_once() == 1
    assert notifier._cursor == 5
    assert len(attempts) == 2


def test_disabled_alerts_never_send(tmp_path):
    sent = []
    notifier = TelegramEventNotifier(
        lambda: {"events": [{"id": 1, "event_type": "PAPER_ENTRY"}]},
        state_path=tmp_path / "cursor.json",
        enabled=False, token="token", chat_id="chat", sender=lambda msg: sent.append(msg) or True,
    )
    assert notifier.start() is False
    assert sent == []


def test_risk_overrun_message_is_explicitly_paper_only():
    message = format_event({"event_type": "RISK_OVERRUN", "symbol": "SBIN", "message": "planned risk exceeded"})
    assert "PAPER RISK OVERRUN" in message
    assert "No broker order was placed" in message


def test_unsupported_event_type_is_rejected():
    import pytest
    with pytest.raises(ValueError, match="Unsupported"):
        format_event({"event_type": "SCAN_PROCESSED"})


def test_entry_and_exit_alerts_include_direction_from_broker_event_message():
    entry = format_event({"event_type": "PAPER_ENTRY", "symbol": "TCS",
                          "message": "Simulated SHORT entry: 2 TCS @ ₹100.", "details": {}})
    exit_message = format_event({"event_type": "PAPER_EXIT", "symbol": "TCS",
                                 "message": "Simulated SHORT exit: 2 TCS @ ₹95; net P&L ₹10.",
                                 "details": {"net_pnl": 10}})
    assert "TCS · SHORT" in entry
    assert "TCS · SHORT" in exit_message


def test_empty_startup_does_not_skip_the_first_future_event(tmp_path):
    sent = []
    events = []
    notifier = TelegramEventNotifier(
        lambda: {"events": events}, state_path=tmp_path / "cursor.json",
        enabled=True, token="token", chat_id="chat", sender=lambda msg: sent.append(msg) or True,
        now_provider=lambda: datetime(2026, 11, 6, 12, 0, tzinfo=IST),
    )
    assert notifier.poll_once() == 0
    assert notifier._cursor == 0
    events.append({"id": 1, "event_type": "PAPER_ENTRY", "symbol": "INFY", "message": "Simulated LONG entry"})
    assert notifier.poll_once() == 1
    assert len(sent) == 1



def _trade_plan(symbol="M&M", signal_id="signal-1"):
    return {
        "signal_id": signal_id,
        "action": "CONDITIONAL_BUY",
        "symbol": symbol,
        "side": "LONG",
        "strategy": "opening_range",
        "strategy_version": "nse_champion_v2_foundation_candidate",
        "signal_time": "2026-11-06T10:00:00+05:30",
        "reference_entry": 100.0,
        "entry_reference_price": 100.05,
        "entry_fill_price": 100.10,
        "stop_loss": 98.0,
        "target_1": 104.0,
        "target_2": 108.0,
        "quantity": 10,
        "risk_budget_inr": 500.0,
        "net_risk_inr": 450.0,
        "net_rr_t1": 1.6,
        "net_rr_t2": 2.4,
        "estimated_margin_inr": 1001.0,
        "quote_age_seconds": 2.0,
        "market_regime": "BULLISH",
        "reasons": ["VWAP & EMA aligned"],
        "data_verified_live": True,
        "margin_verified": True,
    }


def test_trade_plan_alert_is_html_safe_and_deduplicated(tmp_path):
    sent = []
    notifier = TelegramEventNotifier(
        lambda: {"events": []},
        state_path=tmp_path / "cursor.json",
        trade_plan_state_path=tmp_path / "plans.json",
        enabled=True,
        token="token",
        chat_id="chat",
        sender=lambda message: sent.append(message) or True,
        clock=lambda: 1000.0,
        now_provider=lambda: datetime(2026, 11, 6, 10, 0, tzinfo=IST),
    )
    plan = _trade_plan()
    message = format_trade_plan(plan)
    assert "M&amp;M" in message
    assert "<b>Stop-loss:</b> ₹98.00" in message
    assert "<b>Net R:R:</b> T1 1.60" in message
    assert notifier.send_trade_plan(plan) is True
    assert notifier.send_trade_plan(plan) is True
    assert len(sent) == 1
    state = json.loads((tmp_path / "plans.json").read_text())
    assert state["sent_signal_ids"] == ["signal-1"]


def test_trade_plan_rate_limit_and_quiet_hours_withhold_alert(tmp_path):
    sent = []
    now = datetime(2026, 11, 6, 10, 0, tzinfo=IST)
    notifier = TelegramEventNotifier(
        lambda: {"events": []},
        state_path=tmp_path / "cursor.json",
        enabled=True,
        token="token",
        chat_id="chat",
        sender=lambda message: sent.append(message) or True,
        max_per_minute=1,
        max_per_hour=2,
        clock=lambda: 1000.0,
        now_provider=lambda: now,
    )
    assert notifier.send_trade_plan(_trade_plan(signal_id="one")) is True
    assert notifier.send_trade_plan(_trade_plan(symbol="INFY", signal_id="two")) is False
    assert len(sent) == 1

    quiet = TelegramEventNotifier(
        lambda: {"events": []},
        state_path=tmp_path / "quiet-cursor.json",
        enabled=True,
        token="token",
        chat_id="chat",
        sender=lambda message: sent.append(message) or True,
        now_provider=lambda: datetime(2026, 11, 6, 23, 0, tzinfo=IST),
    )
    assert quiet.send_trade_plan(_trade_plan(signal_id="quiet")) is False
    assert len(sent) == 1


def test_eod_digest_is_deduplicated_and_keeps_versions_separate(tmp_path):
    sent = []
    now = datetime.now(IST).replace(hour=15, minute=45, second=0, microsecond=0)
    broker = PaperBroker(tmp_path / "paper.sqlite3")
    with broker._connect() as db:
        for signal_id, version, strategy, pnl in (
            ("champion-vwap-today", PAPER_BASELINE_VERSION, "vwap_trend", 120.0),
            ("champion-breakout-today", PAPER_BASELINE_VERSION, "opening_range", 80.0),
            ("archived-today", "champion_v1", "vwap_trend", 500.0),
        ):
            db.execute(
                """INSERT INTO trades(
                    signal_id,strategy,strategy_version,symbol,side,status,signal_time,entry_time,
                    signal_reference_entry,entry_reference_price,entry_fill_price,stop_loss,target_1,target_2,
                    quantity,risk_budget_inr,position_value_inr,planned_net_risk_inr,market_regime,
                    entry_reason,exit_time,gross_pnl,transaction_charges,slippage_cost,
                    total_estimated_costs,net_pnl,created_at
                ) VALUES(?,?,?,?,'LONG','CLOSED',?,?,?,?,?,98,104,108,1,500,100,100,
                         'BULLISH','digest fixture',?,130,5,5,10,?,?)""",
                (
                    signal_id, strategy, version, "INFY", now.isoformat(), now.isoformat(),
                    100.0, 100.0, 100.05, now.isoformat(), pnl, now.isoformat(),
                ),
            )

        actionable_rejection = {
            "symbol": "HDFCBANK",
            "action": "CONDITIONAL_BUY",
            "side": "LONG",
            "strategy": "vwap_trend",
            "signal_time": "2026-11-09T10:00:00+05:30",
        }
        # Repeated rejection records for the same actionable signal count once.
        broker._record_rejection(db, actionable_rejection, now.isoformat(), "First rejection")
        broker._record_rejection(db, actionable_rejection, now.isoformat(), "Repeated rejection")
        # Neutral scan outcomes must not lower the acceptance rate.
        broker._record_rejection(
            db,
            {"symbol": "SBIN", "action": "WATCH_CANDIDATE", "side": None,
             "strategy": "vwap_trend", "signal_time": "2026-11-09T10:05:00+05:30"},
            now.isoformat(),
            "Watch-only candidate",
        )

        db.execute(
            """INSERT INTO shadow_signals(
                signal_id,strategy_version,strategy_name,symbol,side,signal_time,signal_close,
                stop_loss,target_1,target_2,status,exit_time,gross_pnl,transaction_charges,
                slippage_cost,total_estimated_costs,net_pnl,reason,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,'CLOSED',?,?,?,?,?,?,?,?)""",
            (
                "shadow-today", "challenger_ema_9_21_atr_seed_v2",
                "EMA 9/21 momentum (ATR-seeded)", "TCS", "LONG", now.isoformat(),
                100.0, 98.0, 104.0, 108.0, now.isoformat(), 320.0, 10.0, 10.0,
                20.0, 300.0, "digest fixture", now.isoformat(),
            ),
        )

    # The strategy rows come from the broker snapshot, not hand-written labels.
    snapshot = broker.snapshot()
    champion_rows = [
        row for row in snapshot["strategies"]
        if row["strategy_version"] == PAPER_BASELINE_VERSION
    ]
    assert len(champion_rows) == 2
    assert sum(row["trades"] for row in champion_rows) == 2
    assert sum(row["net_pnl"] for row in champion_rows) == 200.0
    assert sum("trades_today" in row for row in champion_rows) == 1
    assert next(row for row in champion_rows if "trades_today" in row)["trades_today"] == 2
    assert next(row for row in champion_rows if "trades_today" in row)["accept_rate_pct"] == 66.6666666667

    notifier = TelegramEventNotifier(
        lambda: {"events": []},
        state_path=tmp_path / "cursor.json",
        enabled=True,
        token="token",
        chat_id="chat",
        sender=lambda message: sent.append(message) or True,
        now_provider=lambda: now,
    )
    report_date = now.date().isoformat()
    digest = format_daily_digest(snapshot, report_date)
    assert digest.count("nse_champion_v2") == 1
    assert "nse_champion_v2: 2 closed" in digest
    assert "net ₹200.00" in digest
    assert "today 2 trades" in digest
    assert "accept rate 66.67%" in digest
    assert "challenger_ema_9_21_atr_seed_v2" in digest
    assert "champion_v1" not in digest
    assert notifier.send_daily_digest(snapshot, report_date) is True
    assert notifier.send_daily_digest(snapshot, report_date) is True
    assert len(sent) == 1


def test_active_champion_is_present_in_digest_before_any_trade_closes(tmp_path):
    broker = PaperBroker(tmp_path / "empty-paper.sqlite3")
    snapshot = broker.snapshot()

    champion_rows = [
        row for row in snapshot["strategies"]
        if row["strategy_version"] == PAPER_BASELINE_VERSION
        and row["track"] == "CHAMPION"
    ]
    assert len(champion_rows) == 1
    assert champion_rows[0]["trades"] == 0
    assert champion_rows[0]["trades_today"] == 0
    assert champion_rows[0]["accept_rate_pct"] == 0.0

    digest = format_daily_digest(snapshot, datetime.now(IST).date().isoformat())
    assert digest.count("nse_champion_v2") == 1
    assert "nse_champion_v2: 0 closed" in digest
    assert "today 0 trades" in digest
    assert "accept rate 0.00%" in digest


def test_telegram_send_uses_html_parse_mode_for_ampersand_escaping(tmp_path, monkeypatch):
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return b'{"ok": true}'

    def fake_urlopen(request, timeout):
        captured.update(json.loads(request.data.decode("utf-8")))
        return Response()

    monkeypatch.setattr(telegram_alerts, "urlopen", fake_urlopen)
    notifier = TelegramEventNotifier(
        lambda: {"events": []},
        state_path=tmp_path / "cursor.json",
        enabled=True,
        token="token",
        chat_id="chat",
        clock=lambda: 1000.0,
        now_provider=lambda: datetime(2026, 11, 6, 10, 0, tzinfo=IST),
    )
    message = format_event({"event_type": "PAPER_ENTRY", "symbol": "M&M", "details": {}})
    assert "M&amp;M" in message
    assert notifier._send(message) is True
    assert captured["parse_mode"] == "HTML"
    assert "M&amp;M" in captured["text"]
