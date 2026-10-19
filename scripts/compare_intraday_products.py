"""Describe equal-window day-session ranges; these are not strategy returns."""
import csv
import hashlib
import json
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "reports" / "intraday_product_comparison_20261019"
ZONE = timezone(timedelta(hours=8))
START = "2026-08-16 00:00:00"
END = "2026-10-16 23:00:00"
PRODUCTS = {"eg": ("乙二醇", 10, .14), "rb": ("螺纹钢", 10, .12),
            "c": ("玉米", 10, .11), "sa": ("纯碱", 20, .14), "fg": ("玻璃", 20, .14)}


def main():
    results = []
    calendars = {}
    for code, (name, multiplier, margin) in PRODUCTS.items():
        path = OUT / (code + "_1m.csv") if code != "eg" else ROOT / "reports" / "eg_ten_months_20261019" / "main_1m.csv"
        minute = []
        with path.open(encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                dt = datetime.fromtimestamp(int(r["datetime_nano"]) // 1_000_000_000, ZONE).replace(tzinfo=None) + timedelta(minutes=1)
                if not START <= dt.isoformat(sep=" ") <= END or not "09:00:00" < dt.strftime("%H:%M:%S") <= "15:00:00":
                    continue
                b = {"dt": dt, **{k: float(r[k]) for k in ["open", "high", "low", "close", "volume", "close_oi"]}}
                assert b["low"] <= min(b["open"], b["close"]) <= max(b["open"], b["close"]) <= b["high"]
                minute.append(b)
        assert minute and len({b["dt"] for b in minute}) == len(minute)
        assert minute == sorted(minute, key=lambda b: b["dt"])
        days = defaultdict(list)
        groups = defaultdict(list)
        for b in minute:
            dt = b["dt"]
            days[dt.date().isoformat()].append(b)
            parent = dt.replace(minute=dt.minute // 15 * 15)
            if dt.minute % 15:
                parent += timedelta(minutes=15)
            groups[parent].append(b)
        assert all(len(bs) == 225 for bs in days.values())
        assert all(len(bs) == 15 and all((z["dt"] - a["dt"]).total_seconds() == 60 for a, z in zip(bs, bs[1:])) for bs in groups.values())
        calendars[code] = sorted(days)
        ranges = [max(b["high"] for b in bs) - min(b["low"] for b in bs) for bs in days.values()]
        r15 = [max(b["high"] for b in bs) - min(b["low"] for b in bs) for bs in groups.values()]
        range_pct = [100 * (max(b["high"] for b in bs) - min(b["low"] for b in bs)) / bs[0]["open"] for bs in days.values()]
        last = minute[-1]
        results.append({"code": code, "name": name, "days": len(days), "bars15": len(groups),
                        "day_range_median_points": statistics.median(ranges),
                        "day_range_median_CNY_one_lot": statistics.median(ranges) * multiplier,
                        "day_range_pct_median": statistics.median(range_pct),
                        "bar15_range_median_points": statistics.median(r15),
                        "bar15_range_median_CNY_one_lot": statistics.median(r15) * multiplier,
                        "multiplier": multiplier, "last_day_close": last["close"], "last_day_dt": last["dt"].isoformat(sep=" "),
                        "reference_company_margin_rate": margin, "estimated_margin_using_day_close": last["close"] * multiplier * margin,
                        "raw_path": str(path.resolve()), "raw_sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    assert all(v == calendars["eg"] for v in calendars.values())
    payload = {"start": START, "end": END, "sessions": "Shanghai local calendar day session only; 225 minutes/day; no night or overnight gap",
               "description": "Supplier main continuous series, range statistics only, no strategy or fill simulation. Fifteen-minute ranges never cross sessions. Day range includes intraday breaks; it is not continuously executable profit.",
               "margin": "Illustration using 2026-10-16 day CLOSE, not settlement; Guojin 2026-10-19 rates. EG2611 uses its special14%; remaining products use ordinary-month rates; actual broker/contract may differ.",
               "margin_source": "https://www.gjqh.com.cn/ws-2003417-c0002_3-cn/news_95194.shtml", "calendars_equal": True, "results": results}
    (OUT / "comparison.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# 日内波幅对照：不是策略收益", "", "2026-08-17—2026-10-16，北京时间日盘；每个品种均为相同日期、每日225分钟。主力连续数据来自天勤EDB，EG使用此前已保存快照，其余本次下载，原始URL和SHA256见sources.json、comparison.json。", "",
             "每手金额为价格波幅乘合约乘数，尚未扣手续费/滑点，不能当作可获得利润。日盘总波幅含日内休市间隔；15分钟波幅不跨休市，不含夜盘或日盘开盘前的跳空。此次仅比较行情幅度，没有检验哪种策略赚钱。", "",
             "| 品种 | 样本天数 | 日盘波幅中位数：点 | 日盘波幅中位数：元/手 | 15分钟波幅中位数：点 | 15分钟波幅中位数：元/手 |", "|---|---:|---:|---:|---:|---:|"]
    for r in results:
        lines.append(f"| {r['name']} | {r['days']} | {r['day_range_median_points']:g} | {r['day_range_median_CNY_one_lot']:g} | {r['bar15_range_median_points']:g} | {r['bar15_range_median_CNY_one_lot']:g} |")
    lines += ["", "保证金仅按10月16日日盘收盘价与[国金期货10月19日公司标准](https://www.gjqh.com.cn/ws-2003417-c0002_3-cn/news_95194.shtml)估算，非结算账单，非用户账户标准；EG2611采用特殊14%，其他品种按普通月份。",
              "", "| 品种 | 日盘收盘价 | 参考公司保证金率 | 估算一手保证金（元） |", "|---|---:|---:|---:|"]
    for r in results:
        lines.append(f"| {r['name']} | {r['last_day_close']:g} | {r['reference_company_margin_rate']:.0%} | {r['estimated_margin_using_day_close']:,.0f} |")
    lines += ["", "在该样本中，EG波幅明显高于其他四个候选，玉米和螺纹钢不宜仅因低保证金就排在日内波段研究的最前面。纯碱/玻璃可作对照，但其15分钟波幅也小于EG。不能由此推断长期排名不变或EG策略必然获利。",
              "若假设开仓和平仓各多付1跳，EG和RB每手共20元额外滑点；分别相当于15分钟波幅中位数420元和50元的约4.8%和40%。这只是成本尺度示例，不是可实现收益率，且尚未加入手续费、买卖价差及成交路径。",
              "严格日内研究需区分不跨自然日、不跨交易时段和不跨任何休市间隔。EG旧策略包含日盘到夜盘持仓；取消这种持仓需要重新回放完整规则，不能直接从旧结果中减掉亏损单。"]
    (OUT / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
