#!/usr/bin/env python3
"""Frozen candidate evaluation on older history, with public EDB minute snapshots.

python scripts/extend_eg_backtest.py --fetch
python scripts/extend_eg_backtest.py
No production changes. No parameter search. EDB bars have START timestamps.
"""
import argparse
import bisect
import copy
import csv
import hashlib
import io
import json
import math
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import audit_eg_15m as audit
import research_eg_candidates as research

OUT = audit.ROOT / "reports" / "eg_extended_20261019"
ZONE = timezone(timedelta(hours=8))
START = "2026-04-16 09:00:00"
SPLIT = "2026-08-16 09:00:00"
END = "2026-10-16 23:00:00"
REQUESTS = [
    ("main", "KQ.m@DCE.eg", "2026-03-16 00:00:00", "2026-10-17 00:00:00"),
    ("eg2605", "DCE.eg2605", "2026-03-16 00:00:00", "2026-05-10 00:00:00"),
    ("eg2609", "DCE.eg2609", "2026-03-16 00:00:00", "2026-09-25 00:00:00"),
    ("eg2610", "DCE.eg2610", "2026-07-15 00:00:00", "2026-10-17 00:00:00"),
    ("eg2611", "DCE.eg2611", "2026-08-15 00:00:00", "2026-10-17 00:00:00"),
]


def save(name, value):
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def csv_save(name, rows):
    if not rows:
        return
    with (OUT/name).open("w", encoding="utf-8-sig", newline="") as f:
        w=csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)


def fetch():
    manifest=[]
    for name,sym,start,end in REQUESTS:
        q={"period":60,"symbol":sym,"start_time":start,"end_time":end}
        url="https://edb.shinnytech.com/md/kline?"+urllib.parse.urlencode(q)
        meta={"name":name,"symbol":sym,"url":url,"retrieved_at":datetime.now(ZONE).isoformat()}
        req=urllib.request.Request(url,headers={"User-Agent":"ntrend-independent-backtest/1.0"})
        try:
            with urllib.request.urlopen(req,timeout=45) as response:
                raw=response.read();meta["http_date"]=response.headers.get("Date")
            (OUT/(name+"_1m.csv")).write_bytes(raw)
            meta.update(bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest(),lines=raw.count(b"\n"))
        except Exception as exc:
            meta["error"]=str(exc)
        manifest.append(meta)
        save("edb_sources.json",manifest)
        print(json.dumps(meta,ensure_ascii=False),flush=True)


