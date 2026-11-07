import math
import pandas as pd
import pytest
from src.performance_stats import (
    analyze_file, calculate_statistics, evaluate_promotion, load_trades,
    paired_session_bootstrap, session_block_bootstrap,
)

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

def test_loader_combines_daily_files_with_different_optional_columns(tmp_path):
    one=tmp_path/"day1.csv"; two=tmp_path/"day2.csv"
    base={"track":"CHAMPION","strategy_version":"champion_v1","status":"CLOSED","symbol":"AAA","side":"LONG","entry_time":"2026-01-01 10:00+05:30","exit_time":"2026-01-01 10:30+05:30","entry_fill_price":100,"exit_fill_price":101,"quantity":2,"gross_pnl":2,"transaction_charges":1}
    pd.DataFrame([{**base,"scan_as_of":"2026-01-01T10:01+05:30"}]).to_csv(one,index=False)
    pd.DataFrame([{**base,"symbol":"BBB","entry_time":"2026-01-02 10:00+05:30","exit_time":"2026-01-02 10:30+05:30"}]).to_csv(two,index=False)
    combined=load_trades([one,two],track="CHAMPION")
    assert len(combined)==2
    assert set(combined.symbol)=={"AAA","BBB"}



def paired_sample(days, gross_pnl):
    rows = []
    for day in days:
        entry = 100.0
        exit_price = entry + gross_pnl / 10.0
        rows.append({
            "symbol": "AAA", "side": "LONG",
            "entry_time": f"{day} 10:00:00+05:30",
            "exit_time": f"{day} 10:30:00+05:30",
            "entry": entry, "exit": exit_price, "quantity": 10,
            "gross_pnl": gross_pnl, "transaction_charges": 0.0,
        })
    return pd.DataFrame(rows)


def test_paired_session_bootstrap_uses_common_sessions_and_bonferroni_adjustment():
    days = [f"2026-02-{day:02d}" for day in range(1, 21)]
    challenger = paired_sample(days, 20.0)
    champion = paired_sample(days, 0.0)

    result = paired_session_bootstrap(
        challenger, champion, family_size=3, resamples=1000, seed=11,
    )

    assert result["status"] == "estimated"
    assert result["common_sessions"] == 20
    assert result["per_comparison_alpha"] == pytest.approx(0.05 / 3)
    assert result["mean_difference_inr_per_session"] > 0
    assert result["adjusted_lower_bound_inr_per_session"] > 0
    assert "Bonferroni" in result["method"]


def test_paired_session_bootstrap_does_not_invent_nonoverlapping_sessions():
    challenger = paired_sample(["2026-02-01", "2026-02-02"], 20.0)
    champion = paired_sample(["2026-02-03", "2026-02-04"], 0.0)

    result = paired_session_bootstrap(challenger, champion, resamples=1000)

    assert result["common_sessions"] == 0
    assert result["adjusted_lower_bound_inr_per_session"] is None


def test_challenger_promotion_requires_positive_adjusted_paired_comparison():
    stats = calculate_statistics(sample_trades(), bootstrap_resamples=1000)
    config = {
        "thresholds": {
            "minimum_trades": 1, "minimum_sessions": 1,
            "minimum_profit_factor": 0.0,
            "maximum_symbol_or_session_profit_share": 1.0,
        },
        "paired_comparison": {"minimum_common_sessions": 1},
    }

    result = evaluate_promotion(stats, config, require_paired_comparison=True)

    assert result["decision"] == "INCONCLUSIVE"
    assert result["checks"]["minimum_common_paired_sessions"] is False
    assert result["checks"]["positive_multiplicity_adjusted_paired_lower_bound_at_8bps"] is False


def test_inconclusive_analysis_never_overwrites_public_results_document(tmp_path):
    trades_path = tmp_path / "trades.csv"
    sample_trades().to_csv(trades_path, index=False)
    promotion_path = tmp_path / "promotion.yaml"
    promotion_path.write_text(
        "thresholds:\n  minimum_trades: 100\n  minimum_sessions: 20\n"
        "  minimum_profit_factor: 1.0\n  maximum_symbol_or_session_profit_share: 0.30\n",
        encoding="utf-8",
    )
    results_path = tmp_path / "RESULTS.md"
    results_path.write_text("KEEP EXISTING EVIDENCE STATUS", encoding="utf-8")

    _stats, promotion = analyze_file(
        trades_path,
        output_stem=tmp_path / "validation",
        promotion_config=promotion_path,
        results_doc=results_path,
        bootstrap_resamples=1000,
    )

    assert promotion["decision"] == "INCONCLUSIVE"
    assert results_path.read_text(encoding="utf-8") == "KEEP EXISTING EVIDENCE STATUS"
    assert (tmp_path / "validation.json").exists()
    assert (tmp_path / "validation.md").exists()



def test_paired_analysis_rejects_mismatched_or_undeclared_account_profiles(tmp_path):
    challenger = sample_trades()
    challenger["track"] = "CHALLENGER"
    challenger["strategy_version"] = "challenger_test_v1"
    champion = sample_trades()
    champion["track"] = "CHAMPION"
    champion["strategy_version"] = "nse_champion_v2_foundation_candidate"
    challenger_path = tmp_path / "challenger.csv"
    champion_path = tmp_path / "champion.csv"
    challenger.to_csv(challenger_path, index=False)
    champion.to_csv(champion_path, index=False)
    promotion_path = tmp_path / "promotion.yaml"
    promotion_path.write_text(
        "thresholds:\n  minimum_trades: 100\n  minimum_sessions: 20\n"
        "  minimum_profit_factor: 1.0\n  maximum_symbol_or_session_profit_share: 0.30\n"
        "paired_comparison:\n  minimum_common_sessions: 20\n  familywise_alpha: 0.05\n"
        "  family_size: 3\n  adjustment: bonferroni\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="matching explicit profile"):
        analyze_file(
            challenger_path, output_stem=tmp_path / "missing-profile",
            promotion_config=promotion_path, results_doc=tmp_path / "RESULTS.md",
            track="CHALLENGER", strategy_version="challenger_test_v1",
            champion_input_path=champion_path, profile="research",
        )
    with pytest.raises(ValueError, match="matching explicit profile"):
        analyze_file(
            challenger_path, output_stem=tmp_path / "mismatched-profile",
            promotion_config=promotion_path, results_doc=tmp_path / "RESULTS.md",
            track="CHALLENGER", strategy_version="challenger_test_v1",
            champion_input_path=champion_path, profile="research",
            champion_profile="paper",
        )



def test_performance_analysis_rejects_capital_outside_selected_profile(tmp_path):
    trades_path = tmp_path / "trades.csv"
    sample_trades().to_csv(trades_path, index=False)
    promotion_path = tmp_path / "promotion.yaml"
    promotion_path.write_text(
        "thresholds:\n  minimum_trades: 100\n  minimum_sessions: 20\n"
        "  minimum_profit_factor: 1.0\n  maximum_symbol_or_session_profit_share: 0.30\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="must match the selected account profile"):
        analyze_file(
            trades_path, output_stem=tmp_path / "capital-mismatch",
            promotion_config=promotion_path, results_doc=tmp_path / "RESULTS.md",
            profile="research", initial_capital=100_000.0,
        )
