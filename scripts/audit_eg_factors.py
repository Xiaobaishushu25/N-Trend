"""Post-screen volume-denominator and main-roll sensitivity; no winner selection."""
import json
import statistics
from collections import defaultdict

import research_eg_factors as research
import discover_eg_swing as engine

OUT = research.OUT


def main():
    specs = research.specifications()
    bundle, fs = research.prepare(specs)
    bars, ix, mapping, signals = bundle
    history = defaultdict(list); main_ratio = {}; roll_age = {}; previous_sym = previous_session = None; age = 999
    for mb in bars["main"]:
        dt = mb["dt"]; slot = dt[11:16]
        sym = mapping.get(dt)
        own = bars[sym][ix[sym][dt]] if sym else mb
        past = history[slot][-10:]
        main_ratio[dt] = own["v"]/statistics.median(past) if len(past) >= 3 and statistics.median(past) else None
        history[slot].append(own["v"])
        if sym:
            if previous_sym is not None and sym != previous_sym:
                age = 0; previous_session = own["session"]
            elif own["session"] != previous_session:
                age += 1
            roll_age[dt] = age
            previous_sym, previous_session = sym, own["session"]
    variants = ["main_volume15", "same_volume_skip5sessions", "same_volume_skip10sessions"]
    for sym, bs in bars.items():
        if sym == "main": continue
        for name in variants:
            out = []
            for i, b in enumerate(bs):
                d = signals[(sym, "base")][i]
                dt = b["dt"]
                if name == "main_volume15":
                    v = main_ratio.get(dt); keep = v is not None and v >= 1.5
                else:
                    v = fs[sym][i]["season_volume"]
                    keep = v is not None and v >= 1.5 and roll_age.get(dt, 999) >= (5 if name == "same_volume_skip5sessions" else 10)
                out.append(d if d and keep else 0)
            signals[(sym, name)] = out
    base_cfg = json.loads((OUT/"analysis_plan.json").read_text(encoding="utf-8"))["base_configuration"]
    windows = {"initial_train": research.FOLDS[0]["train"], "validate_four": engine.WINDOWS["validate"],
               "later": engine.WINDOWS["later"], "continuous": engine.WINDOWS["continuous"], "reserve": engine.WINDOWS["reserve"]}
    rows = []
    for name in variants:
        cfg = {**base_cfg, "name": name, "family": name}
        for w, span in windows.items():
            for slip in [1, 2, 3]:
                for mode in ["ohlc", "olhc"]:
                    s, ts, _ = engine.run(bundle, cfg, span, slip, mode)
                    s["window"] = w; rows.append(s)
                    if w == "continuous" and slip == 1 and mode == "ohlc":
                        research.save("audit_"+name+"_trades.json", ts)
        print(name, [(r["slip"], r["net_pnl"], r["minute_close_max_drawdown"]) for r in rows if r["name"] == name and r["window"] == "continuous" and r["mode"] == "ohlc"], flush=True)
    original = json.loads((OUT/"continuous_season_volume_2_trades.json").read_text(encoding="utf-8"))
    cohort = []
    for label, pred in [("first10sessions_after_main_roll", lambda x: x < 10), ("other_sessions", lambda x: x >= 10)]:
        ts = [t for t in original if pred(roll_age[t["signal_dt"]])]
        cohort.append({"cohort": label, "trades": len(ts), "net_pnl": sum(t["net_pnl"] for t in ts)})
    research.save("volume_roll_audit.json", {"role": "post-screen robustness diagnostics, not extra declared selection candidates; no choice made by these results",
         "motivation": "same-contract historical volume before a main roll can be smaller because it was then a non-main contract",
         "main_history": "current concrete main volume / median previous10 same-slot main volumes across members; min3; prior slot observations only",
         "roll_session_definition": "a day or night session; first visible member is not treated as a newly rolled contract",
         "additional_scenarios": len(rows), "results": rows, "original_volume_trade_cohorts": cohort,
         "warning": "these90 diagnostic scenarios are additional research exposure; original34-hypothesis maximum-stat audit does not correct for them"})


if __name__ == "__main__":
    main()
