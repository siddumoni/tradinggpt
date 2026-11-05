"""Audit archived candidate CSVs for stop and transaction-cost feasibility."""
from __future__ import annotations
import argparse
from pathlib import Path
import pandas as pd
from src.cost_calculator import ChargeRates,estimate_intraday_round_trip_cost
REQUIRED={"date","symbol","side","entry","stop","target_2"}
def audit_candidates(frame:pd.DataFrame,*,slippage_floor_bps:float=5.0)->pd.DataFrame:
    missing=REQUIRED.difference(frame.columns)
    if missing: raise ValueError(f"Missing required columns: {sorted(missing)}")
    if slippage_floor_bps<5: raise ValueError("slippage floor cannot be below 5 bps per side")
    rows=[]; rates=ChargeRates()
    for x in frame.to_dict("records"):
        e,st,t=(float(x[k]) for k in ("entry","stop","target_2")); q=int(x.get("quantity",1) or 1); side=str(x["side"]).upper()
        if min(e,st,t)<=0 or q<1: raise ValueError("prices and quantity must be positive")
        if (side=="LONG" and not st<e<t) or (side=="SHORT" and not t<e<st): raise ValueError("invalid stop/entry/target ordering")
        slip=float(slippage_floor_bps); bid,ask=x.get("bid"),x.get("ask")
        if pd.notna(bid) and pd.notna(ask) and float(bid)>0 and float(ask)>=float(bid):
            mid=(float(bid)+float(ask))/2; slip=max(slip,(float(ask)-float(bid))/2/mid*10000)
        sc=estimate_intraday_round_trip_cost(side=side,entry_price=e,exit_price=st,quantity=q,rates=rates).total
        tc=estimate_intraday_round_trip_cost(side=side,entry_price=e,exit_price=t,quantity=q,rates=rates).total
        sr=(e+st)*q*slip/10000; tr=(e+t)*q*slip/10000; nr=abs(e-st)*q+sc+sr; nw=abs(t-e)*q-tc-tr; rr=nw/nr if nr>0 else 0
        rows.append({"date":str(x["date"]),"symbol":str(x["symbol"]).upper(),"side":side,"stop_distance_pct":abs(e-st)/e*100,"round_trip_cost_pct":(sc+sr)/(e*q)*100,"net_rr_t2":rr,"slippage_bps_per_side":slip,"passes_1_5_net_rr":rr>=1.5})
    return pd.DataFrame(rows)
def main()->int:
    p=argparse.ArgumentParser(description=__doc__); p.add_argument("input",type=Path); p.add_argument("--output",type=Path); a=p.parse_args(); d=audit_candidates(pd.read_csv(a.input))
    if a.output: a.output.parent.mkdir(parents=True,exist_ok=True); d.to_csv(a.output,index=False)
    if d.empty: print("No candidate rows found."); return 0
    print(d.groupby(["symbol","date"],dropna=False).agg(candidates=("symbol","size"),median_stop_pct=("stop_distance_pct","median"),median_cost_pct=("round_trip_cost_pct","median"),median_net_rr=("net_rr_t2","median"),pass_share=("passes_1_5_net_rr","mean")).to_string())
    print(f"Overall 1.5 net R:R pass share: {d.passes_1_5_net_rr.mean():.1%}"); return 0
if __name__=="__main__": raise SystemExit(main())
