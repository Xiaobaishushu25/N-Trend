#!/usr/bin/env python3
"""Small, disclosed exploratory comparison; NOT an untouched out-of-sample test.

Uses saved snapshots only. Does not modify production code or the original audit.
Run: python scripts/research_eg_candidates.py
"""
import copy
import csv
import hashlib
import json
import math
from contextlib import contextmanager
from pathlib import Path

import audit_eg_15m as audit

OUT = audit.ROOT / "reports" / "eg_candidates_20261019"
SPLIT = "2026-09-16 00:00:00"
CONFIGS = [
    {"name":"original", "signal":"warning", "exit":"ladder", "risk":"fixed", "distance":15, "tp":25, "protect":5, "step":15, "trend":False},
    {"name":"trend_filter", "signal":"warning", "exit":"ladder", "risk":"fixed", "distance":15, "tp":25, "protect":5, "step":15, "trend":True},
    {"name":"wider_exits", "signal":"warning", "exit":"ladder", "risk":"fixed", "distance":30, "tp":50, "protect":10, "step":30, "trend":False},
    {"name":"atr_exits", "signal":"warning", "exit":"ladder", "risk":"atr", "distance":1.0, "trend":False},
    {"name":"channel_trend", "signal":"channel", "exit":"close_trail", "risk":"atr", "distance":1.5, "trend":True},
]
MODES = ["ohlc", "olhc", "adverse_first", "favorable_first"]


def save(name, value):
    (OUT/name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def csv_save(name, rows):
    if not rows:
        return
    with (OUT/name).open("w", encoding="utf-8-sig", newline="") as f:
        w=csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)


def enrich(bars):
    audit.indicators(bars)
    for n in [20,60]:
        ema=None
        for b in bars:
            ema=b["c"] if ema is None else ema+2/(n+1)*(b["c"]-ema)
            b["ema"+str(n)] = ema


