import json
from datetime import datetime,timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import pytest
from src.paper_broker import PaperBroker
from src import paper_broker as paper_broker_module
IST=ZoneInfo("Asia/Kolkata")
def stamp(): return datetime.now(IST).isoformat()
def report_file(tmp_path,candidates,snapshots=None,*,provider="kite",orders=False):
 p=tmp_path/"scan.json"; p.write_text(json.dumps({"status":"RESEARCH_SCAN_ONLY","as_of":stamp(),"provider":"Kite","data_provider_mode":provider,"advisory_only":True,"orders_placed_or_modified":orders,"candidates":candidates,"market_snapshots":snapshots or {}}),encoding="utf-8"); return p
def candidate(symbol="INFY",signal_time="2026-10-26T10:00:00+05:30",**changes):
 x={"symbol":symbol,"action":"CONDITIONAL_BUY","side":"LONG","strategy":"vwap_trend","confidence":80,"market_regime":"BULLISH","signal_time":signal_time,"reference_entry":100.0,"stop_loss":98.0,"target_1":104.0,"target_2":108.0,"quantity":99,"data_verified_live":True,"margin_verified":True,"company_specific_risk":"LOW","reasons":["15-minute trend bullish","VWAP/EMA aligned"]}; x.update(changes); return x
def quote(**changes):
 x={"last_price":100.0,"bid":99.99,"ask":100.01,"timestamp":stamp(),"verified_live":True}; x.update(changes); return x
def test_virtual_trade_opens_then_closes_with_cost_breakdown(tmp_path):
 b=PaperBroker(tmp_path/"paper.sqlite3"); p=report_file(tmp_path,[candidate()],{"INFY":quote()})
 plans=[]
 r=b.process_scan_report(p,before_entry_callback=lambda plan: plans.append(plan) or True); assert r["opened"]==1 and r["rejected"]==0
 assert len(plans)==1 and plans[0]["signal_id"]
 assert plans[0]["net_rr_t1"]>=1.5 and plans[0]["quantity"]>0
 s=b.snapshot(); assert s["account"]["starting_capital"]==100000 and s["account"]["open_positions"]==1
 trade=s["open_positions"][0]; assert trade["quantity"]>0 and trade["entry_fill_price"]>trade["entry_reference_price"]
 assert trade["strategy_version"]==paper_broker_module.PAPER_BASELINE_VERSION
 assert trade["target_1"]==pytest.approx(104.0) and trade["target_2"]==pytest.approx(108.0)
 assert trade["net_rr_t1"]>0 and trade["net_rr_t2"]>trade["net_rr_t1"]
 assert b.update_market_price("INFY",108.1,bid=108.0,ask=108.1,market_time=datetime.now(IST))==1
 final=b.snapshot(); assert final["account"]["open_positions"]==0 and final["account"]["closed_trades"]==1
 t=final["closed_trades"][0]; assert t["exit_reason"]=="TARGET_2" and t["gross_pnl"]>0 and t["transaction_charges"]>0 and t["slippage_cost"]>0
 assert t["total_estimated_costs"]==pytest.approx(t["transaction_charges"]+t["slippage_cost"])
 assert t["net_pnl"]==pytest.approx(t["gross_pnl"]-t["total_estimated_costs"])
def test_duplicate_signal_is_not_opened_twice(tmp_path):
 b=PaperBroker(tmp_path/"paper.sqlite3"); assert b.process_scan_report(report_file(tmp_path,[candidate()],{"INFY":quote()}))["opened"]==1
 r=b.process_scan_report(report_file(tmp_path,[candidate()],{"INFY":quote()})); assert r["opened"]==0 and r["rejected"]==1
def test_stale_quote_cannot_open_position(tmp_path):
 b=PaperBroker(tmp_path/"paper.sqlite3",max_entry_quote_age_seconds=1)
 old=(datetime.now(IST)-timedelta(minutes=5)).isoformat()
 r=b.process_scan_report(report_file(tmp_path,[candidate()],{"INFY":quote(timestamp=old)})); assert r["opened"]==0 and r["rejected"]==1
