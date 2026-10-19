#!/usr/bin/env python3
"""Bounded EG research optimization. Training uses ONLY 2026-04-16..08-15.

The already-viewed later period is a diagnostic, never an untouched holdout.
Run python scripts/tune_eg_strategy.py. Original strategy/engine are unmodified.
"""
import copy
import hashlib
import json
import math
from contextlib import contextmanager
from datetime import datetime,timedelta
from pathlib import Path

import audit_eg_15m as audit
import research_eg_candidates as research
import extend_eg_backtest as extended

OUT=audit.ROOT/"reports"/"eg_tuning_20261019"
STOPS=[15,20,30,40]
REWARDS=[1.5,2.0,2.5]
TRAIN_END="2026-08-15 23:59:59"
WINDOWS={"fit_first_half":(extended.START,"2026-06-15 23:59:59"),
         "fit_second_half":("2026-06-16 09:00:00",TRAIN_END),
         "fit_four":(extended.START,TRAIN_END)}
BASE_SIGNAL=research.make_signal


def save(name,value):
    (OUT/name).write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding="utf-8")


def configs():
    result=[]
    for r in STOPS:
        for rr in REWARDS:
            for entry in ["close","extreme"]:
                for fresh in [True,False]:
                    result.append({"name":f"r{r}_rr{rr:g}_{entry}_fresh{int(fresh)}","signal":"warning","exit":"ladder","risk":"fixed",
                                   "distance":r,"tp":math.ceil(r*rr),"protect":max(1,r//3),"step":r,"trend":False,
                                   "entry_reference":entry,"freshness_filter":fresh,"reward_ratio":rr,"require_current_main":True})
    # Second stage: same coarse grid, adding only the previously defined EMA rule.
    # No indicator periods or new thresholds are searched. Stage-1 artifacts retained.
    result += [{**c,"name":"ema_"+c["name"],"trend":True} for c in list(result)]
    for cfg in [research.CONFIGS[0],research.CONFIGS[2]]:
        result.append({**cfg,"name":"control_"+cfg["name"],"control":True})
    return result


def tuned_signal(bars,i,cfg,symbol):
    if cfg.get("control"):
        return BASE_SIGNAL(bars,i,cfg,symbol)
    b=bars[i]
    # Unchanged strict candle definitions; always retain giant-candle filter.
    sig=audit.signal(bars,i,filters=False)
    if sig is None or b["h"]-b["l"]>2*b["atr20"]:
        return None
    d=sig["direction"]
    if cfg["trend"]:
        if i<60 or (b["ema20"]-b["ema60"])*d<=0 or (b["ema60"]-bars[i-3]["ema60"])*d<=0:
            return None
    if cfg["freshness_filter"] and all((bars[j]["c"]-bars[j]["o"])*d>0 for j in [i-2,i-1]):
        return None
    reference=b["c"] if cfg["entry_reference"]=="close" else b["h"] if d==1 else b["l"]
    r=cfg["distance"]
    return {**sig,"trigger":reference+d,"contract":symbol,"initial_distance":r,"target":cfg["tp"],"protect":cfg["protect"],
            "step":cfg["step"],"exit_model":"ladder","signal_atr":b["atr20"],"reference_close":b["c"]}


@contextmanager
def model():
    old=research.make_signal
    research.make_signal=tuned_signal
    try:
        with research.execution_model():
            yield
    finally:
        research.make_signal=old


def compact(stats):
    return {k:stats[k] for k in ["name","slip","mode","start","end","trades","wins","losses","win_rate_pct","net_pnl","profit_factor","max_drawdown","worst_trade"]} | {
        "normalized_pnl":stats["risk_normalized"]["net_pnl"],"normalized_dd":stats["risk_normalized"]["max_drawdown"]}


def neighbors(cfg,all_cfgs):
    if cfg.get("control"):
        return []
    result=[]
    ri=STOPS.index(cfg["distance"]);qi=REWARDS.index(cfg["reward_ratio"])
    for c in all_cfgs:
        if c.get("control") or c["entry_reference"]!=cfg["entry_reference"] or c["freshness_filter"]!=cfg["freshness_filter"] or c["trend"]!=cfg["trend"]:
            continue
        if abs(STOPS.index(c["distance"])-ri)+abs(REWARDS.index(c["reward_ratio"])-qi)==1:
            result.append(c["name"])
    return result


def evaluate(bars,groups,indices,mapping,cfg,slip,mode,window):
    start,end=window
    return extended.run(bars,groups,indices,mapping,cfg,slip,mode,start,end)


def selection_row(cfg,training,all_cfgs):
    name=cfg["name"]
    first=training[(name,"fit_first_half",1)]
    second=training[(name,"fit_second_half",1)]
    full=training[(name,"fit_four",1)]
    stress=training[(name,"fit_four",2)]
    near=neighbors(cfg,all_cfgs)
    profits=[training[(n,"fit_four",2)]["risk_normalized"]["net_pnl"] for n in near]
    near_positive=sum(p>0 for p in profits)/len(profits) if profits else 0
    samples=sorted([stress["risk_normalized"]["net_pnl"]]+profits)
    n=len(samples)
    median=(samples[(n-1)//2]+samples[n//2])/2
    score=median-.5*stress["risk_normalized"]["max_drawdown"]
    gates={"two_halves_at_least_15_trades":min(first["trades"],second["trades"])>=15,
           "two_halves_positive_1tick":min(first["risk_normalized"]["net_pnl"],second["risk_normalized"]["net_pnl"])>0,
           "full_positive_2ticks":stress["risk_normalized"]["net_pnl"]>0,
           "at_least_two_neighbors":len(near)>=2,
           "at_least_60pct_positive_neighbors_2ticks":near_positive>=.6}
    return {"name":name,"control":bool(cfg.get("control")),"eligible":all(gates.values()),"gates":gates,
            "score":score,"first_1tick_normalized":first["risk_normalized"]["net_pnl"],"second_1tick_normalized":second["risk_normalized"]["net_pnl"],
            "full_1tick_pnl":full["net_pnl"],"full_1tick_normalized":full["risk_normalized"]["net_pnl"],
            "full_2tick_normalized":stress["risk_normalized"]["net_pnl"],"full_2tick_dd_normalized":stress["risk_normalized"]["max_drawdown"],
            "neighbors":near,"positive_neighbor_fraction":near_positive,"neighbor_median_2tick_profit":median}


def verify_candidate(bars,groups,indices,mapping,cfg):
    # Mirror every price, recompute indicators, and require identical long/short PnL.
    # This tests combined EMA filtering, extreme entries and all minute events.
    mirrored=copy.deepcopy(bars);mgroups=copy.deepcopy(groups)
    def reflect(b):
        b["o"],b["h"],b["l"],b["c"]=20000-b["o"],20000-b["l"],20000-b["h"],20000-b["c"]
    for sym,bs in mirrored.items():
        for b in bs:reflect(b)
        if sym!="main":research.enrich(bs)
    for gs in mgroups.values():
        for sub in gs.values():
            for b in sub:reflect(b)
    span=(extended.START,extended.END)
    with model():
        _,one,_=evaluate(bars,groups,indices,mapping,cfg,1,"adverse_first",span)
        _,two,_=evaluate(mirrored,mgroups,indices,mapping,cfg,1,"adverse_first",span)
    assert [(t["signal_dt"],t["entry_dt"],t["exit_dt"],t["direction"],t["net_pnl"]) for t in one]==[
           (t["signal_dt"],t["entry_dt"],t["exit_dt"],-t["direction"],t["net_pnl"]) for t in two]
    for t in one:
        dt=datetime.fromisoformat(t["entry_dt"])
        parent=dt.replace(minute=dt.minute//15*15,second=0)
        if dt.minute%15 or dt.second:parent+=timedelta(minutes=15)
        assert t["contract"]==mapping[parent.isoformat(sep=" ")]
    # Larger target's ladder transitions: 75 -> protect 10, 105 ->75, 135 ->105.
    sig={"signal_dt":"test","direction":1,"trigger":1000,"kind":"test","initial_distance":30,
         "target":75,"protect":10,"step":30,"exit_model":"ladder"}
    p=research.new_position(sig,1000,"test",0)
    research.favorable(p,1075,"test",0);assert p["qty"]==1 and p["stop"]==10
    research.favorable(p,1105,"test",0);assert p["stop"]==75
    research.favorable(p,1135,"test",0);assert p["stop"]==105
    with research.execution_model():audit.segment(p,1135,1100,"test",0)
    assert research.finish(p,3)["net_pnl"]==1788
    return {"passed":True,"checks":["full six-month price-reflection symmetry of signals and minute executions",
             "every selected trade opens on current main", "75/105/135 ladder transitions and accounting"],"selected_trades":len(one)}


def main():
    OUT.mkdir(parents=True,exist_ok=True)
    all_cfgs=configs()
    plan={"created_at":datetime.now(extended.ZONE).isoformat(),"configs":all_cfgs,"candidate_count":len(all_cfgs),
          "stage_history":"Stage 1 evaluated 48 grid candidates + 2 controls, none passed. This disclosed second stage adds the same 48 configurations with fixed EMA20/60 alignment and EMA60 3bar slope. No gates relaxed. Stage 1 artifacts retained in stage1/.",
          "training_windows":WINDOWS,"later_diagnostic":[extended.SPLIT,extended.END],
          "selection_gates":["at least 15 trades per two-month training half","both training halves profitable after fees and 1 tick",
              "full four-month profit positive after 2 ticks","at least 2 immediate stop/target neighbors","at least 60% neighbors profitable after 2 ticks"],
          "score":"median 2tick risk-normalized profit of self+adjacent candidates - 0.5*self 2tick risk-normalized drawdown",
          "selection":"rank eligible candidates by score; if none eligible, report failure and highest-score research candidate without relaxing gates",
          "no_later_period_selection":True,"later_period_warning":"Already inspected previously; NOT an independent untouched holdout",
          "entry_constraint":"New candidates require current main at signal and entry; pending warning canceled when main changes. Controls preserve original behavior.",
          "position":"2 actual lots; normalized outputs diagnostic fractional lots only","fees":"3 CNY per lot per side",
          "inputs":json.loads((extended.OUT/"edb_sources.json").read_text(encoding="utf-8"))}
    save("optimization_plan.json",plan)
    bars,groups,indices,mapping=extended.prepare()
    training={};flat=[]
    with model():
        for n,cfg in enumerate(all_cfgs,1):
            for window in ["fit_first_half","fit_second_half","fit_four"]:
                for slip in ([1,2] if window=="fit_four" else [1]):
                    stats,_,_=evaluate(bars,groups,indices,mapping,cfg,slip,"adverse_first",WINDOWS[window])
                    training[(cfg["name"],window,slip)]=stats
                    flat.append({"window":window,**compact(stats)})
            if n%10==0:
                print("training",n,"/",len(all_cfgs),flush=True)
        rows=[selection_row(c,training,all_cfgs) for c in all_cfgs]
        ranked=sorted(rows,key=lambda r:(r["eligible"],r["score"]),reverse=True)
        selected=next((r for r in ranked if r["eligible"]),ranked[0])
        cfg=next(c for c in all_cfgs if c["name"]==selected["name"])
        # Lock selection BEFORE any later-period run, retaining full failed gates.
        selection={"locked_at":datetime.now(extended.ZONE).isoformat(),"config":cfg,"assessment":selected,
                   "eligible_count":sum(r["eligible"] for r in rows),"trials":len(all_cfgs),"claim":"research candidate; no future-profit claim"}
        save("selection_locked.json",selection)
        save("training_ranking.json",ranked)
        save("training_results.json",flat)
        research.csv_save(str(OUT/"training_comparison.csv"),flat)
        print("SELECTED",json.dumps(selection,ensure_ascii=False),flush=True)
        outputs=[]
        target_cfgs=[cfg]+[c for c in all_cfgs if c.get("control")]
        for c in target_cfgs:
            for window,span in [("older_four",(extended.START,TRAIN_END)),("recent_two",(extended.SPLIT,extended.END)),("six_months",(extended.START,extended.END))]:
                for slip in [0,1,2]:
                    for mode in research.MODES:
                        stats,trades,curve=evaluate(bars,groups,indices,mapping,c,slip,mode,span)
                        outputs.append({"window":window,**stats})
                        label=f"{window}_{c['name']}_slip{slip}_{mode}"
                        save(label+"_trades.json",trades)
                        if slip==1 and mode=="adverse_first":
                            research.csv_save(str(OUT/(label+"_equity.csv")),curve)
                            research.csv_save(str(OUT/(label+"_trades.csv")),[{**{k:v for k,v in t.items() if k!="legs"},"legs":json.dumps(t["legs"])} for t in trades])
                            print(window,c["name"],stats["trades"],stats["net_pnl"],"norm",round(stats["risk_normalized"]["net_pnl"],2),"dd",stats["max_drawdown"],flush=True)
        save("diagnostic_results.json",outputs)
        research.csv_save(str(OUT/"diagnostic_comparison.csv"),[{"window":s["window"],**compact(s)} for s in outputs])
    # Prefix causality and exact reproduction of both controls.
    for c in [cfg]+[c for c in all_cfgs if c.get("control")]:
        for sym,bs in bars.items():
            if sym=="main":continue
            i=len(bs)//2;prefix=copy.deepcopy(bs[:i+1]);research.enrich(prefix)
            assert tuned_signal(prefix,i,c,sym)==tuned_signal(bs,i,c,sym)
    previous=json.loads((extended.OUT/"all_results.json").read_text(encoding="utf-8"))
    for row in outputs:
        if not row["name"].startswith("control_"):continue
        name=row["name"].removeprefix("control_")
        ref=next(r for r in previous if r["window"]==row["window"] and r["name"]==name and r["slip"]==row["slip"] and r["mode"]==row["mode"])
        for k in ["trades","wins","losses","net_pnl","max_drawdown"]:
            assert row[k]==ref[k],(name,k,row[k],ref[k])
    save("verification.json",{"passed":True,"training_scenarios":len(flat),"diagnostic_scenarios":len(outputs),
          "checks":["unchanged original and wider controls reproduce every checked scenario", "prefix causal signals", "exclusive positions, two-lot fees and equity checked by minute runner", "selection locked before later diagnostics"]})
    save("candidate_verification.json",verify_candidate(bars,groups,indices,mapping,cfg))


if __name__=="__main__":
    main()
