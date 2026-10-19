#!/usr/bin/env python3
"""Independent, stdlib-only audit of STRATEGY_EG_15M.md (no production imports).

Fetch once: python scripts/audit_eg_15m.py --fetch
Reproduce:  python scripts/audit_eg_15m.py
Timestamps are provider bar CLOSE labels in Asia/Shanghai. No interpolation.
"""
import argparse
import copy
import csv
import hashlib
import json
import math
import sqlite3
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "reports" / "eg_15m_audit_20261019"
API = "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/=/InnerFuturesNewService."
START = "2026-08-16 09:00:00"
END = "2026-10-16 23:00:00"


def dump(name, obj):
    (OUT / name).write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def write_csv(name, rows):
    if not rows:
        return
    with (OUT / name).open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def decode(raw):
    s = raw.decode("utf-8-sig")
    return json.loads(s[s.index("["):s.rindex("]") + 1])


def fetch():
    manifest = []
    for name, method, query in [
        ("eg0_15m", "getFewMinLine", "symbol=EG0&type=15"),
        ("eg0_5m", "getFewMinLine", "symbol=EG0&type=5"),
        ("eg0_1m", "getFewMinLine", "symbol=EG0&type=1"),
        ("eg0_daily", "getDailyKLine", "symbol=EG0"),
        ("eg2609_15m", "getFewMinLine", "symbol=EG2609&type=15"),
        ("eg2610_15m", "getFewMinLine", "symbol=EG2610&type=15"),
        ("eg2611_15m", "getFewMinLine", "symbol=EG2611&type=15"),
        ("eg2701_15m", "getFewMinLine", "symbol=EG2701&type=15"),
    ]:
        url = API + method + "?" + query
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"})
        meta = {"name": name, "url": url, "retrieved_at": datetime.now(timezone(timedelta(hours=8))).isoformat()}
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                raw = response.read()
                meta["http_date"] = response.headers.get("Date")
            (OUT / (name + ".jsonp")).write_bytes(raw)
            rows = decode(raw)
            meta.update(sha256=hashlib.sha256(raw).hexdigest(), count=len(rows), first=rows[0] if rows else None, last=rows[-1] if rows else None)
        except Exception as e:
            meta["error"] = str(e)
        manifest.append(meta)
        print(json.dumps(meta, ensure_ascii=False))
    dump("sources.json", manifest)


def load(name):
    p = OUT / (name + ".jsonp")
    if not p.exists():
        return []
    rows = decode(p.read_bytes())
    result = []
    for r in rows:
        if isinstance(r, list):
            r = dict(zip(["d", "o", "h", "l", "c", "v", "p"], r))
        result.append({"dt": r["d"], **{k: float(r[k]) for k in ["o", "h", "l", "c", "v", "p"]}})
    assert len({b["dt"] for b in result}) == len(result), "Duplicate timestamps"
    assert result == sorted(result, key=lambda b: b["dt"]), "Not chronological"
    for b in result:
        assert all(math.isfinite(b[k]) for k in ["o", "h", "l", "c", "v", "p"])
        assert b["l"] <= min(b["o"], b["c"]) <= max(b["o"], b["c"]) <= b["h"]
        assert b["v"] >= 0 and all(b[k] == round(b[k]) for k in ["o", "h", "l", "c"])
    return result


def indicators(bars):
    tr = []
    for i, b in enumerate(bars):
        pc = bars[i-1]["c"] if i else b["o"]
        tr.append(max(b["h"]-b["l"], abs(b["h"]-pc), abs(b["l"]-pc)))
        for n in [14, 20]:
            b["atr" + str(n)] = sum(tr[-n:])/n if len(tr) >= n else None


def signal(bars, i, filters=True):
    b = bars[i]
    if b["atr20"] is None or i < 2:
        return None
    o, h, l, c = [b[k] for k in ["o", "h", "l", "c"]]
    r, body = h-l, abs(c-o)
    if r <= 0 or body == 0:
        return None
    d = 1 if c > o else -1
    upper, lower = h-max(o, c), min(o, c)-l
    trend = (body/r >= .75 and (c-l if d == 1 else h-c)/r >= .8
             and r >= .8*b["atr20"] and (upper if d == 1 else lower) <= body)
    pin = (body/r >= .25 and (upper if d == 1 else lower) <= .1*r
           and (lower if d == 1 else upper) >= 1.5*body
           and (lower if d == 1 else upper) >= .4*r
           and (lower if d == 1 else upper) >= .5*b["atr14"])
    if not (trend or pin):
        return None
    if filters and (r > 2*b["atr20"] or all((bars[j]["c"]-bars[j]["o"])*d > 0 for j in [i-2, i-1])):
        return None
    return {"signal_dt": b["dt"], "direction": d, "trigger": c+d, "kind": "trend" if trend else "pinbar"}


