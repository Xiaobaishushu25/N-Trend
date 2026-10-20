#!/usr/bin/env python3
"""Reproducible SR Dual Thrust research; stdlib only, no broker/production imports.

python scripts/backtest_sr_dual_thrust.py             # replay frozen CSV snapshots
python scripts/backtest_sr_dual_thrust.py --fetch     # refresh public EDB snapshots
python -m unittest discover -s tests -p test_sr_dual_thrust.py

The strategy follows vn.py's one-day H-L variant, k1=.4/k2=.6. Changes for
Chinese commodity futures: exchange trading dates, executable tick prices,
and separate pre-close liquidation of night/day sessions. No fitting/search.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import statistics
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "reports" / "sr_dual_thrust_20261021"
ZONE = timezone(timedelta(hours=8))
START, END = date(2026, 9, 21), date(2026, 10, 20)
DOWNLOAD_START, DOWNLOAD_END = "2026-09-14 00:00:00", "2026-10-21 00:00:00"
HOLIDAYS = {date(2026, 9, 25)} | {date(2026, 10, d) for d in range(1, 8)}
NO_NIGHTS = {date(2026, 9, 24), date(2026, 9, 30)}
STRATEGY_URL = "https://github.com/vnpy/vnpy_ctastrategy/blob/main/vnpy_ctastrategy/strategies/dual_thrust_strategy.py"
CALENDAR_URL = "https://www.hongtaqh.com/wsyyt/ywgg/202609/20260921_12313.html"
DATA_URL = "https://doc.shinnytech.com/edb/latest/md_server.html"


@dataclass(frozen=True)
class Config:
    k1: float = .4
    k2: float = .6
    lots: int = 1
    multiplier: int = 10
    tick: int = 1
    slip_ticks: int = 1
    fee_per_side: int = 3
    day_flat: time = time(14, 55)
    night_flat: time = time(22, 55)


@dataclass(frozen=True)
class Bar:
    dt: datetime  # START, Asia/Shanghai naive; immutable supplier observations.
    o: float
    h: float
    l: float
    c: float
    v: float
    oi: float


def save(name, obj):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def write_csv(name, rows):
    if not rows:
        return
    with (OUT / name).open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def configuration():
    return {
        "created_at": datetime.now(ZONE).isoformat(),
        "start_trading_date": str(START), "end_trading_date": str(END),
        "config": asdict(Config()), "contract": "CZCE.SR701 (SR2701)",
        "timeframe": "1 minute; all signal orders activate only on next bar",
        "variant": "vn.py one-previous-trading-day high-low simplified Dual Thrust, NOT N-day HH/LC/HC/LL range",
        "rules": [
            "Bands=trading-day opening price +/- k*previous complete trading-day (high-low).",
            "Upper band ceil to tick; lower band floor to tick. Bands fixed for full exchange trading day.",
            "Flat: after a completed minute, arm upper buy if close>day open, otherwise lower short.",
            "At most one long entry and one short entry per exchange trading day, night/day combined.",
            "Long exits at lower band; short exits at upper. Reverse there only if opposite direction unused.",
            "Pending orders originate from previous completed minute; lunch recess carries orders/positions.",
            "At14:55/22:55 minute OPEN cancel stops, flatten remaining position, no new entries until next session.",
            "Night21:00 belongs to next exchange trading date, including Friday->Monday and holidays.",
            "Every entry/exit adverse1 tick plus3 CNY/lot/side; reversal costs both a close and a new open.",
            "Stop gaps fill at actual minute open if worse than trigger, then add slip.",
        ],
        "parameter_search": False,
        "source": STRATEGY_URL, "data_documentation": DATA_URL, "calendar_source": CALENDAR_URL,
        "limits": "OHLC stop-fill approximation; no order queue, bid/ask, limit-lock or liquidity simulation. No live deployment.",
    }


REQUESTS = [
    ("main", "KQ.m@CZCE.SR", 60), ("SR701", "CZCE.SR701", 60),
    ("main_daily", "KQ.m@CZCE.SR", 86400), ("SR701_daily", "CZCE.SR701", 86400),
    ("SR611_daily", "CZCE.SR611", 86400), ("SR705_daily", "CZCE.SR705", 86400),
]


def fetch():
    manifest = []
    for name, symbol, period in REQUESTS:
        query = dict(symbol=symbol, period=period, start_time=DOWNLOAD_START, end_time=DOWNLOAD_END)
        url = "https://edb.shinnytech.com/md/kline?" + urllib.parse.urlencode(query)
        req = urllib.request.Request(url, headers={"User-Agent": "ntrend-research/1.0"})
        with urllib.request.urlopen(req, timeout=40) as response:
            raw = response.read()
            http_date = response.headers.get("Date")
        rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
        assert rows and "datetime_nano" in rows[0], (name, raw[:200])
        filename = name + ("_1m.csv" if period == 60 else ".csv")
        (OUT / filename).write_bytes(raw)
        manifest.append(dict(name=name, symbol=symbol, period=period, file=filename, url=url,
                             sha256=hashlib.sha256(raw).hexdigest(), rows=len(rows),
                             http_date=http_date, retrieved_at=datetime.now(ZONE).isoformat()))
        save("sources.json", manifest)
        print("Fetched", name, len(rows), flush=True)


def load_csv(filename):
    result = []
    with (OUT / filename).open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            ns = int(row["datetime_nano"])
            assert ns % 60_000_000_000 == 0
            dt = datetime.fromtimestamp(ns // 1_000_000_000, ZONE).replace(tzinfo=None)
            values = [float(row[k]) for k in ["open", "high", "low", "close", "volume", "close_oi"]]
            assert all(math.isfinite(v) for v in values), (filename, dt)
            b = Bar(dt, *values)
            assert b.l <= min(b.o, b.c) <= max(b.o, b.c) <= b.h, (filename, b)
            assert all(p == round(p) for p in [b.o, b.h, b.l, b.c]), (filename, b)
            assert b.v >= 0 and b.oi >= 0
            result.append(b)
    assert result and result == sorted(result, key=lambda b: b.dt)
    assert len({b.dt for b in result}) == len(result), filename
    return result


def is_trading_day(d):
    return d.weekday() < 5 and d not in HOLIDAYS


def previous_trading_day(d):
    d -= timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def trading_date(dt):
    d = dt.date()
    if dt.hour >= 21:
        d += timedelta(days=1)
        while not is_trading_day(d):
            d += timedelta(days=1)
    return d


def expected_minutes(d):
    assert is_trading_day(d)
    result = set()
    for hour, minute, length in [(9, 0, 75), (10, 30, 60), (13, 30, 90)]:
        first = datetime.combine(d, time(hour, minute))
        result.update(first + timedelta(minutes=i) for i in range(length))
    # First day after a holiday has no preceding night session. Friday night
    # before an ordinary Monday is present and belongs to Monday.
    previous = previous_trading_day(d)
    if previous not in NO_NIGHTS:
        first = datetime.combine(previous, time(21))
        result.update(first + timedelta(minutes=i) for i in range(120))
    return result


def prepare():
    manifest = json.loads((OUT / "sources.json").read_text(encoding="utf-8"))
    for source in manifest:
        assert hashlib.sha256((OUT / source["file"]).read_bytes()).hexdigest() == source["sha256"]
    minutes, main = load_csv("SR701_1m.csv"), load_csv("main_1m.csv")
    groups = defaultdict(list)
    for b in minutes:
        groups[trading_date(b.dt)].append(b)
    scored_dates = []
    d = START
    while d <= END:
        if is_trading_day(d):
            scored_dates.append(d)
        d += timedelta(days=1)
    coverage = []
    for d in [previous_trading_day(START)] + scored_dates:
        expected = expected_minutes(d)
        observed = {b.dt for b in groups[d]}
        assert observed == expected, (d, "missing", sorted(expected-observed)[:5], "extra", sorted(observed-expected)[:5])
        coverage.append(dict(trading_date=str(d), warmup=d < START, minutes=len(observed),
                             night_minutes=sum(b.dt.hour >= 21 for b in groups[d]), missing=0, extra=0))
    main_daily = {b.dt.date(): b for b in load_csv("main_daily.csv")}
    member_daily = {b.dt.date(): b for b in load_csv("SR701_daily.csv")}
    member_checks = []
    for d in [previous_trading_day(START)] + scored_dates:
        a, b = main_daily[d], member_daily[d]
        assert (a.h, a.l, a.c, a.oi) == (b.h, b.l, b.c, b.oi), d
        bs = groups[d]
        actual = (bs[0].o, max(x.h for x in bs), min(x.l for x in bs), bs[-1].c)
        assert actual == (b.o, b.h, b.l, b.c), (d, actual, b)
        member_checks.append(dict(trading_date=str(d), contract="SR701", daily_hlc_and_close_oi_match=True,
                                  main_daily_open=a.o, concrete_daily_open=b.o,
                                  minute_aggregation_matches_daily_ohlc=True))
    alternatives = {}
    for name in ["SR611", "SR705"]:
        other = {b.dt.date(): b for b in load_csv(name + "_daily.csv")}
        alternatives[name] = sum((main_daily[d].c, main_daily[d].oi) == (other[d].c, other[d].oi)
                                 for d in [previous_trading_day(START)] + scored_dates)
        assert alternatives[name] == 0
    main_by_time = {b.dt: b for b in main}
    equal = sum((main_by_time[b.dt].c, main_by_time[b.dt].oi) == (b.c, b.oi)
                for b in minutes if START <= trading_date(b.dt) <= END)
    checks = dict(coverage=coverage, contract_daily_checks=member_checks,
                  alternative_daily_close_oi_matches=alternatives,
                  primary_contract="SR701; identified on2026-09-18 before scoring, no in-window roll",
                  scored_minutes=sum(len(groups[d]) for d in scored_dates),
                  main_minute_exact_close_oi_matches=equal,
                  main_minute_differences="Supplier main and member minute snapshots differ; only concrete SR701 prices enter signals and PnL.",
                  calendar_source=CALENDAR_URL, start=str(START), end=str(END))
    save("data_verification.json", checks)
    return groups, scored_dates, checks


def levels(previous, opening, cfg):
    span = max(b.h for b in previous) - min(b.l for b in previous)
    upper = math.ceil((opening + cfg.k1 * span) / cfg.tick - 1e-10) * cfg.tick
    lower = math.floor((opening - cfg.k2 * span) / cfg.tick + 1e-10) * cfg.tick
    return span, upper, lower


def stop_fill(bar, direction, trigger):
    """Unslipped resting stop; one active price level per minute, no path choice."""
    if direction == 1 and bar.h >= trigger:
        return max(bar.o, trigger)
    if direction == -1 and bar.l <= trigger:
        return min(bar.o, trigger)
    return None


def after_flat_time(dt, cfg):
    return dt.time() >= (cfg.night_flat if dt.hour >= 21 else cfg.day_flat)


def replay_day(bars, previous, cfg=Config()):
    """Chronological pending-order replay; also accepts a prefix for causal tests."""
    d = trading_date(bars[0].dt)
    span, upper, lower = levels(previous, bars[0].o, cfg)
    opening = bars[0].o
    entered = set()
    position = None
    pending = None
    trades, curve = [], []
    cash = 0.
    fills = 0

    def enter(direction, base, bar, armed_at):
        nonlocal cash, position, fills
        assert position is None and direction not in entered
        entered.add(direction)
        fill = base + direction * cfg.slip_ticks * cfg.tick
        fee = cfg.fee_per_side * cfg.lots
        cash -= fee
        fills += 1
        position = dict(trading_date=str(d), contract="SR701", direction=direction, lots=cfg.lots,
                        entry_bar=bar.dt.isoformat(sep=" "), entry_armed_at=armed_at,
                        entry_base=base, entry_fill=fill, entry_fee=fee,
                        previous_range=span, day_open=opening, upper=upper, lower=lower)

    def leave(base, bar, reason):
        nonlocal cash, position, fills
        assert position is not None
        p = position
        exit_fill = base - p["direction"] * cfg.slip_ticks * cfg.tick
        gross = (base-p["entry_base"]) * p["direction"] * cfg.multiplier * cfg.lots
        slip = cfg.slip_ticks * cfg.tick * cfg.multiplier * cfg.lots * 2
        fee = cfg.fee_per_side * cfg.lots * 2
        net = gross-slip-fee
        cash += (exit_fill-p["entry_fill"]) * p["direction"] * cfg.multiplier * cfg.lots - fee/2
        fills += 1
        trades.append({**p, "exit_bar": bar.dt.isoformat(sep=" "), "exit_reason": reason,
                       "exit_base": base, "exit_fill": exit_fill, "gross_pnl": gross,
                       "slippage_cost": slip, "fees": fee, "net_pnl": net,
                       "holding_elapsed_minutes": (bar.dt-datetime.fromisoformat(p["entry_bar"])).total_seconds()/60})
        position = None

    for bar in bars:
        assert trading_date(bar.dt) == d
        if after_flat_time(bar.dt, cfg):
            pending = None
            if position:
                leave(bar.o, bar, "night_flat" if bar.dt.hour >= 21 else "day_flat")
        elif pending is not None:
            direction, trigger, action, armed_at = pending
            assert datetime.fromisoformat(armed_at) <= bar.dt
            base = stop_fill(bar, direction, trigger)
            if base is not None:
                if action == "entry":
                    enter(direction, base, bar, armed_at)
                else:
                    leave(base, bar, "opposite_band")
                    if action == "reverse":
                        enter(direction, base, bar, armed_at)
        # Only completed bars can place/update stops for the NEXT minute.
        pending = None
        if not after_flat_time(bar.dt, cfg):
            armed_at = (bar.dt+timedelta(minutes=1)).isoformat(sep=" ")
            if position:
                side = -position["direction"]
                pending = (side, upper if side == 1 else lower,
                           "reverse" if side not in entered else "exit", armed_at)
            else:
                side = 1 if bar.c > opening else -1
                if side not in entered:
                    pending = (side, upper if side == 1 else lower, "entry", armed_at)
        floating = (bar.c-position["entry_fill"]) * position["direction"] * cfg.multiplier * cfg.lots if position else 0.
        curve.append(dict(bar_end=(bar.dt+timedelta(minutes=1)).isoformat(sep=" "), trading_date=str(d),
                          realized_cash=cash, floating_pnl=floating, equity=cash+floating,
                          position=position["direction"] * cfg.lots if position else 0))
    return trades, curve, dict(trading_date=str(d), previous_range=span, day_open=opening, upper=upper, lower=lower,
                              trades=len(trades), fills=fills, net_pnl=cash, open_position=position is not None)


def summarize(trades, curve):
    pnl = [t["net_pnl"] for t in trades]
    wins, losses = [x for x in pnl if x > 0], [x for x in pnl if x < 0]
    avg_win = statistics.mean(wins) if wins else None
    avg_loss = -statistics.mean(losses) if losses else None
    peak, dd = 0., 0.
    for row in curve:
        peak = max(peak, row["equity"])
        dd = max(dd, peak-row["equity"])
    return dict(trades=len(pnl), wins=len(wins), losses=len(losses), breakeven=len(pnl)-len(wins)-len(losses),
                win_rate=len(wins)/len(pnl) if pnl else None,
                average_win=avg_win, average_loss=avg_loss,
                payoff_ratio=avg_win/avg_loss if avg_win is not None and avg_loss else None,
                profit_factor=sum(wins)/-sum(losses) if losses else None,
                gross_pnl=sum(t["gross_pnl"] for t in trades),
                slippage_cost=sum(t["slippage_cost"] for t in trades), fees=sum(t["fees"] for t in trades),
                net_pnl=sum(pnl), average_net_pnl=statistics.mean(pnl) if pnl else None,
                best_trade=max(pnl) if pnl else None, worst_trade=min(pnl) if pnl else None,
                minute_close_max_drawdown=dd)


def replay(groups, dates, cfg=Config()):
    trades, curve, days = [], [], []
    cash = 0.
    for d in dates:
        today, equity, day = replay_day(groups[d], groups[previous_trading_day(d)], cfg)
        assert not day["open_position"], (d, "remaining position")
        assert day["fills"] == len(today)*2
        assert abs(day["net_pnl"]-sum(t["net_pnl"] for t in today)) < 1e-8
        for row in equity:
            row["realized_cash"] += cash
            row["equity"] += cash
        cash += day["net_pnl"]
        days.append(day)
        trades.extend(today)
        curve.extend(equity)
    stats = summarize(trades, curve)
    assert abs(cash-stats["net_pnl"]) < 1e-8
    assert abs(curve[-1]["equity"]-cash) < 1e-8
    assert stats["net_pnl"] == stats["gross_pnl"]-stats["slippage_cost"]-stats["fees"]
    for t in trades:
        assert trading_date(datetime.fromisoformat(t["entry_bar"])) == trading_date(datetime.fromisoformat(t["exit_bar"]))
        a, z = datetime.fromisoformat(t["entry_bar"]), datetime.fromisoformat(t["exit_bar"])
        assert (a.hour >= 21) == (z.hour >= 21), ("crossed night/day close", t)
        assert a.date() == z.date(), ("crossed calendar date", t)
    return stats, trades, curve, days


def report(stats, days, sensitivity, checks):
    def f(value, digits=2):
        return "N/A" if value is None else f"{value:,.{digits}f}"
    lines = ["# SR 日内 Dual Thrust：近一个月固定参数回测", "",
             f"交易日：{START}—{END}，共 {len(days)} 个交易日、{checks['scored_minutes']} 根完整1分钟K线。",
             "夜盘按下一交易日归属，因此首个交易日包含9月18日晚；末日不包含10月20日晚（归属10月21日）。",
             "", "## 结果（1手，开平各1跳滑点、各3元手续费）", "",
             "| 指标 | 结果 |", "|---|---:|",
             f"| 完整开平交易 | {stats['trades']}笔 |",
             f"| 盈利 / 亏损 / 持平 | {stats['wins']} / {stats['losses']} / {stats['breakeven']} |",
             f"| 净收益胜率 | {f(stats['win_rate']*100)}% |",
             f"| 盈亏比（平均净盈利÷平均净亏损绝对值） | {f(stats['payoff_ratio'])} |",
             f"| 平均盈利 / 平均亏损绝对值 | {f(stats['average_win'])} / {f(stats['average_loss'])}元 |",
             f"| 盈利因子（净盈利合计÷净亏损合计绝对值） | {f(stats['profit_factor'])} |",
             f"| 扣成本前毛盈利 | {f(stats['gross_pnl'])}元 |",
             f"| 滑点成本 | {f(stats['slippage_cost'])}元 |",
             f"| 手续费 | {f(stats['fees'])}元 |",
             f"| 净盈利 | {f(stats['net_pnl'])}元 |",
             f"| 单笔平均净收益 | {f(stats['average_net_pnl'])}元 |",
             f"| 分钟收盘权益最大回撤（含浮盈亏） | {f(stats['minute_close_max_drawdown'])}元 |",
             "", "## 策略定义与成交口径", "",
             "- 采用上一轮讨论的vn.py简化版本：前一个完整交易日最高价减最低价为Range；k1=0.4、k2=0.6。没有搜索参数。不同于使用N日HH/LC/HC/LL的另一类Dual Thrust版本。",
             "- 以当前交易日首次开盘（有夜盘时是前一交易日21:00）为中心，固定上下轨；上轨向上取整、下轨向下取整到1元有效价位。全天不重置。",
             "- 空仓时，上一根完整分钟收盘高于当日开盘则挂上轨买入停止单，否则挂下轨卖出停止单；次分钟才可成交。",
             "- 多仓下破下轨退出，空仓上破上轨退出；相反方向当天尚未入场时同时反手。每交易日最多一次开多、一次开空，夜盘/日盘共享计数。没有额外止盈、ATR止损或趋势过滤。",
             "- 日盘14:55、夜盘22:55以该分钟开盘价加不利滑点强平并停止该时段入场。这是预定时间可执行的分钟开盘近似，避免用收盘后才知道的最后价格成交。午间和10:15休息可持仓。",
             "- 每次开、平各1跳不利滑点，SR1跳=1元/吨×10吨/手=10元/手；开、平各3元手续费。一笔完整1手交易总成本26元，反手含一笔平仓及一笔新开仓，分别计费。",
             "- 停止单跳空穿价按该分钟实际开盘价成交，再加滑点；不按较好的触发价虚构成交。每分钟只激活一个触发价位，反手/退出按上一根已完成K线的订单执行，不在同根K线临时追加新停止单。",
             "", "## 数据核验", "",
             "- 数据来自天勤EDB公开历史接口，原始CSV、URL、下载时刻、SHA256见sources.json。此接口无需账号，本次没有连接柜台。",
             "- 主连与SR701（2027年1月，即SR2701）的日线高低收及收盘持仓量在评分窗口和前一热身交易日完全一致；SR611/SR705对照均不匹配。9月18日已经确认SR701身份，窗口内未换月。供应商主连与具体合约的部分日线开盘价不一致，记录保存在data_verification.json；不使用主连开盘价计算信号。",
             "- 具体SR701一分钟OHLC逐交易日聚合与其日线OHLC一致。主连与具体合约分钟快照有差异；主连只用于成员核验，所有信号和成交使用具体SR701原始价格。",
             "- 按日历核验评分区间及前一交易日的每一分钟，无缺失、重复或补价。9月24日/30日无夜盘，中秋与国庆休市；周五夜盘归属周一。详情见data_verification.json。",
             "", "## 成本敏感性（只改滑点，不调参数）", "",
             "| 每边滑点 | 笔数 | 胜率 | 盈亏比 | 净盈利 |", "|---|---:|---:|---:|---:|"]
    for s in sensitivity:
        lines.append(f"| {s['slip_ticks']}跳 | {s['trades']} | {f(s['win_rate']*100)}% | {f(s['payoff_ratio'])} | {f(s['net_pnl'])}元 |")
    lines += ["", "## 逐交易日结果", "", "| 交易日 | 前日振幅 | 开盘价 | 上轨 | 下轨 | 笔数 | 净盈利 |", "|---|---:|---:|---:|---:|---:|---:|"]
    for d in days:
        lines.append(f"| {d['trading_date']} | {f(d['previous_range'],0)} | {f(d['day_open'],0)} | {f(d['upper'],0)} | {f(d['lower'],0)} | {d['trades']} | {f(d['net_pnl'])}元 |")
    lines += ["", "## 局限与复现", "",
              "本次为一个月、固定规则的历史检验，未进行参数优化。未模拟买卖盘价差、限价排队、封板无法成交、市场冲击及保证金约束。一分钟OHLC无法确认逐笔成交时刻；成交时间列标识所属分钟，entry_armed_at是订单最早可生效时刻。最大回撤为分钟收盘权益口径，不是逐笔最大回撤。",
              "没有指定本金，故只报告1手盈亏金额，不计算年化收益率或资金收益率。结果不代表未来表现。研究脚本不接生产执行系统。",
              "", "```powershell", "python scripts/backtest_sr_dual_thrust.py",
              "python -m unittest discover -s tests -p test_sr_dual_thrust.py", "```", "",
              f"原策略：[vn.py源码]({STRATEGY_URL})；数据：[EDB说明]({DATA_URL})；休市：[郑商所通知转载]({CALENDAR_URL})。", ""]
    (OUT / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fetch", action="store_true")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    if not (OUT / "analysis_plan.json").exists():
        save("analysis_plan.json", configuration())
    if args.fetch:
        fetch()
    groups, dates, checks = prepare()
    stats, trades, curve, days = replay(groups, dates)
    sensitivity = []
    for slip in [0, 1, 2]:
        s, ts, _, _ = replay(groups, dates, replace(Config(), slip_ticks=slip))
        assert [(t["entry_bar"], t["exit_bar"], t["direction"], t["entry_base"], t["exit_base"]) for t in ts] == [
            (t["entry_bar"], t["exit_bar"], t["direction"], t["entry_base"], t["exit_base"]) for t in trades]
        s["slip_ticks"] = slip
        sensitivity.append(s)
    save("summary.json", {"start": START, "end": END, "trading_days": len(dates), "primary": stats, "sensitivity": sensitivity})
    write_csv("trades.csv", trades)
    write_csv("equity.csv", curve)
    write_csv("daily.csv", days)
    report(stats, days, sensitivity, checks)
    save("verification.json", dict(passed=True, checks=[
        "source SHA256 verified", "exact session-minute calendar coverage",
        "specific-contract minute/daily OHLC reconciliation", "dominant membership checked",
        "no position across day/night session close", "at most one entry per direction/trading day",
        "next-minute pending orders only", "every round-trip exactly two fees/slips",
        "daily and full-period trade/cash/equity reconciliation", "cost-only sensitivity preserves fills",
    ]))
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    print("Report:", OUT / "REPORT.md")


if __name__ == "__main__":
    main()
