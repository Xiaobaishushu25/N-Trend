"""Independent execution checks and post-screen diagnostics, not more optimization."""
import csv
import json
import random
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime

import audit_eg_15m as audit
import discover_eg_swing as discovery

OUT = discovery.OUT
EXPLORATORY = "failed_break_15m_r1_trail2ATR"


def independent_walker_check():
    rng = random.Random(20261019)
    saved = {k: getattr(audit, k) for k in ["close_leg", "favorable"]}

    def close(p, price, qty, reason, dt, slip):
        fill = price if reason == "target" else price-p["direction"]*slip
        p["legs"].append({"price": fill, "reason": reason})
        p["qty"] -= qty

    def favorable(p, price, dt, slip):
        if p["target_absolute"] is not None and (price-p["target_absolute"])*p["direction"] >= 0:
            close(p, p["target_absolute"], 1, "target", dt, slip)

    tested = 0
    try:
        audit.close_leg, audit.favorable = close, favorable
        for _ in range(1500):
            d = rng.choice([-1, 1]); entry = 5000
            target = entry+d*rng.randint(20, 100) if rng.random() < .5 else None
            stop = entry+d*(rng.randint(-60, -10) if target is not None else rng.randint(-60, 80))
            p = {"entry": entry, "direction": d, "stop": stop, "target": target}
            o = rng.randint(4900, 5100); h = o+rng.randint(0, 100); l = o-rng.randint(0, 100); c = rng.randint(l, h)
            b = {"dt": "2026-01-01 10:01:00", "o": o, "h": h, "l": l, "c": c}
            for mode in ["ohlc", "olhc"]:
                slip = rng.randint(0, 3)
                custom = discovery.minute_exit(p, b, mode, slip)
                original_p = {"entry": entry, "direction": d, "stop": (stop-entry)*d, "target_absolute": target,
                              "qty": 1, "legs": [], "peak": 0, "gap_stop": False}
                _, original = audit.simulate_bar(original_p, None, b, mode, slip)
                assert (custom is None) == (original is None), (p, b, custom, original)
                if custom is not None:
                    leg = original["legs"][-1]
                    assert (custom["exit"], custom["exit_reason"]) == (leg["price"], leg["reason"])
                tested += 1
    finally:
        for k, v in saved.items():
            setattr(audit, k, v)
    return tested


def main():
    checks = independent_walker_check()
    bundles = discovery.prepare()
    cfg = next(c for c in discovery.configs() if c["name"] == EXPLORATORY)
    prefix = "exploratory" if EXPLORATORY.startswith("failed_break") else "channel"
    available = json.loads((OUT/"all_results.json").read_text(encoding="utf-8"))
    stress = []
    for w, span in discovery.WINDOWS.items():
        for mode in ["ohlc", "olhc"]:
            s = next((r for r in available if r["name"] == EXPLORATORY and r["window"] == w and r["slip"] == 3 and r["mode"] == mode), None)
            if s is None:
                s, _, _ = discovery.run(bundles[cfg["tf"]], cfg, span, 3, mode)
                s["window"] = w
            stress.append(s)
    discovery.save(prefix+"_3tick_stress.json", {"name": EXPLORATORY, "role": "additional audit without parameter changes; training selection status is in training_selection.json", "results": stress})
    ts = json.loads((OUT/f"continuous_{EXPLORATORY}_trades.json").read_text(encoding="utf-8"))
    groups = defaultdict(list)
    for t in ts:
        groups[t["entry_dt"][:7]].append(t)
    months = []
    for month, trades in sorted(groups.items()):
        months.append({"month": month, "trades": len(trades), "net_pnl": sum(t["net_pnl"] for t in trades),
                       "wins": sum(t["net_pnl"] > 0 for t in trades), "gap_trades": sum(t["gap_stop"] for t in trades)})
    discovery.save(prefix+"_monthly.json", months)
    # Equity-change weeks include mark-to-market on held positions, not entry cohorts.
    with (OUT/f"continuous_{EXPLORATORY}_equity.csv").open(encoding="utf-8-sig") as f:
        curve = list(csv.DictReader(f))
    weeks = {}
    for e in curve:
        d = datetime.fromisoformat(e["dt"]).isocalendar()
        weeks[f"{d.year}-W{d.week:02d}"] = float(e["equity"])
    increments = []; prev = 0.
    for v in weeks.values():
        increments.append(v-prev); prev = v
    rng = random.Random(20261019); bootstrap = []; n = len(increments)
    for _ in range(3000):
        sample = []
        while len(sample) < n:
            j = rng.randrange(n)
            sample.extend([increments[j], increments[(j+1)%n]])
        bootstrap.append(statistics.mean(sample[:n]))
    bootstrap.sort()
    vals = [t["net_pnl"] for t in ts]; streak = longest = 0
    for v in vals:
        streak = streak+1 if v < 0 else 0; longest = max(longest, streak)
    details = {"name": EXPLORATORY, "independent_minute_walker_cases": checks, "weeks": n,
               "mean_weekly_pnl": statistics.mean(increments), "block_bootstrap_weekly_mean_95pct": [bootstrap[75], bootstrap[2924]],
               "bootstrap_note": "circular2-week block bootstrap3000 samples; descriptive uncertainty on a post-screen selected configuration, not selection-corrected significance or future guarantee",
               "max_consecutive_losses": longest, "positive_months": sum(m["net_pnl"] > 0 for m in months), "months": len(months),
               "top3_trade_profit": sum(sorted(vals, reverse=True)[:3]), "max_margin_assumption25pct": max(t["margin_estimate"] for t in ts),
               "risk_range_CNY": [min(t["planned_stop_loss_with_cost"] for t in ts), max(t["planned_stop_loss_with_cost"] for t in ts)],
               "max_elapsed_holding_hours": max((datetime.fromisoformat(t["exit_dt"])-datetime.fromisoformat(t["entry_dt"])).total_seconds()/3600 for t in ts),
               "exit_reasons": dict(Counter(t["exit_reason"] for t in ts)),
               "worst_gap": sorted([t for t in ts if t["gap_stop"]], key=lambda t: t["net_pnl"])}
    discovery.save(prefix+"_audit.json", details)
    print(json.dumps(details, ensure_ascii=False, indent=2))
    print("3tick continuous", [(r["net_pnl"], r["minute_close_max_drawdown"]) for r in stress if r["window"] == "continuous"])


if __name__ == "__main__":
    if "--calendar" in sys.argv:
        import discover_eg_swing_calendar
        discover_eg_swing_calendar.configure()
        OUT = discovery.OUT
    if "--name" in sys.argv:
        EXPLORATORY = sys.argv[sys.argv.index("--name")+1]
    main()