def test_fails_closed_for_non_kite_or_order_submitting_reports(tmp_path):
 b=PaperBroker(tmp_path/"paper.sqlite3")
 with pytest.raises(ValueError,match="only accepts Kite"): b.process_scan_report(report_file(tmp_path,[],provider="yahoo"))
 with pytest.raises(ValueError,match="no-order assertion"): b.process_scan_report(report_file(tmp_path,[],orders=True))
def test_daily_loss_limit_blocks_new_entries_after_loss(tmp_path):
 b=PaperBroker(tmp_path/"paper.sqlite3",max_daily_loss=50)
 b.process_scan_report(report_file(tmp_path,[candidate()],{"INFY":quote()}))
 b.update_market_price("INFY",96.0,bid=95.9,ask=96.1,market_time=datetime.now(IST))
 assert b.snapshot()["account"]["realized_pnl"]<0
 second=candidate("HDFCBANK","2026-10-26T10:05:00+05:30")
 r=b.process_scan_report(report_file(tmp_path,[second],{"HDFCBANK":quote()})); assert r["opened"]==0 and r["rejected"]==1
 assert any("Daily paper loss limit" in x["message"] for x in b.snapshot()["events"])
 assert any(x["event_type"]=="RISK_OVERRUN" for x in b.snapshot()["events"])
def test_non_directional_scan_stays_no_trade(tmp_path):
 b=PaperBroker(tmp_path/"paper.sqlite3"); c=candidate(action="NO_TRADE",side=None)
 r=b.process_scan_report(report_file(tmp_path,[c],{"INFY":quote()})); assert r["opened"]==0 and r["neutral"]==1 and b.snapshot()["account"]["open_positions"]==0


def test_scan_action_counts_separate_watch_candidates_from_no_trade(tmp_path):
 b=PaperBroker(tmp_path/"paper.sqlite3")
 watch=candidate("RELIANCE",action="WATCH_CANDIDATE")
 no_trade=candidate("HDFCBANK",action="NO_TRADE")
 r=b.process_scan_report(report_file(tmp_path,[watch,no_trade]))
 assert r["opened"]==0
 assert r["watch_candidates"]==1
 assert r["no_trade"]==1
 assert r["conditional_candidates"]==0
 assert r["other_actions"]==0
 assert r["neutral"]==2


def test_risk_assessment_uses_reference_price_and_slipped_fill_for_cash_cap(tmp_path,monkeypatch):
 captured={}
 real_assess=paper_broker_module.assess_trade
 def spy_assess(**kwargs):
  captured.update(kwargs)
  return real_assess(**kwargs)
 monkeypatch.setattr(paper_broker_module,"assess_trade",spy_assess)
 broker=PaperBroker(tmp_path/"paper.sqlite3")
 q=quote(ask=100.01,bid=100.00)
 result=broker.process_scan_report(report_file(tmp_path,[candidate()],{"INFY":q}))
 assert result["opened"]==1
 assert captured["entry_price"]==pytest.approx(100.01)
 assert captured["estimated_margin_per_share"]==pytest.approx(100.01*(1+5/10000))


def test_future_dated_tick_is_rejected(tmp_path):
 broker=PaperBroker(tmp_path/"paper.sqlite3")
 with pytest.raises(ValueError,match="future-dated"):
  broker.update_market_price("INFY",100.0,
   market_time=datetime.now(IST)+timedelta(minutes=5),received_at=datetime.now(IST))


