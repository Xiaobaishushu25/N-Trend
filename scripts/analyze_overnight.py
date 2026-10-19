#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
跨时段/隔夜持仓表现归因对比分析脚本 (Overnight Attribution Analyzer)

用于读取 `data/overnight_attribution.jsonl` 或 `data/overnight_tracking.jsonl`，
对比分析以下两大核心问题：
1. 每天 15:00 / 23:00 收盘前 2 分钟平仓 vs 继续留仓过夜的实际收益差异 (Overnight Delta)
2. 隔夜跳空幅度与止损被击穿的尾部风险分布
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path


def load_records(file_path: Path):
    if not file_path.exists():
        print(f"[!] 找不到数据文件: {file_path}")
        print("    说明：系统实盘运行且发生跨时段持仓后，会自动在 data/ 目录下生成 overnight_attribution.jsonl")
        return []

    records = []
    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                records.append(data)
            except json.JSONDecodeError:
                continue
    return records


def analyze(records):
    if not records:
        print("暂无记录数据可供分析。")
        return

    # 过滤出已完成出场归因的记录（包含 overnight_delta_r）
    finished = [r for r in records if r.get("overnight_delta_r") is not None]
    pending = [r for r in records if r.get("overnight_delta_r") is None]

    print("\n" + "=" * 70)
    print(" 📊 量化实盘：15:00 / 23:00 收盘平仓 vs 留仓过夜 归因分析报告")
    print("=" * 70)
    print(f"总打点快照数: {len(records)} | 已完成归因: {len(finished)} | 持仓中/待归因: {len(pending)}")

    if not finished:
        print("\n当前所有快照持仓仍在运行中，尚未平仓退出。请在持仓出场后再运行本分析。")
        return

    # 1. 总体盈亏对比
    total_hypo_r = sum(r.get("hypo_pnl_r", 0.0) for r in finished)
    total_actual_r = sum(r.get("actual_realized_pnl_r", 0.0) for r in finished)
    total_delta_r = sum(r.get("overnight_delta_r", 0.0) for r in finished)

    hypo_wins = sum(1 for r in finished if r.get("hypo_pnl_r", 0.0) > 0)
    actual_wins = sum(1 for r in finished if r.get("actual_realized_pnl_r", 0.0) > 0)
    improved = sum(1 for r in finished if r.get("overnight_delta_r", 0.0) > 0)
    worsened = sum(1 for r in finished if r.get("overnight_delta_r", 0.0) < 0)

    n = len(finished)
    print("\n【1. 总体盈亏对比】")
    print(f"- 假设收盘前2m平仓累计收益: {total_hypo_r:+.2f} R (胜率: {hypo_wins/n*100:.1f}%)")
    print(f"- 实盘坚持留仓最终累计收益: {total_actual_r:+.2f} R (胜率: {actual_wins/n*100:.1f}%)")
    print(f"- 隔夜超额收益 (Delta R)  : {total_delta_r:+.2f} R (平均每笔: {total_delta_r/n:+.2f} R)")
    print(f"- 留仓带来正收益的比例    : {improved}/{n} ({improved/n*100:.1f}%)")
    print(f"- 留仓导致利润缩水/亏损比例: {worsened}/{n} ({worsened/n*100:.1f}%)")

    # 决策建议提示
    if total_delta_r > 0:
        print(f"  👉 结论倾向: 当前实盘数据支持【维持不平仓/留仓过夜】，隔夜贡献了 +{total_delta_r:.2f}R 的超额收益。")
    else:
        print(f"  👉 结论倾向: 当前实盘数据支持【在收盘前2分钟无条件平仓】，留仓过夜拖累了 {total_delta_r:.2f}R。")

    # 2. 跳空与极端风险分析
    gaps = [r.get("gap_r") for r in finished if r.get("gap_r") is not None]
    stop_penetrated = sum(1 for r in finished if r.get("gap_penetrated_stop") is True)

    print("\n【2. 跳空与风控指标】")
    if gaps:
        avg_gap = sum(gaps) / len(gaps)
        max_fav = max(gaps)
        max_adv = min(gaps)
        print(f"- 开盘跳空有效样本数      : {len(gaps)}")
        print(f"- 平均跳空贡献幅度        : {avg_gap:+.2f} R")
        print(f"- 最大有利跳空 (顺向获利) : {max_fav:+.2f} R")
        print(f"- 最大不利跳空 (逆向亏损) : {max_adv:+.2f} R")
    print(f"- 开盘直接击穿止损的次数  : {stop_penetrated}/{n} ({stop_penetrated/n*100:.1f}%)")
    if stop_penetrated > 0:
        print("  ⚠️ 注意：存在开盘跳空击穿止损的情况，需密切关注极端行情的最大承受能力！")

    # 3. 按收盘时段细分 (15:00 日盘收盘 vs 23:00/夜盘收盘)
    by_session = defaultdict(list)
    for r in finished:
        s_type = r.get("checkpoint_type", "unknown")
        by_session[s_type].append(r)

    print("\n【3. 按收盘时段细分表现】")
    for s_type, s_records in sorted(by_session.items()):
        s_n = len(s_records)
        s_hypo = sum(r.get("hypo_pnl_r", 0.0) for r in s_records)
        s_actual = sum(r.get("actual_realized_pnl_r", 0.0) for r in s_records)
        s_delta = sum(r.get("overnight_delta_r", 0.0) for r in s_records)
        name = "日盘 15:00 收盘" if s_type == "day_close" else "夜盘 23:00 收盘"
        print(f"- {name:16s} (样本={s_n:2d}): 假设平仓={s_hypo:+.2f}R | 实盘留仓={s_actual:+.2f}R | 留仓Delta={s_delta:+.2f}R")

    # 4. 按收盘时浮盈/浮亏状态分化
    gain_records = [r for r in finished if r.get("hypo_pnl_r", 0.0) > 0]
    loss_records = [r for r in finished if r.get("hypn_pnl_r", r.get("hypo_pnl_r", 0.0)) <= 0]

    print("\n【4. 浮盈单 vs 浮亏单的留仓表现分化】")
    if gain_records:
        g_delta = sum(r.get("overnight_delta_r", 0.0) for r in gain_records)
        print(f"- 收盘前处于浮盈状态 (样本={len(gain_records):2d}): 留仓Delta = {g_delta:+.2f}R (均笔: {g_delta/len(gain_records):+.2f}R)")
    if loss_records:
        l_delta = sum(r.get("overnight_delta_r", 0.0) for r in loss_records)
        print(f"- 收盘前处于浮亏状态 (样本={len(loss_records):2d}): 留仓Delta = {l_delta:+.2f}R (均笔: {l_delta/len(loss_records):+.2f}R)")

    # 5. 分品种明细
    by_symbol = defaultdict(list)
    for r in finished:
        sym = r.get("symbol", "UNKNOWN")
        by_symbol[sym].append(r)

    print("\n【5. 各品种明细表】")
    print(f"{'品种':<8} {'样本':<6} {'假设平仓(R)':<14} {'实盘出场(R)':<14} {'留仓Delta(R)':<14} {'胜率变化'}")
    print("-" * 65)
    for sym, sym_recs in sorted(by_symbol.items()):
        sn = len(sym_recs)
        sh = sum(r.get("hypo_pnl_r", 0.0) for r in sym_recs)
        sa = sum(r.get("actual_realized_pnl_r", 0.0) for r in sym_recs)
        sd = sum(r.get("overnight_delta_r", 0.0) for r in sym_recs)
        hw = sum(1 for r in sym_recs if r.get("hypo_pnl_r", 0.0) > 0)
        aw = sum(1 for r in sym_recs if r.get("actual_realized_pnl_r", 0.0) > 0)
        print(f"{sym:<8} {sn:<6} {sh:+12.2f} R {sa:+12.2f} R {sd:+12.2f} R  {hw/sn*100:.0f}% -> {aw/sn*100:.0f}%")
    print("=" * 70 + "\n")


def main():
    parser = argparse.ArgumentParser(description="跨时段/隔夜持仓影子平仓对比分析")
    default_path = Path(__file__).resolve().parent.parent / "data" / "overnight_attribution.jsonl"
    parser.add_argument("--file", "-f", type=Path, default=default_path, help="归因日志文件路径")
    args = parser.parse_args()

    records = load_records(args.file)
    analyze(records)


if __name__ == "__main__":
    main()
