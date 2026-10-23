"""Frozen offline diagnostics for exits, slippage and opportunity capacity.

No strategy tuning, broker connection, database writes or production configuration.
"""
import argparse
import bisect
import collections
import datetime as dt
import hashlib
import json
import pathlib
import statistics

from report_signal_ablation import jsonl, paired, read_json, stats, write_new


def evaluation(row):
    return row["result"]["evaluation"]


def closed(rows):
    return [r for r in rows if evaluation(r)["status"] == "closed"]


def distribution(values):
    values = sorted(values)
    if not values:
        return dict(n=0)
    return dict(n=len(values), mean=statistics.mean(values), median=statistics.median(values),
                p90=values[min(int(.9 * len(values)), len(values)-1)], min=values[0], max=values[-1],
                above_1_tick=sum(x > 1 + 1e-6 for x in values), above_2_ticks=sum(x > 2 + 1e-6 for x in values))


def worst_end(row, symbols):
    times = [b["datetime"] for b in symbols[row["symbol"]]["bars"] if int(b["datetime"][14:16]) % 15 == 0]
    index = bisect.bisect_right(times, row["warning_ts"]) + 71
    return times[index] if index < len(times) else symbols[row["symbol"]]["bars"][-1]["datetime"]


def occupancy(row, symbols, start_mode):
    e = evaluation(row)
    uncertain = e["status"] in ("ambiguous", "missing_data", "rollover", "unfinished", "pending")
    end = worst_end(row, symbols) if uncertain else e["exit_ts"]
    if start_mode == "warning":
        return row["warning_ts"], end, uncertain
    # Entry-only is a capacity sensitivity, not a broker replay with retry queues.
    start = e["entry_ts"]
    if start is None and uncertain:
        # Ambiguous entry can exist in one path only: reserve from first possible
        # entry, or warning if unavailable. Do not silently free unknown exposure.
        paths = row["result"].get("disagreement") or {}
        possible = [v.get("entry_ts") for v in paths.values() if isinstance(v, dict) and v.get("entry_ts")]
        start = min(possible) if possible else row["warning_ts"]
    return start, end, uncertain


def capacity(rows, symbols, start_mode="warning", slots=1, reverse_ties=False):
    intervals = [(occupancy(r, symbols, start_mode), r) for r in rows]
    intervals = [(i, r) for i, r in intervals if i[0] is not None]
    # Stable arbitrary tie order; reverse is a sensitivity, never choose best P&L.
    intervals.sort(key=lambda x: x[1]["event_key"], reverse=reverse_ties)
    intervals.sort(key=lambda x: (x[0][0], x[1]["warning_ts"]))
    active = collections.defaultdict(list)
    accepted, blocked, ledger = [], [], []
    for (start, end, uncertain), row in intervals:
        symbol = row["symbol"]
        active[symbol] = [(stop, key) for stop, key in active[symbol] if stop >= start]
        reject = len(active[symbol]) >= slots
        blockers = [key for _, key in active[symbol]]
        (blocked if reject else accepted).append(row)
        ledger.append(dict(event_key=row["event_key"], symbol=symbol, start=start, end=end,
                           uncertainty_reserved=uncertain, accepted=not reject, blockers=blockers))
        if not reject:
            if end is None or end < start:
                raise ValueError("invalid occupation interval")
            active[symbol].append((end, row["event_key"]))
    return dict(accepted=stats(accepted), blocked=stats(blocked),
                ignored_without_entry=len(rows)-len(intervals),
                blocked_by_unknown=sum(not x["accepted"] and any(
                    next(y for y in ledger if y["event_key"] == k)["uncertainty_reserved"] for k in x["blockers"])
                    for x in ledger), ledger=ledger)


