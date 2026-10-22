#!/usr/bin/env python3
"""Fixed hourly Dual Thrust parameters on three earlier months of SR bars."""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta
from pathlib import Path

import backtest_sr_dual_thrust_article as a

base = a.base
OUT = base.ROOT/"reports"/"sr_dual_thrust_history_20261021"
START, END = date(2026, 6, 21), date(2026, 9, 20)
DOWNLOAD_START, DOWNLOAD_END = "2026-06-08 00:00:00", "2026-10-21 00:00:00"
CFG = a.Config(cycle=5, ks=.3, kx=.5, intraday=True)
CONTRACTS = ["SR609", "SR611", "SR701"]
REQUESTS = [("main_daily.csv", "KQ.m@CZCE.SR", 86400)] + [
    (c+"_daily.csv", "CZCE."+c, 86400) for c in CONTRACTS] + [
    (c+"_1m.csv", "CZCE."+c, 60) for c in ["SR609", "SR701"]]
DATA_DOC = "https://doc.shinnytech.com/edb/latest/md_server.html"
CALENDAR_SOURCE = "https://www.zrhxqh.com/cn/about/detail/id/17804.html"
EXCHANGE_CALENDAR = "https://www.czce.com.cn/cn/gyjys/jysdt/ggytz/webinfo/2026/6/076aff13d5d44c38814746239cd3091e.htm"


def save(name, value):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT/name).write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def write_csv(name, rows):
    if rows:
        with (OUT/name).open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader(); w.writerows(rows)


def fetch_one(request):
    filename, symbol, period = request
    url = "https://edb.shinnytech.com/md/kline?"+urllib.parse.urlencode(dict(
        symbol=symbol, period=period, start_time=DOWNLOAD_START, end_time=DOWNLOAD_END))
    req = urllib.request.Request(url, headers={"User-Agent": "ntrend-research/1.0"})
    with urllib.request.urlopen(req, timeout=50) as r:
        raw, http_date = r.read(), r.headers.get("Date")
    decoded = raw.decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(decoded)))
    assert rows and "datetime_nano" in rows[0], (filename, raw[:200])
    (OUT/filename).write_bytes(raw)
    return dict(file=filename, symbol=symbol, period=period, url=url, rows=len(rows),
                sha256=hashlib.sha256(raw).hexdigest(), http_date=http_date,
                retrieved_at=datetime.now(base.ZONE).isoformat(), first=rows[0], last=rows[-1])


def fetch():
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT/"sources.json"
    sources = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    old = {s["file"]: s for s in sources}
    for name, source in old.items():
        assert hashlib.sha256((OUT/name).read_bytes()).hexdigest() == source["sha256"]
    requests = [r for r in REQUESTS if r[0] not in old]
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(fetch_one, r) for r in requests]
        for f in as_completed(futures):
            source = f.result(); sources.append(source)
            save("sources.json", sorted(sources, key=lambda s: s["file"]))
            print("Fetched", source["file"], source["rows"], flush=True)


def load(name):
    return base.load_csv(OUT/name)


def inspect():
    daily = {c: {b.dt.date(): b for b in load(c+"_daily.csv")} for c in CONTRACTS}
    main = {b.dt.date(): b for b in load("main_daily.csv")}
    last = None; segments = []
    for d, b in main.items():
        matches = [c for c in CONTRACTS if d in daily[c] and
                   (b.h,b.l,b.c,b.oi) == (daily[c][d].h,daily[c][d].l,daily[c][d].c,daily[c][d].oi)]
        assert len(matches) == 1, (d, matches)
        member = matches[0]
        if last != member:
            segments.append(dict(start=str(d), contract=member))
            last = member
    print(json.dumps(dict(main_segments=segments), ensure_ascii=False, indent=2), flush=True)
    save("daily_member_inspection.json", dict(main_segments=segments))


def trading_days(start, end):
    result = []
    while start <= end:
        if base.is_trading_day(start): result.append(start)
        start += timedelta(days=1)
    return result


