import pandas as pd
import pytest
from src.cost_feasibility import audit_candidates
def sample(): return pd.DataFrame([{"date":"2026-11-05","symbol":"TEST","side":"LONG","entry":100,"stop":99,"target_2":103,"quantity":10,"bid":99.9,"ask":100.1}])
def test_audit_reports_stop_cost_and_net_rr():
 r=audit_candidates(sample()); assert r.loc[0,"stop_distance_pct"]==pytest.approx(1); assert r.loc[0,"slippage_bps_per_side"]>=5; assert r.loc[0,"net_rr_t2"]>1.5
def test_audit_refuses_lower_slippage_floor():
 with pytest.raises(ValueError,match="cannot be below"): audit_candidates(sample(),slippage_floor_bps=4)
def test_audit_requires_columns():
 with pytest.raises(ValueError,match="Missing required"): audit_candidates(pd.DataFrame([{"symbol":"X"}]))
