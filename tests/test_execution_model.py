import pandas as pd
import pytest
from src.execution_model import session_aware_atr,spread_slippage_bps_per_side,build_structural_stop
def frame(start,n,gap=0):
 i=pd.date_range(start,periods=n,freq="5min",tz="Asia/Kolkata"); c=[100+gap+j*.1 for j in range(n)]
 return pd.DataFrame({"open":c,"high":[v+.4 for v in c],"low":[v-.4 for v in c],"close":c,"volume":[1000]*n,"vwap":[v-.1 for v in c]},index=i)
def test_atr_seeds_from_previous_session_without_overnight_gap():
 a=frame("2026-11-02 09:15",15)
 b=frame("2026-11-03 09:15",14,50)
 x=session_aware_atr(pd.concat([a,b]))
 assert x.loc[b.index].iloc[0]==pytest.approx(.8)
 assert x.loc[b.index].iloc[-1]==pytest.approx(.8)


def test_atr_is_valid_on_first_completed_bar_when_prior_seed_exists():
 a=frame("2026-11-02 09:15",15)
 b=frame("2026-11-03 09:15",2,50)
 b.iloc[0, b.columns.get_loc("high")] += .6
 b.iloc[0, b.columns.get_loc("low")] -= .6
 x=session_aware_atr(pd.concat([a,b]))
 expected=(13*.8+2.0)/14
 assert x.loc[b.index[0]]==pytest.approx(expected)
 assert x.loc[b.index[1]]==pytest.approx((13*expected+.8)/14)


def test_first_available_session_still_requires_atr_warmup():
 a=frame("2026-11-02 09:15",14)
 x=session_aware_atr(a)
 assert x.iloc[:13].isna().all()
 assert x.iloc[13]==pytest.approx(.8)
def test_spread_cost_respects_floor():
 q={"depth":{"buy":[{"price":99.0}],"sell":[{"price":101.0}]}}; v,status=spread_slippage_bps_per_side(q); assert v==pytest.approx(100.0); assert status=="LIVE_SPREAD_PLUS_FLOOR"; assert spread_slippage_bps_per_side(None)[0]==5
def test_structural_stop_enforces_minimum_distance():
 f=frame("2026-11-02 09:15",20); f["vwap"]=99.95; p=build_structural_stop(side="LONG",entry=102,candles_5m=f,candles_15m=pd.DataFrame(),atr=.8,minimum_distance_pct=.5); assert p.distance_pct>=.5
def test_no_adverse_structure_fails_closed():
 f=frame("2026-11-02 09:15",2); f[["open","high","low","close","vwap"]]=[103.5,104,103,103.7,103.6]
 with pytest.raises(ValueError,match="No valid structural stop"): build_structural_stop(side="LONG",entry=100,candles_5m=f,candles_15m=pd.DataFrame(),atr=200)



def test_atr_does_not_reuse_seed_across_missing_trading_session():
    first = frame("2026-11-02 09:15", 15)
    after_missing_session = frame("2026-11-04 09:15", 14, 50)
    result = session_aware_atr(pd.concat([first, after_missing_session]))
    assert result.loc[after_missing_session.index].iloc[:13].isna().all()
    assert result.loc[after_missing_session.index].iloc[-1] == pytest.approx(0.8)