def freeze_plan():
    spec = dict(parameters=asdict(CFG), optimization=False, period_minutes=60,
                earlier_start=str(START), earlier_end=str(END),
                original_start=str(base.START), original_end=str(base.END),
                earlier_buckets=[["2026-06-21", "2026-07-20"], ["2026-07-21", "2026-08-20"], ["2026-08-21", "2026-09-20"]],
                contract_policy="At next trading-day open, switch only if one candidate has BOTH maximum previous trading-day volume and close OI; otherwise retain incumbent. Candidate contracts SR609/SR611/SR701. Daily main OHLC/OI used only for verification, never routing.",
                history_policy="Indicators use the selected concrete contract's own preceding bars; never splice prices across contracts. No overnight positions; no roll position or roll PnL.",
                fixed_contract_control="SR701 over entire period, reported independently regardless of which policy earns more",
                execution="Completed-minute close -> next available minute open; opposite signal closes first; no new indicator/filter/stop/size search",
                liquidation="14:58/22:58 open, entries blocked for remainder of each session",
                lots=1, tick_size=1, multiplier=10, adverse_slip_ticks_per_side=1, fee_per_side=3,
                calendar=dict(holidays=[str(x) for x in sorted(base.HOLIDAYS)], no_nights=[str(x) for x in sorted(base.NO_NIGHTS)], source=CALENDAR_SOURCE, original=EXCHANGE_CALENDAR),
                source=DATA_DOC, source_directory=str(OUT),
                validation="Parameters were chosen from the later Sep-Oct window; earlier three months were not used for this selection. Retrospective extension, not real-time forward evidence.",
                freeze="No parameter changes after inspecting earlier-period results. Report three-month total, three successive month windows, calendar-month breakdown, original month and four-month total; all fixed 1 lot.")
    path = OUT/"analysis_plan.json"
    if path.exists(): assert json.loads(path.read_text(encoding="utf-8")) == spec
    else: save("analysis_plan.json", spec)


def select_contracts(dates, daily):
    """Routing reads exclusively the prior completed trading day's data."""
    current = None; schedule = []
    for d in dates:
        prev = base.previous_trading_day(d)
        known = {c: bars[prev] for c,bars in daily.items() if prev in bars}
        oi = max(known, key=lambda c: (known[c].oi, c))
        volume = max(known, key=lambda c: (known[c].v, c))
        if oi == volume: current = oi
        assert current is not None, (d, "Need an incumbent when the initial volume/OI maxima disagree")
        schedule.append(dict(trading_date=str(d), known_at_date=str(prev), contract=current,
                             prior_oi_max=oi, prior_volume_max=volume,
                             prior_oi=known[current].oi, prior_volume=known[current].v))
    return schedule


def prepare():
    sources = json.loads((OUT/"sources.json").read_text(encoding="utf-8"))
    for s in sources:
        assert hashlib.sha256((OUT/s["file"]).read_bytes()).hexdigest() == s["sha256"]
    assert {s["file"] for s in sources} == {r[0] for r in REQUESTS}
    daily = {c: {b.dt.date(): b for b in load(c+"_daily.csv")} for c in CONTRACTS}
    main = {b.dt.date(): b for b in load("main_daily.csv")}
    minutes = {c: load(c+"_1m.csv") for c in ["SR609", "SR701"]}
    dates = trading_days(START, base.END)
    schedule = select_contracts(dates, daily)
    groups = {c: defaultdict(list) for c in minutes}
    for c, bars in minutes.items():
        for b in bars: groups[c][base.trading_date(b.dt)].append(b)
    coverage = []
    for item in schedule:
        d = date.fromisoformat(item["trading_date"])
        selected = item["contract"]
        member = daily[selected][d]; benchmark = main[d]
        assert (member.h,member.l,member.c,member.oi) == (benchmark.h,benchmark.l,benchmark.c,benchmark.oi), (d, selected)
        item["matches_supplier_main_daily_hlc_oi"] = True
        for c in {selected, "SR701"}:
            bars = groups[c][d]
            observed = {b.dt for b in bars}; expected = base.expected_minutes(d)
            assert observed == expected, (d, c, "missing", sorted(expected-observed)[:5], "extra", sorted(observed-expected)[:5])
            ds = daily[c][d]
            actual = (bars[0].o,max(b.h for b in bars),min(b.l for b in bars),bars[-1].c,bars[-1].oi)
            assert actual == (ds.o,ds.h,ds.l,ds.c,ds.oi), (d,c,actual,ds)
            coverage.append(dict(trading_date=str(d), contract=c, minutes=len(bars), missing=0, extra=0,
                                 daily_ohlc_and_oi_match=True, night_minutes=sum(b.dt.hour>=21 for b in bars)))
    frozen = a.prepare()
    old = {b.dt:b for b in frozen if base.START <= base.trading_date(b.dt) <= base.END}
    recent = {b.dt:b for b in minutes["SR701"] if base.START <= base.trading_date(b.dt) <= base.END}
    assert old == recent, "New data vintage changed previously scored concrete-contract minutes"
    save("coverage.json", coverage)
    write_csv("contract_schedule.csv", schedule)
    save("data_verification.json", dict(earlier_trading_days=sum(date.fromisoformat(r["trading_date"])<=END for r in schedule),
                                        total_trading_days=len(dates), earlier_minutes=sum(len(groups[r["contract"]][date.fromisoformat(r["trading_date"])]) for r in schedule if date.fromisoformat(r["trading_date"])<=END),
                                        total_minutes=sum(len(groups[r["contract"]][date.fromisoformat(r["trading_date"])]) for r in schedule),
                                        prior_original_minutes_identical=len(old), calendar_source=CALENDAR_SOURCE,
                                        all_prior_day_routes_match_supplier_member=True))
    return minutes, schedule