def test_open_position_daily_pnl_uses_conservative_net_liquidation_mark(tmp_path):
 broker=PaperBroker(tmp_path/"paper.sqlite3")
 broker.process_scan_report(report_file(tmp_path,[candidate()],{"INFY":quote()}))
 now=datetime.now(IST)
 with broker._connect() as db:
  # Write a non-triggering liquidation mark directly: the public tick handler
  # would correctly square off immediately after 15:15.
  db.execute(
   """INSERT INTO market_marks(symbol,last_price,bid,ask,market_time,received_at)
      VALUES(?,?,?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET
      last_price=excluded.last_price,bid=excluded.bid,ask=excluded.ask,
      market_time=excluded.market_time,received_at=excluded.received_at""",
   ("INFY",101.0,100.95,101.05,now.isoformat(),now.isoformat()),
  )
  trade=db.execute("SELECT * FROM trades WHERE status='OPEN'").fetchone()
  assert trade is not None
  result=broker._today_net_pnl(db,now)
  expected_exit_fill=100.95*(1-5/10000)
  from src.cost_calculator import estimate_intraday_round_trip_cost
  from src.risk_engine import load_phase2_settings
  _,rates=load_phase2_settings()
  fees=estimate_intraday_round_trip_cost(side="LONG",entry_price=trade["entry_fill_price"],
   exit_price=expected_exit_fill,quantity=trade["quantity"],rates=rates)
  gross=(100.95-trade["entry_reference_price"])*trade["quantity"]
  slippage=abs(trade["entry_fill_price"]-trade["entry_reference_price"])*trade["quantity"] + abs(expected_exit_fill-100.95)*trade["quantity"]
  expected=gross-fees.total-slippage
 assert result==pytest.approx(expected)


