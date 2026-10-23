"""Summarize immutable ablations, paired subsets, conservative reservations and audit ledgers."""
import argparse
import bisect
import collections
import hashlib
import json
import pathlib
import random


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def jsonl(path):
    with path.open(encoding="utf-8") as f:
        yield from (json.loads(line) for line in f)


def stats(rows):
    closed = [r for r in rows if r["result"]["evaluation"]["status"] == "closed"]
    ev = [r["result"]["evaluation"] for r in closed]
    rs = [e["net_r"] for e in ev]
    cash = [e["net_amount"] for e in ev]
    n = len(ev)
    positive = [r for r in rs if r > 0]
    negative = [r for r in rs if r < 0]
    # Realized exit-order curve only. It does not include floating P&L or margin.
    chronological = sorted(closed, key=lambda r: (r["result"]["evaluation"]["exit_ts"], r["event_key"]))
    equity = peak = dd = 0.0
    for r in chronological:
        equity += r["result"]["evaluation"]["net_r"]
        peak = max(peak, equity)
        dd = max(dd, peak - equity)
    return dict(candidates=len(rows), quality=dict(collections.Counter(r["result"]["evaluation"]["status"] for r in rows)),
                n=n, wins=len(positive), losses=len(negative), flat=n - len(positive) - len(negative),
                win_rate=len(positive) / n if n else None, net_r=sum(rs), mean_net_r=sum(rs) / n if n else None,
                net_amount=sum(cash), mean_win_net_r=sum(positive) / len(positive) if positive else None,
                mean_loss_net_r=sum(negative) / len(negative) if negative else None,
                profit_factor_r=sum(positive) / -sum(negative) if negative else None,
                profit_factor_cash=sum(x for x in cash if x > 0) / -sum(x for x in cash if x < 0) if any(x < 0 for x in cash) else None,
                realized_exit_order_drawdown_r=dd,
                net_r_without_top5=sum(rs) - sum(sorted(positive, reverse=True)[:5]))


def paired(left, right):
    common = sorted(k for k in left.keys() & right.keys()
                    if left[k]["result"]["evaluation"]["status"] == right[k]["result"]["evaluation"]["status"] == "closed")
    a, b = stats([left[k] for k in common]), stats([right[k] for k in common])
    return dict(matched=len(common), left_unmatched=len(left) - len(common), right_unmatched=len(right) - len(common),
                left=a, right=b, delta_net_r_right_minus_left=b["net_r"] - a["net_r"],
                delta_amount_right_minus_left=b["net_amount"] - a["net_amount"])


def reserved_portfolio(rows, bars):
    """Reserve from warning, including pending time; never select on future profitability.

    Unknown paths reserve through the maximum pending+holding horizon. This is a
    conservative capacity diagnostic, not the real account's positions or equity.
    """
    occupied = {}
    accepted, rejected = [], []
    for r in sorted(rows, key=lambda r: (r["warning_ts"], r["event_key"])):
        s, t = r["symbol"], r["warning_ts"]
        if t <= occupied.get(s, ""):
            rejected.append(r)
            continue
        accepted.append(r)
        e = r["result"]["evaluation"]
        if e["status"] in ("ambiguous", "missing_data", "rollover", "unfinished", "pending"):
            closes = [b["datetime"] for b in bars[s]["bars"] if int(b["datetime"][14:16]) % 15 == 0]
            index = bisect.bisect_right(closes, t) + 72 - 1
            end = closes[index] if index < len(closes) else bars[s]["bars"][-1]["datetime"]
        else:
            end = e["exit_ts"] or bars[s]["bars"][-1]["datetime"]
        occupied[s] = end
    return dict(accepted=stats(accepted), rejected_overlap=len(rejected))


def block_interval(rows, repetitions=2000):
    """Five warning-calendar-date moving blocks. Descriptive, not an eligibility test."""
    bins = collections.defaultdict(lambda: [0.0, 0])
    for r in rows:
        e = r["result"]["evaluation"]
        if e["status"] == "closed":
            b = bins[r["warning_ts"][:10]]
            b[0] += e["net_r"]
            b[1] += 1
    dates = sorted(bins)
    if len(dates) < 10:
        return None
    values = [bins[d] for d in dates]
    rng = random.Random(20261022)
    samples = []
    for _ in range(repetitions):
        sampled = []
        while len(sampled) < len(values):
            index = rng.randrange(len(values) - 4)
            sampled.extend(values[index:index + 5])
        sample = sampled[:len(values)]
        samples.append(sum(x[0] for x in sample) / sum(x[1] for x in sample))
    samples.sort()
    return dict(method="5 warning-calendar-date moving blocks, all symbols jointly; diagnostic, not trading-day adjusted",
                dates=len(dates), repetitions=repetitions, seed=20261022,
                mean_net_r_interval95=[samples[int(repetitions * .025)], samples[int(repetitions * .975)]])


