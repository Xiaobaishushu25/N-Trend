"""Bounded, chronological research of five new EG swing hypotheses.

Native one-lot trades, no mandatory15/23 close. Never imports production code.
Forty configurations are declared before PnL; old data are NOT a pristine holdout.
"""
import copy
import csv
import hashlib
import json
import math
import random
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import audit_eg_15m as audit
import extend_eg_backtest as extended
import analyze_eg_ten_months as ten

OUT = audit.ROOT / "reports" / "eg_swing_discovery_20261019"
FAMILIES = ["channel", "pullback", "failed_break", "vwap_reentry", "opening_range"]
NAMES = {"channel": "20根通道突破", "pullback": "EMA顺势回调", "failed_break": "20根假突破反转",
         "vwap_reentry": "日内均价偏离后回归", "opening_range": "开盘30分钟区间突破"}
WINDOWS = {"train": (ten.START, "2026-04-15 23:59:59"),
           "validate": ("2026-04-16 09:00:00", "2026-08-15 23:59:59"),
           "later": ("2026-08-16 09:00:00", ten.END),
           "continuous": (ten.START, ten.END),
           "reserve": ("2025-10-23 09:00:00", "2025-11-16 23:59:59")}


def save(name, data):
    (OUT / name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def configs():
    return [{"name": f"{f}_{tf}m_r{r:g}_{ex}", "family": f, "tf": tf, "atr_stop": r, "exit": ex,
             "qty": 1, "min_stop": 12, "max_stop": 46, "target_R": 2,
             "trail_ATR": 2, "max_trading_minutes": 120, "max_entries_calendar_day": 3,
             "daily_entry_stop_loss": 800, "initial_capital": 40000,
             "margin_assumption": .25, "max_lot_margin": 20000, "max_margin_equity_ratio": .4,
             "warning_max_age_minutes": 30, "max_open_gap_ATR": .25}
            for f in FAMILIES for tf in [5, 15] for r in [1., 1.5] for ex in ["target2R", "trail2ATR"]]


def aggregate(ms, tf):
    groups = defaultdict(list)
    for b in ms:
        d = datetime.fromisoformat(b["dt"])
        end = d.replace(minute=d.minute//tf*tf)
        if d.minute % tf:
            end += timedelta(minutes=tf)
        groups[end.isoformat(sep=" ")].append(b)
    bars = []
    for dt, sub in sorted(groups.items()):
        assert len(sub) == tf
        assert all((datetime.fromisoformat(z["dt"])-datetime.fromisoformat(a["dt"])).total_seconds() == 60 for a, z in zip(sub, sub[1:]))
        bars.append({"dt": dt, "o": sub[0]["o"], "h": max(b["h"] for b in sub), "l": min(b["l"] for b in sub),
                     "c": sub[-1]["c"], "v": sum(b["v"] for b in sub), "p": sub[-1]["p"],
                     "typical_value": sum((b["h"]+b["l"]+b["c"])/3*b["v"] for b in sub), "minutes": sub})
    return bars


def enrich(bs):
    tr = []; changes = []; session = None; sh = sl = num = vol = 0
    fast = slow = None
    for i, b in enumerate(bs):
        prev = bs[i-1]["c"] if i else b["o"]
        tr.append(max(b["h"]-b["l"], abs(b["h"]-prev), abs(b["l"]-prev)))
        changes.append(abs(b["c"]-prev))
        b["atr"] = sum(tr[-20:])/20 if i >= 19 else None
        fast = b["c"] if fast is None else fast + 2/13*(b["c"]-fast)
        slow = b["c"] if slow is None else slow + 2/37*(b["c"]-slow)
        b["fast"], b["slow"] = fast, slow
        b["er"] = abs(b["c"]-bs[i-20]["c"])/sum(changes[-20:]) if i >= 20 and sum(changes[-20:]) else 0.
        b["prior_high"] = max(x["h"] for x in bs[i-20:i]) if i >= 20 else None
        b["prior_low"] = min(x["l"] for x in bs[i-20:i]) if i >= 20 else None
        d = datetime.fromisoformat(b["dt"])
        key = d.date().isoformat() + ("N" if d.hour >= 21 else "D")
        if key != session:
            session = key; sh = float("-inf"); sl = float("inf"); num = vol = 0.
        anchor = d.replace(hour=21 if d.hour >= 21 else 9, minute=0, second=0)
        age = (d-anchor).total_seconds()/60
        b["session"], b["session_age"] = session, age
        b["orb_high"], b["orb_low"] = (sh, sl) if age > 30 else (None, None)
        if age <= 30:
            sh, sl = max(sh, b["h"]), min(sl, b["l"])
        num += b["typical_value"]; vol += b["v"]
        b["vwap_proxy"] = num/vol if vol else b["c"]


def signal(bs, i, family):
    if i < 60:
        return 0
    b, p = bs[i], bs[i-1]
    if b["atr"] is None:
        return 0
    if family == "channel":
        return 1 if b["c"] > b["prior_high"] and b["fast"] > b["slow"] else -1 if b["c"] < b["prior_low"] and b["fast"] < b["slow"] else 0
    if family == "pullback":
        return 1 if b["fast"] > b["slow"] and b["l"] <= b["fast"] and b["c"] > p["h"] else -1 if b["fast"] < b["slow"] and b["h"] >= b["fast"] and b["c"] < p["l"] else 0
    if family == "failed_break":
        return 1 if b["l"] < b["prior_low"] and b["c"] > b["prior_low"] and b["c"] > b["o"] else -1 if b["h"] > b["prior_high"] and b["c"] < b["prior_high"] and b["c"] < b["o"] else 0
    if family == "vwap_reentry":
        if b["session"] != p["session"] or b["er"] >= .35:
            return 0
        return 1 if p["c"] < p["vwap_proxy"]-2*p["atr"] and b["c"] > b["vwap_proxy"]-2*b["atr"] and b["c"] > b["o"] else -1 if p["c"] > p["vwap_proxy"]+2*p["atr"] and b["c"] < b["vwap_proxy"]+2*b["atr"] and b["c"] < b["o"] else 0
    if family == "opening_range":
        if not 30 < b["session_age"] <= 120 or b["orb_high"] is None:
            return 0
        return 1 if b["c"] > b["orb_high"] and p["c"] <= b["orb_high"] else -1 if b["c"] < b["orb_low"] and p["c"] >= b["orb_low"] else 0
    raise ValueError(family)


def prepare():
    _, original_groups, _, mapping15 = ten.prepare()
    minutes = {s: [b for _, sub in sorted(gs.items()) for b in sub] for s, gs in original_groups.items()}
    before = extended.OUT
    extended.OUT = OUT
    try:
        reserve = {"main": extended.load_minutes("main_reserve"), "eg2601": extended.load_minutes("eg2601_reserve")}
    finally:
        extended.OUT = before
    overlaps = {}
    for sym, extra in reserve.items():
        old = {b["dt"]: b for b in minutes[sym]}; overlap = 0
        for b in extra:
            if b["dt"] in old:
                assert old[b["dt"]] == b, (sym, b["dt"])
                overlap += 1
            old[b["dt"]] = b
        minutes[sym] = [old[k] for k in sorted(old)]; overlaps[sym] = overlap
    data = {}; fallbacks = []; calendar_boundaries = {}
    for tf in [5, 15]:
        bars = {s: aggregate(ms, tf) for s, ms in minutes.items()}
        flat_dates = []
        for i, mb in enumerate(bars["main"]):
            dt = datetime.fromisoformat(mb["dt"])
            next_open = datetime.fromisoformat(bars["main"][i+1]["minutes"][0]["dt"])-timedelta(minutes=1) if i+1 < len(bars["main"]) else None
            # Only timestamps/known exchange calendar, never future prices.
            flag = (next_open-dt).total_seconds() > 24*3600 if next_open is not None else dt.weekday() == 4 and dt.hour == 23
            mb["calendar_flat"] = flag
            if flag:
                flat_dates.append({"close": mb["dt"], "next_open": next_open.isoformat(sep=" ") if next_open else "2026-10-19 09:00:00 (end-of-snapshot Friday)"})
        calendar_boundaries[str(tf)] = flat_dates
        for sym, bs in bars.items():
            if sym != "main":
                enrich(bs)
        ix = {s: {b["dt"]: i for i, b in enumerate(bs)} for s, bs in bars.items() if s != "main"}
        mapping = {}; missing = []
        for b in bars["main"]:
            dt = b["dt"]
            if dt < WINDOWS["reserve"][0]:
                continue
            if dt >= ten.START:
                d = datetime.fromisoformat(dt); parent = d.replace(minute=d.minute//15*15)
                if d.minute % 15:
                    parent += timedelta(minutes=15)
                sym = mapping15[parent.isoformat(sep=" ")]
            else:
                matches = [s for s, ind in ix.items() if dt in ind and all(bars[s][ind[dt]][k] == b[k] for k in ["c", "p"])]
                if len(matches) != 1:
                    missing.append(b)
                    continue
                sym = matches[0]
            mapping[dt] = sym
        ordered = [b for b in bars["main"] if b["dt"] >= WINDOWS["reserve"][0]]
        positions = {b["dt"]: i for i, b in enumerate(ordered)}
        for b in missing:
            dt = b["dt"]; i = positions[dt]
            matches = [s for s, ind in ix.items() if dt in ind and all(bars[s][ind[dt]][k] == b[k] for k in ["h", "l", "c"])]
            neighbors = [mapping.get(ordered[j]["dt"]) for j in [i-1, i+1] if 0 <= j < len(ordered)]
            assert len(matches) == 1 and len(neighbors) == 2 and all(s == matches[0] for s in neighbors), (dt, matches, neighbors)
            mapping[dt] = matches[0]
            fallbacks.append({"tf": tf, "dt": dt, "member": matches[0], "criterion": "uniqueHLC and same member on adjacent bars; concrete source prices unchanged"})
        signals = {(sym, f): [signal(bs, i, f) for i in range(len(bs))] for sym, bs in bars.items() if sym != "main" for f in FAMILIES}
        for sym, bs in bars.items():
            if sym == "main":
                continue
            for i in [len(bs)//3, len(bs)//2, len(bs)-2]:
                prefix = copy.deepcopy(bs[:i+1]); enrich(prefix)
                for key in ["atr", "fast", "slow", "er", "prior_high", "prior_low", "orb_high", "orb_low", "vwap_proxy"]:
                    assert prefix[i][key] == bs[i][key], (sym, i, key)
                for f in FAMILIES:
                    assert signal(prefix, i, f) == signals[(sym, f)][i]
        data[tf] = (bars, ix, mapping, signals)
    save("data_verification.json", {"overlap_exact_matches": overlaps, "timeframes": [5, 15], "prefix_causality_sampled": True, "member_fallbacks": fallbacks,
         "reserve_mapping": "unique close+OI match on concrete contracts, no unresolved members", "source": str(ten.OUT.resolve())})
    save("calendar_boundaries.json", {"rule": "flatten at last close before known next market open >24h away; timestamps only, no future prices; includes weekends/holidays", "boundaries": calendar_boundaries})
    return data


def finish(p, price, dt, reason, slip):
    fill = price if reason == "target" else price-p["direction"]*slip
    gross = (fill-p["entry"])*p["direction"]*p.get("multiplier", 10)
    fees = 2*p.get("fee_per_side", 3)
    return {**p, "exit_dt": dt, "exit": fill, "exit_reason": reason, "gross_pnl": gross,
            "fees": fees, "net_pnl": gross-fees, "gap_stop": reason == "gap_stop"}


def minute_exit(p, b, mode, slip):
    d = p["direction"]
    if (b["o"]-p["stop"])*d <= 0:
        return finish(p, b["o"], b["dt"], "gap_stop" if (b["o"]-p["stop"])*d < 0 else "stop", slip)
    if p["target"] is not None and (b["o"]-p["target"])*d >= 0:
        return finish(p, p["target"], b["dt"], "target", slip)
    seq = [b[k] for k in (["o", "h", "l", "c"] if mode == "ohlc" else ["o", "l", "h", "c"])]
    for a, z in zip(seq, seq[1:]):
        if (z-a)*d > 0 and p["target"] is not None and (z-p["target"])*d >= 0:
            return finish(p, p["target"], b["dt"], "target", slip)
        if (z-a)*d < 0 and (z-p["stop"])*d <= 0:
            return finish(p, p["stop"], b["dt"], "stop", slip)
    return None


def run(bundle, cfg, span, slip=1, mode="ohlc"):
    bars, ix, mapping, signals = bundle
    multiplier = cfg.get("multiplier", 10)
    fee_per_side = cfg.get("fee_per_side", 3)
    start, end = span; p = pending = None; trades = []; curve = []; total = 0.
    day = None; day_start = 0.; entries = 0; rejects = Counter(); last = None
    minute_peak = minute_dd = 0.
    minute_trace = cfg.get("_minute_equity_trace")
    for mb in bars["main"]:
        dt = mb["dt"]
        if not start <= dt <= end:
            continue
        sym = (p or pending or {}).get("contract", mapping[dt])
        assert dt in ix[sym], (sym, dt)
        b = bars[sym][ix[sym][dt]]; last = b
        if day != dt[:10]:
            day = dt[:10]; entries = 0
            day_start = total + ((b["o"]-p["entry"])*p["direction"]*multiplier-fee_per_side if p else 0)
        if pending:
            elapsed = (datetime.fromisoformat(b["minutes"][0]["dt"])-datetime.fromisoformat(pending["signal_dt"])).total_seconds()/60
            if pending["contract"] != mapping[dt] or elapsed > cfg["warning_max_age_minutes"]:
                rejects["stale_or_roll"] += 1
            elif entries >= cfg["max_entries_calendar_day"] or total-day_start <= -cfg["daily_entry_stop_loss"]:
                rejects["daily_limit"] += 1
            elif abs(b["o"]-pending["signal_close"]) > max(cfg.get("min_open_gap_points", 2), cfg["max_open_gap_ATR"]*pending["signal_atr"]):
                rejects["entry_gap"] += 1
            else:
                r = max(cfg["min_stop"], math.ceil(cfg["atr_stop"]*pending["signal_atr"]))
                margin = b["o"]*multiplier*cfg["margin_assumption"]
                if r > cfg["max_stop"]:
                    rejects["risk_budget"] += 1
                elif margin > min(cfg["max_lot_margin"], cfg["max_margin_equity_ratio"]*(cfg["initial_capital"]+total)):
                    rejects["margin_budget"] += 1
                else:
                    d = pending["direction"]; entry = b["o"]+d*slip
                    p = {**pending, "entry": entry, "entry_dt": b["minutes"][0]["dt"], "stop": entry-d*r,
                         "target": entry+d*cfg["target_R"]*r if cfg["exit"] == "target2R" else None,
                         "initial_risk_points": r, "planned_stop_loss_with_cost": (r+slip)*multiplier+2*fee_per_side,
                         "multiplier": multiplier, "fee_per_side": fee_per_side,
                         "margin_estimate": margin, "trading_minutes": 0}
                    entries += 1
        pending = None
        if p:
            for m in b["minutes"]:
                p["trading_minutes"] += 1
                done = minute_exit(p, m, mode, slip)
                if done is None and p["trading_minutes"] >= cfg["max_trading_minutes"]:
                    done = finish(p, m["c"], m["dt"], "time", slip)
                if done:
                    trades.append(done); total += done["net_pnl"]; p = None
                minute_equity = total + ((m["c"]-p["entry"])*p["direction"]*multiplier-fee_per_side if p else 0)
                minute_peak = max(minute_peak, minute_equity)
                minute_dd = max(minute_dd, minute_peak-minute_equity)
                if minute_trace is not None:
                    minute_trace.append(minute_equity)
                if done:
                    break
        if p and cfg.get("calendar_flat", False) and mb["calendar_flat"]:
            done = finish(p, b["c"], dt, "calendar_flat", slip)
            trades.append(done); total += done["net_pnl"]; p = None
            minute_peak = max(minute_peak, total)
            minute_dd = max(minute_dd, minute_peak-total)
            if minute_trace is not None:
                minute_trace.append(total)
        if p and cfg["exit"] == "trail2ATR":
            proposed = b["c"]-p["direction"]*math.ceil(cfg["trail_ATR"]*b["atr"])
            p["stop"] = max(p["stop"], proposed) if p["direction"] == 1 else min(p["stop"], proposed)
        equity = total + ((b["c"]-p["entry"])*p["direction"]*multiplier-fee_per_side if p else 0)
        curve.append({"dt": dt, "equity": equity, "qty": 1 if p else 0})
        if not p:
            sym = mapping[dt]; i = ix[sym][dt]; own = bars[sym][i]; d = signals[(sym, cfg["family"])][i]
            if d:
                pending = {"contract": sym, "signal_dt": dt, "signal_close": own["c"], "signal_atr": own["atr"], "direction": d}
    if p:
        done = finish(p, last["c"], last["dt"], "terminal", slip)
        trades.append(done); total += done["net_pnl"]
        curve[-1].update(equity=total, qty=0)
        minute_peak = max(minute_peak, total)
        minute_dd = max(minute_dd, minute_peak-total)
        if minute_trace is not None:
            minute_trace.append(total)
    assert curve
    assert abs(sum(t["net_pnl"] for t in trades)-curve[-1]["equity"]) < 1e-7
    for t in trades:
        assert t["signal_dt"] < t["entry_dt"] <= t["exit_dt"]
        assert t["planned_stop_loss_with_cost"] <= 500
        assert t["fees"] == 2*fee_per_side and t["net_pnl"] == t["gross_pnl"]-t["fees"]
        d = datetime.fromisoformat(t["entry_dt"])
        parent = d.replace(minute=d.minute//cfg["tf"]*cfg["tf"], second=0)
        if d.minute % cfg["tf"] or d.second:
            parent += timedelta(minutes=cfg["tf"])
        assert mapping[parent.isoformat(sep=" ")] == t["contract"]
    for a, z in zip(trades, trades[1:]):
        assert a["exit_dt"] <= z["signal_dt"]
    if cfg.get("calendar_flat", False):
        boundaries = [b["dt"] for b in bars["main"] if b["calendar_flat"] and start <= b["dt"] <= end]
        assert all(not t["entry_dt"] < boundary < t["exit_dt"] for t in trades for boundary in boundaries)
    vals = [t["net_pnl"] for t in trades]; wins = [v for v in vals if v > 0]; losses = [v for v in vals if v < 0]
    high = dd = 0.
    for e in curve:
        high = max(high, e["equity"]); dd = max(dd, high-e["equity"])
    stats = {"name": cfg["name"], "family": cfg["family"], "tf": cfg["tf"], "slip": slip, "mode": mode,
             "start": start, "end": end, "trades": len(vals), "net_pnl": total, "max_drawdown": dd,
             "minute_close_max_drawdown": minute_dd,
             "win_rate_pct": 100*len(wins)/len(vals) if vals else 0,
             "profit_factor": sum(wins)/-sum(losses) if losses else None, "fees": 2*fee_per_side*len(vals),
             "worst_trade": min(vals) if vals else 0, "gap_stop_trades": sum(t["gap_stop"] for t in trades),
             "calendar_flat_trades": sum(t["exit_reason"] == "calendar_flat" for t in trades),
             "long_net": sum(t["net_pnl"] for t in trades if t["direction"] == 1),
             "short_net": sum(t["net_pnl"] for t in trades if t["direction"] == -1),
             "cross_15_or_23": sum(any(t["entry_dt"] < day+" "+hour < t["exit_dt"] for day in {t["entry_dt"][:10], t["exit_dt"][:10]} for hour in ["15:00:00", "23:00:00"]) for t in trades),
             "median_holding_trading_minutes": statistics.median(t["trading_minutes"] for t in trades) if trades else None,
             "rejects": dict(rejects)}
    return stats, trades, curve


def training_selection(results, configurations):
    def get(name, slip, mode):
        return next(r for r in results if r["name"] == name and r["slip"] == slip and r["mode"] == mode)
    eligible = []; diagnostics = []
    for cfg in configurations:
        scenarios = [get(cfg["name"], s, m) for s in [1, 2] for m in ["ohlc", "olhc"]]
        neighbors = [c for c in configurations if (c["family"], c["tf"]) == (cfg["family"], cfg["tf"])]
        positive_neighbors = sum(min(get(c["name"], 1, m)["net_pnl"] for m in ["ohlc", "olhc"]) > 0 for c in neighbors)
        gates = {"train_positive_all_cost_paths": all(r["net_pnl"] > 0 for r in scenarios),
                 "train_pf_at_least_1_1": all(r["profit_factor"] is not None and r["profit_factor"] >= 1.1 for r in scenarios[:2]),
                 "train_at_least_30_trades": all(r["trades"] >= 30 for r in scenarios),
                 "train_dd_below_8000": all(r["max_drawdown"] <= 8000 for r in scenarios),
                 "neighbor_positive_at_least_3_of_4": positive_neighbors >= 3}
        score = min(r["net_pnl"]/max(500, r["max_drawdown"]) for r in scenarios)
        row = {"name": cfg["name"], "gates": gates, "all_passed": all(gates.values()), "score": score, "positive_neighbors": positive_neighbors}
        diagnostics.append(row)
        if row["all_passed"]:
            eligible.append(row)
    eligible.sort(key=lambda r: (-r["score"], r["name"]))
    selected = []; seen = set()
    for r in eligible:
        family = next(c["family"] for c in configurations if c["name"] == r["name"])
        if family not in seen:
            selected.append(r["name"]); seen.add(family)
        if len(selected) == 3:
            break
    return {"selected": selected, "eligible": eligible, "diagnostics": diagnostics,
            "selection_rule": "training only; max three distinct families; maximize worst cost/path profit-to-DD after all gates; never select on later/reserve PnL"}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    cs = configs()
    initial_plan = OUT/"initial_analysis_plan.json"
    created_at = json.loads(initial_plan.read_text(encoding="utf-8"))["created_at"] if initial_plan.exists() else datetime.now(extended.ZONE).isoformat()
    save("analysis_plan.json", {"created_at": created_at, "configs": cs, "trials": len(cs), "windows": WINDOWS,
         "instruction": "latest user removes15/23 compulsory liquidation; native one-lot small swings, not old candle entries", "fee": "3 CNY/lot/side,6 per round trip",
         "costs": "1/2/3 adverse ticks on entries and non-limit exits, resting2R targets no extra slip",
         "execution": "5/15m close signals, next bar open; chronological1m fills with bothOHLC paths; trailing updates only after closed signal bar",
         "risk": "12..46point initial stop; skip larger, no clipping; max planned loss500 including3tick stop slippage and fees; actual gaps can exceed",
         "margin": "25% of notional assumed, not verified historical broker margin;<=20000/lot and<=40% current account equity",
         "vwap": "volume-weighted1m typical price proxy, NOT actual transaction VWAP because turnover absent",
         "time_exit": "120 traded minutes, may cross lunch/night/weekend; no fixed15/23 liquidation; terminal exit only at research window end",
         "daily_limits": "calendar day max3 entries; stop new entries after realized loss800; does not guarantee day loss bound",
         "selection": "first train only; gates: positive1/2ticks bothpaths,PF>=1.1 at1tick,>=30trades,DD<=8000,>=3/4positive family/timeframe neighbors; choose<=3different families by worst profit/DD",
         "later_validation": "all validation/later/reserve1/2tick paths profitable,DD<=10000; evaluate, never retune after failure",
         "contamination": "existing10month market and previous strategies already inspected; later periods are chronological diagnostics, NOT untouched independent holdout; new earlier reserve is not future verification",
         "reserve_sources": json.loads((OUT/"reserve_sources.json").read_text(encoding="utf-8"))})
    if initial_plan.exists():
        assert cs == json.loads(initial_plan.read_text(encoding="utf-8"))["configs"]
    bundles = prepare(); results = []
    # Do not calculate later/reserve PnL until the training shortlist is frozen.
    for cfg in cs:
        for slip in [1, 2]:
            for mode in ["ohlc", "olhc"]:
                s, ts, curve = run(bundles[cfg["tf"]], cfg, WINDOWS["train"], slip, mode)
                s["window"] = "train"; results.append(s)
        print("train", cfg["name"], results[-2]["net_pnl"], flush=True)
    save("train_results.json", results)
    selection = training_selection(results, cs); save("training_selection.json", selection)
    if (OUT/"initial_training_selection.json").exists():
        assert selection["selected"] == json.loads((OUT/"initial_training_selection.json").read_text(encoding="utf-8"))["selected"]
    print("FROZEN_SELECTION", selection["selected"], flush=True)
    # Evaluate every declared trial for transparency; only frozen selections can advance.
    for w in ["validate", "later", "continuous", "reserve"]:
        for cfg in cs:
            for slip in [1, 2]:
                for mode in ["ohlc", "olhc"]:
                    s, ts, curve = run(bundles[cfg["tf"]], cfg, WINDOWS[w], slip, mode)
                    s["window"] = w; results.append(s)
                    if slip == 1 and mode == "ohlc":
                        save(f"{w}_{cfg['name']}_trades.json", ts)
                        with (OUT/f"{w}_{cfg['name']}_equity.csv").open("w", encoding="utf-8-sig", newline="") as f:
                            writer = csv.DictWriter(f, fieldnames=["dt", "equity", "qty"]); writer.writeheader(); writer.writerows(curve)
            if w == "continuous":
                print(w, cfg["name"], results[-2]["net_pnl"], flush=True)
        print("COMPLETED", w, flush=True)
    #3tick stress is only run after selection, on every frozen candidate, no reselection.
    for name in selection["selected"]:
        cfg = next(c for c in cs if c["name"] == name)
        for w in WINDOWS:
            for mode in ["ohlc", "olhc"]:
                s, _, _ = run(bundles[cfg["tf"]], cfg, WINDOWS[w], 3, mode)
                s["window"] = w; results.append(s)
    save("all_results.json", results)
    keys = ["window", "name", "family", "tf", "slip", "mode", "trades", "net_pnl", "max_drawdown", "win_rate_pct", "profit_factor", "worst_trade", "gap_stop_trades", "cross_15_or_23"]
    with (OUT/"comparison.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=keys); writer.writeheader(); writer.writerows({k: r[k] for k in keys} for r in results)
    verdict = []
    for name in selection["selected"]:
        held = [r for r in results if r["name"] == name and r["window"] in ["validate", "later", "reserve"] and r["slip"] <= 2]
        verdict.append({"name": name, "passed_later_checks": all(r["net_pnl"] > 0 and r["max_drawdown"] <= 10000 for r in held),
                        "failed_scenarios": [{k: r[k] for k in ["window", "slip", "mode", "net_pnl", "max_drawdown"]} for r in held if r["net_pnl"] <= 0 or r["max_drawdown"] > 10000]})
    save("verdict.json", {"frozen_candidates": verdict, "all_passed": [r["name"] for r in verdict if r["passed_later_checks"]],
         "warning": "research only; bounded search and reused market history cannot establish stable future profits"})
    save("verification.json", {"passed": True, "trials": len(cs), "scenarios": len(results), "checks": ["closed-bar indicator/signal prefix sampling", "main constituent at entry", "next-bar entry and market-gap gate", "one-lot fees/risk budget/equity/exclusive positions", "training shortlist saved before later PnL", "exact raw overlap"]})


if __name__ == "__main__":
    main()
