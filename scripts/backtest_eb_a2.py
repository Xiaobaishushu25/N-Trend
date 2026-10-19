"""Frozen EG A2 transfer to EB, with explicit contract value conversion.

Daily prices identify download coverage only; minute close/OI identify the
constituent at every signal timestamp. No PnL-dependent parameter selection.
"""
import argparse
import concurrent.futures
import copy
import csv
import hashlib
import io
import json
import statistics
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import discover_eg_swing as engine
import research_eg_factors as factors

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT/"reports"/"eb_a2_transfer_20261019"
START = "2025-12-16 09:00:00"
END = "2026-10-16 23:00:00"
WARMUP = "2025-11-17 00:00:00"


def save(name, value):
    (OUT/name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def fetch_minutes():
    probe = json.loads((OUT/"daily_member_probe.json").read_text(encoding="utf-8"))
    requests = []
    for sym, bounds in probe["members"].items():
        first = datetime.fromisoformat(bounds["first"]).replace(tzinfo=None)-timedelta(days=35)
        last = datetime.fromisoformat(bounds["last"]).replace(tzinfo=None)+timedelta(days=7)
        start = max(WARMUP, first.isoformat(sep=" "))
        end = min("2026-10-17 00:00:00", last.isoformat(sep=" "))
        requests.append((sym, start, end))

    def one(item):
        sym, start, end = item
        path = OUT/(sym+"_1m.csv")
        url = "https://edb.shinnytech.com/md/kline?"+urllib.parse.urlencode({"period":60,"symbol":"DCE."+sym,"start_time":start,"end_time":end})
        req = urllib.request.Request(url, headers={"User-Agent":"ntrend-independent-backtest/1.0"})
        with urllib.request.urlopen(req, timeout=45) as response:
            raw = response.read(); hd = response.headers.get("Date")
        rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
        assert rows and "datetime_nano" in rows[0]
        path.write_bytes(raw)
        print("FETCH", sym, len(rows), flush=True)
        return {"name":sym,"symbol":"DCE."+sym,"period":60,"url":url,"sha256":hashlib.sha256(raw).hexdigest(),
                "rows":len(rows),"http_date":hd,"retrieved_at":datetime.now(engine.extended.ZONE).isoformat()}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(one, requests))
    manifest = json.loads((OUT/"sources.json").read_text(encoding="utf-8"))
    manifest = [m for m in manifest if m["name"] not in probe["members"]]+results
    save("sources.json", manifest)


