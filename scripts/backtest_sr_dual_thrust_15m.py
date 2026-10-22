#!/usr/bin/env python3
"""15-minute article Dual Thrust: controlled comparison and bounded search.

Indicator timeframe changes; completed-minute signals and next-minute-open
execution remain identical to the previous hourly experiment. No live orders.
"""
from __future__ import annotations

import csv
import hashlib
import json
import sys
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path

import optimize_sr_dual_thrust_article as opt

a = opt.article
base = opt.base
OUT = base.ROOT / "reports" / "sr_dual_thrust_15m_20261021"
FIXED = a.Config(cycle=5, ks=.3, kx=.5, intraday=True)


def save(name, value):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT/name).write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def write_csv(name, rows):
    if rows:
        with (OUT/name).open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)


def grid():
    return [c for c in opt.grid() if c.intraday]


def freeze_plan():
    spec = dict(start=str(base.START), end=str(base.END), actual_contract="CZCE.SR701 / SR2701",
                indicator_period_minutes=15, fixed_control=asdict(FIXED),
                execution="Completed 1-minute close confirmation -> next available 1-minute open, as in hourly run",
                aggregation="Shanghai clock-aligned 00/15/30/45 buckets; no empty recess bars",
                window="Previous Cycle-1 completed 15-minute bars plus current opening snapshot; freeze bands within each 15-minute bar",
                session_flat="14:58 and 22:58 open; cancel pending entry and block entry for remainder of session",
                lots=1, multiplier=10, tick_size=1, fee_per_side=3, adverse_slip_ticks_per_side=1,
                cycles=opt.CYCLES, ks=opt.COEFFICIENTS, kx=opt.COEFFICIENTS,
                combinations=len(grid()), train_end=str(opt.TRAIN_END), validation_start=str(opt.VALIDATION_START),
                selection="Same rule as hourly: >=8 training trades, positive own training net, >=half local neighbors positive; maximize neighbor median training net minus 0.5*own training drawdown",
                protocol="Freeze plan -> training only -> save selected config -> full-month grid -> report validation; no refinement after results",
                disclosure="Same month has been examined repeatedly. This is reused-data chronological validation, NOT an unseen out-of-sample experiment.")
    path = OUT/"analysis_plan.json"
    if path.exists():
        assert json.loads(path.read_text(encoding="utf-8")) == spec
    else:
        save("analysis_plan.json", spec)


def emit(name, minutes, features, cfg, start=base.START, end=base.END):
    result = opt.assert_reference(features, minutes, cfg, start, end)
    s, ts, eq, bands = result
    assert s["net_pnl"] == sum(t["net_pnl"] for t in ts) == eq[-1]["equity"]
    assert s["cross_session_trades"] == 0
    save(name+"_summary.json", dict(period_minutes=features.period_minutes, config=asdict(cfg), stats=s))
    for suffix, rows in [("trades", ts), ("equity", eq), ("bands", bands)]:
        write_csv(name+"_"+suffix+".csv", rows)
    return result