def key(e):
    return "|".join(e[x] for x in ("symbol", "direction", "s0_ts", "s1_ts", "s2_ts", "warning_ts"))


def write_new(path, value):
    with path.open("x", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)


def report(input_path, batch):
    input_path, batch = pathlib.Path(input_path), pathlib.Path(batch)
    source = read_json(input_path)
    manifest = read_json(batch / "manifest.json")
    baseline = {r["event_key"]: r["evaluation"] for r in jsonl(batch / "baseline.jsonl")}
    cases = collections.defaultdict(dict)
    for r in jsonl(batch / "evaluations.jsonl"):
        assert r["event_key"] not in cases[r["scenario"]]
        cases[r["scenario"]][r["event_key"]] = r
    assert len(baseline) == manifest["golden_baseline_checked"] == 3014
    universe = set(next(iter(cases.values())))
    assert len(universe) == manifest["candidates"] == 1352
    assert all(set(rows) == universe for rows in cases.values())
    result = dict(manifest=manifest, scenarios={}, pairs={}, groups={}, old_to_new_flips=[], old_price_inconsistencies=[])
    for name, rows in cases.items():
        result["scenarios"][name] = stats(list(rows.values()))
        closed = [r for r in rows.values() if r["result"]["evaluation"]["status"] == "closed"]
        gross_r = sum(r["result"]["evaluation"]["gross_points"] / (r["result"]["evaluation"]["risk"] * r["result"]["evaluation"]["volume"]) for r in closed)
        fee_sensitivity = sum(r["tick_size"] / r["result"]["evaluation"]["risk"] for r in closed)
        result["scenarios"][name]["cost_decomposition"] = dict(
            r_before_extra_fee_after_scenario_slippage=gross_r,
            amount_before_extra_fee_after_scenario_slippage=sum(r["result"]["evaluation"]["gross_points"] * r["multiplier"] for r in closed),
            extra_roundtrip_fee_break_even_ticks_equal_r=gross_r / fee_sensitivity if fee_sensitivity else None)
    policies = ("original_s1", "fixed_one_r", "legacy_staircase", "actual_tier")
    for policy in policies:
        for cost in ("zero", "slip1_fee", "slip2_fee"):
            left, right = f"{policy}/nf5/{cost}", f"{policy}/nf_off/{cost}"
            result["pairs"][left + " -> " + right] = paired(cases[left], cases[right])
        for cost in ("zero", "slip1_fee"):
            left, right = f"actual_tier/nf5/{cost}", f"{policy}/nf5/{cost}"
            result["pairs"][left + " -> " + right] = paired(cases[left], cases[right])
    all_four = set.intersection(*(set(k for k, r in cases[f"{p}/nf5/slip1_fee"].items() if r["result"]["evaluation"]["status"] == "closed") for p in policies))
    result["four_policy_common_sample"] = {p: stats([cases[f"{p}/nf5/slip1_fee"][k] for k in sorted(all_four)]) for p in policies}
    for policy in ("actual_tier", "legacy_staircase"):
        left, right = f"{policy}/nf5/slip1_fee", f"{policy}/nf6/slip1_fee"
        result["pairs"][left + " -> " + right] = paired(cases[left], cases[right])
    result["reservation_portfolios"] = {}
    for p in policies:
        for cost in ("zero", "slip1_fee", "slip2_fee"):
            name = f"{p}/nf5/{cost}"
            result["reservation_portfolios"][name] = reserved_portfolio(list(cases[name].values()), source["symbols"])
    for name in ("original_s1/nf5/zero", "original_s1/nf5/slip1_fee", "original_s1/nf5/slip2_fee", "actual_tier/nf5/slip1_fee"):
        rows = list(cases[name].values())
        result["scenarios"][name]["block_interval"] = block_interval(rows)
        gs = {}
        for group, grouper in (("month", lambda r: r["warning_ts"][:7]), ("direction", lambda r: r["direction"]),
                               ("symbol", lambda r: r["symbol"]), ("score", lambda r: "3.5+" if r["score"] >= 3.5 else "2.5-3.5"),
                               ("rr", lambda r: "1+" if r["rr"] >= 1 else "below1")):
            bins = collections.defaultdict(list)
            for r in rows:
                bins[grouper(r)].append(r)
            gs[group] = {k: stats(v) for k, v in sorted(bins.items())}
        result["groups"][name] = gs
    audited = cases["actual_tier/nf5/slip1_fee"]
    transitions = collections.Counter((baseline[k]["status"], r["result"]["evaluation"]["status"]) for k, r in audited.items())
    shared = [k for k in audited if baseline[k]["status"] == audited[k]["result"]["evaluation"]["status"] == "closed"]
    changed = [k for k in shared if abs(baseline[k]["net_amount"] - audited[k]["result"]["evaluation"]["net_amount"]) > 1e-6 or baseline[k]["exit_ts"] != audited[k]["result"]["evaluation"]["exit_ts"]]
    result["adapter_audit"] = dict(transitions={a + " -> " + b: v for (a, b), v in transitions.items()},
                                   shared_closed=len(shared), changed_pnl_or_exit=len(changed),
                                   note="Two-path agreement is not exhaustive intrabar proof; production adapter unchanged")
    old = {key(r): r for r in source["old_events"]}
    matched_old = [k for k in old.keys() & baseline.keys() if old[k]["outcome"] in ("win", "loss") and baseline[k]["status"] == "closed"]
    result["old_structural_matched_closed"] = len(matched_old)
    for k in sorted(matched_old):
        if old[k]["outcome"] == "win" and baseline[k]["net_amount"] <= 0:
            result["old_to_new_flips"].append(dict(event_key=k, old=old[k], baseline=baseline[k],
                diagnostics={name: cases[name][k]["result"] for name in (
                    "original_s1/nf5/zero", "original_s1/nf5/slip1_fee", "fixed_one_r/nf5/slip1_fee",
                    "actual_tier/nf5/slip1_fee", "actual_tier/nf_off/slip1_fee", "actual_tier/nf6/slip1_fee")}))
    result["flip_summary"] = dict(n=len(result["old_to_new_flips"]),
        baseline_reasons=dict(collections.Counter(r["baseline"]["exit_reason"] for r in result["old_to_new_flips"])))
    for scenario in ("original_s1/nf5/zero", "original_s1/nf5/slip1_fee", "actual_tier/nf_off/slip1_fee", "actual_tier/nf6/slip1_fee"):
        rs = [r["diagnostics"][scenario]["evaluation"] for r in result["old_to_new_flips"]]
        result["flip_summary"][scenario] = dict(quality=dict(collections.Counter(e["status"] for e in rs)),
                                               positive=sum(e["net_amount"] is not None and e["net_amount"] > 0 for e in rs))
    archive_path = pathlib.Path(source["provenance"]["archive"])
    if "archive_sha256" in source["provenance"]:
        assert hashlib.sha256(archive_path.read_bytes()).hexdigest() == source["provenance"]["archive_sha256"], "Archive changed during experiment"
    trade_history = archive_path.with_name("trade-history.json")
    if trade_history.exists():
        trades = read_json(trade_history).get("data", [])
        fee_fields = sorted({k for r in trades for k in r if any(t in k.lower() for t in ("commission", "fee", "手续费"))})
        result["real_cost_evidence"] = dict(archived_records=len(trades), explicit_fee_fields=fee_fields,
                                           actual_cost_qualification=False)
    for r in source["old_events"]:
        if r["outcome"] != "win" or r["exit_ts"] is None or r["exit_price"] is None or r["symbol"] not in source["symbols"]:
            continue
        bars = source["symbols"][r["symbol"]]["bars"]
        start = r["trigger_bar_ts"] or r["trigger_ts"] or r["warning_ts"]
        if start < bars[0]["datetime"] or r["exit_ts"] > bars[-1]["datetime"]:
            continue
        window = [b for b in bars if r["warning_ts"] < b["datetime"] <= r["exit_ts"]]
        if not window:
            continue
        favorable = min(b["low"] for b in window) if r["direction"] == "down" else max(b["high"] for b in window)
        beyond = r["exit_price"] < favorable - 1e-5 if r["direction"] == "down" else r["exit_price"] > favorable + 1e-5
        if beyond:
            result["old_price_inconsistencies"].append(dict(old=r, favorable_in_retained_raw=favorable))
    code_paths = ("crates/n-core/src/v3/ablation.rs", "crates/n-core/src/bin/signal_ablation.rs", "scripts/export_signal_ablation.py", "scripts/report_signal_ablation.py")
    root = pathlib.Path(__file__).resolve().parents[1]
    result["source_sha256"] = {p: hashlib.sha256((root / p).read_bytes()).hexdigest() for p in code_paths}
    write_new(batch / "summary.json", result)

    header = "| 规则/情景 | 完成笔数 | 净胜率 | 平均净R | 累计净R | 模拟金额（元） | 歧义数 |"
    separator = "|---|---:|---:|---:|---:|---:|---:|"
    def line(name, s):
        rate = f"{s['win_rate']:.2%}" if s["win_rate"] is not None else "—"
        mean = f"{s['mean_net_r']:+.4f}" if s["mean_net_r"] is not None else "—"
        return f"| {name} | {s['n']} | {rate} | {mean} | {s['net_r']:+.2f} | {s['net_amount']:+,.2f} | {s['quality'].get('ambiguous', 0)} |"
    lines = ["# 冻结信号退出规则与成本消融实验（2026-10-22）", "",
             "仅历史开发诊断；未部署、未重新训练模型、未改动生产/研究数据库与实盘策略参数。", "",
             "## 可复现性和实验边界", "",
             f"全部{manifest['golden_baseline_checked']}条原始V3回放逐字段复算一致，最大数值差{manifest['golden_max_numeric_delta']:.3g}。预设评分/风险门槛固定后1352个候选进入34组情景，共45968条情景记录；其余1662条过滤信号仍保留原始基线账本。行情仍为22品种、82149根5分钟K线，截至2026-10-22 11:25。",
             "",
             "所有规则共用成交、跳空、失效、风险重算及观察上限。S1、固定1R和阶梯策略用1手；实际分档保留小仓1手/大仓2手。R按各笔初始总风险归一化；金额另列。零成本零滑点仅诊断毛优势。费用仍是额外往返2.5 tick/手代理，缺少真实历史费用和具体合约映射，不能给可交易结论。",
             "",
             "独立诊断适配器比较O-H-L-C与O-L-H-C两种单调路径，状态或结果分歧标歧义且不赋净标签；两条路径一致也没有穷尽所有盘中来回波动。旧阶梯策略在此使用共同5分钟输入、实际成交风险和同一费用，不是原15分钟实现的逐字复制。歧义比例高的策略不能仅凭已闭合子集均值判定整体好坏。", "",
             "## 全部候选：无跟随第5根启用", "", header, separator]
    for cost in ("zero", "fee_only", "slip1_fee", "slip2_fee"):
        lines += [line(f"{p}/{cost}", result["scenarios"][f"{p}/nf5/{cost}"]) for p in policies]
    lines += ["", "zero=零滑点零费用；fee_only=零滑点+费用代理；slip1_fee/slip2_fee=进出各1/2跳不利滑点+费用代理。完成笔数随成交与价格路径变化，以上不是同笔数配对；下一表统一子集。", "",
              "## 四规则都可评价的相同子集：1跳+费用代理", "", header, separator]
    lines += [line(p, s) for p, s in result["four_policy_common_sample"].items()]
    lines += ["", "该交集受阶梯策略较多的歧义排除影响，损失样本占比显著增加，不代表全候选总体；主判断同时参考全部状态与两两配对，不能用交集低胜率反推信号总体无效。", "",
              "## 主要规则两两配对：1跳+费用代理", "", header, separator]
    for p in ("original_s1", "fixed_one_r"):
        pair = result["pairs"][f"actual_tier/nf5/slip1_fee -> {p}/nf5/slip1_fee"]
        lines += [line("actual_tier vs " + p, pair["left"]), line(p + " vs actual_tier", pair["right"])]
    lines += ["", "## 无跟随开/关：相同可评价机会（1跳+费用代理）", "", header, separator]
    for p in policies:
        pair = result["pairs"][f"{p}/nf5/slip1_fee -> {p}/nf_off/slip1_fee"]
        lines += [line(p + "/nf5", pair["left"]), line(p + "/nf_off", pair["right"])]
    lines += ["", "配对排除的候选数量和所有原始状态见summary.json；未完成、歧义和换月没有按0收益填入。", "",
              "## 费用代理的盈亏平衡敏感度", "",
              "以下在各情景成交与退出固定时，只变动额外费用，不能当作改变滑点的策略重放或真实费率建议。", "",
              "| 规则/情景 | 额外费用前累计R | 等风险口径可承受的额外往返费用（tick） |", "|---|---:|---:|"]
    for name in ("original_s1/nf5/zero", "original_s1/nf5/slip1_fee", "original_s1/nf5/slip2_fee", "actual_tier/nf5/slip1_fee"):
        d = result["scenarios"][name]["cost_decomposition"]
        lines.append(f"| {name} | {d['r_before_extra_fee_after_scenario_slippage']:+.3f} | {d['extra_roundtrip_fee_break_even_ticks_equal_r']:+.3f} |")
    lines += ["", "若盈亏平衡费用为负，表示此情景在额外费用为0时已亏损。归档76条成交记录没有明确手续费字段，尚不能确认2.5 tick代理偏高或偏低，也没有把滑点再次作为费用扣除。", "",
              "## 每品种最多一个机会的容量诊断", "", header, separator]
    for p in policies:
        name = f"{p}/nf5/slip1_fee"
        lines.append(line(name, result["reservation_portfolios"][name]["accepted"]))
    lines += ["", "从预警时起占用机会槽，含等待成交时间，按预警时间与完整事件键稳定排序；结束前的新预警不再占槽。歧义、缺失、换月等不明状态保守占用至最多12根等待+60根持仓的窗口末端。以上仅统计可定价闭合收益，未知状态仍单列，不是完整账户收益；没有保证金、资金、具体合约及浮动盈亏。", "",
              "## 适配层审计与旧盈利翻转", "",
              f"原V3与诊断适配器共同闭合{len(shared)}笔，其中金额或退出时间不同{len(changed)}笔；原899笔的亏损没有因重算而消失。原81笔歧义中，两个典型路径一致判闭合25笔、一致判失效1笔，其余55笔仍歧义；这是路径敏感度诊断，未替换生产判定。",
              "",
              f"旧盈利转新亏损{result['flip_summary']['n']}笔已生成逐笔账本：" + json.dumps(result["flip_summary"], ensure_ascii=False),
              "", f"另复核到{len(result['old_price_inconsistencies'])}笔旧盈利的退出价无法由保留行情覆盖；明细在summary.json。这不等于已定位根因，也不足以解释整体胜率差距。", "",
              "## 按品种、月份、方向、评分和原始RR分组", ""]
    for name, gs in result["groups"].items():
        lines += [f"### {name}", ""]
        for group, bins in gs.items():
            lines += [f"#### {group}", "", header, separator]
            lines += [line(k, s) for k, s in bins.items()]
            lines.append("")
    lines += ["## 不确定性与后续条件", "",
              "summary.json保存所有情景状态、剔除头部5笔、均赢亏、R/金额利润因子、按退出时间的已实现R回撤，以及描述性5个预警日期移动块bootstrap区间。该区间不是交易日校正的前瞻资格检验，也不能消除已反复研究这批数据的选择偏差。",
              "",
              "本批次没有真实合约/手续费证据，没有独立前瞻样本；模型维持challenger。应先针对候选规则作可交易成本与有效空间审计，再冻结规则收集新数据；不能从34组中挑最高收益后宣称获得稳定策略。",
              "", "命令：先用export_signal_ablation.py导出全新输入；cargo run -p n-core --bin signal-ablation -- <input.json> <frozen-signals.json> <全新输出目录>；再用report_signal_ablation.py --input <input.json> --batch <输出目录>。已存在批次拒绝覆盖。", ""]
    with (batch / "实验报告.md").open("x", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(json.dumps(dict(report=str(batch / "实验报告.md"), adapter_audit=result["adapter_audit"], flip_summary=result["flip_summary"], common=result["four_policy_common_sample"]), ensure_ascii=True, indent=2))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--input", required=True)
    p.add_argument("--batch", required=True)
    a = p.parse_args()
    report(a.input, a.batch)
