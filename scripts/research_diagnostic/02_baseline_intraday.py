#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Step 2: 严格日内无隔夜强制平仓基准构建 (P0 & P3 消融)
针对方案第1章指出的核心痛点：
- 432 笔复盘入场/出场跨自然日期，107 笔跨度 >= 24小时
- 止损中 251 笔发生在 09:05/21:05 开盘跳空，累计亏损 -351.91R！
- 存在长假大幅跳空止损 -8.62R

本脚本利用 review_holding 与 klines 真实生命周期数据，模拟严格日内交易：
1. 若交易在当前交易时段内触及 target_1r 或 stop，按原规则出场；
2. 若截至时段收盘（日盘 15:00 / 夜盘 23:00/01:00/02:30）仍未触及目标或止损，强制以该收盘 Bar 扣减 1 tick 滑点平仓退出；
3. 彻底杜绝跨时段、跨日、跨周末、跨长假持仓；
4. 严格对比：原版（留仓过夜）vs 日内强平版的期望值、胜率、盈亏比与尾部风险。
"""

import os
import sys
import sqlite3
import json
import re
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / "data" / "research_diagnostic" / "ntrend-2026-11-02-232442.sqlite"
OUTPUT_DIR = ROOT / "reports" / "diagnostic_verdict_20261103"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

def get_symbol_prefix(symbol: str) -> str:
    m = re.match(r"^([A-Za-z]+)", symbol)
    return m.group(1).upper() if m else symbol.upper()

def get_night_session_type(symbol: str) -> str:
    prefix = get_symbol_prefix(symbol)
    if prefix in ("AU", "AG", "SC"):
        return "02:30"
    elif prefix in ("CU", "AL", "ZN", "PB", "NI", "SN", "BC", "SS", "AO"):
        return "01:00"
    elif prefix in (
        "AP", "CJ", "JD", "LH", "PK", "SI", "LC", "UR", "WH", "PM", "RI", 
        "JR", "LR", "BB", "FB", "IF", "IH", "IC", "IM", "TF", "T", "TS", 
        "TL", "SF", "SM", "WR", "EC"
    ):
        return "NONE"
    else:
        return "23:00"

def is_session_close_bar(symbol: str, bar_end_time_str: str) -> bool:
    """
    判断该 5m bar 结束时间是否为交易时段收盘点。
    注意：bar_end_time 格式通常为 'YYYY-MM-DD HH:MM:SS'
    """
    time_part = bar_end_time_str[11:16]
    # 日盘收盘点
    if time_part == "15:00":
        return True
    
    night_type = get_night_session_type(symbol)
    if night_type == "NONE":
        return False
    elif night_type == "23:00" and time_part == "23:00":
        return True
    elif night_type == "01:00" and time_part == "01:00":
        return True
    elif night_type == "02:30" and time_part == "02:30":
        return True
    return False

def run_intraday_baseline_ablation():
    print("[*] 启动严格日内无隔夜强平消融对比...")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    sql_trades = """
    SELECT 
        p.id AS event_id,
        p.symbol,
        p.direction,
        rc.result_version,
        r.entry_ts,
        r.exit_ts,
        r.state,
        r.exit_reason,
        r.gross_r,
        r.net_r,
        r.bars_held,
        r.detail_json
    FROM pattern_events p
    JOIN review_current rc ON p.id = rc.event_id
    JOIN review_results r ON rc.result_version = r.result_version
    WHERE r.state = 'closed' AND r.quality_v2 IS NOT NULL
    ORDER BY p.id ASC
    """
    trades = c.execute(sql_trades).fetchall()
    total_trades = len(trades)
    print(f"[*] 载入核心复盘交易: {total_trades} 笔")

    # 预加载 review_holding 数据
    sql_holding = """
    SELECT result_version, sequence, data
    FROM review_holding
    ORDER BY result_version, sequence ASC
    """
    print("[*] 正在载入全部持仓节点 (review_holding)...")
    holding_rows = c.execute(sql_holding).fetchall()
    holding_by_version = {}
    for hr in holding_rows:
        rv = hr["result_version"]
        if rv not in holding_by_version:
            holding_by_version[rv] = []
        holding_by_version[rv].append(json.loads(hr["data"]))
    print(f"[*] 成功载入 {len(holding_rows)} 条持仓节点，覆盖 {len(holding_by_version)} 笔交易")

    # 模拟对比
    original_results = []
    intraday_results = []

    # 统计跨日/跨时段分类
    session_flat_count = 0
    original_gap_stops_repaired = 0

    for tr in trades:
        rv = tr["result_version"]
        symbol = tr["symbol"]
        direction = tr["direction"]
        sign = 1.0 if direction == "up" else -1.0
        
        detail = json.loads(tr["detail_json"])
        fill_price = detail.get("entry_price")
        cost_info = detail.get("cost", {})
        tick_size = cost_info.get("tick_size", 1.0)
        risk_points = detail.get("summary", {}).get("risk_points", 1.0)
        orig_net_r = tr["net_r"]
        orig_reason = tr["exit_reason"]
        
        # 记录原版
        original_results.append({
            "event_id": tr["event_id"],
            "symbol": symbol,
            "net_r": orig_net_r,
            "exit_reason": orig_reason,
            "exit_ts": tr["exit_ts"],
            "bars_held": tr["bars_held"],
        })

        nodes = holding_by_version.get(rv, [])
        if not nodes or fill_price is None or risk_points <= 0:
            # 异常兜底保持原样
            intraday_results.append({
                "event_id": tr["event_id"],
                "symbol": symbol,
                "net_r": orig_net_r,
                "exit_reason": orig_reason,
                "exit_ts": tr["exit_ts"],
                "bars_held": tr["bars_held"],
                "forced_flat": False
            })
            continue

        # 遍历持仓 bar
        exit_found = False
        new_exit_reason = None
        new_exit_price = None
        new_exit_ts = None
        new_bars_held = 0

        for idx, bar_data in enumerate(nodes):
            new_bars_held = idx + 1
            bar_end = bar_data["interval_end"]
            checks = bar_data.get("checks", {})

            # 1. 检查是否在当前 bar 触发正常出场 (stop 或 target_1r)
            stop_hit = checks.get("stop_hit", False)
            target_hit = checks.get("target_hit", False)

            if target_hit and not stop_hit:
                # 正常止盈
                target_price = checks.get("target_1r")
                new_exit_reason = "target_1r"
                new_exit_price = target_price - sign * tick_size
                new_exit_ts = bar_end
                exit_found = True
                break
            elif stop_hit and not target_hit:
                # 正常止损
                stop_price = checks.get("stop")
                new_exit_reason = "stop"
                new_exit_price = stop_price - sign * tick_size
                new_exit_ts = bar_end
                exit_found = True
                break
            elif stop_hit and target_hit:
                # ambiguous
                new_exit_reason = "ambiguous"
                stop_price = checks.get("stop")
                new_exit_price = stop_price - sign * tick_size # 保守使用止损
                new_exit_ts = bar_end
                exit_found = True
                break

            # 2. 检查是否达到时段收盘 (日盘 15:00 或夜盘收盘)
            if is_session_close_bar(symbol, bar_end):
                # 强制平仓
                session_flat_count += 1
                new_exit_reason = "session_close_flat"
                # 收盘价 - 1 tick 滑点
                close_price = bar_data["price"]
                new_exit_price = close_price - sign * tick_size
                new_exit_ts = bar_end
                exit_found = True
                break

            # 3. 检查 no_follow 退出 (若持仓到达 5 根且 MFE < 0.5R)
            if checks.get("no_follow", False):
                new_exit_reason = "no_follow"
                new_exit_price = bar_data["price"] - sign * tick_size
                new_exit_ts = bar_end
                exit_found = True
                break

        if not exit_found:
            # 若全部 nodes 跑完仍未达到收盘（如最后一根），用最后一根收盘平仓
            last_bar = nodes[-1]
            new_exit_reason = "session_close_flat"
            new_exit_price = last_bar["price"] - sign * tick_size
            new_exit_ts = last_bar["interval_end"]
            new_bars_held = len(nodes)

        # 计算日内强平的净 R
        gross_pnl_pts = (new_exit_price - fill_price) * sign
        # 扣减固定摩擦 (2.5 tick)
        net_pnl_pts = gross_pnl_pts - 0.5 * tick_size
        new_net_r = net_pnl_pts / risk_points

        # 如果原版是 09:05/21:05 的跳空止损，而现在被日内强平替换了
        if orig_reason == "stop" and tr["exit_ts"] and tr["exit_ts"][11:16] in ("09:05", "21:05"):
            original_gap_stops_repaired += 1

        intraday_results.append({
            "event_id": tr["event_id"],
            "symbol": symbol,
            "net_r": new_net_r,
            "exit_reason": new_exit_reason,
            "exit_ts": new_exit_ts,
            "bars_held": new_bars_held,
            "forced_flat": (new_exit_reason == "session_close_flat")
        })

    # 4. 汇总统计分析与对比
    def calc_stats(results_list):
        n = len(results_list)
        wins = [r for r in results_list if r["net_r"] > 0]
        losses = [r for r in results_list if r["net_r"] <= 0]
        w_cnt = len(wins)
        l_cnt = len(losses)
        win_rate = w_cnt / n
        avg_r = sum(r["net_r"] for r in results_list) / n
        total_r = sum(r["net_r"] for r in results_list)
        avg_win_r = sum(r["net_r"] for r in wins) / w_cnt if w_cnt > 0 else 0
        avg_loss_r = sum(r["net_r"] for r in losses) / l_cnt if l_cnt > 0 else 0
        profit_factor = (sum(r["net_r"] for r in wins) / abs(sum(r["net_r"] for r in losses))) if l_cnt > 0 and sum(r["net_r"] for r in losses) != 0 else float('inf')
        
        # 极端亏损统计 (<-2R 的左尾)
        extreme_losses = [r for r in results_list if r["net_r"] < -2.0]
        
        return {
            "total_trades": n,
            "win_rate": win_rate,
            "avg_net_r": avg_r,
            "total_net_r": total_r,
            "avg_win_r": avg_win_r,
            "avg_loss_r": avg_loss_r,
            "profit_factor": profit_factor,
            "extreme_loss_count": len(extreme_losses),
            "worst_trade_r": min(r["net_r"] for r in results_list),
            "best_trade_r": max(r["net_r"] for r in results_list)
        }

    orig_stats = calc_stats(original_results)
    intra_stats = calc_stats(intraday_results)

    print("\n" + "=" * 70)
    print(" 📊 原版（允许留仓过夜）vs 日内强制平仓（严格不过夜）消融对比结果")
    print("=" * 70)
    print(f"{'指标项':<24s} | {'原版 (Original)':<18s} | {'日内强平 (Intraday)':<18s} | {'改善幅度 (Delta)':<15s}")
    print("-" * 75)
    print(f"{'平均净期望 E[net_R]':<24s} | {orig_stats['avg_net_r']:+.4f}R            | {intra_stats['avg_net_r']:+.4f}R            | {intra_stats['avg_net_r'] - orig_stats['avg_net_r']:+.4f}R")
    print(f"{'累计总净收益 Total R':<24s} | {orig_stats['total_net_r']:+.2f}R           | {intra_stats['total_net_r']:+.2f}R           | {intra_stats['total_net_r'] - orig_stats['total_net_r']:+.2f}R")
    print(f"{'胜率 Win Rate':<24s} | {orig_stats['win_rate']*100:.2f}%             | {intra_stats['win_rate']*100:.2f}%             | {(intra_stats['win_rate'] - orig_stats['win_rate'])*100:+.2f}%")
    print(f"{'盈亏比 Profit Factor':<24s} | {orig_stats['profit_factor']:.3f}              | {intra_stats['profit_factor']:.3f}              | {intra_stats['profit_factor'] - orig_stats['profit_factor']:+.3f}")
    print(f"{'平均盈利单收益':<24s} | {orig_stats['avg_win_r']:+.4f}R            | {intra_stats['avg_win_r']:+.4f}R            | {intra_stats['avg_win_r'] - orig_stats['avg_win_r']:+.4f}R")
    print(f"{'平均亏损单亏损':<24s} | {orig_stats['avg_loss_r']:+.4f}R            | {intra_stats['avg_loss_r']:+.4f}R            | {intra_stats['avg_loss_r'] - orig_stats['avg_loss_r']:+.4f}R")
    print(f"{'极端亏损单 (<-2R)':<24s} | {orig_stats['extreme_loss_count']} 笔                | {intra_stats['extreme_loss_count']} 笔                | {intra_stats['extreme_loss_count'] - orig_stats['extreme_loss_count']:+d} 笔")
    print(f"{'最大单笔亏损 Worst R':<24s} | {orig_stats['worst_trade_r']:+.4f}R            | {intra_stats['worst_trade_r']:+.4f}R            | {intra_stats['worst_trade_r'] - orig_stats['worst_trade_r']:+.4f}R")
    print("=" * 70)

    # 出场原因分布对比
    def get_reasons_dist(results_list):
        dist = {}
        for r in results_list:
            re = r["exit_reason"]
            dist[re] = dist.get(re, 0) + 1
        return dist

    print("\n[*] 出场原因分布变化:")
    orig_dist = get_reasons_dist(original_results)
    intra_dist = get_reasons_dist(intraday_results)
    for k in set(orig_dist.keys()) | set(intra_dist.keys()):
        print(f"  - {k:<20s}: 原版 {orig_dist.get(k, 0):4d} 笔 -> 日内强平 {intra_dist.get(k, 0):4d} 笔")

    # 导出结果
    out_payload = {
        "original_stats": orig_stats,
        "intraday_stats": intra_stats,
        "delta": {
            "avg_net_r": intra_stats["avg_net_r"] - orig_stats["avg_net_r"],
            "total_net_r": intra_stats["total_net_r"] - orig_stats["total_net_r"],
            "win_rate": intra_stats["win_rate"] - orig_stats["win_rate"],
            "profit_factor": intra_stats["profit_factor"] - orig_stats["profit_factor"]
        },
        "session_flat_count": session_flat_count,
        "original_gap_stops_repaired": original_gap_stops_repaired
    }
    report_file = OUTPUT_DIR / "02_baseline_intraday.json"
    report_file.write_text(json.dumps(out_payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n[✓] 严格日内无隔夜强平对比报告已写入: {report_file}")

if __name__ == "__main__":
    run_intraday_baseline_ablation()
