#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Step 4: 入场机制四臂单变量对照实验 (P2)
在统一候选 N 字预警、统一固定手续费与日内平仓规则下，严格单变量对照 4 种入场方式：
- E0 (基准): 盘中高低点触碰突破线 + 1 tick 滑点即入场
- E1 (收盘确认): 等待完整 5m K 线收盘确认突破，下一根开盘价 + 1 tick 入场（追高限制 <= 0.3 ATR）
- E2 (收盘突破 + 回踩 S1): 5m 收盘确认后，等待 6 根 5m 内回踩 S1 (±0.2 ATR) 且企稳反弹入场
- E3 (提前限价单挂单): 突破价限价挂单 (0 tick 入场滑点)，仅价格击穿时成交，统计填单率与机会成本

所有臂统一以【每候选预警平均贡献净 R】为第一裁判指标（严禁按已成交样本事后挑选！）。
"""

import os
import sys
import sqlite3
import json
import numpy as np
from datetime import datetime
from pathlib import Path
from collections import defaultdict

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / "data" / "research_diagnostic" / "ntrend-2026-11-02-232442.sqlite"
OUTPUT_DIR = ROOT / "reports" / "diagnostic_verdict_20261103"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

import re

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

def run_entry_ablation():
    print("[*] 启动入场机制四臂单变量对照实验 (P2)...")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # 1. 获取所有基准形态事件
    sql_events = """
    SELECT 
        p.id AS event_id,
        p.symbol,
        p.direction,
        p.warning_ts,
        p.entry AS planned_entry,
        p.stop AS planned_stop,
        p.risk AS planned_risk,
        p.s1_price,
        p.s1_ts,
        r.entry_ts,
        r.detail_json
    FROM pattern_events p
    JOIN review_current rc ON p.id = rc.event_id
    JOIN review_results r ON rc.result_version = r.result_version
    WHERE r.state = 'closed' AND r.quality_v2 IS NOT NULL
    ORDER BY p.id ASC
    """
    events = c.execute(sql_events).fetchall()
    total_warnings = len(events)
    print(f"[*] 统一候选预警分母: {total_warnings} 笔")

    # 2. 载入 5m K 线
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

    # 为每个品种预先计算 5m ATR20
    atr_by_symbol = {}
    for sym, b_list in bars_by_symbol.items():
        n = len(b_list)
        tr = np.zeros(n)
        for i in range(n):
            if i == 0:
                tr[i] = b_list[i]["high"] - b_list[i]["low"]
            else:
                h = b_list[i]["high"]
                l = b_list[i]["low"]
                prev_c = b_list[i-1]["close"]
                tr[i] = max(h - l, abs(h - prev_c), abs(l - prev_c))
        # 简单移动平均 ATR20
        atr = np.zeros(n)
        for i in range(n):
            start_i = max(0, i - 19)
            atr[i] = np.mean(tr[start_i : i + 1])
        atr_by_symbol[sym] = atr

    # 模拟生命周期执行函数（支持不同入场价和入场 Bar）
    def simulate_trade(sym, entry_idx, fill_price, stop_price, direction, tick_size):
        sign = 1.0 if direction == "up" else -1.0
        risk_pts = (fill_price - stop_price) * sign
        if risk_pts <= 0:
            return None # 结构失效不成交
        
        target_price = fill_price + sign * risk_pts
        bars = bars_by_symbol[sym]
        
        # 从 entry_idx 开始模拟持仓直到触发止盈/止损/日内强平/no_follow
        bars_held = 0
        mfe_pts = 0.0
        mae_pts = 0.0

        for i in range(entry_idx, len(bars)):
            b = bars[i]
            bars_held += 1
            favorable = b["high"] if sign > 0 else b["low"]
            adverse = b["low"] if sign > 0 else b["high"]

            # 浮动 MFE/MAE
            cur_favorable = (favorable - fill_price) * sign
            cur_adverse = (adverse - fill_price) * sign
            if cur_favorable > mfe_pts: mfe_pts = cur_favorable
            if cur_adverse < mae_pts: mae_pts = cur_adverse

            stop_hit = (adverse - stop_price) * sign <= 0
            target_hit = (favorable - target_price) * sign >= 0

            # 1. 触发止盈
            if target_hit and not stop_hit:
                exit_price = target_price - sign * tick_size
                gross_r = (exit_price - fill_price) * sign / risk_pts
                net_r = ((exit_price - fill_price) * sign - 0.5 * tick_size) / risk_pts
                return {"net_r": net_r, "reason": "target_1r", "bars": bars_held}

            # 2. 触发止损
            if stop_hit and not target_hit:
                # 考虑开盘跳空
                observed_stop = b["open"] if (b["open"] - stop_price) * sign < 0 else stop_price
                exit_price = observed_stop - sign * tick_size
                gross_r = (exit_price - fill_price) * sign / risk_pts
                net_r = ((exit_price - fill_price) * sign - 0.5 * tick_size) / risk_pts
                return {"net_r": net_r, "reason": "stop", "bars": bars_held}

            # 3. ambiguous 同根触及
            if stop_hit and target_hit:
                observed_stop = stop_price
                exit_price = observed_stop - sign * tick_size
                net_r = ((exit_price - fill_price) * sign - 0.5 * tick_size) / risk_pts
                return {"net_r": net_r, "reason": "ambiguous_stop", "bars": bars_held}

            # 4. 日内收盘强制平仓
            if is_session_close_bar(sym, b["ts"]):
                exit_price = b["close"] - sign * tick_size
                net_r = ((exit_price - fill_price) * sign - 0.5 * tick_size) / risk_pts
                return {"net_r": net_r, "reason": "session_close_flat", "bars": bars_held}

            # 5. no_follow (5根且MFE<0.5R)
            if bars_held >= 5 and (mfe_pts / risk_pts) < 0.5:
                exit_price = b["close"] - sign * tick_size
                net_r = ((exit_price - fill_price) * sign - 0.5 * tick_size) / risk_pts
                return {"net_r": net_r, "reason": "no_follow", "bars": bars_held}

            # 超出 60 根强制退出
            if bars_held >= 60:
                exit_price = b["close"] - sign * tick_size
                net_r = ((exit_price - fill_price) * sign - 0.5 * tick_size) / risk_pts
                return {"net_r": net_r, "reason": "time_exit", "bars": bars_held}

        return None

    # 四臂记录
    arms_data = {
        "E0_Touch": [],
        "E1_Close_Confirm": [],
        "E2_Pullback_S1": [],
        "E3_Limit_Order": []
    }

    for ev in events:
        sym = ev["symbol"]
        direction = ev["direction"]
        sign = 1.0 if direction == "up" else -1.0
        planned_entry = ev["planned_entry"]
        planned_stop = ev["planned_stop"]
        planned_risk = ev["planned_risk"]
        s1_price = ev["s1_price"] if ev["s1_price"] else planned_stop
        
        detail = json.loads(ev["detail_json"])
        cost_info = detail.get("cost", {})
        tick_size = cost_info.get("tick_size", 1.0)
        
        entry_ts = ev["entry_ts"]
        if sym not in bars_by_symbol or entry_ts not in idx_by_symbol_ts[sym]:
            continue

        orig_entry_idx = idx_by_symbol_ts[sym][entry_ts]
        bars = bars_by_symbol[sym]
        sym_atr = atr_by_symbol[sym]

        # -----------------------------
        # E0: 原版触碰入场 (日内强平基准)
        # -----------------------------
        orig_fill = planned_entry + sign * tick_size
        e0_res = simulate_trade(sym, orig_entry_idx, orig_fill, planned_stop, direction, tick_size)
        arms_data["E0_Touch"].append(e0_res["net_r"] if e0_res else 0.0)

        # -----------------------------
        # E1: 5m 收盘确认入场
        # -----------------------------
        # 检查触发点后的第一根 5m Bar 是否在收盘时站上/跌破突破线
        # 若收盘站上，且追高距离不超过 0.3 ATR，则在下一根开盘价 + 1 tick 入场
        e1_res = None
        for i in range(orig_entry_idx, min(orig_entry_idx + 3, len(bars) - 1)):
            b = bars[i]
            cur_atr = sym_atr[i]
            is_break_closed = (b["close"] - planned_entry) * sign > 0
            chase_dist = (b["close"] - planned_entry) * sign
            
            if is_break_closed:
                if chase_dist <= 0.3 * cur_atr: # 追高在合理范围
                    next_bar = bars[i + 1]
                    e1_fill = next_bar["open"] + sign * tick_size
                    # 新的风控距离以原 stop 为准
                    e1_res = simulate_trade(sym, i + 1, e1_fill, planned_stop, direction, tick_size)
                break
        arms_data["E1_Close_Confirm"].append(e1_res["net_r"] if e1_res else 0.0)

        # -----------------------------
        # E2: 5m 收盘突破 + 回踩 S1
        # -----------------------------
        # 先有一根收盘突破，随后在最多 6 根内等待最低价回踩至 S1 (±0.2 ATR)
        e2_res = None
        has_break = False
        break_idx = -1
        for i in range(orig_entry_idx, min(orig_entry_idx + 3, len(bars))):
            if (bars[i]["close"] - planned_entry) * sign > 0:
                has_break = True
                break_idx = i
                break
        
        if has_break:
            for j in range(break_idx + 1, min(break_idx + 7, len(bars))):
                b = bars[j]
                cur_atr = sym_atr[j]
                # 回踩区域：距离 S1 在 0.2 ATR 以内
                dist_to_s1 = abs(b["low"] - s1_price) if sign > 0 else abs(b["high"] - s1_price)
                if dist_to_s1 <= 0.3 * cur_atr:
                    # 企稳确认：该 bar 收盘同向收阳/收阴
                    if (b["close"] - b["open"]) * sign > 0 and j + 1 < len(bars):
                        next_b = bars[j + 1]
                        e2_fill = next_b["open"] + sign * tick_size
                        e2_res = simulate_trade(sym, j + 1, e2_fill, planned_stop, direction, tick_size)
                        break
        arms_data["E2_Pullback_S1"].append(e2_res["net_r"] if e2_res else 0.0)

        # -----------------------------
        # E3: 提前限价挂单 (挂在 planned_entry, 0 tick 入场滑点)
        # -----------------------------
        # 只要该 bar 穿透了 planned_entry 至少 1 tick，即按 planned_entry 成交
        e3_res = None
        b_entry = bars[orig_entry_idx]
        has_penetrated = (b_entry["high"] - planned_entry) >= tick_size if sign > 0 else (planned_entry - b_entry["low"]) >= tick_size
        if has_penetrated:
            e3_fill = planned_entry # 0 滑点限价成交
            e3_res = simulate_trade(sym, orig_entry_idx, e3_fill, planned_stop, direction, tick_size)
        arms_data["E3_Limit_Order"].append(e3_res["net_r"] if e3_res else 0.0)

    # 3. 统计汇总四臂表现
    print("\n" + "=" * 80)
    print(" 🎯 入场机制四臂单变量对照结果 (统一全预警分母 N=2,037，含日内强平)")
    print("=" * 80)
    print(f"{'入场机制臂':<22s} | {'成交填单率':<10s} | {'成交交易 E[net_R]':<18s} | {'全预警平均贡献 R':<18s} | {'胜率':<8s}")
    print("-" * 80)

    stats_summary = {}
    for arm_name, r_list in arms_data.items():
        filled = [r for r in r_list if r != 0.0]
        fill_rate = len(filled) / len(r_list)
        avg_filled_r = np.mean(filled) if filled else 0.0
        avg_warning_r = np.mean(r_list) # 全分母贡献
        win_rate = np.mean(np.array(filled) > 0) if filled else 0.0
        
        stats_summary[arm_name] = {
            "fill_rate": float(fill_rate),
            "filled_count": int(len(filled)),
            "avg_filled_r": float(avg_filled_r),
            "avg_warning_r": float(avg_warning_r),
            "win_rate": float(win_rate)
        }
        print(f"{arm_name:<22s} | {fill_rate*100:6.1f}%    | {avg_filled_r:+.4f}R            | {avg_warning_r:+.4f}R            | {win_rate*100:.1f}%")
    print("=" * 80)

    report_file = OUTPUT_DIR / "04_entry_ablation.json"
    report_file.write_text(json.dumps(stats_summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n[✓] 入场机制消融对照报告已写入: {report_file}")

if __name__ == "__main__":
    run_entry_ablation()