def new_position(sig, price, dt, slip):
    return {**sig, "entry": price+sig["direction"]*slip, "entry_dt": dt,
            "qty": 2, "stop": -15.0, "peak": 0.0, "legs": [], "gap_stop": False}


def close_leg(p, price, qty, reason, dt, slip):
    # Profit-taking is a resting limit; stop/terminal exits receive adverse slippage.
    fill = price - p["direction"]*slip if reason != "tp25" else price
    p["legs"].append({"dt": dt, "qty": qty, "price": fill, "reason": reason,
                      "pnl": round((fill-p["entry"])*p["direction"]*10*qty, 8)})
    p["qty"] -= qty


def favorable(p, price, dt, slip):
    x = (price-p["entry"])*p["direction"]
    p["peak"] = max(p["peak"], x)
    if p["qty"] == 2 and x >= 25:
        close_leg(p, p["entry"]+p["direction"]*25, 1, "tp25", dt, slip)
        p["stop"] = 5.0
    if p["qty"] == 1 and x >= 40:
        p["stop"] = max(p["stop"], 25+15*(math.floor((x-25)/15)-1))


def open_gap(p, price, dt, slip):
    if (price-p["entry"])*p["direction"] <= p["stop"]:
        p["gap_stop"] = (price-p["entry"])*p["direction"] < p["stop"]
        close_leg(p, price, p["qty"], "gap_stop" if p["gap_stop"] else "stop", dt, slip)
    else:
        favorable(p, price, dt, slip)


def segment(p, a, z, dt, slip):
    if (z-a)*p["direction"] >= 0:
        favorable(p, z, dt, slip)
    elif (z-p["entry"])*p["direction"] <= p["stop"]:
        close_leg(p, p["entry"]+p["direction"]*p["stop"], p["qty"], "stop", dt, slip)


def finish(p, fee):
    p["gross_pnl"] = round(sum(x["pnl"] for x in p["legs"]), 8)
    p["fees"] = 4*fee  # Two lots in, two lots out.
    p["net_pnl"] = p["gross_pnl"]-p["fees"]
    p["exit_dt"] = p["legs"][-1]["dt"]
    return p


def paths(bar, mode, d):
    if mode == "ohlc" or (mode == "adverse_first" and d == -1) or (mode == "favorable_first" and d == 1):
        return [bar[k] for k in ["o", "h", "l", "c"]]
    return [bar[k] for k in ["o", "l", "h", "c"]]


def simulate_bar(pos, pending, b, mode, slip):
    p = copy.deepcopy(pos)
    armed = copy.deepcopy(pending)
    finished = None
    d = p["direction"] if p else armed["direction"] if armed else 1
    nodes = paths(b, mode, d)
    if p:
        open_gap(p, nodes[0], b["dt"], slip)
    elif armed and (nodes[0]-armed["trigger"])*d >= 0:
        p = new_position(armed, nodes[0], b["dt"], slip)
        armed = None
    if p and p["qty"] == 0:
        finished, p = p, None
    for a, z in zip(nodes, nodes[1:]):
        if finished:
            break
        if p is None and armed and (a-armed["trigger"])*d <= 0 and (z-armed["trigger"])*d >= 0:
            p = new_position(armed, armed["trigger"], b["dt"], slip)
            armed = None
            a = p["entry"]-d*slip  # Actual market crossing level, before execution slippage.
        if p:
            segment(p, a, z, b["dt"], slip)
            if p["qty"] == 0:
                finished, p = p, None
    return p, finished