def prepare():
    sources = json.loads((OUT/"sources.json").read_text(encoding="utf-8"))
    names = ["main"]+[s["name"] for s in sources if s.get("period") == 60 and s["name"] != "main"]
    assert len(names) == len(set(names))
    before = engine.extended.OUT
    engine.extended.OUT = OUT
    try:
        minutes = {sym:engine.extended.load_minutes(sym) for sym in names}
    finally:
        engine.extended.OUT = before
    bars = {sym:engine.aggregate(ms, 15) for sym, ms in minutes.items()}
    for sym, bs in bars.items():
        if sym != "main": engine.enrich(bs)
    ix = {sym:{b["dt"]:i for i,b in enumerate(bs)} for sym,bs in bars.items() if sym != "main"}
    mapping = {}; missing = []
    for b in bars["main"]:
        dt = b["dt"]
        matches = [s for s,indices in ix.items() if dt in indices and all(bars[s][indices[dt]][k] == b[k] for k in ["c","p"])]
        if len(matches) == 1: mapping[dt] = matches[0]
        else: missing.append(b)
    fallback = []; positions = {b["dt"]:i for i,b in enumerate(bars["main"])}
    for b in missing:
        dt = b["dt"]; i = positions[dt]
        matches = [s for s,indices in ix.items() if dt in indices and all(bars[s][indices[dt]][k] == b[k] for k in ["h","l","c"])]
        neighbors = [mapping.get(bars["main"][j]["dt"]) for j in [i-1,i+1] if 0 <= j < len(bars["main"])]
        if len(matches) == 1 and len(neighbors) == 2 and all(x == matches[0] for x in neighbors):
            sym = matches[0]
            reason = "uniqueHLC and same member at both adjacent bars, prices unchanged"
            votes = None
        else:
            # At a roll boundary a supplier's last-minute snapshot may differ.
            # Identify by the other14 raw minute close/OI pairs, never alter an
            # execution price or carry an unresolved member into the backtest.
            votes = {s:sum(all(m[k] == n[k] for k in ["c","p"]) for m,n in zip(b["minutes"], bars[s][indices[dt]]["minutes"]))
                     for s,indices in ix.items() if dt in indices}
            strongest = [s for s,v in votes.items() if v >= 14]
            assert len(strongest) == 1 and strongest[0] in neighbors, (dt,matches,neighbors,votes)
            sym = strongest[0]
            assert all(v == 0 for s,v in votes.items() if s != sym), (dt,votes)
            reason = "14of15 raw minute close/OI pairs exact, alternatives0, adjacent member confirms; concreteprices unchanged"
        mapping[dt] = sym
        fallback.append({"dt":dt,"contract":sym,"reason":reason,"minute_votes":votes})
    boundaries = []; members = []
    for i, b in enumerate(bars["main"]):
        dt = datetime.fromisoformat(b["dt"])
        next_open = datetime.fromisoformat(bars["main"][i+1]["minutes"][0]["dt"])-timedelta(minutes=1) if i+1 < len(bars["main"]) else None
        b["calendar_flat"] = (next_open-dt).total_seconds() > 86400 if next_open else dt.weekday() == 4 and dt.hour == 23
        if b["calendar_flat"]: boundaries.append(b["dt"])
        sym = mapping[b["dt"]]
        if not members or sym != members[-1]["contract"]: members.append({"dt":b["dt"],"contract":sym})
    signals = {}; prefix_tests = 0
    spec = next(x for x in factors.specifications() if x["id"] == "season_volume_2")
    for sym, bs in bars.items():
        if sym == "main": continue
        sig = [engine.signal(bs,i,"channel") for i in range(len(bs))]
        fs = factors.features(bs,sig)
        signals[(sym,"base")] = sig
        signals[(sym,"a2")] = [d if d and factors.passes(f,spec) else 0 for d,f in zip(sig,fs)]
        for i in [len(bs)//3,len(bs)//2,len(bs)-2]:
            prefix = copy.deepcopy(bs[:i+1]); engine.enrich(prefix)
            ps = [engine.signal(prefix,j,"channel") for j in range(len(prefix))]
            assert factors.features(prefix,ps)[i] == fs[i]
            assert (ps[i] if ps[i] and factors.passes(fs[i],spec) else 0) == signals[(sym,"a2")][i]
            prefix_tests += 1
    save("data_verification.json", {"coverage":{s:{"minutes":len(ms),"first":ms[0]["dt"],"last":ms[-1]["dt"]} for s,ms in minutes.items()},
        "members":members,"fallbacks":fallback,"unresolved":0,"prefix_tests":prefix_tests,
        "calendar_boundaries":boundaries,"source_hashes_verified":True})
    for s in sources:
        if s.get("period") != 60: continue
        assert hashlib.sha256((OUT/(s["name"]+"_1m.csv")).read_bytes()).hexdigest() == s["sha256"]
    return bars,ix,mapping,signals


def main():
    parser = argparse.ArgumentParser(); parser.add_argument("--fetch", action="store_true"); args = parser.parse_args()
    if args.fetch: fetch_minutes()
    plan = json.loads((OUT/"initial_plan.json").read_text(encoding="utf-8"))
    cfgs = plan["configs"]
    primary = cfgs["risk_equivalent"]
    cfgs["base_risk_equivalent"] = {**primary,"name":"eb_base_risk_equivalent","family":"base"}
    save("frozen_execution_plan.json", {**plan,"configs":cfgs,"fees_primary":3,
         "fees_stress":[6,12],"fee_note":"fixed comparison assumptions, not historical broker receipt; publishedexchangeFee3to1from2026July9night",
         "additional_exchange_fee_audit":"not used for selection; schedules may be inspected separately",
         "risk_conversion":"EG12--46points x10 -> EB24--92points x5, monetary stop120--460 unchanged",
         "source_hashes":{s:hashlib.sha256((ROOT/"scripts"/s).read_bytes()).hexdigest() for s in ["backtest_eb_a2.py","discover_eg_swing.py","research_eg_factors.py"]}})
    bundle = prepare()
    windows = {"continuous":(START,END),"first_four":(START,"2026-04-15 23:59:59"),
               "middle_four":("2026-04-16 09:00:00","2026-08-15 23:59:59"),
               "recent_two":("2026-08-16 09:00:00",END)}
    rows = []; summaries = {}; monthly = []
    for profile,cfg in cfgs.items():
        for window, span in windows.items():
            for slip in [1,2,3]:
                for mode in ["ohlc","olhc"]:
                    s,ts,curve = engine.run(bundle,cfg,span,slip,mode); s.update(profile=profile,window=window,fee_per_side=3)
                    rows.append(s)
                    save(f"{window}_{profile}_slip{slip}_{mode}_trades.json",ts)
                    if mode == "ohlc":
                        with (OUT/f"{window}_{profile}_slip{slip}_equity.csv").open("w",encoding="utf-8-sig",newline="") as f:
                            w=csv.DictWriter(f,fieldnames=["dt","equity","qty"]);w.writeheader();w.writerows(curve)
                    if window == "continuous" and slip == 1 and mode == "ohlc":
                        summaries[profile] = {"max_margin_assumption":max((t["margin_estimate"] for t in ts),default=0),
                            "median_signal_atr":statistics.median(t["signal_atr"] for t in ts) if ts else None}
                        levels={};prev=0
                        for e in curve:levels[e["dt"][:7]]=e["equity"]
                        for month,level in levels.items():
                            monthly.append({"profile":profile,"month":month,"net_equity_change":level-prev,
                                            "trades":sum(t["entry_dt"].startswith(month) for t in ts)})
                            prev=level
                    print(profile,window,slip,mode,s["net_pnl"],"DD",s["minute_close_max_drawdown"],"n",s["trades"],flush=True)
    for fee in [6,12]:
        for slip in [1,2,3]:
            for mode in ["ohlc","olhc"]:
                cfg={**primary,"fee_per_side":fee}
                s,_,_=engine.run(bundle,cfg,windows["continuous"],slip,mode)
                s.update(profile="risk_equivalent",window="continuous",fee_per_side=fee);rows.append(s)
    bars,ix,mapping,_=bundle
    market=[]
    for b in bars["main"]:
        if START<=b["dt"]<=END:
            sym=mapping[b["dt"]];market.append(bars[sym][ix[sym][b["dt"]]])
    summaries["market"]={"median_main_atr20_points":statistics.median(b["atr"] for b in market),
        "median_main_atr20_one_lot_cny":statistics.median(b["atr"]*5 for b in market)}
    save("all_results.json",rows);save("monthly_equity_changes.json",monthly);save("summary.json",summaries)
    save("verification.json", {"passed":True,"scenarios":len(rows),"optimization_trials":0,
        "primary_profile":"risk_equivalent","other_profiles":"literal-point sensitivity and no-volume baseline; no winner selection",
        "checks":["rawhashes","unique mainminute-bar close/OI or disclosedHLCneighbors","prefixcausality",
                  "1lot native5CNYpoint","fee/risk/margin assertions","calendar flat","equity reconciliation"],
        "limitations":"fixedfee/margin assumptions, no live fill validation, EDBmain may differ SinaEB0"})


if __name__ == "__main__":
    main()