def overlap_profile(rows, symbols):
    events = []
    for row in rows:
        start, end, unknown = occupancy(row, symbols, "entry")
        if start is not None:
            events.extend([(start, 0, row), (end, 1, row)])
    active = collections.defaultdict(dict)
    peak_symbol, peak_account, peak_initial_risk = {}, 0, 0.
    opposite = set()
    for _, action, row in sorted(events, key=lambda x: (x[0], x[1], x[2]["event_key"])):
        symbol, key = row["symbol"], row["event_key"]
        if action == 0:
            active[symbol][key] = row
            if len({r["direction"] for r in active[symbol].values()}) > 1:
                opposite.add(key)
            peak_symbol[symbol] = max(peak_symbol.get(symbol, 0), len(active[symbol]))
            peak_account = max(peak_account, sum(len(v) for v in active.values()))
            risk = sum(evaluation(r)["risk"] * evaluation(r)["volume"] * r["multiplier"]
                       for v in active.values() for r in v.values() if evaluation(r)["fill"] is not None)
            peak_initial_risk = max(peak_initial_risk, risk)
        else:
            active[symbol].pop(key, None)
    return dict(peak_by_symbol=peak_symbol, peak_signals_all_symbols=peak_account,
                peak_sum_initial_stop_risk_yuan=peak_initial_risk,
                entries_while_opposite_direction_present=len(opposite),
                note="5m intervals, unknown exposure conservatively reserved; initial risk is not actual floating loss/margin")


def exits(cases):
    actual, fixed, s1 = [cases[p + "/nf5/slip1_fee"] for p in ("actual_tier", "fixed_one_r", "original_s1")]
    common = sorted(k for k in actual.keys() & fixed.keys() if evaluation(actual[k])["status"] == evaluation(fixed[k])["status"] == "closed")
    groups = {}
    for label, keys in (("small", [k for k in common if actual[k]["score"] < 3.5]),
                        ("large", [k for k in common if actual[k]["score"] >= 3.5])):
        groups[label] = paired({k: fixed[k] for k in keys}, {k: actual[k] for k in keys})
    small_changes = [k for k in common if actual[k]["score"] < 3.5 and any(
        evaluation(actual[k])[field] != evaluation(fixed[k])[field]
        for field in ("entry_ts", "exit_ts", "exit_reason", "fill", "gross_points", "net_amount"))]
    # Under the frozen 1/2-lot config, small-tier policy must equal fixed 1R.
    if small_changes:
        raise ValueError("small policy differs from fixed 1R: " + str(small_changes[:3]))
    groups["small_identical_checks"] = len([k for k in common if actual[k]["score"] < 3.5])
    transitions = collections.defaultdict(lambda: dict(n=0, delta_r=0., delta_per_initial_lot_yuan=0.))
    for k in common:
        if actual[k]["score"] < 3.5:
            continue
        a, b = evaluation(fixed[k]), evaluation(actual[k])
        g = transitions[a["exit_reason"] + " -> " + b["exit_reason"]]
        g["n"] += 1
        g["delta_r"] += b["net_r"] - a["net_r"]
        g["delta_per_initial_lot_yuan"] += b["net_amount"] / b["volume"] - a["net_amount"] / a["volume"]
    groups["large_transitions_fixed_to_actual"] = dict(transitions)
    legs = collections.defaultdict(lambda: dict(n=0, gross_yuan=0., allocated_net_yuan=0.))
    first_count = 0
    for row in closed(list(actual.values())):
        e, fills = evaluation(row), row["result"]["fills"]
        if e["volume"] <= 1:
            continue
        partial = len(fills) >= 3 and fills[1]["reason"] == "target_1r"
        first_count += partial
        for index, fill in enumerate(fills[1:]):
            label = "first_1r" if partial and index == 0 else "runner/" + fill["reason"] if partial else "before_first/" + fill["reason"]
            # Allocate total commission pro rata to closed volume for additive
            # attribution only; individual fee-leg truth remains in original rows.
            gross = (fill["price"] - fills[0]["price"]) * (1 if row["direction"] == "up" else -1) * fill["volume"] * row["multiplier"]
            legs[label]["n"] += 1
            legs[label]["gross_yuan"] += gross
            legs[label]["allocated_net_yuan"] += gross - row["cost_repricing"]["commission"] * fill["volume"] / e["volume"]
    groups["large_leg_attribution"] = dict(legs)
    groups["large_signals_first_profit_taken"] = first_count
    groups["s1_vs_fixed_same_closed"] = paired(s1, fixed)
    groups["s1_vs_actual_same_closed"] = paired(s1, actual)
    groups["actual_nofollow_off_same_closed"] = paired(actual, cases["actual_tier/nf_off/slip1_fee"])
    return groups