def metrics(trades, equity):
    vals = [p["net_pnl"] for p in trades]
    wins, losses = [v for v in vals if v > 0], [v for v in vals if v < 0]
    streak = maxstreak = 0
    for v in vals:
        streak = streak+1 if v < 0 else 0
        maxstreak = max(streak, maxstreak)
    high = dd = 0.0
    for e in equity:
        high = max(high, e)
        dd = max(dd, high-e)
    return {"trades": len(vals), "wins": len(wins), "losses": len(losses), "flat": vals.count(0),
            "win_rate_pct": round(100*len(wins)/len(vals), 4) if vals else 0,
            "gross_profit": sum(wins), "gross_loss": sum(losses), "net_pnl": sum(vals),
            "profit_factor": round(sum(wins)/-sum(losses), 6) if losses else None,
            "max_consecutive_losses": maxstreak, "max_drawdown": dd,
            "long_pnl": sum(p["net_pnl"] for p in trades if p["direction"] == 1),
            "short_pnl": sum(p["net_pnl"] for p in trades if p["direction"] == -1),
            "worst_trade": min(vals) if vals else 0,
            "gap_stop_trades": sum(p["gap_stop"] for p in trades),
            "terminal_liquidations": sum(any(l["reason"] == "terminal" for l in p["legs"]) for p in trades)}