def make_signal(bars, i, cfg, symbol):
    b=bars[i]
    if b["atr20"] is None:
        return None
    if cfg["signal"] == "warning":
        sig=audit.signal(bars,i)
    else:
        if i < 60:
            return None
        hi=max(x["h"] for x in bars[i-20:i])
        lo=min(x["l"] for x in bars[i-20:i])
        d=1 if b["c"] > hi else -1 if b["c"] < lo else 0
        sig={"signal_dt":b["dt"],"direction":d,"trigger":b["c"],"kind":"20bar_close_breakout","market":True} if d else None
    if not sig:
        return None
    d=sig["direction"]
    if cfg["trend"]:
        if i < 60 or (b["ema20"]-b["ema60"])*d <= 0 or (b["ema60"]-bars[i-3]["ema60"])*d <= 0:
            return None
    r=math.ceil(cfg["distance"] * (b["atr20"] if cfg["risk"]=="atr" else 1))
    if cfg["exit"]=="ladder":
        tp=cfg.get("tp",2*r); protect=cfg.get("protect",max(1,r//3)); step=cfg.get("step",r)
    else:
        tp=protect=step=None
    return {**sig,"contract":symbol,"initial_distance":r,"target":tp,"protect":protect,"step":step,
            "exit_model":cfg["exit"],"signal_atr":b["atr20"],"reference_close":b["c"]}


def new_position(sig, price, dt, slip):
    return {**sig,"entry":price+sig["direction"]*slip,"entry_dt":dt,"qty":2,
            "stop":-float(sig["initial_distance"]),"peak":0.0,"legs":[],"gap_stop":False,
            "planned_initial_risk":20*sig["initial_distance"],
            "risk_weight":300/(20*sig["initial_distance"])}


def close_leg(p, price, qty, reason, dt, slip):
    fill=price if reason=="tp_limit" else price-p["direction"]*slip
    p["legs"].append({"dt":dt,"qty":qty,"price":fill,"reason":reason,
                      "pnl":round((fill-p["entry"])*p["direction"]*10*qty,8)})
    p["qty"]-=qty


def favorable(p, price, dt, slip):
    x=(price-p["entry"])*p["direction"]
    p["peak"]=max(p["peak"],x)
    if p["exit_model"] != "ladder":
        return  # Close-only trailing updates outside the intrabar simulation.
    if p["qty"]==2 and x >= p["target"]:
        close_leg(p,p["entry"]+p["direction"]*p["target"],1,"tp_limit",dt,slip)
        p["stop"]=p["protect"]
    if p["qty"]==1 and x >= p["target"]+p["step"]:
        p["stop"]=max(p["stop"], p["target"]+p["step"]*(math.floor((x-p["target"])/p["step"])-1))


@contextmanager
def execution_model():
    # The independent audit's verified path walker calls these module-level hooks.
    old={k:getattr(audit,k) for k in ["new_position","close_leg","favorable"]}
    audit.new_position=new_position; audit.close_leg=close_leg; audit.favorable=favorable
    try:
        yield
    finally:
        for k,v in old.items():
            setattr(audit,k,v)


def finish(p, fee):
    p=audit.finish(p,fee)
    p["risk_normalized_pnl"]=p["net_pnl"]*p["risk_weight"]
    return p


def summarize(trades, equity, normalized_equity):
    stats=audit.metrics(trades,equity)
    normalized=[{**t,"net_pnl":t["risk_normalized_pnl"]} for t in trades]
    stats["risk_normalized"]=audit.metrics(normalized,normalized_equity)
    stats["risk_range"]=[min(t["planned_initial_risk"] for t in trades),max(t["planned_initial_risk"] for t in trades)] if trades else []
    for name, pred in [("first",lambda t:t["entry_dt"]<SPLIT),("second",lambda t:t["entry_dt"]>=SPLIT)]:
        group=[t for t in trades if pred(t)]
        vals=[t["net_pnl"] for t in group]
        nvals=[t["risk_normalized_pnl"] for t in group]
        stats[name]={"trades":len(group),"net_pnl":sum(vals),"risk_normalized_pnl":sum(nvals)}
    return stats


def run(main, mapping, contracts, indices, cfg, slip, mode):
    p=pending=None
    trades=[]; curve=[]; total=ntotal=0.0; gap_skips=0; last=None
    for bmain in main:
        dt=bmain["dt"]
        if not audit.START <= dt <= audit.END:
            continue
        sym=(p or pending or {}).get("contract",mapping[dt])
        b=contracts[sym][indices[sym][dt]]
        last=b
        if pending and pending.get("market"):
            if abs(b["o"]-pending["reference_close"]) > .5*pending["signal_atr"]:
                pending=None;gap_skips+=1
            else:
                pending={**pending,"trigger":b["o"]}
        p,done=audit.simulate_bar(p,pending,b,mode,slip)
        pending=None
        if done:
            done=finish(done,3)
            trades.append(done);total+=done["net_pnl"];ntotal+=done["risk_normalized_pnl"]
        if p and p["exit_model"]=="close_trail":
            # ATR at current close is causal, and the changed stop activates NEXT bar.
            distance=math.ceil(2*b["atr20"])
            p["stop"]=max(p["stop"],(b["c"]-p["entry"])*p["direction"]-distance)
        if p:
            mark=sum(l["pnl"] for l in p["legs"])+(b["c"]-p["entry"])*p["direction"]*10*p["qty"]-(4-p["qty"])*3
            eq=total+mark;neq=ntotal+mark*p["risk_weight"]
        else:
            eq=total;neq=ntotal
            sym=mapping[dt]
            pending=make_signal(contracts[sym],indices[sym][dt],cfg,sym)
        curve.append({"dt":dt,"equity":eq,"normalized_equity":neq,"qty":p["qty"] if p else 0})
    if p:
        close_leg(p,last["c"],p["qty"],"terminal",last["dt"],slip)
        done=finish(p,3);trades.append(done)
        total+=done["net_pnl"];ntotal+=done["risk_normalized_pnl"]
        curve[-1].update(equity=total,normalized_equity=ntotal,qty=0)
    for t in trades:
        assert sum(l["qty"] for l in t["legs"])==2 and t["qty"]==0
        assert abs(sum(l["pnl"] for l in t["legs"])-12-t["net_pnl"]) < 1e-7
        assert t["signal_dt"] < t["entry_dt"] <= t["exit_dt"]
    for prev,nxt in zip(trades,trades[1:]):
        assert prev["exit_dt"] <= nxt["signal_dt"]
    assert abs(sum(t["net_pnl"] for t in trades)-curve[-1]["equity"]) < 1e-7
    assert abs(sum(t["risk_normalized_pnl"] for t in trades)-curve[-1]["normalized_equity"]) < 1e-7
    stats=summarize(trades,[q["equity"] for q in curve],[q["normalized_equity"] for q in curve])
    stats.update(name=cfg["name"],slip=slip,mode=mode,fee_per_lot_side=3,skipped_entry_gaps=gap_skips)
    return stats,trades,curve


def main():
    OUT.mkdir(parents=True,exist_ok=True)
    # Fixed before the first candidate computation; all candidates and stress scenarios are retained.
    plan={"candidates":CONFIGS,"slippage_ticks":[0,1,2],"modes":MODES,"fee_per_lot_side":3,
          "window":[audit.START,audit.END],"split":SPLIT,"selection_rule":"No parameter tuning or automatic best-model selection",
          "caveat":"Both periods were inspected in prior audit. This is exploratory, NOT independent validation.",
          "risk_normalization":"Each hypothetical trade scaled to 300 CNY initial stop risk. Fractional lots; diagnostic only, not executable sizing."}
    save("experiment_plan.json",plan)
    main_bars=audit.load("eg0_15m")
    mapping=json.loads((audit.OUT/"contract_mapping.json").read_text(encoding="utf-8"))["mapping"]
    contracts={sym:audit.load(sym+"_15m") for sym in sorted(set(mapping.values()))}
    for bars in contracts.values():
        enrich(bars)
    indices={s:{b["dt"]:i for i,b in enumerate(bs)} for s,bs in contracts.items()}
    results=[]
    with execution_model():
        for cfg in CONFIGS:
            for slip in [0,1,2]:
                for mode in MODES:
                    stats,trades,curve=run(main_bars,mapping,contracts,indices,cfg,slip,mode)
                    results.append(stats)
                    label=f"{cfg['name']}_slip{slip}_{mode}"
                    save(label+"_trades.json",trades)
                    csv_save(label+"_equity.csv",curve)
                    if slip==1 and mode=="adverse_first":
                        csv_save(label+"_trades.csv",[{**{k:v for k,v in t.items() if k!="legs"},"legs":json.dumps(t["legs"])} for t in trades])
                        print(json.dumps({"name":cfg["name"],"trades":stats["trades"],"net":stats["net_pnl"],"dd":stats["max_drawdown"],
                                          "first":stats["first"],"second":stats["second"],"risk_normalized":stats["risk_normalized"]["net_pnl"],
                                          "risk_range":stats["risk_range"]},ensure_ascii=False))
    save("all_results.json",results)
    csv_save("comparison.csv",[{k:s[k] for k in ["name","slip","mode","trades","win_rate_pct","net_pnl","profit_factor","max_drawdown"]}
        |{"first_pnl":s["first"]["net_pnl"],"second_pnl":s["second"]["net_pnl"],"normalized_pnl":s["risk_normalized"]["net_pnl"],
          "normalized_first":s["first"]["risk_normalized_pnl"],"normalized_second":s["second"]["risk_normalized_pnl"]} for s in results])
    # Independent engine extension must reproduce the previously saved baseline, before interpreting variants.
    expected=json.loads((audit.OUT/"summary.json").read_text(encoding="utf-8"))
    for s in results:
        if s["name"]=="original" and s["mode"]=="adverse_first" and s["slip"] in [0,1]:
            ref=expected["contracts_fee3_only" if s["slip"]==0 else "contracts_fee3_slip1"]
            for k in ["trades","wins","losses","net_pnl","max_drawdown","worst_trade"]:
                assert s[k]==ref[k],(k,s[k],ref[k])
    # Prefix causality of new EMA/channel features.
    for cfg in CONFIGS:
        for sym,bs in contracts.items():
            i=len(bs)//2
            prefix=copy.deepcopy(bs[:i+1]);enrich(prefix)
            assert make_signal(prefix,i,cfg,sym)==make_signal(bs,i,cfg,sym)
    save("verification.json",{"passed":True,"scenarios":len(results),"checks":["prior audit baseline equality", "indicator prefix causality", "exclusive positions", "two-lot accounting", "fees", "terminal equity", "risk-normalized equity"]})
    save("input_hashes.json",[{"file":p.name,"sha256":hashlib.sha256(p.read_bytes()).hexdigest()} for p in sorted(audit.OUT.glob("*.jsonp"))])


if __name__=="__main__":
    main()