def replay_schedule(minutes, schedule, cfg=CFG, fixed_contract=None):
    trades, equity, bands, days = [], [], [], []
    cash = 0.
    for item in schedule:
        d = date.fromisoformat(item["trading_date"])
        contract = fixed_contract or item["contract"]
        s, ts, eq, audit = a.replay(minutes[contract], cfg, start=d, end=d, contract=contract)
        assert s["cross_session_trades"] == 0 and s["terminal_trades"] == 0
        assert s["net_pnl"] == sum(t["net_pnl"] for t in ts) == eq[-1]["equity"]
        for row in eq:
            equity.append({**row, "realized_cash":row["realized_cash"]+cash, "equity":row["equity"]+cash,
                           "trading_date":str(d), "contract":contract})
        cash += s["net_pnl"]
        trades.extend({**t, "trading_date":str(d)} for t in ts)
        bands.extend({**b, "trading_date":str(d), "contract":contract} for b in audit)
        days.append(dict(trading_date=str(d), contract=contract, trades=len(ts), net_pnl=s["net_pnl"],
                         gross_pnl=s["gross_pnl"], slippage_cost=s["slippage_cost"], fees=s["fees"],
                         cumulative_net=cash, minute_close_max_drawdown=s["minute_close_max_drawdown"]))
    stats = base.summarize(trades, equity)
    assert stats["net_pnl"] == cash == equity[-1]["equity"]
    return stats, trades, equity, bands, days


def window(result, start, end):
    _, trades, curve, _, _ = result
    ts = [t for t in trades if start <= date.fromisoformat(t["trading_date"]) <= end]
    before = sum(t["net_pnl"] for t in trades if date.fromisoformat(t["trading_date"]) < start)
    eq = [{**r, "realized_cash":r["realized_cash"]-before, "equity":r["equity"]-before}
          for r in curve if start <= date.fromisoformat(r["trading_date"]) <= end]
    stats = base.summarize(ts, eq)
    assert eq and stats["net_pnl"] == eq[-1]["equity"] == sum(t["net_pnl"] for t in ts)
    return stats


def windows():
    return [("新增月1", date(2026,6,21),date(2026,7,20)),
            ("新增月2", date(2026,7,21),date(2026,8,20)),
            ("新增月3", date(2026,8,21),date(2026,9,20)),
            ("新增三个月", START,END), ("原选参月份",base.START,base.END), ("四个月合计",START,base.END)]


def plot_equity(rolling, fixed):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    fig, (ax, ddax) = plt.subplots(2, 1, figsize=(11,6), sharex=True, gridspec_kw={"height_ratios":[2.6,1]})
    for result, label, color in [(rolling,"Historical main contract", "#176ca5"), (fixed,"Fixed SR2701 control", "#c17620")]:
        rows = result[2]
        xs = [datetime.fromisoformat(r["bar_end"]) for r in rows]
        ys = [r["equity"] for r in rows]
        peak = 0.; drawdowns = []
        for y in ys:
            peak = max(peak, y); drawdowns.append(y-peak)
        ax.plot(xs,ys,label=label,color=color,linewidth=1)
        ddax.plot(xs,drawdowns,color=color,linewidth=.8)
    for axis in [ax,ddax]:
        axis.axvspan(datetime.combine(base.START,datetime.min.time()), datetime(2026,10,21),color="#999999",alpha=.12)
        axis.axhline(0,color="#777777",linewidth=.6)
        axis.grid(alpha=.2)
    ax.set_title("SR hourly Dual Thrust | Cycle=5, Ks=0.3, Kx=0.5 | 1 lot | net of costs")
    ax.set_ylabel("Cumulative net P&L (CNY)")
    ax.legend(loc="best",fontsize=9)
    ax.text(.99,.02,"Shaded: previously optimized Sep 21 - Oct 20 window",transform=ax.transAxes,ha="right",fontsize=8,color="#555555")
    ddax.set_ylabel("Drawdown (CNY)")
    ddax.xaxis.set_major_locator(mdates.MonthLocator())
    ddax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    fig.tight_layout()
    fig.savefig(OUT/"equity.png",dpi=160)
    plt.close(fig)


