"""Old wide exits, with user-fixed 15:00/23:00 liquidation, lunch retained.

Uses the same immutable ten-month EDB snapshots and verified concrete-contract
engine as the prior report. No strategy parameter selection or production edits.
"""
import json
import statistics
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import audit_eg_15m as audit
import research_eg_candidates as research
import extend_eg_backtest as extended
import analyze_eg_ten_months as ten

OUT = audit.ROOT / "reports" / "eg_session_flat_20261019"
TIMES = ["15:00:00", "23:00:00"]
WINDOWS = {**ten.WINDOWS, "continuous_six": ("2026-04-16 09:00:00", ten.END)}
PRIOR = json.loads((ten.OUT / "all_results.json").read_text(encoding="utf-8"))


def save(name, value):
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def config(name):
    cfg = ten.configuration("baseline")
    cfg["name"] = name
    if name != "original":
        cfg["flat_times"] = TIMES
        cfg["cancel_signals_at_flat_time"] = name == "flat_cancel_close_signal"
    return cfg


def verify_positions(trades, curve, enforced, bars=None, indices=None, slip=None):
    if not enforced:
        return
    assert all(e["qty"] == 0 for e in curve if e["dt"][11:19] in TIMES)
    for t in trades:
        a, z = datetime.fromisoformat(t["entry_dt"]), datetime.fromisoformat(t["exit_dt"])
        day = a.replace(hour=0, minute=0, second=0)
        while day <= z:
            for h in [15, 23]:
                boundary = day.replace(hour=h)
                assert not a < boundary < z, (t, boundary)
            day += timedelta(days=1)
        for leg in t["legs"]:
            if leg["reason"] == "session_flat":
                assert leg["dt"][11:19] in TIMES
                actual = bars[t["contract"]][indices[t["contract"]][leg["dt"]]]
                assert leg["price"] == actual["c"]-t["direction"]*slip
    assert not any(any(l["reason"] == "terminal" for l in t["legs"]) for t in trades)


def extra_stats(stats, trades):
    stats["session_flat_trades"] = sum(any(l["reason"] == "session_flat" for l in t["legs"]) for t in trades)
    stats["session_flat_lots"] = sum(l["qty"] for t in trades for l in t["legs"] if l["reason"] == "session_flat")
    stats["fees"] = sum(t["fees"] for t in trades)
    stats["gross_pnl_before_fees"] = sum(t["gross_pnl"] for t in trades)
    stats["median_holding_elapsed_minutes"] = statistics.median((datetime.fromisoformat(t["exit_dt"])-datetime.fromisoformat(t["entry_dt"])).total_seconds()/60 for t in trades) if trades else None


def trade_key(t):
    return t["contract"], t["signal_dt"], t["entry_dt"], t["direction"], t["entry"]