def backtest(bars, mode, start=START, end=END, filters=True, fee=0, slip=0, lower=None, contract_data=None):
    p = pending = None
    trades, equity, ambiguity = [], [], []
    realized = 0.0
    selected = [(i,b) for i,b in enumerate(bars) if start <= b["dt"] <= end]
    lower_map = defaultdict(list)
    if lower:
        for b in lower:
            dt = datetime.fromisoformat(b["dt"])
            # End-labelled 1/5-minute bars -> parent 15-minute end label.
            parent = dt.replace(minute=(dt.minute//15)*15, second=0)
            if dt.minute % 15:
                parent += timedelta(minutes=15)
            lower_map[parent.isoformat(sep=" ")].append(b)
    refined = 0
    last_close = None
    for i, b in selected:
        main_dt = b["dt"]
        if contract_data:
            mapping, contract_bars, contract_indices = contract_data
            sym = (p or pending or {}).get("contract", mapping[main_dt])
            b = contract_bars[sym][contract_indices[sym][main_dt]]
        last_close = b["c"]
        # Pending orders expire after exactly this bar, even across a session break.
        sub = lower_map.get(b["dt"], [])
        expected = 15 if lower and len(lower) > 1 and (datetime.fromisoformat(lower[1]["dt"])-datetime.fromisoformat(lower[0]["dt"])).total_seconds() == 60 else 3
        if len(sub) == expected and all(abs(x-y)<1e-9 for x,y in zip(
            [sub[0]["o"],max(q["h"] for q in sub),min(q["l"] for q in sub),sub[-1]["c"]],
            [b[k] for k in ["o","h","l","c"]])):
            execution = sub
            refined += 1
        else:
            execution = [b]
        if p or pending:
            one = simulate_bar(p, pending, b, "ohlc", slip)
            two = simulate_bar(p, pending, b, "olhc", slip)
            if one != two:
                ambiguity.append(b["dt"])
        active_pending = pending
        for subbar in execution:
            old_p = p
            p, done = simulate_bar(p, active_pending, subbar, mode, slip)
            if old_p or p or done:
                active_pending = None
            if done:
                trades.append(finish(done, fee))
                realized += done["net_pnl"]
                # Once this warning has filled, it cannot re-enter later in this parent bar.
                break
        pending = None
        if p:
            # Mark at bar close; include actual paid opening fees and realized lot exit fees.
            mark = sum(x["pnl"] for x in p["legs"])+(b["c"]-p["entry"])*p["direction"]*10*p["qty"]
            accrued_fees = (2 + 2-p["qty"])*fee
            eq = realized+mark-accrued_fees
        else:
            eq = realized
            if contract_data:
                sym = mapping[main_dt]
                pending = signal(contract_bars[sym], contract_indices[sym][main_dt], filters)
                if pending:
                    pending["contract"] = sym
            else:
                pending = signal(bars, i, filters)
        equity.append({"dt": b["dt"], "equity": eq, "realized": realized, "qty": p["qty"] if p else 0})
    if p:
        close_leg(p, last_close, p["qty"], "terminal", selected[-1][1]["dt"], slip)
        trades.append(finish(p, fee))
        equity[-1]["equity"] = realized+p["net_pnl"]
        equity[-1]["realized"] = realized+p["net_pnl"]
        equity[-1]["qty"] = 0
    stats = metrics(trades, [e["equity"] for e in equity])
    stats.update(mode=mode, start=start, end=end, bars=len(selected), fee_per_lot_side=fee,
                 slippage_ticks=slip, filters=filters, path_sensitive_bars=len(ambiguity), refined_bars=refined)
    stats["contract_execution"] = bool(contract_data)
    stats["periods"] = {}
    for name, lo, hi in [("month1", START, "2026-09-16 00:00:00"), ("month2", "2026-09-16 00:00:00", "2026-10-17 00:00:00")]:
        ts = [t for t in trades if lo <= t["entry_dt"] < hi]
        curve, total = [], 0
        for t in ts:
            total += t["net_pnl"]
            curve.append(total)
        stats["periods"][name] = metrics(ts, curve)
    return stats, trades, equity, ambiguity


def build_contract_data(bars):
    contract_bars = {sym: load(sym+"_15m") for sym in ["eg2609", "eg2610", "eg2611"]}
    indices = {sym: {b["dt"]:i for i,b in enumerate(bs)} for sym,bs in contract_bars.items()}
    for bs in contract_bars.values():
        indicators(bs)
    mapping = {}
    changes = []
    for b in bars:
        matches = [sym for sym,bs in contract_bars.items() if b["dt"] in indices[sym]
                   and all(bs[indices[sym][b["dt"]]][k] == b[k] for k in ["c", "p"])]
        assert len(matches) == 1, (b["dt"], matches)
        mapping[b["dt"]] = matches[0]
        if not changes or changes[-1]["contract"] != matches[0]:
            changes.append({"dt":b["dt"], "contract":matches[0]})
    dump("contract_mapping.json", {"criterion":"unique exact close AND open-interest match", "changes":changes,
                                   "rows":dict(Counter(mapping.values())), "mapping":mapping})
    return mapping, contract_bars, indices


def audit_aggregation(bars, freq):
    groups = defaultdict(list)
    for b in load("eg0_"+freq):
        dt = datetime.fromisoformat(b["dt"])
        parent = dt.replace(minute=dt.minute//15*15)
        if dt.minute % 15:
            parent += timedelta(minutes=15)
        groups[parent.isoformat(sep=" ")].append(b)
    full, mismatch = [], []
    for b in bars:
        if not START <= b["dt"] <= END:
            continue
        sub = groups.get(b["dt"], [])
        if len(sub) != (3 if freq == "5m" else 15):
            continue
        full.append(b["dt"])
        agg = [sub[0]["o"], max(q["h"] for q in sub), min(q["l"] for q in sub), sub[-1]["c"]]
        direct = [b[k] for k in ["o", "h", "l", "c"]]
        if agg != direct:
            mismatch.append({"dt":b["dt"], "direct":direct, "aggregated":agg})
    dump("aggregation_"+freq+"_audit.json", {"full_parents":len(full), "mismatches":mismatch})


def audit_external_source():
    # Optional second-provider probe. Never silently replaces the primary series.
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=114.egm&klt=15&fqt=0&beg=20260801&end=20261019&lmt=10000&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
    p = OUT / "eastmoney_15m.jsonp"
    meta = {"url":url, "used_in_backtest":False}
    if not p.exists():
        dump("external_source_audit.json",meta)
        return
    raw = p.read_bytes()
    rows = json.loads(raw)["data"]["klines"]
    vals = [float(r.split(",")[j]) for r in rows for j in [1,2,3,4]]
    meta.update(sha256=hashlib.sha256(raw).hexdigest(), rows=len(rows), first=rows[0], last=rows[-1],
                min_price=min(vals), max_price=max(vals),
                reason="Incomplete requested window and anomalous scale in returned historical prices; rejected, no repair guessed.")
    dump("external_source_audit.json",meta)


def audit_data(bars):
    sample = [b for b in bars if START <= b["dt"] <= END]
    counts = Counter(b["dt"][:10] for b in sample)
    gaps = []
    for a,b in zip(sample,sample[1:]):
        delta = b["o"]-a["c"]
        if abs(delta) >= 50:
            gaps.append({"previous_dt":a["dt"], "dt":b["dt"], "previous_close":a["c"], "open":b["o"], "gap":delta})
    info = {"source_rows":len(bars), "source_first":bars[0]["dt"], "source_last":bars[-1]["dt"],
            "window_rows":len(sample), "window_first":sample[0], "window_last":sample[-1],
            "warmup_rows":sum(b["dt"] < START for b in bars),
            "month1_bars":sum(b["dt"] < "2026-09-16" for b in sample),
            "month2_bars":sum(b["dt"] >= "2026-09-16" for b in sample),
            "min_low":min(b["l"] for b in sample), "max_high":max(b["h"] for b in sample),
            "calendar_day_counts":dict(counts), "large_opening_gaps":gaps}
    slots = ["09:15","09:30","09:45","10:00","10:15","10:45","11:00","11:15","11:30",
             "13:45","14:00","14:15","14:30","14:45","15:00",
             "21:15","21:30","21:45","22:00","22:15","22:30","22:45","23:00"]
    info["unusual_day_slots"] = {d:sorted(set(slots)-{b["dt"][11:16] for b in sample if b["dt"].startswith(d)})
                                 for d,n in counts.items() if n != 23}
    # Read-only local DB cross-check, never used as the primary backtest input.
    db = ROOT / "data" / "ntrend.db"
    if db.exists():
        con = sqlite3.connect(db.as_uri()+"?mode=ro",uri=True)
        cached = {r[0]:r[1:] for r in con.execute("select ts,open,high,low,close from klines where symbol='EG0' and timeframe='15m'")}
        con.close()
        overlap = [b for b in sample if b["dt"] in cached]
        diff = [{"dt":b["dt"], "public":[b[k] for k in ["o","h","l","c"]], "local":cached[b["dt"]]} for b in overlap if tuple(b[k] for k in ["o","h","l","c"]) != cached[b["dt"]]]
        info["local_db"] = {"cached_rows":len(cached), "first":min(cached) if cached else None, "last":max(cached) if cached else None,
                            "overlap_rows":len(overlap), "ohlc_mismatch_count":len(diff), "mismatches":diff}
    dump("data_audit.json",info)
    write_csv("daily_bar_counts.csv", [{"date":d,"bars":n} for d,n in counts.items()])
    return info


def self_checks():
    sig = {"signal_dt":"test", "direction":1, "trigger":100, "kind":"trend"}
    p = new_position(sig, 100, "test", 0)
    favorable(p, 125, "test", 0)
    assert p["qty"] == 1 and p["stop"] == 5
    favorable(p, 140, "test", 0)
    assert p["stop"] == 25
    favorable(p, 155, "test", 0)
    assert p["stop"] == 40
    segment(p, 155, 130, "test", 0)
    assert finish(p, 0)["net_pnl"] == 650
    p = new_position(sig,100,"test",0)
    open_gap(p,80,"test",0)
    assert finish(p,0)["net_pnl"] == -400
    # Low before entry must NOT stop a position that does not exist yet.
    b = {"dt":"test","o":95,"h":130,"l":80,"c":125}
    p, done = simulate_bar(None,sig,b,"olhc",0)
    assert done is None and p["qty"] == 1 and p["stop"] == 5
    b = {"dt":"test","o":100,"h":130,"l":80,"c":110}
    _, a = simulate_bar(None,sig,b,"ohlc",0)
    _, z = simulate_bar(None,sig,b,"olhc",0)
    assert finish(a,0)["net_pnl"] == 300 and finish(z,0)["net_pnl"] == -300
    # Price reflection yields exactly symmetric short rules.
    short = {**sig,"direction":-1,"trigger":100}
    reflected = {"dt":"test","o":100,"h":120,"l":70,"c":90}
    _, q = simulate_bar(None,short,reflected,"olhc",0)
    assert finish(q,0)["net_pnl"] == 300
    # A non-crossing next bar leaves no position or fill.
    p, q = simulate_bar(None,sig,{"dt":"test","o":90,"h":99,"l":85,"c":95},"ohlc",0)
    assert p is None and q is None
    # An entry gap is filled at open, never at an unavailable warning close.
    p, q = simulate_bar(None,sig,{"dt":"test","o":110,"h":120,"l":105,"c":115},"ohlc",0)
    assert p["entry"] == 110 and q is None
    # A newly raised stop takes effect within the same bar.
    p = new_position(sig,100,"test",0)
    segment(p,100,140,"test",0)
    segment(p,140,120,"test",0)
    assert finish(p,0)["net_pnl"] == 500


def verify_run(bars, stats, trades, equity):
    for t in trades:
        assert t["signal_dt"] < t["entry_dt"] <= t["exit_dt"]
        assert t["qty"] == 0 and sum(l["qty"] for l in t["legs"]) == 2
        assert t["net_pnl"] == sum(l["pnl"] for l in t["legs"])-t["fees"]
        assert t["net_pnl"] == t["gross_pnl"]-4*stats["fee_per_lot_side"]
    for prev, nxt in zip(trades,trades[1:]):
        assert prev["exit_dt"] <= nxt["signal_dt"], "Position exclusivity or close-only signals violated"
    assert sum(t["net_pnl"] for t in trades) == stats["net_pnl"] == equity[-1]["equity"]
    assert sum(stats["periods"][m]["net_pnl"] for m in ["month1","month2"]) == stats["net_pnl"]


def verify_causality_and_symmetry(bars):
    for i in range(20,len(bars),37):
        prefix = copy.deepcopy(bars[:i+1])
        indicators(prefix)
        assert signal(prefix,i) == signal(bars,i)
    reflected = copy.deepcopy(bars)
    for b in reflected:
        b["o"],b["h"],b["l"],b["c"] = 20000-b["o"],20000-b["l"],20000-b["h"],20000-b["c"]
    indicators(reflected)
    _, a, _, _ = backtest(bars,"adverse_first")
    _, z, _, _ = backtest(reflected,"adverse_first")
    assert [(t["signal_dt"],t["entry_dt"],t["net_pnl"],t["direction"]) for t in a] == [
        (t["signal_dt"],t["entry_dt"],t["net_pnl"],-t["direction"]) for t in z]


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--fetch",action="store_true")
    args=parser.parse_args()
    OUT.mkdir(parents=True,exist_ok=True)
    if args.fetch:
        fetch()
    self_checks()
    bars=load("eg0_15m")
    assert bars, "Run with --fetch first"
    indicators(bars)
    write_csv("eg0_15m.csv",bars)
    audit_data(bars)
    for freq in ["5m", "1m"]:
        audit_aggregation(bars, freq)
    audit_external_source()
    contract_data = build_contract_data(bars)
    summaries={}
    cases=[(mode,{},mode) for mode in ["ohlc","olhc","adverse_first","favorable_first"]]
    cases += [("adverse_first",{"fee":3,"slip":1},"fee3_slip1"),
              ("adverse_first",{"fee":3},"fee3_only"),
              ("adverse_first",{"filters":False},"no_filters"),
              ("adverse_first",{"start":"2026-08-19 00:00:00"},"latest_two_calendar_months")]
    cases += [(mode,{"contract_data":contract_data},"contracts_"+mode) for mode in ["ohlc","olhc","adverse_first","favorable_first"]]
    cases += [("adverse_first",{"contract_data":contract_data,"fee":3},"contracts_fee3_only"),
              ("adverse_first",{"contract_data":contract_data,"fee":3,"slip":1},"contracts_fee3_slip1"),
              ("adverse_first",{"contract_data":contract_data,"filters":False},"contracts_no_filters"),
              ("adverse_first",{"contract_data":contract_data,"start":"2026-08-19 00:00:00"},"contracts_latest_two_calendar_months")]
    for freq in ["5m","1m"]:
        lower=load("eg0_"+freq)
        if lower:
            cases += [(mode,{"lower":lower},freq+"_"+mode) for mode in ["adverse_first","favorable_first"]]
    for mode,kwargs,label in cases:
        stats,trades,equity,amb=backtest(bars,mode,**kwargs)
        verify_run(bars,stats,trades,equity)
        summaries[label]=stats
        dump(label+"_trades.json",trades)
        write_csv(label+"_trades.csv",[{k:v for k,v in t.items() if k != "legs"} | {"legs":json.dumps(t["legs"],ensure_ascii=False)} for t in trades])
        write_csv(label+"_equity.csv",equity)
        dump(label+"_ambiguous_bars.json",amb)
        print(label,json.dumps({k:stats[k] for k in ["trades","wins","losses","net_pnl","profit_factor","max_drawdown","worst_trade","gap_stop_trades","refined_bars"]},ensure_ascii=False))
    dump("summary.json",summaries)
    verify_causality_and_symmetry(bars)
    manifest = []
    for p in sorted(OUT.glob("*.jsonp")):
        manifest.append({"file":p.name,"bytes":p.stat().st_size,"sha256":hashlib.sha256(p.read_bytes()).hexdigest()})
    dump("snapshot_manifest.json",manifest)
    dump("verification.json", {"passed":True,"cases":len(cases),
        "checks":["ATR prefix causality", "long/short price-reflection symmetry", "one-bar entry expiry",
                  "entry gap pricing", "intrabar ladder activation", "same-bar path ambiguity",
                  "pre-entry extremes excluded", "gap stops at open", "exclusive positions",
                  "two-lot accounting", "fee accounting", "monthly sum", "terminal equity reconciliation"]})


if __name__ == "__main__":
    main()