def report(results, breakdown, stress):
    def f(x): return "N/A" if x is None else f"{x:,.2f}"
    def row(title, start, end, s):
        return f"| {title} | {start}—{end} | {s['trades']} | {s['win_rate']:.2%} | {f(s['payoff_ratio'])} | {f(s['net_pnl'])} | {f(s['minute_close_max_drawdown'])} |"
    lines = ["# SR小时Dual Thrust：固定参数向前追溯三个月", "",
             "固定Cycle=5、Ks=0.3、Kx=0.5，未再优化。新增区间2026-06-21—09-20，实际交易日6/22—9/18；原区间9/21—10/20独立展示。每次1手，开平各1跳滑点、各3元手续费，完整一笔成本26元。",
             "主结果按前一交易日成交量、收盘持仓量同时最大的合约在下一交易日切换；两者不一致则保留原合约。该因果规则与此期间供应商主力成员日线逐日完全一致。6/22—8/12使用SR609（SR2609），8/13起SR701（SR2701）。",
             "", "## 可交易主力合约结果", "",
             "| 区间 | 日期 | 笔数 | 净胜率 | 盈亏比 | 净盈亏/元 | 分钟权益最大回撤/元 |", "|---|---|---:|---:|---:|---:|---:|"]
    for title,start,end in windows(): lines.append(row(title,start,end,window(results["rolling"],start,end)))
    lines += ["", "参数来自后面9—10月的搜索，本次将其原样用于更早行情，是追溯历史检验，不是实际前向运行的成绩。三个新增月份按21日至次月20日划分，和原来一个月窗口一致。", "",
              "## 始终交易SR2701的同合约对照", "",
              "这组用于区分换月政策与合约自身的影响，独立展示，无论哪组收益高都保留。早期SR2701不是主力，真实成交与本研究固定滑点假设可能不同。", "",
              "| 区间 | 日期 | 笔数 | 净胜率 | 盈亏比 | 净盈亏/元 | 分钟权益最大回撤/元 |", "|---|---|---:|---:|---:|---:|---:|"]
    for title,start,end in windows(): lines.append(row(title,start,end,window(results["fixed_SR701"],start,end)))
    lines += ["", "## 按自然月拆分的主力结果", "",
              "| 月份 | 笔数 | 毛利/元 | 滑点/元 | 手续费/元 | 净盈亏/元 |", "|---|---:|---:|---:|---:|---:|"]
    for r in breakdown:
        if r["version"] == "rolling":
            lines.append(f"| {r['calendar_month']} | {r['trades']} | {f(r['gross_pnl'])} | {f(r['slippage_cost'])} | {f(r['fees'])} | {f(r['net_pnl'])} |")
    lines += ["", "## 新增三个月成本敏感性", "",
              "| 版本 | 每边滑点 | 笔数 | 净盈亏/元 |", "|---|---:|---:|---:|"]
    for s in stress:
        lines.append(f"| {s['version']} | {s['slip_ticks']}跳 | {s['trades']} | {f(s['net_pnl'])} |")
    lines += ["", "## 权益与回撤", "", f"![权益与回撤]({(OUT/'equity.png').as_posix()})", "",
              "图为四个月连续的分钟收盘权益；灰色区域为原来参与参数搜索的月份。回撤含持仓浮盈亏，逐笔净利已扣双边成本，未指定本金，不计算资金收益率或年化。", "",
              "## 实现与数据核验", "",
              "- 每个自然小时开盘，用前4根已完成小时K线加当前开盘快照计算Range，轨道整小时固定；不会提前用当前小时高低收。休息时段形成的部分小时保留真实分钟，不补空K线。",
              "- 一分钟收盘确认，下一可交易分钟开盘成交；反向突破先平仓，后续分钟再入场。14:58/22:58开盘强平并停止该时段入场。",
              "- 换月时已经空仓；使用新合约自己的历史K线热身，不用连续合约的换月价差构造Range或收益。contract_schedule.csv保存每个交易日选择及其前日依据。",
              "- 评分期实际合约逐分钟与预期交易日历一致，无缺失或补价；按分钟聚合的日线OHLC及末笔持仓量逐日匹配具体合约日线。6/19端午休市，6/18无夜盘；6/22从日盘恢复。",
              "- 新下载的原窗口5280根SR2701分钟与原冻结数据逐字段一致；原窗口收益精确复现。sources.json记录URL、抓取时刻和SHA256，coverage.json记录覆盖核验。",
              "- 仍是分钟行情的成交近似，未模拟买卖盘、排队、涨跌停、保证金约束和市场冲击。固定1跳滑点是假设，不是实盘成交保证。", "",
              f"数据接口：[天勤EDB文档]({DATA_DOC})；日历：[郑商所通知转载]({CALENDAR_SOURCE})（[原通知]({EXCHANGE_CALENDAR})）。", "",
              "```powershell", "python scripts/backtest_sr_dual_thrust_history.py --fetch",
              "python scripts/backtest_sr_dual_thrust_history.py",
              "python -m unittest discover -s tests -p 'test_sr_dual_thrust*.py'", "```", "",
              "results.json包含各窗口统计；window_summary.csv、calendar_months.csv、*_trades.csv、*_equity.csv、*_bands.csv、*_days.csv包含完整复现明细。", ""]
    (OUT/"REPORT.md").write_text("\n".join(lines),encoding="utf-8")


