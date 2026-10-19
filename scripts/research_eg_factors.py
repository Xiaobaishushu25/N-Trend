"""Bounded factor research on frozen native-one-lot EG channel baseline.

No execution, sizing, stop, target or holding-policy parameter search.
All hypotheses and chronological selection gates are written before evaluation.
"""
import copy
import csv
import hashlib
import json
import math
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np

import discover_eg_swing as engine

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT/"reports"/"eg_swing_calendarflat_20261019"
OUT = ROOT/"reports"/"eg_factor_research_20261019"
BASE_NAME = "channel_15m_r1_target2R"
FOLDS = [
    {"id": "f1", "train": (engine.ten.START, "2026-04-15 23:59:59"), "test": ("2026-04-16 09:00:00", "2026-06-15 23:59:59")},
    {"id": "f2", "train": (engine.ten.START, "2026-06-15 23:59:59"), "test": ("2026-06-16 09:00:00", "2026-08-15 23:59:59")},
    {"id": "f3", "train": (engine.ten.START, "2026-08-15 23:59:59"), "test": ("2026-08-16 09:00:00", engine.ten.END)},
]


def save(name, value):
    (OUT/name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def specifications():
    out = [{"id": "base", "group": "base", "label": "候选A基线", "tests": []}]
    groups = [
        ("volume20", "相对前20根放量", "volume20", ">=", [1., 1.5]),
        ("season_volume", "相对历史同时段放量", "season_volume", ">=", [1., 1.5]),
        ("oi_rising", "三根持仓量增加", "oi3", ">=", [0., .001]),
        ("oi_falling", "三根持仓量减少", "oi3", "<=", [0., -.001]),
        ("efficiency", "方向效率下限", "er", ">=", [.25, .4]),
        ("atr_rank", "ATR相对分位区间", "atr_rank", "band", [[.1, .9], [.25, .75]]),
        ("atr_expansion", "ATR较五根前扩张", "atr_expansion", ">=", [1., 1.1]),
        ("ema_separation", "均线间距/ATR下限", "ema_separation", ">=", [.25, .5]),
        ("ema_slope", "慢均线四根斜率/ATR下限", "ema_slope", ">=", [.1, .2]),
        ("close_location", "顺向收盘位置下限", "close_location", ">=", [.75, .9]),
        ("break_distance", "突破距离/ATR上限", "break_distance", "<=", [.25, .5]),
        ("body_fraction", "实体占比下限", "body_fraction", ">=", [.5, .7]),
        ("compression", "前20根宽度/前ATR上限", "compression", "<=", [4., 6.]),
    ]
    for group, label, key, op, thresholds in groups:
        for j, value in enumerate(thresholds):
            out.append({"id": f"{group}_{j+1}", "group": group, "label": f"{label} {value}", "tests": [[key, op, value]]})
    for name, key, op, value, label in [
        ("day_only", "day_session", "==", True, "仅日盘入场"),
        ("night_only", "day_session", "==", False, "仅夜盘入场"),
        ("after_open30", "session_age", ">", 30, "避开每个时段前30分钟"),
        ("avoid_last30", "minutes_to_close", ">=", 30, "避开日/夜收盘前30分钟入场"),
    ]:
        out.append({"id": name, "group": name, "label": label, "tests": [[key, op, value]]})
    combinations = [
        ("combo_volume_oi", "放量且增仓", [["season_volume", ">=", 1.], ["oi3", ">=", 0.]]),
        ("combo_efficiency_time", "有效率且避开开盘30分钟", [["er", ">=", .25], ["session_age", ">", 30]]),
        ("combo_volume_body", "同时段放量且实体充分", [["season_volume", ">=", 1.], ["body_fraction", ">=", .5]]),
        ("combo_rank_separation", "中等波动分位且均线分离", [["atr_rank", "band", [.1, .9]], ["ema_separation", ">=", .25]]),
    ]
    out += [{"id": name, "group": name, "label": label, "tests": tests} for name, label, tests in combinations]
    assert len(out) == 35
    return out


def features(bs, signals):
    out = []; slots = defaultdict(list)
    for i, b in enumerate(bs):
        slot = b["dt"][11:16]; past = slots[slot][-10:]
        previous_volume = [x["v"] for x in bs[max(0, i-20):i]]
        atr = b["atr"]
        previous_atr = [x["atr"] for x in bs[max(0, i-120):i] if x["atr"] is not None]
        d = signals[i]
        r = b["h"]-b["l"]
        rank = (sum(x < atr for x in previous_atr)+.5*sum(x == atr for x in previous_atr))/120 if atr and len(previous_atr) == 120 else None
        out.append({
            "volume20": b["v"]/statistics.mean(previous_volume) if len(previous_volume) == 20 and sum(previous_volume) else None,
            "season_volume": b["v"]/statistics.median(past) if len(past) >= 3 and statistics.median(past) else None,
            "oi3": b["p"]/bs[i-3]["p"]-1 if i >= 3 and bs[i-3]["p"] else None,
            "er": b["er"], "atr_rank": rank,
            "atr_expansion": atr/bs[i-5]["atr"] if i >= 5 and atr and bs[i-5]["atr"] else None,
            "ema_separation": d*(b["fast"]-b["slow"])/atr if atr else None,
            "ema_slope": d*(b["slow"]-bs[i-4]["slow"])/atr if i >= 4 and atr else None,
            "close_location": ((b["c"]-b["l"]) if d == 1 else (b["h"]-b["c"]))/r if r and d else None,
            "break_distance": ((b["c"]-b["prior_high"]) if d == 1 else (b["prior_low"]-b["c"]))/atr if d and atr and b["prior_high"] is not None else None,
            "body_fraction": abs(b["c"]-b["o"])/r if r else 0.,
            "compression": (b["prior_high"]-b["prior_low"])/bs[i-1]["atr"] if i and b["prior_high"] is not None and bs[i-1]["atr"] else None,
            "day_session": int(b["dt"][11:13]) < 21,
            "session_age": b["session_age"],
            "minutes_to_close": (15*60 if int(b["dt"][11:13]) < 21 else 23*60)-int(b["dt"][11:13])*60-int(b["dt"][14:16]),
        })
        slots[slot].append(b["v"])
    return out


def passes(f, spec):
    for key, op, value in spec["tests"]:
        x = f[key]
        if x is None:
            return False
        if op == ">=" and not x >= value: return False
        if op == "<=" and not x <= value: return False
        if op == ">" and not x > value: return False
        if op == "==" and not x == value: return False
        if op == "band" and not value[0] <= x <= value[1]: return False
    return True


def prepare(specs):
    for name in ["main_reserve_1m.csv", "eg2601_reserve_1m.csv", "reserve_sources.json"]:
        (OUT/name).write_bytes((SOURCE/name).read_bytes())
    engine.OUT = OUT
    bars, ix, mapping, original_signals = engine.prepare()[15]
    all_features = {}; filtered_signals = {}
    for sym, bs in bars.items():
        if sym == "main": continue
        sig = original_signals[(sym, "channel")]
        fs = features(bs, sig); all_features[sym] = fs
        for spec in specs:
            filtered_signals[(sym, spec["id"])] = [d if d and passes(f, spec) else 0 for d, f in zip(sig, fs)]
        # Several historical prefixes for every symbol and every declared hypothesis.
        for i in [len(bs)//3, len(bs)//2, len(bs)-2]:
            prefix = copy.deepcopy(bs[:i+1]); engine.enrich(prefix)
            prefix_signal = [engine.signal(prefix, j, "channel") for j in range(len(prefix))]
            pf = features(prefix, prefix_signal)
            assert pf[i] == fs[i], (sym, i)
            for spec in specs:
                assert (prefix_signal[i] if prefix_signal[i] and passes(pf[i], spec) else 0) == filtered_signals[(sym, spec["id"])][i]
    return (bars, ix, mapping, filtered_signals), all_features


def choose(training, specs):
    def get(name, slip, mode):
        return next(r for r in training if r["name"] == name and r["slip"] == slip and r["mode"] == mode)
    base = get("base", 2, "ohlc"); diagnostics = []
    for spec in specs:
        if spec["id"] == "base": continue
        rs = [get(spec["id"], s, m) for s in [1, 2] for m in ["ohlc", "olhc"]]
        siblings = [x for x in specs if x["group"] == spec["group"]]
        neighbor_ok = all(get(x["id"], 2, m)["net_pnl"] > 0 for x in siblings for m in ["ohlc", "olhc"]) if len(siblings) > 1 else True
        gates = {"positive_all_paths_costs": all(r["net_pnl"] > 0 for r in rs),
                 "at_least_30_and_35pct_base_trades": all(r["trades"] >= max(30, .35*base["trades"]) for r in rs),
                 "pf_at_least_1_1_at_1tick": all(get(spec["id"], 1, m)["profit_factor"] is not None and get(spec["id"], 1, m)["profit_factor"] >= 1.1 for m in ["ohlc", "olhc"]),
                 "2tick_improvement_500_or_20pct": all(get(spec["id"], 2, m)["net_pnl"] >= get("base", 2, m)["net_pnl"]+max(500, .2*abs(get("base", 2, m)["net_pnl"])) for m in ["ohlc", "olhc"]),
                 "dd_not_materially_worse": all(r["minute_close_max_drawdown"] <= min(8000, 1.25*base["minute_close_max_drawdown"]) for r in rs),
                 "sibling_thresholds_positive_2tick": neighbor_ok}
        score = min(get(spec["id"], 2, m)["net_pnl"]/max(500, get(spec["id"], 2, m)["minute_close_max_drawdown"]) for m in ["ohlc", "olhc"])
        diagnostics.append({"name": spec["id"], "label": spec["label"], "all_passed": all(gates.values()), "gates": gates, "score": score})
    eligible = sorted([r for r in diagnostics if r["all_passed"]], key=lambda r: (-r["score"], r["name"]))
    return {"selected": eligible[0]["name"] if eligible else "base", "eligible": eligible, "diagnostics": diagnostics,
            "fallback": "base if no factor meets all training-only improvement/risk/activity gates"}


def weekly_changes(curve):
    levels = {}
    for row in curve:
        d = datetime.fromisoformat(row["dt"]).isocalendar()
        levels[f"{d.year}-W{d.week:02d}"] = row["equity"]
    out = {}; previous = 0.
    for week, level in levels.items():
        out[week] = level-previous; previous = level
    return out


def multiple_test_audit(weekly):
    names = sorted(n for n in weekly if n != "base")
    weeks = sorted(weekly["base"])
    base = np.array([weekly["base"][w] for w in weeks])
    matrix = np.array([[weekly[n][w] for w in weeks] for n in names])-base
    means = matrix.mean(axis=1); observed = float(means.max())
    centered = matrix-means[:, None]
    rng = np.random.default_rng(20261019); null = []; count = len(weeks)
    for _ in range(2000):
        starts = rng.integers(0, count, size=(count+1)//2)
        indices = np.column_stack((starts, (starts+1)%count)).ravel()[:count]
        null.append(float(centered[:, indices].mean(axis=1).max()))
    return {"hypotheses": len(names), "weeks": count, "hindsight_best": names[int(means.argmax())],
            "observed_best_weekly_increment": observed,
            "centered_block_maxmean_p": (1+sum(v >= observed for v in null))/2001,
            "method": "paired strategy-minus-base weekly MTM changes; common circular2-week blocks; centered null max mean over all34 alternatives;2000 draws",
            "limitations": "simplified familywise audit for this batch only; not an independent future test; does not account for previous research batches; no proof of profitability"}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    specs = specifications()
    base_cfg = next(c for c in json.loads((SOURCE/"analysis_plan.json").read_text(encoding="utf-8"))["configs"] if c["name"] == BASE_NAME)
    configs = {s["id"]: {**base_cfg, "name": s["id"], "family": s["id"]} for s in specs}
    save("analysis_plan.json", {"created_at": datetime.now(engine.extended.ZONE).isoformat(), "hypotheses_including_base": len(specs), "specifications": specs,
         "base_configuration": base_cfg, "folds": FOLDS, "fixed_exit_risk_and_size": True,
         "training_gate": "all1/2tick paths positive;>=30 and35%base entries;1tickPF>=1.1;2tick profit improves>=max(500,20%abs(base));minuteDD<=min(8000,1.25base2tickDD);siblings2tick positive",
         "ranking": "highest worst2tick profit/minuteDD, training only; fallbackbase", "combinations": "only4 declared economic combinations, no all-pairs sweep",
         "history_warning": "all these market periods and earlier strategy results already inspected; chronological selection is NOT untouched out-of-sample validation",
         "data": str(SOURCE.resolve()), "oi_interpretation": "end-of-bar open interest level/change, no inferred buy/sell flow",
         "time_normalization": "current volume divided by median of up to10 PRIOR same-clock-slot observations, at least3; current excluded",
         "costs": "native1lot,3CNY/side,1/2tick bothpaths all tests;3tick audit on prior-selected candidates, never changes selection",
         "walk_forward": "every2month decision; positions settled at each scheduled re-selection boundary; fees/slip charged; realized equity carried to next segment; ordinary15/23 not compulsory",
         "research_inputs_sha256": {f: hashlib.sha256((ROOT/"scripts"/f).read_bytes()).hexdigest() for f in ["research_eg_factors.py", "discover_eg_swing.py"]}})
    bundle, all_features = prepare(specs)
    prior = json.loads((SOURCE/"all_results.json").read_text(encoding="utf-8"))
    results = []; selections = {}; weekly = {1: {}, 2: {}}; reference_count = 0

    def evaluate(window, span, prior_window=None):
        nonlocal reference_count
        rows = []
        for spec in specs:
            for slip in [1, 2]:
                for mode in ["ohlc", "olhc"]:
                    stats, ts, curve = engine.run(bundle, configs[spec["id"]], span, slip, mode)
                    stats["window"] = window; rows.append(stats); results.append(stats)
                    if spec["id"] == "base" and prior_window:
                        ref = next(r for r in prior if r["window"] == prior_window and r["name"] == BASE_NAME and r["slip"] == slip and r["mode"] == mode)
                        for k in ["trades", "net_pnl", "minute_close_max_drawdown", "worst_trade", "fees"]:
                            assert stats[k] == ref[k], (window, k, stats[k], ref[k])
                        reference_count += 1
                    if slip == 1 and mode == "ohlc":
                        save(f"{window}_{spec['id']}_trades.json", ts)
                        if window == "continuous":
                            with (OUT/f"{window}_{spec['id']}_equity.csv").open("w", encoding="utf-8-sig", newline="") as f:
                                writer = csv.DictWriter(f, fieldnames=["dt", "equity", "qty"]); writer.writeheader(); writer.writerows(curve)
                    if window == "continuous" and mode == "ohlc":
                        weekly[slip][spec["id"]] = weekly_changes(curve)
            if window == "continuous":
                r = next(r for r in rows if r["name"] == spec["id"] and r["slip"] == 1 and r["mode"] == "ohlc")
                print(window, spec["id"], r["net_pnl"], "DD", r["minute_close_max_drawdown"], flush=True)
        return rows

    for fold in FOLDS:
        training = evaluate(fold["id"]+"_train", fold["train"], "train" if fold["id"] == "f1" else None)
        selection = choose(training, specs); selections[fold["id"]] = selection
        save(fold["id"]+"_selection_before_test.json", selection)
        print("FROZEN", fold["id"], selection["selected"], "eligible", len(selection["eligible"]), flush=True)
        evaluate(fold["id"]+"_test", fold["test"], "later" if fold["id"] == "f3" else None)
    evaluate("validate_four", engine.WINDOWS["validate"], "validate")
    evaluate("continuous", engine.WINDOWS["continuous"], "continuous")
    evaluate("reserve", engine.WINDOWS["reserve"], "reserve")
    save("all_core_results.json", results)
    save("all_selections.json", selections)
    # Fixed candidates are nominated by the first4-month training only.
    fixed = [r["name"] for r in selections["f1"]["eligible"][:3]]
    audit_names = sorted(set(["base"]+fixed+[s["selected"] for s in selections.values()]))
    extra = []
    for name in audit_names:
        for w, span in {"initial_train": FOLDS[0]["train"], "validate_four": engine.WINDOWS["validate"], "later": engine.WINDOWS["later"], "continuous": engine.WINDOWS["continuous"], "reserve": engine.WINDOWS["reserve"]}.items():
            for mode in ["ohlc", "olhc"]:
                s, _, _ = engine.run(bundle, configs[name], span, 3, mode)
                s["window"] = w; extra.append(s)
    save("three_tick_audit.json", extra)
    # Operational walk-forward: fixed decision rule, next two months only, capital carried.
    walks = []
    for policy in ["adaptive", "base"]:
        for slip in [1, 2, 3]:
            for mode in ["ohlc", "olhc"]:
                cumulative = high = dd = 0.; trades = []; segments = []; curve = []
                for fold in FOLDS:
                    name = selections[fold["id"]]["selected"] if policy == "adaptive" else "base"
                    trace = []
                    cfg = {**configs[name], "initial_capital": 40000+cumulative, "_minute_equity_trace": trace}
                    s, ts, eq = engine.run(bundle, cfg, fold["test"], slip, mode)
                    local_high = local_dd = 0.
                    for v in trace:
                        local_high = max(local_high, v); local_dd = max(local_dd, local_high-v)
                        high = max(high, cumulative+v); dd = max(dd, high-cumulative-v)
                    assert abs(local_dd-s["minute_close_max_drawdown"]) < 1e-7
                    segments.append({"fold": fold["id"], "factor": name, "net_pnl": s["net_pnl"], "trades": s["trades"], "start_capital": cfg["initial_capital"]})
                    curve.extend({**e, "equity": e["equity"]+cumulative} for e in eq)
                    for t in ts: t["factor"] = name
                    trades.extend(ts); cumulative += s["net_pnl"]
                r = {"policy": policy, "slip": slip, "mode": mode, "net_pnl": cumulative, "minute_close_max_drawdown": dd, "trades": len(trades), "segments": segments}
                walks.append(r)
                if slip == 1 and mode == "ohlc":
                    save("walk_"+policy+"_trades.json", trades)
                    with (OUT/("walk_"+policy+"_equity.csv")).open("w", encoding="utf-8-sig", newline="") as f:
                        writer = csv.DictWriter(f, fieldnames=["dt", "equity", "qty"]); writer.writeheader(); writer.writerows(curve)
                print("WALK", policy, slip, mode, cumulative, "DD", dd, flush=True)
    save("walk_forward_results.json", walks)
    save("multiple_testing_audit.json", {str(s): multiple_test_audit(weekly[s]) for s in [1, 2]})
    confirmations = []
    for name in fixed:
        rows = [r for r in results if r["name"] == name and r["window"] in ["validate_four", "f3_test", "reserve"]]
        failures = []
        for r in rows:
            ref = next(x for x in results if x["name"] == "base" and x["window"] == r["window"] and x["slip"] == r["slip"] and x["mode"] == r["mode"])
            if r["net_pnl"] <= 0 or r["net_pnl"] <= ref["net_pnl"] or r["minute_close_max_drawdown"] > 10000:
                failures.append({k: r[k] for k in ["window", "slip", "mode", "net_pnl", "minute_close_max_drawdown"]})
        confirmations.append({"name": name, "all_passed": not failures, "failures": failures})
    save("confirmation_verdict.json", {"fixed_initial_candidates": confirmations, "fully_passed": [r["name"] for r in confirmations if r["all_passed"]],
         "warning": "post-training comparisons across already-researched history; never call this independent future validation"})
    # Descriptive factor values of baseline trades, not a substitute for rerunning filters.
    baseline_ts = json.loads((OUT/"continuous_base_trades.json").read_text(encoding="utf-8"))
    bars, ix, _, _ = bundle
    for t in baseline_ts:
        t["signal_factors"] = all_features[t["contract"]][ix[t["contract"]][t["signal_dt"]]]
    save("baseline_trade_factor_observations.json", baseline_ts)
    fields = ["window", "name", "slip", "mode", "trades", "net_pnl", "minute_close_max_drawdown", "profit_factor", "win_rate_pct", "worst_trade", "fees"]
    with (OUT/"comparison.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields); w.writeheader(); w.writerows({k: r[k] for k in fields} for r in results)
    save("verification.json", {"passed": True, "factor_variants_plus_base": len(specs), "core_scenarios": len(results), "prior_baseline_scenarios_reproduced": reference_count,
         "checks": ["all signal factors identical on sampled historical prefixes", "original A baseline exact", "native1lot costs/risk/margin/entry-main/no-overlap inherited assertions", "weekend/holiday positions flat", "training selection saved before each next test", "walk capital carry and minuteDD trace reconciliation"],
         "prior_research_exposure": "40strategy rules under2holding policies were already examined before this35factor batch"})


if __name__ == "__main__":
    main()
