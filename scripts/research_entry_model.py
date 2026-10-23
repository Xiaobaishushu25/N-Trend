"""Frozen, purged walk-forward S1 entry-model experiment. No production writes.

Train only on one-tick, independent, completed S1 trades. Predict every warning;
reuse the exact same decisions under two-tick execution, then rerun capacity.
"""
import argparse
import collections
import datetime as dt
import json
import math
import pathlib
import random

from entry_model import CONFIG, CONTRACT, FEATURE_GROUPS, completed_context, features, fit_bundle, geometry, predict, price_limit
from report_signal_ablation import jsonl, key, read_json, stats
from report_signal_search import distribution
from reprice_signal_costs import TradingDates, reprice
from validate_execution_research import capacity, digest, evaluation


SCENARIO = "score3_friction20"
CUTS = ["2026-08-17 00:00:00", "2026-09-07 00:00:00", "2026-09-28 00:00:00", "2026-10-23 00:00:00"]
UNKNOWN = {"ambiguous", "missing_data", "rollover", "unfinished", "pending"}


def dump(path, data):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)


def write_rows(path, rows):
    with path.open("x", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")


def accepted_rows(rows, symbols):
    result = capacity(rows, symbols, "entry", 1)
    keys = {line["event_key"] for line in result["ledger"] if line["accepted"]}
    return result, [r for r in rows if r["event_key"] in keys]


def observed_training(accepted, snapshots, cutoff):
    output = []
    for row in accepted:
        e = evaluation(row)
        if row["warning_ts"] >= cutoff or e["status"] != "closed" or not e["exit_ts"] or e["exit_ts"] >= cutoff:
            continue
        if e["net_r"] is None or not math.isfinite(e["net_r"]):
            raise ValueError("closed training return missing")
        if not row["warning_ts"] < e["entry_ts"] <= e["exit_ts"]:
            raise ValueError("invalid training chronology")
        output.append(dict(event_key=row["event_key"], warning_ts=row["warning_ts"],
                           entry_ts=e["entry_ts"], exit_ts=e["exit_ts"], net_r=e["net_r"],
                           features=snapshots[row["event_key"]]["features"]))
    return sorted(output, key=lambda r: (r["warning_ts"], r["event_key"]))


def classification(rows, predictions, prior):
    closed = [row for row in rows if evaluation(row)["status"] == "closed"]
    pairs = [(predictions[row["event_key"]], int(evaluation(row)["net_r"] > 0)) for row in closed]
    if not pairs:
        return dict(n=0)
    def brier(name):
        return sum((p[name]-y)**2 for p, y in pairs) / len(pairs)
    baseline = sum((prior-y)**2 for _, y in pairs) / len(pairs)
    positives = [p["p_win"] for p, y in pairs if y]
    negatives = [p["p_win"] for p, y in pairs if not y]
    auc = sum((p > q) + .5*(p == q) for p in positives for q in negatives) / (len(positives)*len(negatives)) if positives and negatives else None
    bands = {}
    for lo, hi in ((0, .4), (.4, .5), (.5, .6), (.6, 1.01)):
        values = [(p, y) for p, y in pairs if lo <= p["p_win"] < hi]
        bands[f"{lo:.1f}-{min(hi,1):.1f}"] = dict(n=len(values),
            mean_probability=sum(p["p_win"] for p, _ in values)/len(values) if values else None,
            realized_win_rate=sum(y for _, y in values)/len(values) if values else None)
    return dict(n=len(pairs), auc=auc, brier_raw=brier("p_win_raw"), brier=brier("p_win"),
                constant_brier=baseline, brier_skill=1-brier("p_win")/baseline if baseline else None,
                mean_predicted_net_r=sum(p["expected_net_r"] for p, _ in pairs)/len(pairs),
                realized_mean_net_r=sum(evaluation(r)["net_r"] for r in closed)/len(closed), bands=bands)


def summarize_policy(rows, symbols):
    reservation, accepted = accepted_rows(rows, symbols)
    months = collections.defaultdict(list)
    products = collections.defaultdict(list)
    for row in accepted:
        months[row["warning_ts"][:7]].append(row)
        products[row["symbol"]].append(row)
    return dict(independent=stats(rows), accepted=stats(accepted),
                distribution=distribution(accepted), blocked=reservation["blocked"],
                accepted_unknown=sum(evaluation(r)["status"] in UNKNOWN for r in accepted),
                ignored_without_entry=reservation["ignored_without_entry"],
                blocked_by_unknown=reservation["blocked_by_unknown"],
                months={k: stats(v) for k, v in sorted(months.items())},
                symbols={k: stats(v) for k, v in sorted(products.items())}), accepted, reservation["ledger"]


def incremental_interval(base, candidate, all_dates, repetitions=2000):
    """Joint five-observed-date blocks, including dates with zero selected trades.

    Resamples fixed realized ledgers; does not rerun path-dependent capacity in
    bootstrap samples. Descriptive development diagnostic only.
    """
    values = {date: [0.0, 0.0] for date in all_dates}
    for direction, rows in ((-1, base), (1, candidate)):
        for row in rows:
            e = evaluation(row)
            if e["status"] == "closed":
                values[row["warning_ts"][:10]][0] += direction * e["net_r"]
                values[row["warning_ts"][:10]][1] += direction * e["net_amount"]
    series = list(values.values())
    if len(series) < 5:
        return None
    rng, samples = random.Random(20261023), [[], []]
    for _ in range(repetitions):
        drawn = []
        while len(drawn) < len(series):
            start = rng.randrange(len(series)-4)
            drawn.extend(series[start:start+5])
        for axis in (0, 1):
            samples[axis].append(sum(x[axis] for x in drawn[:len(series)]))
    for axis in samples:
        axis.sort()
    return dict(delta_total_net_r=sum(x[0] for x in series),
                delta_net_amount=sum(x[1] for x in series),
                delta_total_net_r_interval95=[samples[0][int(repetitions*q)] for q in (.025, .975)],
                delta_amount_interval95=[samples[1][int(repetitions*q)] for q in (.025, .975)],
                observed_warning_dates=len(series), repetitions=repetitions,
                method="5 observed warning dates, joint symbols, fixed-ledger descriptive blocks; not an eligibility test")


def entry_preview(snapshot, prediction):
    """Human-readable research decision, with no realized label in the payload."""
    g = snapshot["geometry"]
    if not snapshot["rule_warning_eligible"]:
        decision = "空间、成本或基础规则不满足"
    elif prediction["expectancy_accept"]:
        decision = "净期望筛选通过（仅研究）"
    else:
        decision = "等待：预估净期望不为正"
    return dict(event_key=snapshot["event_key"], symbol=snapshot["symbol"],
                decision_at=snapshot["warning_ts"], forecast_fill=g["forecast_fill"],
                stop=snapshot["stop"], target=snapshot["target"],
                structural_entry_price_limit=snapshot["structural_entry_price_limit"],
                price_limit_kind=snapshot["price_limit_kind"],
                planned_stop_risk_yuan=g["forecast_stop_risk_yuan"],
                target_profit_yuan=g["forecast_target_profit_yuan"],
                net_reward_to_stop_loss=g["net_reward_loss"],
                estimated_net_r=prediction["expected_net_r"], p_win=prediction["p_win"],
                break_even_probability=prediction["break_even_probability"],
                payoff_group_n=prediction["payoff_group_n"], research_decision=decision,
                display_status="研究估计，尚无经验证开仓资格",
                qualified_for_trading=False, price_changed_requires_rescore=True)


def admission_changes(base, candidate):
    left, right = [{r["event_key"]: r for r in rows} for rows in (base, candidate)]
    added = [r for k, r in right.items() if k not in left]
    removed = [r for k, r in left.items() if k not in right]
    return dict(common_accepted=len(left.keys() & right.keys()),
                newly_admitted=stats(added), removed=stats(removed),
                added_unknown=[k for k, r in right.items() if k not in left and evaluation(r)["status"] in UNKNOWN],
                removed_unknown=[k for k, r in left.items() if k not in right and evaluation(r)["status"] in UNKNOWN])


def load_inputs(input_path, signals_path, priced_path, batch_path, fees_path, protocol_path):
    protocol, manifest = read_json(protocol_path), read_json(batch_path / "manifest.json")
    summary_path = priced_path.parent / "summary.json"
    for path, expected in ((input_path, manifest["input_sha256"]),
                           (signals_path, manifest["signals_sha256"]),
                           (fees_path, manifest["fees_sha256"]),
                           (summary_path, protocol["result_sha256"]),
                           (batch_path / "manifest.json", protocol["trial_manifest_sha256"])):
        if digest(path) != expected:
            raise ValueError("frozen input fingerprint mismatch: " + str(path))
    source, signals, profile = read_json(input_path), read_json(signals_path), read_json(fees_path)
    pricing_summary = read_json(summary_path)
    raw_path = batch_path / "evaluations.jsonl"
    expected_raw = [value for path, value in pricing_summary["source_hashes"].items()
                    if pathlib.Path(path).name == raw_path.name and pathlib.Path(path).parent.name == batch_path.name]
    if len(expected_raw) != 1 or digest(raw_path) != expected_raw[0]:
        raise ValueError("raw replay fingerprint mismatch")
    cases = {1: {}, 2: {}}
    for row in jsonl(priced_path):
        if row["scenario"] not in (SCENARIO + "/slip1", SCENARIO + "/slip2"):
            continue
        slip = int(row["scenario"][-1])
        if row["event_key"] in cases[slip]:
            raise ValueError("duplicate policy event")
        e = evaluation(row)
        if e["status"] == "closed":
            expected_net = e["gross_points"] * row["multiplier"] - row["cost_repricing"]["commission"]
            if e["volume"] != 1 or abs(expected_net-e["net_amount"]) > 1e-6 or abs(
                expected_net/(e["risk"]*row["multiplier"])-e["net_r"]) > 1e-9:
                raise ValueError("priced one-lot labels do not reconcile")
        cases[slip][row["event_key"]] = row
    if not cases[1] or cases[1].keys() != cases[2].keys():
        raise ValueError("inconsistent primary/stress universe")
    calendar, checked = TradingDates(source["symbols"]), 0
    for raw in jsonl(raw_path):
        if raw["scenario"] in (SCENARIO + "/slip1", SCENARIO + "/slip2"):
            slip = int(raw["scenario"][-1])
            reproduced = reprice(raw, profile, calendar)
            if reproduced["result"] != cases[slip][raw["event_key"]]["result"]:
                raise ValueError("priced label differs from frozen raw replay and fees")
            checked += 1
    if checked != len(cases[1]) + len(cases[2]):
        raise ValueError("priced/raw replay coverage mismatch")
    events = {key(event): event for event in signals}
    if len(events) != len(signals) or cases[1].keys() - events.keys():
        raise ValueError("signal identity mismatch")
    return source, events, profile, cases


def run(input_path, signals_path, priced_path, batch_path, fees_path, protocol_path, output):
    input_path, signals_path, priced_path, batch_path, fees_path, protocol_path, output = map(
        pathlib.Path, (input_path, signals_path, priced_path, batch_path, fees_path, protocol_path, output))
    if output.exists():
        raise FileExistsError("refuse to overwrite immutable research")
    source, events, profile, cases = load_inputs(input_path, signals_path, priced_path, batch_path, fees_path, protocol_path)
    output.mkdir(parents=True)
    dependency_paths = [input_path, signals_path, priced_path, priced_path.parent / "summary.json",
                        batch_path / "manifest.json", batch_path / "evaluations.jsonl", fees_path, protocol_path]
    dependency_paths += [pathlib.Path(__file__), *[pathlib.Path(__file__).with_name(name) for name in
        ("entry_model.py", "validate_execution_research.py", "reprice_signal_costs.py", "report_signal_ablation.py", "report_signal_search.py")]]
    manifest = dict(contract=CONTRACT, source="historical_development", base_policy=SCENARIO,
                    feature_groups=FEATURE_GROUPS, primary_model="context", config=CONFIG, folds=CUTS,
                    costs="published frozen fee snapshot; no historical actual-contract billing claim",
                    decision_at="completed warning15m; forecast fill = frozen breakout + 1 adverse tick",
                    stress="same one-tick model and decisions; reprice/replay and capacity differ under two ticks",
                    train="closed primary-policy one-symbol-slot trades; label end strictly before cutoff",
                    calibration="purged final25% training dates, otherwise identity; no validation tuning",
                    position_size=1, account_cap=None, validation_starts_flat=True,
                    candidate_count=len(cases[1]), qualified_for_trading=False, production_changed=False,
                    sources={str(path.resolve()): digest(path) for path in dependency_paths})
    dump(output / "manifest.json", manifest)  # freeze before any model fit
    contexts = {s: completed_context(raw["bars"], raw["rollovers"]) for s, raw in source["symbols"].items()}
    snapshots = {}
    for event_key, row in cases[1].items():
        event = events[event_key]
        product = event["symbol"].rstrip("0123456789").upper()
        spec, multiplier = profile["products"][product], row["multiplier"]
        g = geometry(event, spec, multiplier)
        f = features(event, spec, multiplier, contexts[event["symbol"]].get(event["warning_ts"]))
        warning_gate = (f["setup_quality"] >= 3 and f["risk_ticks"] + 1e-6 >= 10 and
                        g["friction_r"] <= .2 + 1e-9 and g["net_reward_loss"] is not None and
                        g["net_reward_loss"] + 1e-7 >= 1)
        snapshots[event_key] = dict(event_key=event_key, symbol=event["symbol"],
            warning_ts=event["warning_ts"], features=f, geometry=g, rule_warning_eligible=warning_gate,
            structural_entry_price_limit=price_limit(event, spec, multiplier),
            price_limit_kind="net-space boundary only; model must rescore a changed fill",
            entry=event["entry"], stop=event["stop"], target=event["target"],
            feature_as_of=event["warning_ts"])
        expected_gate = evaluation(row)["status"] not in ("filtered_warning_context", "filtered_net_rr_plan")
        if warning_gate != expected_gate:
            raise ValueError("warning gate does not reproduce frozen policy")
    write_rows(output / "warning-snapshots.jsonl", snapshots.values())
    full_reservation, full_accepted = accepted_rows(list(cases[1].values()), source["symbols"])
    expected = read_json(protocol_path)["historical_snapshot"][SCENARIO]["slip1"]["stats"]
    for field in ("n", "net_r", "net_amount"):
        if not math.isclose(full_reservation["accepted"][field], expected[field], abs_tol=1e-6):
            raise ValueError("frozen baseline capacity regression: " + field)
    predictions = {name: {} for name in FEATURE_GROUPS}
    folds, bundles, ledger = [], {}, []
    primary_valid = [row for row in cases[1].values() if CUTS[0] <= row["warning_ts"] < CUTS[-1]]
    _, metric_rows = accepted_rows(primary_valid, source["symbols"])
    for number, (start, end) in enumerate(zip(CUTS, CUTS[1:]), 1):
        train = observed_training(full_accepted, snapshots, start)
        valid = [r for r in primary_valid if start <= r["warning_ts"] < end]
        fold = dict(fold=number, start=start, end=end, train=len(train),
                    validation_warnings=len(valid), models={})
        for name, names in FEATURE_GROUPS.items():
            bundle = fit_bundle(train, names, start)
            bundles[f"fold{number}/{name}"] = bundle
            for row in valid:
                event_key = row["event_key"]
                result = predict(bundle, snapshots[event_key]["features"])
                result.update(fold=number, model=name, event_key=event_key, decision_at=row["warning_ts"],
                              qualified_for_trading=False,
                              rule_warning_eligible=snapshots[event_key]["rule_warning_eligible"])
                predictions[name][event_key] = result
                ledger.append(result)
            labelled = [r for r in metric_rows if start <= r["warning_ts"] < end]
            fold["models"][name] = dict(audit=bundle["audit"], calibrator=bundle["calibrator"],
                metrics=classification(labelled, predictions[name], bundle["audit"]["constant_win_rate"]))
        folds.append(fold)
        print(f"fold{number}: train={len(train)} warnings={len(valid)}", flush=True)
    write_rows(output / "predictions.jsonl", ledger)
    write_rows(output / "entry-previews.jsonl", [entry_preview(snapshots[k], p)
               for k, p in predictions["context"].items()])
    policies = {"rules": set(predictions["context"])}
    for name, mapping in predictions.items():
        for mode, field in (("probability", "probability_accept"), ("expectancy", "expectancy_accept")):
            policies[name + "/" + mode] = {k for k, p in mapping.items() if p[field]}
    summary = dict(manifest=manifest, folds=folds, scenarios={}, incremental={}, admission_changes={},
                   baseline_regression=full_reservation["accepted"], qualified_for_trading=False,
                   production_changed=False, independent_validation=False, new_observations=0)
    accepted_maps, admission_ledger = {}, []
    all_dates = sorted({row["warning_ts"][:10] for row in primary_valid})
    for slip in (1, 2):
        for policy, selected in policies.items():
            rows = [r for k, r in cases[slip].items() if k in selected]
            name = f"{policy}/slip{slip}"
            report, accepted, admissions = summarize_policy(rows, source["symbols"])
            report["selected_warnings"] = len(selected)
            summary["scenarios"][name] = report
            accepted_maps[name] = accepted
            admission_ledger.extend(dict(policy=policy, slip=slip, **line) for line in admissions)
        base = accepted_maps[f"rules/slip{slip}"]
        for policy in policies:
            if policy != "rules":
                name = f"{policy}/slip{slip}"
                summary["incremental"][name] = incremental_interval(base, accepted_maps[name], all_dates)
                summary["admission_changes"][name] = admission_changes(base, accepted_maps[name])
    primary = [summary["incremental"][f"context/expectancy/slip{slip}"] for slip in (1, 2)]
    summary["primary_development_diagnostic_pass"] = all(
        row["delta_total_net_r"] > 0 and row["delta_net_amount"] > 0 and
        row["delta_total_net_r_interval95"][0] > 0 and row["delta_amount_interval95"][0] > 0
        for row in primary) and summary["scenarios"]["context/expectancy/slip1"]["accepted"]["n"] >= 100
    latest = max(b["datetime"] for raw in source["symbols"].values() for b in raw["bars"])
    final_cutoff = (dt.datetime.fromisoformat(latest) + dt.timedelta(seconds=1)).isoformat(sep=" ")
    final_train = observed_training(full_accepted, snapshots, final_cutoff)
    for name, names in FEATURE_GROUPS.items():
        bundles["final/" + name] = fit_bundle(final_train, names, final_cutoff)
    dump(output / "models.json", bundles)
    write_rows(output / "admission-ledger.jsonl", admission_ledger)
    summary["limits"] = [
        "Historical policy and all data already used for development; no independent validation.",
        "Closed-trade probability is conditional on rule-eligible filled/evaluable trades, not all warning outcomes.",
        "Unknown outcomes remain unknown and conservatively occupy slots; reported cash is the priced component only.",
        "No account capital/margin or cross-product exposure constraints; drawdown is realized-only.",
        "Payoff bands are shrunk training means, not a full conditional return distribution or tail model.",
        "Warning forecast uses one tick; actual proposed-fill rescore and live feature parity are not deployed.",
        "Price limits are structural net-space limits, never an EV guarantee; gap losses can exceed planned risk.",
        "Categorical frozen trend60 plus causal ATR and clock only; no certified per-product holiday forecast.",
        "Bootstrap resamples fixed ledgers and is descriptive, not a corrected significance test.",
    ]
    summary["verification"] = dict(warning_gate_reproduced=len(snapshots),
                                    priced_replay_reproduced=len(cases[1]) + len(cases[2]),
                                    frozen_baseline_reproduced=True,
                                    one_tick_decision_keys_identical_under_stress=True)
    dump(output / "summary.json", summary)
    (output / "模型优化试验报告.md").write_text(markdown(summary), encoding="utf-8")
    print(json.dumps({k: dict(n=v["accepted"]["n"], win=v["accepted"]["win_rate"],
        mean_r=v["accepted"]["mean_net_r"], cash=v["accepted"]["net_amount"], unknown=v["accepted_unknown"])
        for k, v in summary["scenarios"].items()}, ensure_ascii=False, indent=2), flush=True)


def markdown(summary):
    lines = ["# 开仓净期望模型首轮试验", "", "仅历史开发试验；没有生产晋升或下单。", "",
             "同一S1规则，2026-08-17起从空仓开始；各模型同品种一槽、统一一手，无账户资金上限。", "",
             "|方案|1跳闭合数|1跳净胜率|1跳平均R|1跳净额|2跳净额|1跳已实现回撤|1跳未知接纳|",
             "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for policy in ["rules", *[f"{name}/{mode}" for name in FEATURE_GROUPS for mode in ("probability", "expectancy")]]:
        a, b = [summary["scenarios"][f"{policy}/slip{slip}"] for slip in (1, 2)]
        s = a["accepted"]
        rate = f"{s['win_rate']:.2%}" if s["n"] else "—"
        mean = f"{s['mean_net_r']:+.4f}" if s["n"] else "—"
        lines.append(f"|{policy}|{s['n']}|{rate}|{mean}|{s['net_amount']:+.2f}|"
                     f"{b['accepted']['net_amount']:+.2f}|{a['distribution']['realized_exit_order_drawdown_amount']:.2f}|{a['accepted_unknown']}|")
    lines += ["", "主模型事先指定context；所有四组特征和两种筛选都完整披露，不按结果更换主模型。", "",
              f"主模型开发诊断通过：{summary['primary_development_diagnostic_pass']}；实盘资格：False。", "",
              "净额只含可定价闭合部分；规则先筛选模型再重跑机会接纳，不能视为完整账户收益。", "",
              "## 限制", ""]
    lines += ["- " + limit for limit in summary["limits"]]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for argument in ("input", "signals", "priced", "batch", "fees", "protocol", "out"):
        parser.add_argument("--" + argument, required=True)
    args = parser.parse_args()
    run(args.input, args.signals, args.priced, args.batch, args.fees, args.protocol, args.out)
