import json

from src.telegram_alerts import TelegramEventNotifier, format_event


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
