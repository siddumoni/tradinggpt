import math
import pandas as pd
import pytest
from src.performance_stats import calculate_statistics, evaluate_promotion, load_trades, session_block_bootstrap

def sample_trades():
    rows=[]
    for day,vals in [("2026-01-05",[100,-20]),("2026-01-06",[80,-10]),("2026-01-07",[60,-15])]:
        for j,pnl in enumerate(vals):
            entry=100.0; exit_price=entry+pnl/10
            rows.append({"symbol":"AAA" if j==0 else "BBB","side":"LONG","entry_time":f"{day} 10:00:00+05:30","exit_time":f"{day} 10:30:00+05:30","entry":entry,"exit":exit_price,"quantity":10,"gross_pnl":pnl,"transaction_charges":1.0,"accepted":True,"mae_r":.2,"mfe_r":1.2})
    return pd.DataFrame(rows)

def test_stats_reprice_fixed_trades_at_eight_bps_and_calculate_metrics():
    stats=calculate_statistics(sample_trades(),bootstrap_resamples=1000,seed=7)
    assert stats["sample"]=={"trades":6,"sessions":3,"first_session":"2026-01-05","last_session":"2026-01-07"}
    assert stats["slippage_bps_per_side"]==8
    assert stats["expectancy_inr_per_trade"] < sample_trades().gross_pnl.mean()
    assert stats["expectancy_ci_95_inr"]["resamples"]==1000
    assert stats["mae_r_mean"]==pytest.approx(.2)
    assert stats["mfe_r_mean"]==pytest.approx(1.2)
    assert stats["profit_concentration"]["max_symbol_share"] > 0
    assert stats["max_losing_streak"]==1

def test_session_block_bootstrap_is_reproducible():
    values=pd.Series([1.,2.,-1.,-2.]); sessions=pd.Series(["d1","d1","d2","d2"])
    assert session_block_bootstrap(values,sessions,resamples=1000,seed=99)==session_block_bootstrap(values,sessions,resamples=1000,seed=99)

def test_statistics_reject_gross_pnl_mismatch():
    trades=sample_trades(); trades.loc[0,"gross_pnl"]=999
    with pytest.raises(ValueError,match="does not reconcile"): calculate_statistics(trades,bootstrap_resamples=1000)

def test_promotion_inconclusive_for_small_sample_and_promote_only_all_checks_pass():
    stats=calculate_statistics(sample_trades(),bootstrap_resamples=1000)
    config={"thresholds":{"minimum_trades":1,"minimum_sessions":1,"minimum_profit_factor":1.0,"maximum_symbol_or_session_profit_share":1.0}}
    result=evaluate_promotion(stats,config)
    assert result["decision"] in {"PROMOTE","INCONCLUSIVE"}
    config["thresholds"]["minimum_trades"]=100
    assert evaluate_promotion(stats,config)["decision"]=="INCONCLUSIVE"

def test_profit_factor_handles_no_losses_without_nonfinite_json_values():
    trades=sample_trades()
    trades["gross_pnl"]=[100,80,60,40,30,20]
    trades["exit"]=100+trades["gross_pnl"]/10
    stats=calculate_statistics(trades,bootstrap_resamples=1000)
    assert stats["profit_factor"] is None
    assert stats["profit_factor_status"]=="infinite_no_losses"

def test_daily_report_loader_requires_explicit_track_and_strategy_version(tmp_path):
    path=tmp_path/"daily_trades.csv"
    pd.DataFrame([
        {"track":"CHAMPION","strategy_version":"champion_v1","status":"CLOSED","symbol":"AAA","side":"LONG","entry_time":"2026-01-01 10:00+05:30","exit_time":"2026-01-01 10:30+05:30","entry_fill_price":100,"exit_fill_price":101,"quantity":2,"gross_pnl":2,"transaction_charges":1},
        {"track":"CHALLENGER","strategy_version":"challenger_a","status":"CLOSED","symbol":"BBB","side":"LONG","entry_time":"2026-01-01 10:00+05:30","exit_time":"2026-01-01 10:30+05:30","entry_fill_price":100,"exit_fill_price":102,"quantity":2,"gross_pnl":4,"transaction_charges":1},
        {"track":"CHALLENGER","strategy_version":"challenger_b","status":"PENDING_ENTRY","symbol":"CCC","side":"LONG","entry_time":"2026-01-01 10:00+05:30","exit_time":"2026-01-01 10:30+05:30","entry_fill_price":100,"exit_fill_price":102,"quantity":2,"gross_pnl":4,"transaction_charges":1},
    ]).to_csv(path,index=False)
    with pytest.raises(ValueError,match="multiple tracks"): load_trades(path)
    with pytest.raises(ValueError,match="multiple strategy versions"): load_trades(path,track="CHALLENGER")
    selected=load_trades(path,track="CHAMPION")
    assert len(selected)==1 and selected.loc[0,"entry"]==100
    assert selected.attrs["selected_track"]=="CHAMPION"
    selected=load_trades(path,track="CHALLENGER",strategy_version="challenger_a")
    assert len(selected)==1 and selected.loc[0,"symbol"]=="BBB"

def test_single_session_does_not_claim_a_bootstrap_confidence_interval():
    trades=sample_trades()
    trades["entry_time"]="2026-01-05 10:00:00+05:30"
    trades["exit_time"]="2026-01-05 10:30:00+05:30"
    stats=calculate_statistics(trades,bootstrap_resamples=1000)
    assert stats["expectancy_ci_95_inr"]["lower"] is None
    assert stats["expectancy_ci_95_inr"]["status"]=="not_estimable_fewer_than_two_sessions"
    config={"thresholds":{"minimum_trades":1,"minimum_sessions":1,"minimum_profit_factor":0,"maximum_symbol_or_session_profit_share":1}}
    assert evaluate_promotion(stats,config)["checks"]["positive_95pct_expectancy_lower_bound_at_8bps"] is False