def load_minutes(name):
    path=OUT/(name+"_1m.csv")
    rows=[]
    with path.open(encoding="utf-8-sig",newline="") as f:
        for r in csv.DictReader(f):
            nano=int(r["datetime_nano"])
            # Exact integer timestamp conversion: EDB start -> minute end label.
            dt=datetime.fromtimestamp(nano//1_000_000_000,ZONE).replace(tzinfo=None)+timedelta(minutes=1)
            b={"dt":dt.isoformat(sep=" "),**{short:float(r[long]) for short,long in [
                ("o","open"),("h","high"),("l","low"),("c","close"),("v","volume"),("p","close_oi")]}}
            if not all(math.isfinite(b[k]) for k in ["o","h","l","c","v","p"]):
                raise ValueError((name,r,"Nonfinite data"))
            assert b["l"]<=min(b["o"],b["c"])<=max(b["o"],b["c"])<=b["h"]
            assert all(b[k]==round(b[k]) for k in ["o","h","l","c"])
            assert b["v"]>=0
            rows.append(b)
    assert rows and rows==sorted(rows,key=lambda b:b["dt"])
    assert len({b["dt"] for b in rows})==len(rows)
    return rows


def aggregate(minutes):
    groups=defaultdict(list)
    for b in minutes:
        dt=datetime.fromisoformat(b["dt"])
        parent=dt.replace(minute=dt.minute//15*15)
        if dt.minute%15:
            parent+=timedelta(minutes=15)
        groups[parent.isoformat(sep=" ")].append(b)
    bars=[]
    incomplete=[]
    for dt,sub in sorted(groups.items()):
        if len(sub)!=15:
            incomplete.append({"dt":dt,"minutes":len(sub)})
            continue
        assert all((datetime.fromisoformat(z["dt"])-datetime.fromisoformat(a["dt"])).total_seconds()==60 for a,z in zip(sub,sub[1:]))
        bars.append({"dt":dt,"o":sub[0]["o"],"h":max(b["h"] for b in sub),"l":min(b["l"] for b in sub),
                     "c":sub[-1]["c"],"v":sum(b["v"] for b in sub),"p":sub[-1]["p"]})
    return bars,groups,incomplete


def prepare():
    data={name:load_minutes(name) for name,*_ in REQUESTS}
    bars={};groups={};incomplete={}
    for name,mins in data.items():
        bars[name],groups[name],incomplete[name]=aggregate(mins)
        if name!="main":
            research.enrich(bars[name])
    indices={s:{b["dt"]:i for i,b in enumerate(bs)} for s,bs in bars.items() if s!="main"}
    mapping={};unmatched=[];changes=[]
    for b in bars["main"]:
        if b["dt"]<START:
            continue
        matches=[s for s,ix in indices.items() if b["dt"] in ix
                 and all(bars[s][ix[b["dt"]]][k]==b[k] for k in ["c","p"])]
        if len(matches)!=1:
            unmatched.append({"dt":b["dt"],"matches":matches,"main":b})
            continue
        mapping[b["dt"]]=matches[0]
        if not changes or changes[-1]["contract"]!=matches[0]:
            changes.append({"dt":b["dt"],"contract":matches[0]})
    fallbacks=[]
    ordered=[b for b in bars["main"] if b["dt"]>=START]
    positions={b["dt"]:i for i,b in enumerate(ordered)}
    unresolved=[]
    for miss in unmatched:
        b=miss["main"];i=positions[b["dt"]]
        # One vendor OI discrepancy can be identified by unique HLC equality AND
        # matching constituent on both neighboring bars. Never alter the prices.
        matches=[s for s,ix in indices.items() if b["dt"] in ix
                 and all(bars[s][ix[b["dt"]]][k]==b[k] for k in ["h","l","c"])]
        neighbors=[mapping.get(ordered[j]["dt"]) for j in [i-1,i+1] if 0<=j<len(ordered)]
        if len(matches)==1 and len(neighbors)==2 and all(s==matches[0] for s in neighbors):
            mapping[b["dt"]]=matches[0]
            fallbacks.append({**miss,"contract":matches[0],"criterion":"unique HLC match and same constituent on both adjacent bars; signals and fills use unmodified contract data"})
        else:
            unresolved.append(miss)
    unmatched=unresolved
    save("mapping_diagnostics.json",{"changes":changes,"unmatched":unmatched,"fallback_identifications":fallbacks,"incomplete_groups":incomplete})
    assert not unmatched,("Missing constituent contracts",unmatched[:5])
    coverage={}
    for name,bs in bars.items():
        coverage[name]={"minute_rows":len(data[name]),"bars15":len(bs),"first":bs[0]["dt"],"last":bs[-1]["dt"],"incomplete":len(incomplete[name])}
    save("data_coverage.json",coverage)
    csv_save("main_15m.csv",bars["main"])
    save("main_contract_mapping.json",mapping)
    return bars,groups,indices,mapping


def run(bars, groups, indices, mapping, cfg, slip, mode, start, end, minute_execution=True):
    p=pending=None
    # Optional user-requested session liquidation; absent keeps prior runs exact.
    flat_times=set(cfg.get("flat_times",[]))
    trades=[];curve=[];total=ntotal=0.0;last=None;gap_skips=0;rollover_cancels=0
    main=[b for b in bars["main"] if start<=b["dt"]<=end]
    for mainbar in main:
        dt=mainbar["dt"]
        if pending and cfg.get("require_current_main",False) and pending["contract"]!=mapping[dt]:
            pending=None;rollover_cancels+=1
        sym=(p or pending or {}).get("contract",mapping[dt])
        if dt not in indices[sym]:
            raise ValueError((sym,dt,"Held contract bar missing"))
        b=bars[sym][indices[sym][dt]];last=b
        if pending and pending.get("market"):
            if abs(b["o"]-pending["reference_close"])>.5*pending["signal_atr"]:
                pending=None;gap_skips+=1
            else:
                pending={**pending,"trigger":b["o"]}
        active=pending
        execution=groups[sym][dt] if minute_execution else [b]
        for sub in execution:
            prev=p
            p,done=audit.simulate_bar(p,active,sub,mode,slip)
            if prev or p or done:
                active=None
            if done:
                done=research.finish(done,3)
                trades.append(done);total+=done["net_pnl"];ntotal+=done["risk_normalized_pnl"]
                break
        pending=None
        if p and dt[11:19] in flat_times:
            research.close_leg(p,b["c"],p["qty"],"session_flat",dt,slip)
            done=research.finish(p,3)
            trades.append(done);total+=done["net_pnl"];ntotal+=done["risk_normalized_pnl"]
            p=None
        if p and p["exit_model"]=="close_trail":
            p["stop"]=max(p["stop"],(b["c"]-p["entry"])*p["direction"]-math.ceil(2*b["atr20"]))
        if p:
            mark=sum(l["pnl"] for l in p["legs"])+(b["c"]-p["entry"])*p["direction"]*10*p["qty"]-(4-p["qty"])*3
            eq=total+mark;neq=ntotal+mark*p["risk_weight"]
        else:
            eq=total;neq=ntotal
            sym=mapping[dt]
            pending=research.make_signal(bars[sym],indices[sym][dt],cfg,sym)
            if dt[11:19] in flat_times and cfg.get("cancel_signals_at_flat_time",False):
                pending=None
        curve.append({"dt":dt,"equity":eq,"normalized_equity":neq,"qty":p["qty"] if p else 0})
    if p:
        research.close_leg(p,last["c"],p["qty"],"terminal",last["dt"],slip)
        done=research.finish(p,3);trades.append(done);total+=done["net_pnl"];ntotal+=done["risk_normalized_pnl"]
        curve[-1].update(equity=total,normalized_equity=ntotal,qty=0)
    assert curve
    for t in trades:
        assert t["signal_dt"]<t["entry_dt"]<=t["exit_dt"]
        assert t["qty"]==0 and sum(l["qty"] for l in t["legs"])==2
        assert t["net_pnl"]==sum(l["pnl"] for l in t["legs"])-12
    for a,z in zip(trades,trades[1:]):
        assert a["exit_dt"]<=z["signal_dt"]
    assert abs(sum(t["net_pnl"] for t in trades)-curve[-1]["equity"])<1e-7
    assert abs(sum(t["risk_normalized_pnl"] for t in trades)-curve[-1]["normalized_equity"])<1e-7
    stats=audit.metrics(trades,[e["equity"] for e in curve])
    stats["risk_normalized"]=audit.metrics([{**t,"net_pnl":t["risk_normalized_pnl"]} for t in trades],[e["normalized_equity"] for e in curve])
    stats.update(name=cfg["name"],slip=slip,mode=mode,start=start,end=end,bars15=len(main),execution="1m" if minute_execution else "15m",gap_skips=gap_skips,rollover_entry_cancellations=rollover_cancels)
    monthly=[]
    for month in sorted({e["dt"][:7] for e in curve}):
        ts=[t for t in trades if t["entry_dt"].startswith(month)]
        monthly.append({"month":month,"trades":len(ts),"net_pnl":sum(t["net_pnl"] for t in ts),"normalized_pnl":sum(t["risk_normalized_pnl"] for t in ts)})
    stats["monthly"]=monthly
    assert abs(sum(m["net_pnl"] for m in monthly)-stats["net_pnl"])<1e-7
    assert abs(sum(m["normalized_pnl"] for m in monthly)-stats["risk_normalized"]["net_pnl"])<1e-7
    return stats,trades,curve


def verify_engine_equivalence(bars,groups,indices,mapping):
    # Same EDB inputs, same 15m path: extended engine must reproduce prior runner.
    previous_start,previous_end=audit.START,audit.END
    try:
        audit.START,audit.END=SPLIT,END
        for cfg in [research.CONFIGS[0],research.CONFIGS[2]]:
            one,one_trades,_=run(bars,groups,indices,mapping,cfg,1,"adverse_first",SPLIT,END,False)
            two,two_trades,_=research.run(bars["main"],mapping,{s:bs for s,bs in bars.items() if s!="main"},indices,cfg,1,"adverse_first")
            for key in ["trades","wins","losses","net_pnl","max_drawdown","worst_trade"]:
                assert one[key]==two[key],(cfg["name"],key,one[key],two[key])
            assert [(t["signal_dt"],t["entry_dt"],t["exit_dt"],t["net_pnl"]) for t in one_trades]==[
                (t["signal_dt"],t["entry_dt"],t["exit_dt"],t["net_pnl"]) for t in two_trades]
    finally:
        audit.START,audit.END=previous_start,previous_end


def main():
    parser=argparse.ArgumentParser();parser.add_argument("--fetch",action="store_true");parser.add_argument("--prepare-only",action="store_true")
    args=parser.parse_args();OUT.mkdir(parents=True,exist_ok=True)
    save("frozen_plan.json",{"configs":research.CONFIGS,"windows":{"older_four":[START,"2026-08-15 23:59:59"],"recent_two":[SPLIT,END],"six_months":[START,END]},
          "slippage":[0,1,2],"modes":research.MODES,"fee_per_lot_side":3,"main_source":"EDB KQ.m@DCE.eg", "execution":"full 1-minute chronological replay; OHLC path assumptions remain WITHIN each minute",
          "selection":"No tuning. All previously declared candidates retained. Older four months not previously performance-tested.",
          "aggregation":"EDB start timestamps to minute end; require exactly 15 consecutive minutes per 15m signal bar"})
    if args.fetch:
        fetch()
    bars,groups,indices,mapping=prepare()
    print("COVERAGE",json.dumps(json.loads((OUT/"data_coverage.json").read_text(encoding="utf-8")),ensure_ascii=False),flush=True)
    if args.prepare_only:
        return
    windows={"older_four":(START,"2026-08-15 23:59:59"),"recent_two":(SPLIT,END),"six_months":(START,END)}
    results=[]
    with research.execution_model():
        verify_engine_equivalence(bars,groups,indices,mapping)
        for window,(start,end) in windows.items():
            for cfg in research.CONFIGS:
                for slip in [0,1,2]:
                    for mode in research.MODES:
                        stats,trades,curve=run(bars,groups,indices,mapping,cfg,slip,mode,start,end)
                        stats["window"]=window;results.append(stats)
                        label=f"{window}_{cfg['name']}_slip{slip}_{mode}"
                        save(label+"_trades.json",trades)
                        if slip==1 and mode=="adverse_first":
                            csv_save(label+"_equity.csv",curve)
                            csv_save(label+"_trades.csv",[{**{k:v for k,v in t.items() if k!="legs"},"legs":json.dumps(t["legs"])} for t in trades])
                            print(window,cfg["name"],stats["trades"],stats["net_pnl"],round(stats["risk_normalized"]["net_pnl"],2),"DD",stats["max_drawdown"],flush=True)
        # Recent source/granularity bridge: same rules, but execution on 15m OHLC.
        for cfg in research.CONFIGS:
            stats,trades,_=run(bars,groups,indices,mapping,cfg,1,"adverse_first",SPLIT,END,False)
            stats["window"]="recent_two_15m_execution";results.append(stats)
            save("recent_two_15m_"+cfg["name"]+"_trades.json",trades)
    save("all_results.json",results)
    csv_save("comparison.csv",[{k:s[k] for k in ["window","name","execution","slip","mode","trades","wins","losses","net_pnl","profit_factor","max_drawdown"]}
             |{"normalized_pnl":s["risk_normalized"]["net_pnl"],"normalized_dd":s["risk_normalized"]["max_drawdown"]} for s in results])
    save("verification.json",{"passed":True,"scenarios":len(results),"checks":["no NaN, duplicates or disordered minute records", "integer ticks and valid OHLC", "15 consecutive minutes per signal bar", "main constituent identification with one disclosed HLC/neighbor fallback", "held contract exists", "close-only causal signals", "exclusive position", "two-lot accounting", "fees", "equity reconciliation", "monthly sums", "same-input 15m engine equivalence against prior runner"]})


if __name__=="__main__":
    main()
