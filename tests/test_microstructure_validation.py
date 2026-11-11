from src.microstructure_validation import evaluate_order_flow_predictive_accuracy


def test_order_flow_accuracy_uses_only_future_next_candle_and_stays_provisional():
    events = [
        {
            "symbol": "SBIN",
            "timestamp": "2026-11-11T09:15:00+05:30",
            "last_trade_time": "2026-11-11T09:15:00+05:30",
            "last_price": 100.10,
            "last_quantity": 10,
            "best_bid": 100.00,
            "best_ask": 100.10,
            "depth_buy": [{"price": 100.00, "quantity": 500}],
            "depth_sell": [{"price": 100.10, "quantity": 200}],
        },
        {
            "symbol": "SBIN",
            "timestamp": "2026-11-11T09:16:00+05:30",
            "last_trade_time": "2026-11-11T09:16:00+05:30",
            "last_price": 100.10,
            "last_quantity": 20,
            "best_bid": 100.00,
            "best_ask": 100.10,
            "depth_buy": [{"price": 100.00, "quantity": 500}],
            "depth_sell": [{"price": 100.10, "quantity": 200}],
        },
        {
            "symbol": "SBIN",
            "timestamp": "2026-11-11T09:20:00+05:30",
            "last_trade_time": "2026-11-11T09:20:00+05:30",
            "last_price": 99.90,
            "last_quantity": 10,
            "best_bid": 99.90,
            "best_ask": 100.00,
            "depth_buy": [{"price": 99.90, "quantity": 200}],
            "depth_sell": [{"price": 100.00, "quantity": 500}],
        },
        {
            "symbol": "SBIN",
            "timestamp": "2026-11-11T09:21:00+05:30",
            "last_trade_time": "2026-11-11T09:21:00+05:30",
            "last_price": 99.90,
            "last_quantity": 20,
            "best_bid": 99.90,
            "best_ask": 100.00,
            "depth_buy": [{"price": 99.90, "quantity": 200}],
            "depth_sell": [{"price": 100.00, "quantity": 500}],
        },
    ]
    candles = [
        {"date": "2026-11-11T09:20:00+05:30", "close": 101.00},
        {"date": "2026-11-11T09:25:00+05:30", "close": 99.00},
    ]

    result = evaluate_order_flow_predictive_accuracy(events, candles)

    assert result["predictions"] == 2
    assert result["correct_predictions"] == 2
    assert result["accuracy_pct"] == 100.0
    assert result["sessions_evaluated"] == 1
    assert result["status"] == "INSUFFICIENT_PROSPECTIVE_EVIDENCE"


def test_order_flow_accuracy_is_not_tested_without_forward_candles():
    result = evaluate_order_flow_predictive_accuracy(
        [
            {
                "symbol": "SBIN",
                "timestamp": "2026-11-11T09:15:00+05:30",
                "last_trade_time": "2026-11-11T09:15:00+05:30",
                "last_price": 100.10,
                "last_quantity": 10,
                "best_bid": 100.00,
                "best_ask": 100.10,
                "depth_buy": [{"price": 100.00, "quantity": 500}],
                "depth_sell": [{"price": 100.10, "quantity": 200}],
            }
        ],
        [],
    )

    assert result["status"] == "NOT_TESTED"
    assert result["predictions"] == 0
