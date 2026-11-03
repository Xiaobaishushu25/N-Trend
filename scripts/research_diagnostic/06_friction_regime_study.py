#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Step 6: 交易摩擦门槛与大周期过滤消融实验 (P4)
针对方案第1章与P4指出的关键结构性死穴：
1. 交易摩擦与目标空间比 (Friction-to-Target Ratio):
   - 当 1R 目标空间仅为 8 tick 时，2.5 tick 往返摩擦吞噬了 31.25% 的利润空间！
   - 过滤摩擦占比：无门槛 vs <=20% vs <=15% vs <=10%
2. 60m 大周期顺势过滤 (60m Regime Alignment):
   - 基于 warning_ts 前的 60m K 线计算 EMA20 与 EMA60
   - 多头必须 Price > EMA20 > EMA60，空头必须 Price < EMA20 < EMA60
3. 组合最优策略：
   - 摩擦门槛 + 60m 顺势 + 入场优化 (E3限价/E1收盘确认) + 止盈优化 (1.5R/2.0R)
   - 进行 1.5 倍滑点压力测试，验证最终期望 E[net_R] 是否稳定大于 0！
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

def run_friction_and_regime_study():
    print("[*] 启动交易摩擦门槛与大周期过滤消融实验 (P4)...")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    sql_events = """
    SELECT 
        p.id AS event_id,
        p.symbol,
        p.direction,
        p.warning_ts,
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

    # 1. 载入 5m 与 15m/60m K 线数据
    print("[*] 载入 K 线数据构建 60m 大周期趋势特征...")
    sql_5m = "SELECT symbol, ts, open, high, low, close FROM klines WHERE timeframe = '5m' ORDER BY symbol, ts ASC"
    rows_5m = c.execute(sql_5m).fetchall()
    bars_5m = defaultdict(list)
    idx_5m = defaultdict(dict)
    for r in rows_5m:
        sym = r["symbol"]
        ts = r["ts"]
        bars = bars_5m[sym]
        idx_5m[sym][ts] = len(bars)
        bars.append({"ts": ts, "open": r["open"], "high": r["high"], "low": r["low"], "close": r["close"]})

    # 从 5m 合成 60m K 线用于严谨计算 warning_ts 前的 EMA20 与 EMA60
    # 每 12 根 5m 为一个 60m 周期
    bars_60m = defaultdict(list)
    for sym, b_list in bars_5m.items():
        cur_60 = None
        for b in b_list:
            dt = datetime.fromisoformat(b["ts"])
            h_bucket = dt.replace(minute=0, second=0)
            if cur_60 is None or cur_60["bucket"] != h_bucket:
                if cur_60:
                    bars_60m[sym].append(cur_60)
                cur_60 = {
                    "bucket": h_bucket,
                    "ts": b["ts"],
                    "open": b["open"],
                    "high": b["high"],
                    "low": b["low"],
                    "close": b["close"]
                }
            else:
                cur_60["high"] = max(cur_60["high"], b["high"])
                cur_60["low"] = min(cur_60["low"], b["low"])
                cur_60["close"] = b["close"]
                cur_60["ts"] = b["ts"]
        if cur_60:
            bars_60m[sym].append(cur_60)

    # 计算 60m EMA
    def calc_ema(values, span):
        alpha = 2.0 / (span + 1.0)
        ema = np.zeros(len(values))
        for i, v in enumerate(values):
            if i == 0:
                ema[i] = v
            else:
                ema[i] = alpha * v + (1.0 - alpha) * ema[i-1]
        return ema

    regime_60m_by_sym = {}
    for sym, b60 in bars_60m.items():
        closes = [b["close"] for b in b60]
        ema20 = calc_ema(closes, 20)
        ema60 = calc_ema(closes, 60)
        regime_60m_by_sym[sym] = {
            "ts_list": [b["ts"] for b in b60],
            "close": closes,
            "ema20": ema20,
            "ema60": ema60
        }

    def get_60m_regime(sym, warning_ts):
        if sym not in regime_60m_by_sym:
            return "unknown"
        data = regime_60m_by_sym[sym]
        ts_list = data["ts_list"]
        # 找到 warning_ts 之前最新已完成的 60m bar (严格防未来函数)
        idx = -1
        for i, t in enumerate(ts_list):
            if t <= warning_ts:
                idx = i
            else:
                break
        if idx < 60: # 历史不足
            return "neutral"
        c = data["close"][idx]
        e20 = data["ema20"][idx]
        e60 = data["ema60"][idx]
        if c > e20 and e20 > e60:
            return "bull"
        elif c < e20 and e20 < e60:
            return "bear"
        else:
            return "neutral"

    # 仿真函数：支持不同摩擦系数压力测试与止盈目标
    def simulate_trade_extended(sym, entry_idx, fill_price, stop_price, direction, tick_size, target_mult, friction_multiplier=1.0):
        sign = 1.0 if direction == "up" else -1.0
        risk_pts = (fill_price - stop_price) * sign
        if risk_pts <= 0:
            return None
        
        target_price = fill_price + sign * (risk_pts * target_mult)
        bars = bars_5m[sym]
        
        # 摩擦模型：基准往返 2.5 tick
        total_friction_pts = (2.5 * friction_multiplier) * tick_size
        
        bars_held = 0
        mfe_pts = 0.0

        for i in range(entry_idx, min(entry_idx + 80, len(bars))):
            b = bars[i]
            bars_held += 1
            favorable = b["high"] if sign > 0 else b["low"]
            adverse = b["low"] if sign > 0 else b["high"]

            cur_favorable = (favorable - fill_price) * sign
            if cur_favorable > mfe_pts: mfe_pts = cur_favorable

            stop_hit = (adverse - stop_price) * sign <= 0
            target_hit = (favorable - target_price) * sign >= 0

            if target_hit and not stop_hit:
                exit_price = target_price - sign * (tick_size * friction_multiplier)
                gross_pnl = (exit_price - fill_price) * sign
                net_r = (gross_pnl - 0.5 * tick_size * friction_multiplier) / risk_pts
                return {"net_r": net_r, "reason": "target"}

            if stop_hit and not target_hit:
                observed = b["open"] if (b["open"] - stop_price) * sign < 0 else stop_price
                exit_price = observed - sign * (tick_size * friction_multiplier)
                gross_pnl = (exit_price - fill_price) * sign
                net_r = (gross_pnl - 0.5 * tick_size * friction_multiplier) / risk_pts
                return {"net_r": net_r, "reason": "stop"}

            if stop_hit and target_hit:
                exit_price = stop_price - sign * (tick_size * friction_multiplier)
                gross_pnl = (exit_price - fill_price) * sign
                net_r = (gross_pnl - 0.5 * tick_size * friction_multiplier) / risk_pts
                return {"net_r": net_r, "reason": "ambiguous"}

            # 日内强平
            if is_session_close_bar(sym, b["ts"]):
                exit_price = b["close"] - sign * (tick_size * friction_multiplier)
                gross_pnl = (exit_price - fill_price) * sign
                net_r = (gross_pnl - 0.5 * tick_size * friction_multiplier) / risk_pts
                return {"net_r": net_r, "reason": "session_flat"}

            # no_follow
            if bars_held >= 5 and (mfe_pts / risk_pts) < 0.5:
                exit_price = b["close"] - sign * (tick_size * friction_multiplier)
                gross_pnl = (exit_price - fill_price) * sign
                net_r = (gross_pnl - 0.5 * tick_size * friction_multiplier) / risk_pts
                return {"net_r": net_r, "reason": "no_follow"}

        last_b = bars[min(entry_idx + bars_held - 1, len(bars) - 1)]
        exit_price = last_b["close"] - sign * (tick_size * friction_multiplier)
        net_r = ((exit_price - fill_price) * sign - 0.5 * tick_size * friction_multiplier) / risk_pts
        return {"net_r": net_r, "reason": "timeout"}

    # 2. 统计摩擦空间比分布
    # friction_ratio = (2.5 tick) / planned_risk
    parsed_trades = []
    for ev in events:
        sym = ev["symbol"]
        direction = ev["direction"]
        planned_risk = ev["planned_risk"]
        planned_entry = ev["planned_entry"]
        planned_stop = ev["planned_stop"]
        warning_ts = ev["warning_ts"]
        entry_ts = ev["entry_ts"]
        
        detail = json.loads(ev["detail_json"])
        cost_info = detail.get("cost", {})
        tick_size = cost_info.get("tick_size", 1.0)

        risk_in_ticks = planned_risk / tick_size if tick_size > 0 else 10.0
        friction_ratio = 2.5 / risk_in_ticks if risk_in_ticks > 0 else 1.0
        regime_60 = get_60m_regime(sym, warning_ts)
        is_trend_aligned = (direction == "up" and regime_60 == "bull") or (direction == "down" and regime_60 == "bear")

        parsed_trades.append({
            "event_id": ev["event_id"],
            "symbol": sym,
            "direction": direction,
            "warning_ts": warning_ts,
            "entry_ts": entry_ts,
            "planned_entry": planned_entry,
            "planned_stop": planned_stop,
            "planned_risk": planned_risk,
            "tick_size": tick_size,
            "risk_in_ticks": risk_in_ticks,
            "friction_ratio": friction_ratio,
            "regime_60": regime_60,
            "is_trend_aligned": is_trend_aligned,
            "orig_net_r": ev["orig_net_r"]
        })

    print(f"\n[*] 样本总数: {len(parsed_trades)} 笔")
    f_ratios = [t["friction_ratio"] for t in parsed_trades]
    print(f"[*] 往返摩擦占目标空间比分布:")
    print(f"  - 平均摩擦占比: {np.mean(f_ratios)*100:.2f}%")
    print(f"  - 中位数摩擦占比: {np.median(f_ratios)*100:.2f}%")
    print(f"  - 摩擦占比 > 25% (极度危险微空间): {sum(1 for f in f_ratios if f > 0.25)} 笔 ({sum(1 for f in f_ratios if f > 0.25)/len(f_ratios)*100:.1f}%)")
    print(f"  - 摩擦占比 <= 15% (健康目标空间): {sum(1 for f in f_ratios if f <= 0.15)} 笔 ({sum(1 for f in f_ratios if f <= 0.15)/len(f_ratios)*100:.1f}%)")

    # 3. 摩擦门槛网格测试 (在日内强平基准下)
    print("\n" + "=" * 85)
    print(" 🔬 摩擦门槛消融实验 (在日内强平 + 1R 目标下，测试不同摩擦比上限)")
    print("=" * 85)
    print(f"{'摩擦比过滤门槛':<22s} | {'保留样本数':<12s} | {'保留比例':<10s} | {'平均净期望 E[net_R]':<18s} | {'胜率':<8s}")
    print("-" * 85)

    friction_thresholds = [1.0, 0.25, 0.20, 0.15, 0.10]
    thresh_labels = ["无门槛 (全部保留)", "<= 25% (排除超微止损)", "<= 20%", "<= 15% (推荐)", "<= 10% (严格大空间)"]

    friction_results = {}
    for th, lbl in zip(friction_thresholds, thresh_labels):
        subset = [t for t in parsed_trades if t["friction_ratio"] <= th]
        r_list = []
        for t in subset:
            sym = t["symbol"]
            if sym not in bars_5m or t["entry_ts"] not in idx_5m[sym]:
                continue
            e_idx = idx_5m[sym][t["entry_ts"]]
            sign = 1.0 if t["direction"] == "up" else -1.0
            fill = t["planned_entry"] + sign * t["tick_size"]
            sim = simulate_trade_extended(sym, e_idx, fill, t["planned_stop"], t["direction"], t["tick_size"], target_mult=1.0)
            if sim: r_list.append(sim["net_r"])
        
        arr = np.array(r_list)
        mean_r = float(np.mean(arr)) if len(arr) > 0 else 0.0
        win_rate = float(np.mean(arr > 0)) if len(arr) > 0 else 0.0
        friction_results[lbl] = {
            "threshold": th,
            "count": len(arr),
            "mean_r": mean_r,
            "win_rate": win_rate
        }
        print(f"{lbl:<22s} | {len(arr):4d} 笔      | {len(arr)/len(parsed_trades)*100:5.1f}%    | {mean_r:+.4f}R            | {win_rate*100:.1f}%")
    print("=" * 85)

    # 4. 终极组合消融与 1.5 倍滑点压力测试
    # 策略组合测试：
    # C0: 原版科研基准 (含隔夜跳空)
    # C1: 日内强平 + E0 触碰
    # C2: 日内强平 + 摩擦比 <= 15%
    # C3: 日内强平 + 摩擦比 <= 15% + 60m 顺势
    # C4: 日内强平 + 摩擦比 <= 15% + E3 限价挂单
    # C5: 日内强平 + 摩擦比 <= 15% + E3 限价挂单 + 1.5R 盈亏比
    # C6: C5 在 1.5 倍滑点压力测试下表现

    print("\n" + "=" * 95)
    print(" 🚀 策略组合进阶改造与压力测试对比表 (从负期望到正期望的归因路径)")
    print("=" * 95)
    print(f"{'策略组合描述':<42s} | {'样本量':<8s} | {'E[net_R]':<12s} | {'胜率':<8s} | {'盈亏比 PF':<10s} | {'期望翻正':<8s}")
    print("-" * 95)

    combinations = [
        ("C0_原版复盘基准 (含隔夜跳空)", lambda t: True, "touch", 1.0, 1.0, True),
        ("C1_消除隔夜 (严格日内强平)", lambda t: True, "touch", 1.0, 1.0, False),
        ("C2_日内强平 + 摩擦比<=15%", lambda t: t["friction_ratio"] <= 0.15, "touch", 1.0, 1.0, False),
        ("C3_C2 + 60m大周期顺势过滤", lambda t: t["friction_ratio"] <= 0.15 and t["is_trend_aligned"], "touch", 1.0, 1.0, False),
        ("C4_C2 + E3限价单入场(0滑点)", lambda t: t["friction_ratio"] <= 0.15, "limit", 1.0, 1.0, False),
        ("C5_C4 + 1.5R止盈扩张 (黄金组合)", lambda t: t["friction_ratio"] <= 0.15, "limit", 1.5, 1.0, False),
        ("C6_黄金组合 1.5倍滑点压力测试", lambda t: t["friction_ratio"] <= 0.15, "limit", 1.5, 1.5, False),
    ]

    combo_summary = {}

    for name, flt, entry_mode, target_m, fric_mult, allow_over in combinations:
        subset = [t for t in parsed_trades if flt(t)]
        r_list = []
        for t in subset:
            if name.startswith("C0"):
                r_list.append(t["orig_net_r"])
                continue
            sym = t["symbol"]
            if sym not in bars_5m or t["entry_ts"] not in idx_5m[sym]:
                continue
            e_idx = idx_5m[sym][t["entry_ts"]]
            sign = 1.0 if t["direction"] == "up" else -1.0
            
            # 入场价
            if entry_mode == "touch":
                fill = t["planned_entry"] + sign * (t["tick_size"] * fric_mult)
            else:
                # 限价单：按 planned_entry 成交
                fill = t["planned_entry"]

            sim = simulate_trade_extended(sym, e_idx, fill, t["planned_stop"], t["direction"], t["tick_size"], target_mult=target_m, friction_multiplier=fric_mult)
            if sim:
                r_list.append(sim["net_r"])

        arr = np.array(r_list)
        wins = arr[arr > 0]
        losses = arr[arr <= 0]
        mean_r = float(np.mean(arr)) if len(arr) > 0 else 0.0
        win_rate = float(len(wins) / len(arr)) if len(arr) > 0 else 0.0
        pf = float(np.sum(wins) / abs(np.sum(losses))) if len(losses) > 0 and np.sum(losses) != 0 else float('nan')
        is_pos = "✅ 翻正" if mean_r > 0 else "❌ 负期望"

        combo_summary[name] = {
            "count": len(arr),
            "mean_r": mean_r,
            "win_rate": win_rate,
            "profit_factor": pf,
            "is_positive": (mean_r > 0)
        }
        print(f"{name:<42s} | {len(arr):4d} 笔  | {mean_r:+.4f}R     | {win_rate*100:5.1f}%   | {pf:6.3f}     | {is_pos}")
    print("=" * 95)

    report_file = OUTPUT_DIR / "06_friction_regime.json"
    report_file.write_text(json.dumps({
        "friction_grid": friction_results,
        "combinations": combo_summary
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n[✓] 交易摩擦与环境过滤消融报告已写入: {report_file}")

if __name__ == "__main__":
    run_friction_and_regime_study()
