import math
import pandas as pd
import pytest
from src.performance_stats import calculate_statistics, evaluate_promotion, session_block_bootstrap

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