def entry_main_matches(trades, mapping):
    for t in trades:
        d = datetime.fromisoformat(t["entry_dt"])
        parent = d.replace(minute=d.minute//15*15, second=0)
        if d.minute % 15 or d.second:
            parent += timedelta(minutes=15)
        assert mapping[parent.isoformat(sep=" ")] == t["contract"]


def compare_trades(old, new):
    a = {trade_key(t): t for t in old}; b = {trade_key(t): t for t in new}
    assert len(a) == len(old) and len(b) == len(new)
    common = a.keys() & b.keys(); added = b.keys()-a.keys(); removed = a.keys()-b.keys()
    rows = [{"contract": k[0], "signal_dt": k[1], "entry_dt": k[2], "direction": k[3],
             "old_net": a[k]["net_pnl"], "new_net": b[k]["net_pnl"],
             "delta": b[k]["net_pnl"]-a[k]["net_pnl"], "old_exit": a[k]["exit_dt"], "new_exit": b[k]["exit_dt"],
             "new_forced_flat": any(l["reason"] == "session_flat" for l in b[k]["legs"])} for k in sorted(common)]
    summary = {"common_entries": len(common), "common_entry_pnl_delta": sum(r["delta"] for r in rows),
               "new_entries": len(added), "new_entries_net": sum(b[k]["net_pnl"] for k in added),
               "removed_old_entries": len(removed), "removed_old_entries_net": sum(a[k]["net_pnl"] for k in removed)}
    summary["total_pnl_delta"] = summary["common_entry_pnl_delta"]+summary["new_entries_net"]-summary["removed_old_entries_net"]
    assert summary["total_pnl_delta"] == sum(t["net_pnl"] for t in new)-sum(t["net_pnl"] for t in old)
    save("trade_change_attribution.json", {"summary": summary, "common": rows,
         "new_entries": [b[k] for k in sorted(added)], "removed_old_entries": [a[k] for k in sorted(removed)]})
    return summary


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    save("analysis_plan.json", {"created_at": datetime.now(extended.ZONE).isoformat(),
         "user_change": "force remaining position flat at 15:00 and23:00 only; retain lunch and morning recess positions",
         "configs": {n: config(n) for n in ["original", "session_flat", "flat_cancel_close_signal"]},
         "windows": WINDOWS, "data": str(ten.OUT.resolve()),
         "main": "EG0 interpreted as main-continuous; exact same EDB KQ.m@DCE.eg and concrete-member mapping as previous report, not Sina EG0 data",
         "closing_execution": "normal 1m replay through the final minute, then last close minus directional slip for every remaining lot; close price execution is a modeling assumption, not a guaranteed live fill",
         "primary_signal_policy": "retain original next-trading-bar warning validity; a signal generated at15/23 may enter at next session, but there is no open position across those closes",
         "secondary_signal_policy": "separately disclose cancellation of15/23 close-generated warnings; this is an additional change, not mixed into primary comparison",
         "costs": "two actual lots;3 CNY/lot/side;0/1/2ticks on entries, stops and session liquidation, no extra slip on first target resting limit",
         "paths": research.MODES, "no_optimization": True})
    bars, groups, indices, mapping = ten.prepare()
    results = []; data = {}; previous_checks = 0
    old_six = json.loads((extended.OUT / "all_results.json").read_text(encoding="utf-8"))
    with ten.model():
        for w, span in WINDOWS.items():
            for name in ["original", "session_flat"]:
                for slip in [0, 1, 2]:
                    for mode in research.MODES:
                        s, ts, curve = extended.run(bars, groups, indices, mapping, config(name), slip, mode, *span)
                        s["window"] = w
                        extra_stats(s, ts); results.append(s)
                        verify_positions(ts, curve, name == "session_flat", bars, indices, slip)
                        entry_main_matches(ts, mapping)
                        if name == "original":
                            refs = PRIOR if w != "continuous_six" else old_six
                            ref = next(r for r in refs if r["window"] == (w if w != "continuous_six" else "six_months") and r["name"] == ("baseline" if w != "continuous_six" else "wider_exits") and r["slip"] == slip and r["mode"] == mode)
                            for k in ["trades", "wins", "losses", "net_pnl", "max_drawdown", "gap_stop_trades"]:
                                assert s[k] == ref[k], (w, slip, mode, k, s[k], ref[k])
                            previous_checks += 1
                        label = f"{w}_{name}_slip{slip}_{mode}"
                        save(label + "_trades.json", ts)
                        if slip == 1 and mode == "adverse_first":
                            research.csv_save(str(OUT/(label + "_trades.csv")), ts)
                            research.csv_save(str(OUT/(label + "_equity.csv")), curve)
                            data[(w, name)] = (s, ts, curve)
                            print(w, name, s["trades"], s["net_pnl"], "DD", s["max_drawdown"], "flat", s["session_flat_trades"], "gaps", s["gap_stop_trades"], flush=True)
        # Signal-cancellation policy is a separately labeled sensitivity, fixed in advance.
        for slip in [1, 2]:
            for mode in research.MODES:
                s, ts, curve = extended.run(bars, groups, indices, mapping, config("flat_cancel_close_signal"), slip, mode, *WINDOWS["ten_months"])
                s["window"] = "ten_months"; extra_stats(s, ts); results.append(s)
                verify_positions(ts, curve, True, bars, indices, slip); entry_main_matches(ts, mapping)
                label = f"ten_months_flat_cancel_close_signal_slip{slip}_{mode}"
                save(label + "_trades.json", ts)
    save("all_results.json", results)
    keys = ["window", "name", "slip", "mode", "trades", "wins", "losses", "net_pnl", "profit_factor", "max_drawdown", "worst_trade", "gap_stop_trades", "session_flat_trades", "fees"]
    research.csv_save(str(OUT / "comparison.csv"), [{k: s[k] for k in keys} for s in results])
    monthly = {}
    for name in ["original", "session_flat"]:
        s, ts, curve = data[("ten_months", name)]
        monthly[name] = ten.monthly_attribution(bars, indices, mapping, ts, curve)
    rows = []
    for old, new in zip(monthly["original"], monthly["session_flat"]):
        assert old["month"] == new["month"]
        rows.append({"month": old["month"], "original_trades": old["trades"], "flat_trades": new["trades"],
                     "original_entry_month_net": old["cohort_net_pnl"], "flat_entry_month_net": new["cohort_net_pnl"],
                     "entry_month_delta": new["cohort_net_pnl"]-old["cohort_net_pnl"],
                     "original_calendar_equity_change": old["month_close_equity_change"], "flat_calendar_equity_change": new["month_close_equity_change"],
                     "original_gap_trades": old["gap_stop_trades"], "flat_gap_trades": new["gap_stop_trades"],
                     "original_gap_extra_loss": old["extra_loss_from_gaps"], "flat_gap_extra_loss": new["extra_loss_from_gaps"]})
    save("monthly_comparison.json", rows); research.csv_save(str(OUT / "monthly_comparison.csv"), rows)
    save("full_monthly_attribution.json", monthly)
    attrib = compare_trades(data[("ten_months", "original")][1], data[("ten_months", "session_flat")][1])
    save("verification.json", {"passed": True, "scenarios": len(results), "prior_scenarios_reproduced": previous_checks,
         "checks": ["all original scenarios match saved results", "zero remaining position at every15:00/23:00 close", "no position crosses forced-close boundary", "all entries match supplier main", "old two-lot/fee/equity/monthly accounting checks", "entry-change PnL reconciliation"], "entry_change_attribution": attrib})


if __name__ == "__main__":
    main()