def test_backtest_and_paper_broker_share_identical_candle_exit(tmp_path):
    import pandas as pd
    from dataclasses import replace
    from src.backtest import BacktestConfig, backtest_fixed_levels
    from src.exit_policy import load_exit_policy

    policy = replace(load_exit_policy(), time_exit_ist="23:59")
    start = datetime(2026, 11, 5, 10, 0, tzinfo=IST)
    stamps = pd.date_range(start, periods=3, freq="5min")
    candles = pd.DataFrame(
        {
            "open": [100.0, 100.0, 100.0],
            "high": [100.5, 100.5, 109.0],
            "low": [99.5, 99.5, 99.5],
            "close": [100.0, 100.0, 108.0],
            "volume": [1000, 1000, 1000],
        },
        index=stamps,
    )

    def signal(history, _index):
        if len(history) != 1:
            return None
        return {"side": "LONG", "stop_loss": 98.0, "target": 108.0}

    backtest_trades, _summary = backtest_fixed_levels(
        "INFY", candles, signal,
        BacktestConfig(initial_capital=100_000, risk_per_trade=500,
                       entry_cutoff="23:55", exit_policy=policy),
    )
    assert len(backtest_trades) == 1

    broker = PaperBroker(tmp_path / "paper.sqlite3", exit_policy=policy)
    with broker._connect() as db:
        db.execute(
            """INSERT INTO trades(
                signal_id,strategy,strategy_version,symbol,side,status,signal_time,entry_time,
                signal_reference_entry,entry_reference_price,entry_fill_price,stop_loss,target_1,target_2,
                quantity,risk_budget_inr,position_value_inr,planned_net_risk_inr,market_regime,
                entry_reason,created_at
            ) VALUES(?,?,?,?,?,'OPEN',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("parity-test", "vwap_trend", "champion_v1", "INFY", "LONG",
             stamps[0].isoformat(), stamps[1].isoformat(), 100.0, 100.0, 100.05,
             98.0, 104.0, 108.0, 100, 500.0, 10005.0, 220.0,
             "BULLISH", "deterministic parity fixture", stamps[0].isoformat()),
        )
    for stamp, bar in candles.iloc[1:].iterrows():
        closed = broker.process_candle(
            "INFY", stamp.to_pydatetime(),
            open_price=float(bar["open"]), high=float(bar["high"]),
            low=float(bar["low"]), close=float(bar["close"]),
        )
    assert closed == 1

    paper_trade = broker.snapshot()["closed_trades"][0]
    assert paper_trade["exit_reason"] == "TARGET_2"
    assert backtest_trades.iloc[0]["exit_reason"] == "TARGET_2"
    assert paper_trade["exit_reference_price"] == pytest.approx(backtest_trades.iloc[0]["exit"])
    assert pd.Timestamp(paper_trade["exit_time"]) == pd.Timestamp(backtest_trades.iloc[0]["exit_time"])


def test_manual_close_uses_fresh_executable_quote_and_records_costs(tmp_path):
    broker = PaperBroker(tmp_path / "paper.sqlite3")
    broker.process_scan_report(report_file(tmp_path, [candidate()], {"INFY": quote()}))
    trade = broker.snapshot()["open_positions"][0]
    result = broker.close_position(trade["id"])
    assert result["exit_reason"] == "MANUAL_EXIT"
    assert result["exit_reference_price"] == pytest.approx(99.99)
    assert result["exit_fill_price"] < result["exit_reference_price"]
    assert result["transaction_charges"] > 0
    assert result["slippage_cost"] > 0
    state = broker.snapshot()
    assert state["account"]["open_positions"] == 0
    closed = state["closed_trades"][0]
    assert closed["exit_reason"] == "MANUAL_EXIT"
    assert closed["net_pnl"] == pytest.approx(
        closed["gross_pnl"] - closed["total_estimated_costs"]
    )
    assert any(
        event["event_type"] == "PAPER_EXIT"
        and event["details"]["exit_reason"] == "MANUAL_EXIT"
        for event in state["events"]
    )


def test_manual_close_fails_closed_without_fresh_quote_or_for_closed_trade(tmp_path):
    broker = PaperBroker(tmp_path / "paper.sqlite3")
    broker.process_scan_report(report_file(tmp_path, [candidate()], {"INFY": quote()}))
    trade = broker.snapshot()["open_positions"][0]
    with broker._connect() as db:
        db.execute("UPDATE market_marks SET received_at=?", (
            (datetime.now(IST) - timedelta(minutes=5)).isoformat(),
        ))
    with pytest.raises(ValueError, match="stale"):
        broker.close_position(trade["id"])
    assert broker.snapshot()["account"]["open_positions"] == 1
    with broker._connect() as db:
        db.execute("UPDATE market_marks SET received_at=?", (datetime.now(IST).isoformat(),))
    broker.close_position(trade["id"])
    with pytest.raises(ValueError, match="already closed"):
        broker.close_position(trade["id"])


def test_manual_close_rejects_unknown_position(tmp_path):
    broker = PaperBroker(tmp_path / "paper.sqlite3")
    with pytest.raises(ValueError, match="not found"):
        broker.close_position(999)


def test_paper_broker_refuses_minimum_net_rr_below_1_5(tmp_path):
    import pytest

    with pytest.raises(ValueError, match="minimum_net_rr must be at least 1.5"):
        PaperBroker(tmp_path / "paper.sqlite3", minimum_net_rr=1.4)


def test_paper_broker_reserves_ten_percent_of_risk_budget_for_stop_gaps(tmp_path):
    broker = PaperBroker(tmp_path / "paper.sqlite3")
    result = broker.process_scan_report(report_file(tmp_path, [candidate()], {"INFY": quote()}))
    assert result["opened"] == 1
    trade = broker.snapshot()["open_positions"][0]
    assert trade["risk_budget_inr"] == pytest.approx(500.0)
    assert trade["planned_net_risk_inr"] <= 450.0
    entry = next(event for event in broker.snapshot()["events"] if event["event_type"] == "PAPER_ENTRY")
    assert entry["details"]["stop_gap_risk_reserve_pct"] == pytest.approx(0.10)
    assert entry["details"]["sizing_risk_budget_inr"] == pytest.approx(450.0)



def test_nse_champion_v2_freezes_all_previous_champion_evidence(tmp_path):
    broker = PaperBroker(tmp_path / "paper.sqlite3")
    registry = {row["strategy_version"]: row for row in broker.snapshot()["strategy_registry"]}
    assert paper_broker_module.PAPER_BASELINE_VERSION == "nse_champion_v2"
    assert registry["champion_v1"]["status"] == "FROZEN_VERSION"
    assert registry["nse_champion_v2_foundation_candidate"]["status"] == "FROZEN_VERSION"
    assert registry["nse_champion_v2_foundation_candidate"]["track"] == "CHAMPION_ARCHIVE"
    assert registry["nse_champion_v2"]["status"] == "ACTIVE"
    assert registry["nse_champion_v2"]["track"] == "CHAMPION"
    assert sum(1 for row in registry.values() if row["track"] == "CHALLENGER" and row["status"] == "SHADOW") == 3
    for version in (
        "challenger_ema_9_21_v1",
        "challenger_vwap_momentum_v1",
        "challenger_ema_pullback_v1",
    ):
        assert registry[version]["status"] == "SUPERSEDED_VERSION"
    for version in (
        "challenger_ema_9_21_atr_seed_v2",
        "challenger_vwap_momentum_atr_seed_v2",
        "challenger_ema_pullback_atr_seed_v2",
    ):
        assert registry[version]["status"] == "SHADOW"



def test_failed_pre_entry_alert_withholds_paper_entry(tmp_path):
 b=PaperBroker(tmp_path/"paper.sqlite3")
 r=b.process_scan_report(
  report_file(tmp_path,[candidate()],{"INFY":quote()}),
  before_entry_callback=lambda plan: False,
 )
 assert r["opened"]==0 and r["rejected"]==1
 assert b.snapshot()["account"]["open_positions"]==0
 assert any("trade-plan alert was not delivered" in row["message"] for row in b.snapshot()["events"])



def test_snapshot_exposes_microstructure_strategies_on_separate_shadow_track(tmp_path):
    broker = PaperBroker(tmp_path / "paper.sqlite3")
    snapshot = broker.snapshot()

    expected = {
        "micro_vp_value_rejection_ofi_v1",
        "micro_vp_value_acceptance_breakout_v1",
        "micro_orderflow_price_continuation_v1",
    }
    displayed = {
        item["strategy_version"]: item
        for item in snapshot["strategies"]
    }
    registered = {
        item["strategy_version"]: item
        for item in snapshot["strategy_registry"]
    }

    assert expected.issubset(displayed)
    assert expected.issubset(registered)
    for version in expected:
        assert displayed[version]["track"] == "MICROSTRUCTURE_SHADOW"
        assert registered[version]["track"] == "MICROSTRUCTURE_SHADOW"
        assert registered[version]["status"] == "SHADOW_ENABLED_NOT_VALIDATED"



def test_out_of_order_market_tick_cannot_overwrite_newer_position_mark(tmp_path):
    broker = PaperBroker(tmp_path / "paper.sqlite3")
    broker.process_scan_report(
        report_file(tmp_path, [candidate()], {"INFY": quote()}),
        before_entry_callback=lambda plan: True,
    )
    received = datetime.now(IST)
    broker.update_market_price(
        "INFY", 101.0, bid=100.99, ask=101.01,
        market_time=received, received_at=received,
    )

    accepted = broker.update_market_price(
        "INFY", 99.0, bid=98.99, ask=99.01,
        market_time=received - timedelta(seconds=10), received_at=received,
    )

    snapshot = broker.snapshot()
    assert accepted == 0
    assert snapshot["open_positions"][0]["last_price"] == pytest.approx(101.0)
    assert snapshot["open_positions"][0]["market_age_seconds"] is not None
    assert snapshot["open_positions"][0]["market_age_seconds"] < 5