def slippage(cases):
    result = {}
    for policy in ("original_s1", "fixed_one_r", "actual_tier"):
        maps = [cases[f"{policy}/nf5/{cost}"] for cost in ("zero", "slip1_fee", "slip2_fee")]
        keys = sorted(k for k in maps[0] if all(evaluation(m[k])["status"] == "closed" for m in maps))
        same = [stats([m[k] for k in keys]) for m in maps]
        transitions = {}
        for i in (0,1):
            counts = collections.Counter((evaluation(maps[i][k])["status"], evaluation(maps[i+1][k])["status"]) for k in maps[i])
            transitions[f"{i}_to_{i+1}"] = {a+" -> "+b: n for (a,b),n in sorted(counts.items())}
        result[policy] = dict(common_closed=len(keys), same_sample_0_1_2=same,
            all_candidates_status_transitions=transitions,
            same_sample_entry_time_changes_0_to_1=sum(evaluation(maps[0][k])["entry_ts"] != evaluation(maps[1][k])["entry_ts"] for k in keys),
            same_sample_exit_time_changes_0_to_1=sum(evaluation(maps[0][k])["exit_ts"] != evaluation(maps[1][k])["exit_ts"] for k in keys))
    return result


def timestamp(value):
    for fmt in ("%Y%m%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return dt.datetime.strptime(value, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    raise ValueError("unrecognized archived trade timestamp")


def archive_evidence(archive, ticks):
    history = read_json(archive / "trade-history.json")["data"]
    snapshot = read_json(archive / "execution-snapshot.json")["data"]
    groups = collections.defaultdict(list)
    for row in history:
        groups[row["condition_order_id"]].append(row)
    offsets, excluded, ledger = [], collections.Counter(), []
    for key, rows in groups.items():
        known = [(r["fill_price"], r["planned_entry_price"], r["symbol"], r["direction"])
                 for r in rows if r.get("planned_entry_price") is not None]
        if len(set(known)) != 1:
            excluded["missing_or_inconsistent_entry_basis"] += 1
            continue
        fill, planned, symbol, direction = known[0]
        if symbol not in ticks:
            excluded["symbol_not_in_frozen_tick_metadata"] += 1
            continue
        adverse = (fill-planned) * (1 if direction == "up" else -1) / ticks[symbol]
        offsets.append(adverse)
        ledger.append(dict(condition_id=key, symbol=symbol, adverse_entry_from_plan_ticks=adverse,
                           open_ts=timestamp(rows[0]["open_ts"]), close_legs=len(rows)))
    leg_offsets = collections.defaultdict(list)
    for r in history:
        if r["symbol"] not in ticks:
            continue
        sign = 1 if r["direction"] == "up" else -1
        reference = (r["fill_price"] + sign * r["risk"] if r["exit_reason"] == "target_1r"
                     else r["initial_stop_price"] if r["exit_reason"] == "stop" else None)
        if reference is not None:
            leg_offsets[r["exit_reason"]].append((reference-r["exit_price"]) * sign / ticks[r["symbol"]])
    active_positions = [p for p in snapshot.get("managed_positions",[]) if not p["is_closed"]]
    return dict(closed_legs=len(history), unique_conditions=len(groups), entry_vs_plan=distribution(offsets),
                excluded=dict(excluded), entry_ledger=ledger,
                exit_vs_rule_reference={k:distribution(v) for k,v in leg_offsets.items()},
                active_snapshot_positions=len(active_positions),
                observation_start=min(timestamp(r["open_ts"]) for r in history),
                observation_end=max(timestamp(r["exit_ts"]) for r in history),
                note="Archive may mix execution versions; entry-plan offset includes spread, gap and delay, not pure arrival-price slippage. No fill-time bid/ask or complete fee ledger; cannot calibrate actual market impact.")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(input_path, batch, archive, output):
    input_path, batch, archive, output = map(pathlib.Path, (input_path,batch,archive,output))
    if output.exists():
        raise FileExistsError("output must be a new frozen batch")
    source, cost_summary = read_json(input_path), read_json(batch / "summary.json")
    if digest(input_path) != cost_summary["input_sha256"]:
        raise ValueError("input hash mismatch")
    cases = collections.defaultdict(dict)
    ticks = {}
    for row in jsonl(batch / "evaluations.jsonl"):
        if row["event_key"] in cases[row["scenario"]]:
            raise ValueError("duplicate opportunity")
        cases[row["scenario"]][row["event_key"]] = row
        ticks[row["symbol"]] = row["tick_size"]
    output.mkdir()
    result = dict(exit=exits(cases), slippage=slippage(cases), capacity={}, overlap={},
                  archive=archive_evidence(archive,ticks),
                  source_hashes={str(p):digest(p) for p in [input_path, batch/"summary.json", batch/"evaluations.jsonl",
                    archive/"trade-history.json", archive/"execution-snapshot.json", pathlib.Path(__file__),
                    pathlib.Path(__file__).with_name("report_signal_ablation.py")]},
                  qualified_for_trading=False, source="historical_development",
                  limitations=["Public fee snapshot, not historical actual bills; unchanged from previous research.",
                    "No L1/tick fill path, order queue or margin simulation. No claim of actual account P&L.",
                    "Capacity constraints are sensitivities; current live code does not impose one-slot-per-symbol.",
                    "Entry-only capacity rejects a blocked first trigger, does not simulate later retries.",
                    "5m equal timestamps are conservatively treated as overlapping; reverse ties reported separately.",
                    "No parameter search or model retraining; all historical data already used in development."])
    with (output / "capacity-ledger.jsonl").open("x",encoding="utf-8") as f:
        for policy in ("original_s1", "actual_tier"):
            for cost in ("zero", "slip1_fee", "slip2_fee"):
                name = f"{policy}/nf5/{cost}"
                rows = list(cases[name].values())
                result["overlap"][name] = overlap_profile(rows, source["symbols"])
                variants = {}
                for mode in ("warning", "entry"):
                    for slots in (1,2):
                        for reverse in (False,True):
                            label = f"{mode}/slots{slots}/{'reverse' if reverse else 'forward'}"
                            diagnostic = capacity(rows,source["symbols"],mode,slots,reverse)
                            for entry in diagnostic.pop("ledger"):
                                f.write(json.dumps(dict(scenario=name,capacity=label,**entry),ensure_ascii=False)+"\n")
                            variants[label] = diagnostic
                result["capacity"][name] = variants
    write_new(output/"summary.json",result)
    print(json.dumps({"exit":result["exit"],"slippage":{k:dict(common_closed=v["common_closed"],
        results=[{x:s[x] for x in ("n","win_rate","net_amount","mean_net_r")} for s in v["same_sample_0_1_2"]]) for k,v in result["slippage"].items()},
        "archive":{k:v for k,v in result["archive"].items() if k!='entry_ledger'}},ensure_ascii=False,indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("input","batch","archive","output"):
        parser.add_argument(name)
    args = parser.parse_args()
    run(args.input,args.batch,args.archive,args.output)