def run():
    freeze_plan()
    minutes, schedule = prepare()
    results = {}; summary = {}; window_rows = []; breakdown = []; stress = []
    for version, fixed in [("rolling",None),("fixed_SR701","SR701")]:
        result = replay_schedule(minutes,schedule,fixed_contract=fixed)
        results[version] = result
        summary[version] = {}
        for suffix, rows in zip(["trades","equity","bands","days"],result[1:]):
            write_csv(version+"_"+suffix+".csv",rows)
        for title,start,end in windows():
            stats = window(result,start,end)
            summary[version][title] = stats
            window_rows.append(dict(version=version,window=title,start=str(start),end=str(end),**stats))
        for month,first,last in [("2026-06",date(2026,6,21),date(2026,6,30)),
                                 ("2026-07",date(2026,7,1),date(2026,7,31)),
                                 ("2026-08",date(2026,8,1),date(2026,8,31)),
                                 ("2026-09",date(2026,9,1),date(2026,9,30)),
                                 ("2026-10",date(2026,10,1),base.END)]:
            breakdown.append(dict(version=version,calendar_month=month,start=str(first),end=str(last),**window(result,first,last)))
        earlier = [r for r in schedule if date.fromisoformat(r["trading_date"]) <= END]
        original_fills = [(t["contract"],t["entry_bar"],t["exit_bar"],t["direction"]) for t in result[1] if date.fromisoformat(t["trading_date"]) <= END]
        for slip in [1,2,3]:
            other = replay_schedule(minutes,earlier,replace(CFG,slip_ticks=slip),fixed_contract=fixed)
            assert original_fills == [(t["contract"],t["entry_bar"],t["exit_bar"],t["direction"]) for t in other[1]]
            stress.append(dict(version=version,slip_ticks=slip,**other[0]))
        print("Completed",version,flush=True)
    original = json.loads((base.ROOT/"reports"/"sr_dual_thrust_article_optimization_20261021"/"results.json").read_text(encoding="utf-8"))["hindsight"]["intraday"]["stats"]
    for version in summary:
        for k,v in summary[version]["原选参月份"].items(): assert original[k] == v, (version,k,v,original[k])
        assert sum(summary[version][x]["net_pnl"] for x in ["新增月1","新增月2","新增月3"]) == summary[version]["新增三个月"]["net_pnl"]
        assert summary[version]["新增三个月"]["net_pnl"]+summary[version]["原选参月份"]["net_pnl"] == summary[version]["四个月合计"]["net_pnl"]
    write_csv("window_summary.csv",window_rows)
    write_csv("calendar_months.csv",breakdown)
    save("results.json",dict(parameters=asdict(CFG),windows=summary,stress=stress))
    plot_equity(results["rolling"],results["fixed_SR701"])
    report(results,breakdown,stress)
    paths = [__file__,a.__file__,base.__file__]
    save("verification.json",dict(passed=True,code_hashes={str(p):hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths},
                                  checks=["frozen parameters; no optimization", "causal prior-day routing; supplier member daily matches",
                                          "actual contract minute coverage and daily OHLC/OI", "old5280 minute snapshot identical",
                                          "original month statistics reproduced", "no cross-session or terminal positions",
                                          "daily/trade/cash/equity/cost reconciliation", "monthly and total PnL reconcile", "cost-only scenarios preserve fills"]))
    print(json.dumps(summary,ensure_ascii=False,indent=2),flush=True)
    print("Report:",OUT/"REPORT.md",flush=True)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--fetch", action="store_true")
    args = p.parse_args()
    if args.fetch: fetch()
    inspect()
    run()


if __name__ == "__main__":
    main()
