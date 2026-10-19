#!/usr/bin/env python3
"""Restore the fixed 30/50 wide-exit baseline; extend to 2025-12-16.

Simple causal ATR timing hypotheses are fixed before performance evaluation.
No V2 parameter optimization, no automatic production strategy changes.
python scripts/analyze_eg_ten_months.py
"""
import copy
import json
import math
import statistics
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime,timedelta
from pathlib import Path

import audit_eg_15m as audit
import research_eg_candidates as research
import extend_eg_backtest as extended

OUT=audit.ROOT/"reports"/"eg_ten_months_20261019"
START="2025-12-16 09:00:00"
END=extended.END
OLD_OUT=extended.OUT
BASE_SIGNAL=research.make_signal
VARIANTS=["baseline","atr_floor20_prev","atr_band20_60_prev","atr_rank_upper_half_prev",
          "atr_expanding5_prev","atr_band20_60_current","er025_prev","atr_band20_60_prev_er025"]
WINDOWS={"new_older_four":(START,"2026-04-15 23:59:59"),
         "previous_four":("2026-04-16 09:00:00","2026-08-15 23:59:59"),
         "recent_two":("2026-08-16 09:00:00",END),"ten_months":(START,END)}


def save(name,value):
    (OUT/name).write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding="utf-8")


def configuration(name):
    return {**research.CONFIGS[2],"name":name,"timing":name,"require_current_main":True}


def prepare():
    # Feed merged, immutable minute snapshots into the previously checked parser.
    before=(extended.OUT,extended.START,extended.REQUESTS)
    try:
        extended.OUT=OUT;extended.START=START
        extended.REQUESTS=[(n,"","","") for n in ["main","eg2601","eg2605","eg2609","eg2610","eg2611"]]
        bars,groups,indices,mapping=extended.prepare()
    finally:
        extended.OUT,extended.START,extended.REQUESTS=before
    return bars,groups,indices,mapping


def features(bs,i):
    b=bs[i]
    prior=bs[i-1]["atr20"] if i else None
    hist=[x["atr20"] for x in bs[max(0,i-120):i] if x["atr20"] is not None]
    rank=(sum(v<prior for v in hist)+.5*sum(v==prior for v in hist))/len(hist) if prior is not None and len(hist)==120 else None
    er=momentum=None
    if i>=21:
        momentum=bs[i-1]["c"]-bs[i-21]["c"]
        total=sum(abs(bs[j]["c"]-bs[j-1]["c"]) for j in range(i-20,i))
        er=abs(momentum)/total if total else 0.0
    older=bs[i-6]["atr20"] if i>=6 else None
    return {"atr_signal":b["atr20"],"atr_prev":prior,"atr_rank_prev":rank,"er20_prev":er,
            "atr_prev5":older,"momentum20_prev":momentum}


def timing_signal(bs,i,cfg,symbol):
    sig=BASE_SIGNAL(bs,i,cfg,symbol)
    if sig is None:return None
    f=features(bs,i);atr=f["atr_prev"];name=cfg["timing"]
    if name=="atr_floor20_prev" and (atr is None or atr<20):return None
    if name in ["atr_band20_60_prev","atr_band20_60_prev_er025"] and (atr is None or not 20<=atr<=60):return None
    if name=="atr_rank_upper_half_prev" and (f["atr_rank_prev"] is None or f["atr_rank_prev"]<.5):return None
    if name=="atr_expanding5_prev" and (atr is None or f["atr_prev5"] is None or atr<f["atr_prev5"]):return None
    if name=="atr_band20_60_current" and not 20<=f["atr_signal"]<=60:return None
    if name in ["er025_prev","atr_band20_60_prev_er025"] and (f["er20_prev"] is None or f["er20_prev"]<.25):return None
    return {**sig,**f}


@contextmanager
def model():
    old=research.make_signal;research.make_signal=timing_signal
    try:
        with research.execution_model():yield
    finally:research.make_signal=old


def compact(s):
    return {k:s[k] for k in ["name","slip","mode","start","end","trades","wins","losses","win_rate_pct","net_pnl","profit_factor","max_drawdown","worst_trade","gap_stop_trades"]}


