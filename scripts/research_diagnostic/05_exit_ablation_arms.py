#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Step 5: 出场机制与止盈扩展消融对比 (P3)
在固定入场规则下（测试 E0基准 与 E3限价/E1收盘确认），单变量测试不同出场机制：
- X0: 原版固定 1R + 结构止损 + 5 根 no_follow (含留仓过夜)
- X1: 固定 1R + 强制日内平仓 (严格不过夜)
- X2a: 止盈扩张至 1.5R + 结构止损 + 日内平仓
- X2b: 止盈扩张至 2.0R + 结构止损 + 日内平仓
- X4: 快速失效退出 (入场后 3 根 5m 内若反向跌破入场线或 MFE < 0.2R，提前微亏离场，不硬抗到结构止损)

重点度量：
- 净期望 E[net_R]、盈亏比 Profit Factor、胜率
- 极端亏损占比与尾部收敛程度
"""

import os
import sys
import sqlite3
import json
import numpy as np
import re
from datetime import datetime
from pathlib import Path
from collections import defaultdict

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
    time_part = bar_end_time_str[11:16]
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

def run_exit_ablation():
    print("[*] 启动出场机制消融对比 (P3)...")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    sql_events = """
    SELECT 
        p.id AS event_id,
        p.symbol,
        p.direction,
        p.entry AS planned_entry,
        p.stop AS planned_stop,
        p.risk AS planned_risk,
        r.entry_ts,
        r.net_r AS orig_net_r,
        r.detail_json
    FROM pattern_events p
    JOIN review_current rc ON p.id = rc.event_id
    JOIN review_results r ON rc.result_version = r.result_version
    WHERE r.state = 'closed' AND r.quality_v2 IS NOT NULL
    ORDER BY p.id ASC
    """
    events = c.execute(sql_events).fetchall()
    total_events = len(events)
    print(f"[*] 载入评估样本: {total_events} 笔")

    # 载入 5m K 线
    sql_klines = """
    SELECT symbol, ts, open, high, low, close
    FROM klines
    WHERE timeframe = '5m'
    ORDER BY symbol, ts ASC
    """
    klines_rows = c.execute(sql_klines).fetchall()
    bars_by_symbol = defaultdict(list)
    idx_by_symbol_ts = defaultdict(dict)
    for r in klines_rows:
        sym = r["symbol"]
        ts = r["ts"]
        bars = bars_by_symbol[sym]
        idx_by_symbol_ts[sym][ts] = len(bars)
        bars.append({
            "ts": ts,
            "open": r["open"],
            "high": r["high"],
            "low": r["low"],
            "close": r["close"]
        })

    # 通用仿真函数支持不同出场规则
    def simulate_exit(sym, entry_idx, fill_price, stop_price, direction, tick_size, target_multiple, early_exit, allow_overnight):
        sign = 1.0 if direction == "up" else -1.0
        risk_pts = (fill_price - stop_price) * sign
        if risk_pts <= 0:
            return None
        
        target_price = fill_price + sign * (risk_pts * target_multiple)
        bars = bars_by_symbol[sym]
        
        bars_held = 0
        mfe_pts = 0.0

        for i in range(entry_idx, min(entry_idx + 120, len(bars))):
            b = bars[i]
            bars_held += 1
            favorable = b["high"] if sign > 0 else b["low"]
            adverse = b["low"] if sign > 0 else b["high"]

            cur_favorable = (favorable - fill_price) * sign
            if cur_favorable > mfe_pts: mfe_pts = cur_favorable

            stop_hit = (adverse - stop_price) * sign <= 0
            target_hit = (favorable - target_price) * sign >= 0

            # 1. 正常止盈
            if target_hit and not stop_hit:
                exit_price = target_price - sign * tick_size
                net_r = ((exit_price - fill_price) * sign - 0.5 * tick_size) / risk_pts
                return {"net_r": net_r, "reason": f"target_{target_multiple}r"}

            # 2. 正常止损
            if stop_hit and not target_hit:
                observed = b["open"] if (b["open"] - stop_price) * sign < 0 else stop_price
                exit_price = observed - sign * tick_size
                net_r = ((exit_price - fill_price) * sign - 0.5 * tick_size) / risk_pts
                return {"net_r": net_r, "reason": "stop"}

            # 3. ambiguous 同根触及
            if stop_hit and target_hit:
                exit_price = stop_price - sign * tick_size
                net_r = ((exit_price - fill_price) * sign - 0.5 * tick_size) / risk_pts
                return {"net_r": net_r, "reason": "ambiguous"}

            # 4. 快速失效退出 (X4): 入场后 3 根 5m 内若反向跌破入场线且 MFE < 0.2R
            if early_exit and bars_held == 3:
                cur_close = b["close"]
                fell_back = (cur_close - fill_price) * sign < 0
                if fell_back and (mfe_pts / risk_pts) < 0.2:
                    exit_price = cur_close - sign * tick_size
                    net_r = ((exit_price - fill_price) * sign - 0.5 * tick_size) / risk_pts
                    return {"net_r": net_r, "reason": "early_invalidation"}

            # 5. 日内收盘强制平仓 (若不允许留仓过夜)
            if not allow_overnight and is_session_close_bar(sym, b["ts"]):
                exit_price = b["close"] - sign * tick_size
                net_r = ((exit_price - fill_price) * sign - 0.5 * tick_size) / risk_pts
                return {"net_r": net_r, "reason": "session_close_flat"}

            # 6. no_follow
            if bars_held >= 5 and (mfe_pts / risk_pts) < 0.5:
                exit_price = b["close"] - sign * tick_size
                net_r = ((exit_price - fill_price) * sign - 0.5 * tick_size) / risk_pts
                return {"net_r": net_r, "reason": "no_follow"}

        # 兜底超时
        last_b = bars[min(entry_idx + bars_held - 1, len(bars) - 1)]
        exit_price = last_b["close"] - sign * tick_size
        net_r = ((exit_price - fill_price) * sign - 0.5 * tick_size) / risk_pts
        return {"net_r": net_r, "reason": "timeout"}

    # 五组出场消融测试
    exits_config = {
        "X0_Original (1R, Overnight)": {"target": 1.0, "early": False, "overnight": True},
        "X1_Intraday (1R, No Overnight)": {"target": 1.0, "early": False, "overnight": False},
        "X2a_Target_1.5R (No Overnight)": {"target": 1.5, "early": False, "overnight": False},
        "X2b_Target_2.0R (No Overnight)": {"target": 2.0, "early": False, "overnight": False},
        "X4_Early_Invalidate (1R, No Overnight)": {"target": 1.0, "early": True, "overnight": False},
    }

    results = {k: [] for k in exits_config}

    for ev in events:
        sym = ev["symbol"]
        direction = ev["direction"]
        sign = 1.0 if direction == "up" else -1.0
        planned_entry = ev["planned_entry"]
        planned_stop = ev["planned_stop"]
        
        detail = json.loads(ev["detail_json"])
        cost_info = detail.get("cost", {})
        tick_size = cost_info.get("tick_size", 1.0)
        
        entry_ts = ev["entry_ts"]
        if sym not in bars_by_symbol or entry_ts not in idx_by_symbol_ts[sym]:
            continue

        orig_entry_idx = idx_by_symbol_ts[sym][entry_ts]
        fill_price = planned_entry + sign * tick_size # E0 基准入场价

        for x_name, cfg in exits_config.items():
            if x_name == "X0_Original (1R, Overnight)":
                # 直接记录原版真实复盘结果
                results[x_name].append(ev["orig_net_r"])
            else:
                sim = simulate_exit(
                    sym, orig_entry_idx, fill_price, planned_stop, direction, tick_size,
                    target_multiple=cfg["target"],
                    early_exit=cfg["early"],
                    allow_overnight=cfg["overnight"]
                )
                if sim:
                    results[x_name].append(sim["net_r"])

    # 统计指标
    print("\n" + "=" * 85)
    print(" 🚪 出场机制消融对比结果 (统一 E0 入场路径，N=2,037)")
    print("=" * 85)
    print(f"{'出场策略臂':<35s} | {'平均净期望 E[net_R]':<18s} | {'胜率':<10s} | {'盈亏比 PF':<12s} | {'极端亏损(<-2R)':<10s}")
    print("-" * 85)

    summary_stats = {}
    for arm_name, r_list in results.items():
        arr = np.array(r_list)
        wins = arr[arr > 0]
        losses = arr[arr <= 0]
        mean_r = float(np.mean(arr))
        win_rate = float(len(wins) / len(arr))
        pf = float(np.sum(wins) / abs(np.sum(losses))) if len(losses) > 0 and np.sum(losses) != 0 else float('nan')
        extreme_count = int(np.sum(arr < -2.0))

        summary_stats[arm_name] = {
            "mean_r": mean_r,
            "win_rate": win_rate,
            "profit_factor": pf,
            "extreme_loss_count": extreme_count,
            "total_count": len(arr)
        }
        print(f"{arm_name:<35s} | {mean_r:+.4f}R            | {win_rate*100:6.1f}%    | {pf:6.3f}       | {extreme_count:3d} 笔")
    print("=" * 85)

    report_file = OUTPUT_DIR / "05_exit_ablation.json"
    report_file.write_text(json.dumps(summary_stats, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n[✓] 出场机制消融对比报告已写入: {report_file}")

if __name__ == "__main__":
    run_exit_ablation()
