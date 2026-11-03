#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Step 0: 数据隔离、冻结与基准对齐校验
验证当前数据库中的基准记录与方案第一章完全吻合：
- 2,037 笔 closed 且 quality_v2 非空
- 胜率 40.35% (822 胜)
- 平均净 R = -0.2559, gross_r = -0.2124
- target_1r (750 笔, +0.8793R), stop (846 笔, -1.2200R), no_follow (441 笔, -0.3369R)
"""

import os
import sys
import sqlite3
import json
import hashlib
from pathlib import Path

# 保证 UTF-8 输出
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / "data" / "research_diagnostic" / "ntrend-2026-11-02-232442.sqlite"
OUTPUT_DIR = ROOT / "reports" / "diagnostic_verdict_20261103"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

def verify_and_freeze():
    print(f"[*] 检查数据库文件: {DB_PATH}")
    if not DB_PATH.exists():
        raise FileNotFoundError(f"数据库不存在: {DB_PATH}")

    # 计算数据库文件 SHA-256
    print("[*] 正在计算基准数据库 SHA-256 校验和...")
    hasher = hashlib.sha256()
    with open(DB_PATH, "rb") as f:
        while chunk := f.read(1024 * 1024 * 16):
            hasher.update(chunk)
    db_sha256 = hasher.hexdigest()
    print(f"[*] 数据库 SHA-256: {db_sha256}")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # 1. 验证 2,037 笔核心基准
    sql_base = """
    SELECT 
        p.id AS event_id,
        p.symbol,
        p.direction,
        p.level,
        p.grade,
        p.warning_ts,
        p.trigger_ts,
        p.entry AS planned_entry,
        p.stop AS planned_stop,
        p.target AS planned_target,
        p.risk AS planned_risk,
        p.entry_score,
        rc.result_version,
        r.entry_ts,
        r.exit_ts,
        r.state,
        r.exit_reason,
        r.terminal_reason,
        r.quality_v2,
        r.morphology_v2,
        r.gross_amount,
        r.net_amount,
        r.gross_r,
        r.net_r,
        r.mfe_r,
        r.mae_r,
        r.bars_held,
        r.detail_json
    FROM pattern_events p
    JOIN review_current rc ON p.id = rc.event_id
    JOIN review_results r ON rc.result_version = r.result_version
    WHERE r.state = 'closed' AND r.quality_v2 IS NOT NULL
    ORDER BY p.id ASC
    """
    rows = c.execute(sql_base).fetchall()
    total_count = len(rows)
    print(f"[*] 提取核心分析样本: {total_count} 笔")
    assert total_count == 2037, f"预期 2037 笔，实际得到 {total_count} 笔"

    wins = [r for r in rows if r["net_r"] > 0]
    win_count = len(wins)
    win_rate = win_count / total_count
    avg_net_r = sum(r["net_r"] for r in rows) / total_count
    avg_gross_r = sum(r["gross_r"] for r in rows) / total_count
    total_net_r = sum(r["net_r"] for r in rows)

    print(f"[*] 胜单数: {win_count}/{total_count} = {win_rate*100:.2f}% (方案预期: 40.35%)")
    print(f"[*] 平均净 R: {avg_net_r:.4f} (方案预期: -0.2559)")
    print(f"[*] 平均毛 R: {avg_gross_r:.4f} (方案预期: -0.2124)")
    print(f"[*] 累计总净 R: {total_net_r:.2f}R")

    # 2. 按出场原因核对
    reason_stats = {}
    for r in rows:
        reason = r["exit_reason"]
        if reason not in reason_stats:
            reason_stats[reason] = []
        reason_stats[reason].append(r["net_r"])

    print("\n[*] 出场原因分布核对:")
    for reason, r_list in sorted(reason_stats.items()):
        cnt = len(r_list)
        avg_r = sum(r_list) / cnt
        print(f"  - {reason:12s}: {cnt:4d} 笔, 平均净 R = {avg_r:+.4f}")

    # 3. 隔夜跳空止损核对 (09:05 / 21:05)
    open_stop_trades = [
        r for r in rows 
        if r["exit_reason"] == "stop" and r["exit_ts"] and r["exit_ts"][11:16] in ("09:05", "21:05")
    ]
    open_stop_count = len(open_stop_trades)
    open_stop_sum_r = sum(r["net_r"] for r in open_stop_trades)
    print(f"\n[*] 开盘跳空止损核对 (09:05/21:05):")
    print(f"  - 笔数: {open_stop_count} 笔 (方案预期: 251 笔)")
    print(f"  - 累计亏损: {open_stop_sum_r:.2f}R (方案预期: -351.9R)")
    print(f"  - 占全系统亏损比例: {open_stop_sum_r / total_net_r * 100:.2f}%")

    # 保存基准元数据
    metadata = {
        "db_path": str(DB_PATH),
        "db_sha256": db_sha256,
        "sample_count": total_count,
        "win_count": win_count,
        "win_rate": win_rate,
        "avg_net_r": avg_net_r,
        "avg_gross_r": avg_gross_r,
        "total_net_r": total_net_r,
        "open_stop_count": open_stop_count,
        "open_stop_sum_r": open_stop_sum_r,
        "reason_breakdown": {k: {"count": len(v), "avg_net_r": sum(v)/len(v)} for k, v in reason_stats.items()}
    }

    out_file = OUTPUT_DIR / "00_baseline_frozen.json"
    out_file.write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n[✓] 基准冻结元数据已写入: {out_file}")

if __name__ == "__main__":
    verify_and_freeze()