def median(xs):
    xs=[x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def monthly_attribution(bars,indices,mapping,trades,curve):
    grouped=defaultdict(list);market=defaultdict(list);equity=defaultdict(list)
    for t in trades:grouped[t["entry_dt"][:7]].append(t)
    for b in bars["main"]:
        if not START<=b["dt"]<=END:continue
        sym=mapping[b["dt"]];i=indices[sym][b["dt"]];own=bars[sym][i]
        market[b["dt"][:7]].append({**own,**features(bars[sym],i)})
    for e in curve:equity[e["dt"][:7]].append(e)
    rows=[];last_equity=0
    for month in sorted(market):
        ts=grouped[month];ms=market[month];es=equity[month]
        vals=[t["net_pnl"] for t in ts];wins=[v for v in vals if v>0];losses=[v for v in vals if v<0]
        gp=sum(wins);gl=sum(losses);net=sum(vals)
        aw=statistics.mean(wins) if wins else None;al=-statistics.mean(losses) if losses else None
        extras=0.0
        for t in ts:
            for leg in t["legs"]:
                if leg["reason"]=="gap_stop":extras+=max(0,(t["stop"]-1)*10*leg["qty"]-leg["pnl"])
        end_equity=es[-1]["equity"]
        row={"month":month,"first_bar":ms[0]["dt"],"last_bar":ms[-1]["dt"],"bars":len(ms),"trades":len(ts),
             "wins":len(wins),"losses":len(losses),"win_rate_pct":100*len(wins)/len(ts) if ts else None,
             "cohort_net_pnl":net,"month_close_equity_change":end_equity-last_equity,"end_equity":end_equity,
             "gross_profit_after_costs":gp,"gross_loss_after_costs":gl,"average_win":aw,"average_loss_abs":al,
             "break_even_win_rate_pct":100*al/(aw+al) if aw and al else None,
             "net_per_trade":net/len(ts) if ts else None,"fees":sum(t["fees"] for t in ts),
             "long_pnl":sum(t["net_pnl"] for t in ts if t["direction"]==1),
             "short_pnl":sum(t["net_pnl"] for t in ts if t["direction"]==-1),
             "top3_pnl":sum(sorted(vals,reverse=True)[:3]),"gap_stop_trades":sum(t["gap_stop"] for t in ts),
             "extra_loss_from_gaps":extras,"atr_prev_median":median(x["atr_prev"] for x in ms),
             "atr_signal_median":median(x["atr_signal"] for x in ms),
             "atr_prev_pct_price_median":median(100*x["atr_prev"]/x["c"] for x in ms if x["atr_prev"] is not None),
             "range_median":median(x["h"]-x["l"] for x in ms),"er20_prev_median":median(x["er20_prev"] for x in ms),
             "volume_median":median(x["v"] for x in ms),"open_interest_median":median(x["p"] for x in ms),
             "signal_atr_median_on_trades":median(t["atr_prev"] for t in ts)}
        last_equity=end_equity;rows.append(row)
    assert abs(sum(r["cohort_net_pnl"] for r in rows)-sum(t["net_pnl"] for t in trades))<1e-7
    assert abs(sum(r["month_close_equity_change"] for r in rows)-curve[-1]["equity"])<1e-7
    return rows


def trade_feature_buckets(trades):
    buckets=defaultdict(list)
    for t in trades:
        atr=t["atr_prev"]
        key="<20" if atr<20 else "20-30" if atr<30 else "30-40" if atr<40 else "40-60" if atr<60 else ">=60"
        buckets[key].append(t)
    result=[]
    for key in ["<20","20-30","30-40","40-60",">=60"]:
        ts=buckets[key];vals=[t["net_pnl"] for t in ts]
        result.append({"atr_prev_bucket":key,"trades":len(ts),"wins":sum(v>0 for v in vals),"net_pnl":sum(vals),
                       "net_per_trade":statistics.mean(vals) if vals else None})
    return result


def correlation(xs,ys):
    if len(xs)<3 or len(set(xs))<2 or len(set(ys))<2:return None
    return statistics.correlation(xs,ys)


def main():
    OUT.mkdir(parents=True,exist_ok=True)
    plan={"created_at":datetime.now(extended.ZONE).isoformat(),"baseline":configuration("baseline"),"windows":WINDOWS,
          "timing_tests":VARIANTS,"atr_formula":"SMA20 true range on individual contract; same as original strategy, NOT Wilder smoothing",
          "lag":"prev means t-1, before warning candle; current means closed warning candle t, still known before next-bar entry",
          "rank":"ATR20_(t-1) midrank among prior 120 ATR20 values, all ending before warning candle",
          "er":"abs(C_(t-1)-C_(t-21))/sum(abs(close changes), previous20), causal directional-efficiency comparator",
          "threshold_rationale":"20 points=2/3 of fixed30 stop; 60 points=2*stop; rank50%, ER25% are coarse fixed hypotheses, no threshold search",
          "execution":"15m signals / chronological 1m replay / main at signal and entry, old warning canceled on rollover",
          "costs":"3 CNY/lot/side; base 0/1/2tick stress; timing 1/2ticks; two actual lots; NO normalized values in headline tables",
          "no_optimization":True,"warning":"all timing alternatives reported, not automatically chosen or deployed based on full-sample best profit",
          "source_manifest":json.loads((OUT/"merged_inputs.json").read_text(encoding="utf-8"))}
    save("analysis_plan.json",plan)
    bars,groups,indices,mapping=prepare()
    results=[];baseline_data={}
    with model():
        for window,span in WINDOWS.items():
            for variant in VARIANTS:
                slips=[0,1,2] if variant=="baseline" else [1,2]
                modes=research.MODES if variant=="baseline" else ["adverse_first","favorable_first"]
                for slip in slips:
                    for mode in modes:
                        cfg=configuration(variant)
                        stats,trades,curve=extended.run(bars,groups,indices,mapping,cfg,slip,mode,*span)
                        stats["window"]=window;results.append(stats)
                        label=f"{window}_{variant}_slip{slip}_{mode}"
                        save(label+"_trades.json",trades)
                        if slip==1 and mode=="adverse_first":
                            research.csv_save(str(OUT/(label+"_equity.csv")),curve)
                            research.csv_save(str(OUT/(label+"_trades.csv")),trades)
                            print(window,variant,stats["trades"],stats["net_pnl"],"DD",stats["max_drawdown"],flush=True)
                            if variant=="baseline":baseline_data[window]=(stats,trades,curve)
        # All indicators/filters must produce identical signals on the same prefix.
        for variant in VARIANTS:
            for sym,bs in bars.items():
                if sym=="main":continue
                for i in [len(bs)//3,len(bs)//2,len(bs)-2]:
                    prefix=copy.deepcopy(bs[:i+1]);research.enrich(prefix)
                    assert timing_signal(prefix,i,configuration(variant),sym)==timing_signal(bs,i,configuration(variant),sym)
    save("all_results.json",results)
    research.csv_save(str(OUT/"comparison.csv"),[{"window":s["window"],**compact(s)} for s in results])
    stats,ts,curve=baseline_data["ten_months"]
    months=monthly_attribution(bars,indices,mapping,ts,curve)
    save("monthly_attribution.json",months);research.csv_save(str(OUT/"monthly_attribution.csv"),months)
    buckets=trade_feature_buckets(ts);save("atr_trade_buckets.json",buckets);research.csv_save(str(OUT/"atr_trade_buckets.csv"),buckets)
    pairs=[r for r in months if r["trades"]>=1]
    save("descriptive_correlations.json",{"months":len(pairs),"atr_vs_month_cohort_net":correlation([r["atr_prev_median"] for r in pairs],[r["cohort_net_pnl"] for r in pairs]),
         "atr_vs_net_per_trade":correlation([r["atr_prev_median"] for r in pairs],[r["net_per_trade"] for r in pairs]),
         "er_vs_net_per_trade":correlation([r["er20_prev_median"] for r in pairs],[r["net_per_trade"] for r in pairs]),
         "warning":"descriptive small-sample association, includes partial months; NOT causal evidence or a timing signal"})
    # Preserve direct comparability of existing four- and two-month results.
    previous=json.loads((OLD_OUT/"all_results.json").read_text(encoding="utf-8"))
    for window,old_window in [("previous_four","older_four"),("recent_two","recent_two")]:
        for s in results:
            if s["window"]!=window or s["name"]!="baseline":continue
            ref=next(r for r in previous if r["window"]==old_window and r["name"]=="wider_exits" and r["slip"]==s["slip"] and r["mode"]==s["mode"])
            for k in ["trades","wins","losses","net_pnl","max_drawdown"]:
                assert s[k]==ref[k],(window,k,s[k],ref[k])
    mismatches=[]
    for t in ts:
        d=datetime.fromisoformat(t["entry_dt"]);parent=d.replace(minute=d.minute//15*15,second=0)
        if d.minute%15 or d.second:parent+=timedelta(minutes=15)
        if mapping[parent.isoformat(sep=" ")]!=t["contract"]:mismatches.append(t["entry_dt"])
    assert not mismatches
    save("verification.json",{"passed":True,"scenarios":len(results),"checks":["existing baseline windows reproduced with no performance changes", "causal prefix equality for every ATR/ER alternative", "all baseline entries are current main", "trade and calendar-month equity reconciliation", "two-lot fees and position accounting", "merged-overlap raw data equality"]})


if __name__=="__main__":main()