def report(runs, selected, stress, ranked):
    def row(title, cfg, stats):
        return opt.row(title, cfg, stats)
    lines = ["# SR Dual Thrust：15分钟与1小时对照", "",
             "区间2026-09-21—10-20，16个交易日、5280根一分钟K线，实际合约SR701/SR2701。固定1手，乘数10；开、平各1跳滑点、各3元手续费，完整一笔合计26元。",
             "15分钟指轨道刷新与Range统计的K线周期；仍用完成的一分钟收盘价确认、下一可交易分钟开盘成交。日盘14:58、夜盘22:58清仓。没有改成等15分钟收盘才检查买卖信号。",
             "", "## 直接改周期与重新选参", "",
             "| 方案 | Cycle | Ks/Kx | 笔数 | 净胜率 | 盈亏比 | 净盈亏/元 | 分钟权益最大回撤/元 |",
             "|---|---:|---:|---:|---:|---:|---:|---:|",
             row("1小时，上轮最优参数", FIXED, runs["hourly_control_full"][0]),
             row("15分钟，只改周期", FIXED, runs["fixed15_full"][0]),
             row("15分钟，整月回看最优", ranked[0][0], runs["best15_full"][0])]
    if selected:
        lines.append(row("15分钟，前半段选参后全月展示", selected, runs["selected15_full"][0]))
    lines += ["", "整月最优是所搜648组参数中的事后最高净利，并非预先选定；小时对照也曾按整月挑选。Cycle=5在15分钟版代表前4根已完成15分钟K线加当前开盘快照，回看时间也随之缩短。", "",
              f"搜索范围：Cycle={opt.CYCLES}；Ks、Kx各取{opt.COEFFICIENTS}。固定日内模式，无止损止盈、额外过滤器或仓位放大。", "",
              "## 前后半段", "",
              "训练9/21—10/8，8个交易日；验证10/9—10/20，8个交易日。训练选择先落盘，验证结果不参与换参。该月已反复研究，时间切分不能消除复用数据偏差。", "",
              "| 方案/区间 | Cycle | Ks/Kx | 笔数 | 净胜率 | 盈亏比 | 净盈亏/元 | 分钟权益最大回撤/元 |",
              "|---|---:|---:|---:|---:|---:|---:|---:|",
              row("固定参数15分钟/训练", FIXED, runs["fixed15_train"][0]),
              row("固定参数15分钟/验证", FIXED, runs["fixed15_validation"][0])]
    if selected:
        for suffix, title in [("train", "训练"), ("validation", "验证")]:
            lines.append(row("训练选参15分钟/"+title, selected, runs["selected15_"+suffix][0]))
    else:
        lines += ["", "没有参数通过既定训练门槛；不根据验证结果另选一组。"]
    lines += ["", "训练门槛：至少8笔、净利为正、同持仓模式的相邻网格至少一半盈利；评分为邻域训练净利中位数−0.5×自身训练回撤。", "",
              "## 成本压力（固定成交时点和方向）", "",
              "| 版本 | 每边滑点 | 笔数 | 毛利/元 | 滑点/元 | 手续费/元 | 净盈亏/元 |",
              "|---|---:|---:|---:|---:|---:|---:|"]
    for s in stress:
        lines.append(f"| {s['version']} | {s['slip_ticks']}跳 | {s['trades']} | {opt.f(s['gross_pnl'])} | {opt.f(s['slippage_cost'])} | {opt.f(s['fees'])} | {opt.f(s['net_pnl'])} |")
    lines += ["", "## 15分钟全月前五名（事后排名）", "",
              "| Cycle | Ks/Kx | 笔数 | 净利/元 | 训练净利/元 | 训练邻域盈利比例 |",
              "|---|---:|---:|---:|---:|---:|"]
    for cfg, s, t, d in ranked[:5]:
        lines.append(f"| {cfg.cycle} | {cfg.ks:g}/{cfg.kx:g} | {s['trades']} | {opt.f(s['net_pnl'])} | {opt.f(t['net_pnl'])} | {d['neighbor_positive_fraction']:.2%} |")
    lines += ["", "## 实现与复现边界", "",
              "保持文章算法：新K线开盘时，Range=max(HH−LC,HC−LL)，样本只含已完成历史和当前开盘快照；上轨=开盘价+Ks×Range，下轨=开盘价−Kx×Range。当前15分钟内的未来高低收不提前进入轨道。反向突破先平仓，后续分钟才允许再开。",
              "按北京时间自然15分钟聚合，休市缺口不补空K线；不是作者平台每秒轮询或模拟Tick的精确复现。仍未模拟限价排队、涨跌停成交和保证金约束。",
              "最大回撤按分钟收盘权益计算；盈亏比为平均净盈利/平均净亏损绝对值。原始数据及文章哈希经过校验，小时基准精确复现；快速搜索与逐笔引擎逐字段核对。",
              "", "```powershell", "python scripts/backtest_sr_dual_thrust_15m.py",
              "python -m unittest discover -s tests -p 'test_sr_dual_thrust*.py'", "```", "",
              "analysis_plan.json记录冻结方案；training_selection.json记录训练选参；parameter_search.csv记录全部648组；*_trades.csv、*_equity.csv、*_bands.csv提供逐笔、分钟权益和15分钟开盘轨道。", ""]
    (OUT/"REPORT.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    freeze_plan()
    minutes = a.prepare()
    features = opt.Features(minutes, period_minutes=15)
    hourly = opt.Features(minutes)
    runs = {"hourly_control_full": emit("hourly_control_full", minutes, hourly, FIXED)}
    prior = json.loads((opt.OUT/"results.json").read_text(encoding="utf-8"))["hindsight"]["intraday"]
    assert asdict(FIXED) == prior["config"]
    assert runs["hourly_control_full"][0] == prior["stats"]
    for name, start, end in [("full", base.START, base.END), ("train", base.START, opt.TRAIN_END),
                              ("validation", opt.VALIDATION_START, base.END)]:
        runs["fixed15_"+name] = emit("fixed15_"+name, minutes, features, FIXED, start, end)
    candidates = grid()
    training = {}
    for i, cfg in enumerate(candidates, 1):
        training[opt.key(cfg)] = opt.compact(features, cfg, base.START, opt.TRAIN_END)
        if i % 162 == 0:
            print(f"Training {i}/{len(candidates)}", flush=True)
    chosen, diagnostics = opt.select(training, candidates)
    selected = chosen[True]
    save("training_selection.json", dict(created_at=datetime.now(base.ZONE).isoformat(),
                                        config=asdict(selected) if selected else None,
                                        stats=training[opt.key(selected)] if selected else None,
                                        rule="Training only, frozen before full-month grid evaluation"))
    full = {}; rows = []
    for i, cfg in enumerate(candidates, 1):
        s = opt.compact(features, cfg)
        full[opt.key(cfg)] = s
        rows.append(dict(period_minutes=15, **opt.flat_row(cfg, training[opt.key(cfg)], s, diagnostics[opt.key(cfg)])))
        if i % 162 == 0:
            print(f"Full month {i}/{len(candidates)}", flush=True)
    write_csv("parameter_search.csv", rows)
    ranked = sorted([(c, full[opt.key(c)], training[opt.key(c)], diagnostics[opt.key(c)]) for c in candidates],
                    key=lambda x: opt.full_rank(x[0], x[1]), reverse=True)
    best = ranked[0][0]
    runs["best15_full"] = emit("best15_full", minutes, features, best)
    if selected:
        for name, start, end in [("train", base.START, opt.TRAIN_END), ("validation", opt.VALIDATION_START, base.END),
                                  ("full", base.START, base.END)]:
            runs["selected15_"+name] = emit("selected15_"+name, minutes, features, selected, start, end)
    stress = []
    for version, cfg in [("fixed15", FIXED), ("best15", best), ("selected15", selected)]:
        if cfg is None:
            continue
        original = runs[version+"_full"][1]
        original_fills = [(t["entry_bar"], t["exit_bar"], t["direction"]) for t in original]
        for slip in [1, 2, 3]:
            s, ts, *_ = opt.assert_reference(features, minutes, replace(cfg, slip_ticks=slip), base.START, base.END)
            assert original_fills == [(t["entry_bar"], t["exit_bar"], t["direction"]) for t in ts]
            stress.append(dict(version=version, slip_ticks=slip, **s))
    summary = dict(hourly_control=runs["hourly_control_full"][0],
                   fixed_15m=dict(config=asdict(FIXED), **{k: runs["fixed15_"+k][0] for k in ["full", "train", "validation"]}),
                   best_15m=dict(config=asdict(best), full=runs["best15_full"][0]),
                   selected_15m=dict(config=asdict(selected), **{k: runs["selected15_"+k][0] for k in ["full", "train", "validation"]}) if selected else None,
                   stress=stress)
    save("results.json", summary)
    report(runs, selected, stress, ranked)
    paths = [a.__file__, opt.__file__, __file__]
    save("verification.json", dict(passed=True, grid_count=len(candidates),
                                    code_hashes={str(p): hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths},
                                    checks=["source hashes and complete 5280 scored minutes", "hourly benchmark reproduced exactly",
                                            "training choice saved before full grid", "compact/detailed replay agreement",
                                            "cash/trade/equity/cost reconciliation", "zero cross-session positions", "cost scenarios preserve fills"]))
    print(json.dumps({k:v for k,v in summary.items() if k != "stress"}, ensure_ascii=False, indent=2), flush=True)
    print("Report:", OUT/"REPORT.md", flush=True)


if __name__ == "__main__":
    main()
