"""Frozen A2 backward check. Explicitly incomplete if older data are unavailable.

No new factor search. Existing source covers 2025-10-20 onward; allow three
sessions of warm-up, report 2025-10-23--12-15 as a PARTIAL four-month extension.
Never imply that these seven-plus weeks cover 2025-08-16--12-15.
"""
import csv
import hashlib
import json
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import research_eg_factors as research
import discover_eg_swing as engine

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "reports" / "eg_a2_older_four_20261019"
FACTOR_OUT = research.OUT
PARTIAL = ("2025-10-23 09:00:00", "2025-12-15 23:59:59")


def save(name, value):
    (OUT/name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    OUT.mkdir(exist_ok=True)
    plan = json.loads((OUT/"frozen_plan.json").read_text(encoding="utf-8"))
    save("partial_execution_plan.json", {
        "written_before_evaluation": datetime.now(engine.extended.ZONE).isoformat(),
        "requested_window": plan["window"], "actually_available_test": PARTIAL,
        "missing_test_dates": ["2025-08-16", "2025-10-22"],
        "reason": "EDB HTTP403 for >1year minute access; first cached minutes2025-10-20;3sessions warmup",
        "variants": ["base", "season_volume_2"], "tuning": False,
        "windows": {"partial": PARTIAL, "previous_reserve": engine.WINDOWS["reserve"],
                    "new_tail": ("2025-11-17 09:00:00", PARTIAL[1])},
        "costs": [1, 2, 3], "paths": ["ohlc", "olhc"],
        "inputs_sha256": {s: hashlib.sha256((ROOT/"scripts"/s).read_bytes()).hexdigest() for s in
                          ["extend_eg_a2_older.py", "research_eg_factors.py", "discover_eg_swing.py"]},
    })
    research.OUT = OUT
    specs = [s for s in research.specifications() if s["id"] in ["base", "season_volume_2"]]
    bundle, _ = research.prepare(specs)
    cfg = plan["base_configuration"]
    rows = []; equity = {}; all_trades = {}
    windows = {"partial": PARTIAL, "previous_reserve": engine.WINDOWS["reserve"],
               "new_tail": ("2025-11-17 09:00:00", PARTIAL[1]),
               "original_ten": engine.WINDOWS["continuous"]}
    prior_core = json.loads((FACTOR_OUT/"all_core_results.json").read_text(encoding="utf-8"))
    prior_stress = json.loads((FACTOR_OUT/"three_tick_audit.json").read_text(encoding="utf-8"))
    reproduced = 0
    for window, span in windows.items():
        for spec in specs:
            name = spec["id"]
            for slip in [1, 2, 3]:
                for mode in ["ohlc", "olhc"]:
                    s, ts, curve = engine.run(bundle, {**cfg, "name": name, "family": name}, span, slip, mode)
                    s["window"] = window
                    if window in ["previous_reserve", "original_ten"]:
                        w = "reserve" if window == "previous_reserve" else "continuous"
                        ref = next(x for x in (prior_stress if slip == 3 else prior_core)
                                   if x["window"] == w and x["name"] == name and x["slip"] == slip and x["mode"] == mode)
                        for key in ["net_pnl", "minute_close_max_drawdown", "trades", "fees", "worst_trade"]:
                            assert s[key] == ref[key], (window, name, slip, mode, key)
                        reproduced += 1
                    rows.append(s)
                    save(f"{window}_{name}_slip{slip}_{mode}_trades.json", ts)
                    if mode == "ohlc":
                        with (OUT/f"{window}_{name}_slip{slip}_equity.csv").open("w", encoding="utf-8-sig", newline="") as f:
                            w = csv.DictWriter(f, fieldnames=["dt", "equity", "qty"]); w.writeheader(); w.writerows(curve)
                    if window == "partial" and slip == 1 and mode == "ohlc":
                        equity[name] = curve; all_trades[name] = ts
                    print(window, name, slip, mode, s["net_pnl"], "DD", s["minute_close_max_drawdown"], flush=True)
    save("all_results.json", rows)
    monthly = []
    bars, ix, mapping, signals = bundle
    for name in ["base", "season_volume_2"]:
        levels = {}; ts = all_trades[name]
        for e in equity[name]: levels[e["dt"][:7]] = e["equity"]
        prev = 0.
        for month, level in levels.items():
            cohort = [t for t in ts if t["entry_dt"].startswith(month)]
            market = []
            for b in bars["main"]:
                if PARTIAL[0] <= b["dt"] <= PARTIAL[1] and b["dt"].startswith(month):
                    sym = mapping[b["dt"]]; market.append(bars[sym][ix[sym][b["dt"]]])
            monthly.append({"name": name, "month": month, "net_equity_change": level-prev,
                            "entry_cohort_net": sum(t["net_pnl"] for t in cohort), "trades": len(cohort),
                            "median_main_atr20": statistics.median(b["atr"] for b in market),
                            "median_main_bar_range": statistics.median(b["h"]-b["l"] for b in market)})
            prev = level
        assert abs(sum(x["net_equity_change"] for x in monthly if x["name"] == name)-sum(t["net_pnl"] for t in ts)) < 1e-8
    save("monthly_partial.json", monthly)
    save("verification.json", {"passed": True, "scenarios": len(rows), "prior_results_exactly_reproduced": reproduced,
         "full_requested_four_months_completed": False, "partial_window": PARTIAL,
         "checks": ["unchanged baseline and A2", "sampled prefix causality", "monthly equity/trades reconciliation",
                    "one-lot risk and margin", "calendar flat assertions", "original10months and reserve exact"]})


if __name__ == "__main__":
    main()
