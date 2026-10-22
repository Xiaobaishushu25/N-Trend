#!/usr/bin/env python3
"""Public article screenshot rules, causal minute approximation on frozen SR.

The supplied article's visible code is a rolling hourly strategy without
session liquidation. This is NOT the earlier daily-opening Dual Thrust.
No parameter fitting. No broker/production imports. Market orders from each
completed minute execute at the next available minute OPEN.
"""
from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import asdict, dataclass, replace
from datetime import datetime, time, timedelta
from pathlib import Path

import backtest_sr_dual_thrust as base

OUT = base.ROOT / "reports" / "sr_dual_thrust_article_20261021"
ARTICLE = "https://baijiahao.baidu.com/s?id=1850642692355790177&wfr=spider&for=pc"
EARLY_ARTICLE = "https://www.youquant.com/bbs-topic/4757"
EARLY_CODE = "https://www.youquant.com/strategy/177504"
FMZ_DOC = "https://www.fmz.com/lang/en/syntax-guide/fun/market/exchange.getrecords"


@dataclass(frozen=True)
class Config:
    ks: float = 3
    kx: float = 2
    cycle: int = 5
    intraday: bool = False
    slip_ticks: int = 1

    @property
    def label(self):
        return f"ks{self.ks}_kx{self.kx}_{'intraday' if self.intraday else 'carry'}"


CONFIGS = [Config(), Config(ks=1), Config(intraday=True), Config(ks=1, intraday=True)]


def save(name, obj):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def write_csv(name, rows):
    if rows:
        with (OUT / name).open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)


def plan():
    spec = dict(start=str(base.START), end=str(base.END), configs=[asdict(c) for c in CONFIGS],
                original=ARTICLE, early_article=EARLY_ARTICLE, early_code=EARLY_CODE, api_documentation=FMZ_DOC,
                no_optimization=True, source_data=str(base.OUT),
                interpretation=[
                    "Visible code hardcodes3*Range /2*Range even though screenshot UI saysKs1/Kx2. Test literal3/2 and parameterized1/2 separately.",
                    "Source GetRecords default1h; at first observation of a NEW hourly bar refresh bands only once.",
                    "Cycle5 includes4 completed hourly bars plus current forming bar at first known opening tick (OHLC all=open). Never use completed current-hour high/low/close at its opening.",
                    "Aggregate Shanghai clock-hour buckets from1m starts; break-spanning partial clock hours remain partial bars, no invented minutes. FMZ original aggregation alignment unverified.",
                    "Observe a signal on completed1minute close, execute next trading minute OPEN. Approximation to1-second source loop, not FMZ tick-by-tick replication.",
                    "Source opens long/short while flat; on opposite break CLOSE ONLY. Next callback may reenter, modeled on next completed minute then next open; no immediate atomic reversal.",
                    "No daily/session anchor or position reset in source. Primary carry retains overnight/weekend/holiday positions.",
                    "Separate intraday adaptations flat at14:58/22:58 open and cancel pending orders; only these variants enforce original user intraday preference.",
                    "Month ends by scheduled liquidation at2026-10-20 14:59 minute OPEN. Terminal trade explicitly flagged; no end-price hidden position.",
                    "1actual lot;10CNY/point;every fill1adverse tick+3CNY fee. Original quoted limit +/-1 is not an additional paid slip here; no limit-order queue or fill simulation.",
                ],
                article_curve=dict(symbol="rb000", period="1hour", start="2015-02-22", end="2019-12-06",
                                   capital=1000000, cumulative_percent=4.399, reported_annual_percent=.634,
                                   net_pnl=43987.302, source="visible screenshots, not independently reproduced"))
    path = OUT / "analysis_plan.json"
    if path.exists():
        assert json.loads(path.read_text(encoding="utf-8")) == spec
    else:
        save("analysis_plan.json", spec)


def prepare():
    article = json.loads((OUT / "article_source.json").read_text(encoding="utf-8"))
    assert hashlib.sha256((OUT / "article.html").read_bytes()).hexdigest() == article["sha256"]
    for src in json.loads((OUT / "image_sources.json").read_text(encoding="utf-8")):
        assert hashlib.sha256((OUT / src["file"]).read_bytes()).hexdigest() == src["sha256"]
    sources = json.loads((base.OUT / "sources.json").read_text(encoding="utf-8"))
    for src in sources:
        assert hashlib.sha256((base.OUT / src["file"]).read_bytes()).hexdigest() == src["sha256"]
    minutes = base.load_csv("SR701_1m.csv")
    scored = [b for b in minutes if base.START <= base.trading_date(b.dt) <= base.END]
    dates = sorted({base.trading_date(b.dt) for b in scored})
    assert len(dates) == 16 and len(scored) == 5280
    for d in dates:
        assert {b.dt for b in scored if base.trading_date(b.dt) == d} == base.expected_minutes(d)
    save("inputs.json", dict(sources=sources, actual_contract="SR701 / SR2701", scored_days=16, scored_minutes=5280,
                             last_complete_minute_start=str(scored[-1].dt), first_score_minute=str(scored[0].dt)))
    return minutes


def hour_key(dt):
    return period_key(dt, 60)


def period_key(dt, period_minutes=60):
    """Clock-aligned bars; exchange recesses never generate synthetic bars."""
    if not isinstance(period_minutes, int) or period_minutes <= 0 or 60 % period_minutes:
        raise ValueError("Period must be a positive whole-minute divisor of 60")
    return dt.replace(minute=(dt.minute//period_minutes)*period_minutes, second=0, microsecond=0)


def make_bands(completed, opening, cfg):
    if len(completed) < cfg.cycle-1:
        return None
    # Current forming bar at its OPEN: H=L=C=O. Subsequent minutes cannot
    # influence this hour's bands, even though its final OHLC exists in CSV.
    sample = completed[-(cfg.cycle-1):] + [(opening, opening, opening)]
    hh = max(h for h,l,c in sample)
    hc = max(c for h,l,c in sample)
    ll = min(l for h,l,c in sample)
    lc = min(c for h,l,c in sample)
    span = max(hh-lc, hc-ll)
    assert span > 0, "Zero-width source bands would cause same-callback churn; cannot model silently"
    return span, round(opening+cfg.ks*span, 2), round(opening-cfg.kx*span, 2)


def signal(side, price, upper, lower):
    if side == 0:
        if price >= upper:
            return "open_long"
        if price <= lower:
            return "open_short"
    elif (side == 1 and price <= lower) or (side == -1 and price >= upper):
        return "close"
    return None


def flat_time(b):
    return b.dt.time() >= (time(22,58) if b.dt.hour >= 21 else time(14,58))


def replay(minutes, cfg=Config(), terminal=True, start=None, end=None, period_minutes=60, contract="SR701"):
    start = base.START if start is None else start
    end = base.END if end is None else end
    completed = []
    current = None
    bounds = None
    pending = position = None
    cash = 0.
    trades, equity, audit = [], [], []
    scored = [b for b in minutes if start <= base.trading_date(b.dt) <= end]
    assert scored, "No scored minutes in requested window"
    last = scored[-1].dt if terminal else None

    def enter(side, b, intent):
        nonlocal position, cash
        assert position is None
        position = dict(contract=contract, direction=side, lots=1, signal_bar=intent[1],
                        entry_bar=str(b.dt), entry_base=b.o, entry_fill=b.o+side*cfg.slip_ticks,
                        ks=cfg.ks, kx=cfg.kx, range=intent[2], upper=intent[3], lower=intent[4])
        cash -= 3

    def leave(b, reason):
        nonlocal position, cash
        assert position is not None
        p = position
        fill = b.o-p["direction"]*cfg.slip_ticks
        gross = (b.o-p["entry_base"])*p["direction"]*10
        slip = cfg.slip_ticks*20
        cash += (fill-p["entry_fill"])*p["direction"]*10-3
        trades.append({**p, "exit_bar":str(b.dt), "exit_base":b.o, "exit_fill":fill,
                       "exit_reason":reason, "gross_pnl":gross, "slippage_cost":slip, "fees":6,
                       "net_pnl":gross-slip-6,
                       "holding_elapsed_minutes":(b.dt-datetime.fromisoformat(p["entry_bar"])).total_seconds()/60})
        position = None

    for b in minutes:
        in_score = start <= base.trading_date(b.dt) <= end
        if base.trading_date(b.dt) > end:
            break
        # The queued market instruction exists before current minute prices.
        if in_score:
            forced = (cfg.intraday and flat_time(b)) or b.dt == last
            if forced:
                pending = None
                if position:
                    leave(b, "terminal" if b.dt == last else "session_flat")
            elif pending:
                action = pending[0]
                assert datetime.fromisoformat(pending[1])+timedelta(minutes=1) <= b.dt
                if action == "close":
                    leave(b, "opposite_band")
                else:
                    enter(1 if action == "open_long" else -1, b, pending)
                pending = None
        key = period_key(b.dt, period_minutes)
        if current is None or key != current["key"]:
            if current:
                completed.append((current["h"], current["l"], current["c"]))
            current = dict(key=key, h=b.o, l=b.o, c=b.o)
            bounds = make_bands(completed, b.o, cfg)
            if in_score:
                assert bounds is not None
                audit.append({"hour_bucket" if period_minutes == 60 else "bar_bucket": str(key),
                              "first_observation": str(b.dt), "opening": b.o,
                              "completed_history_count": min(len(completed),cfg.cycle-1),
                              "range": bounds[0], "upper": bounds[1], "lower": bounds[2]})
        current.update(h=max(current["h"],b.h), l=min(current["l"],b.l), c=b.c)
        if not in_score:
            continue
        if not ((cfg.intraday and flat_time(b)) or b.dt == last):
            action = signal(position["direction"] if position else 0, b.c, bounds[1], bounds[2])
            if action:
                pending = (action, str(b.dt), *bounds)
        floating = (b.c-position["entry_fill"])*position["direction"]*10 if position else 0.
        equity.append(dict(bar_end=str(b.dt+timedelta(minutes=1)), realized_cash=cash,
                           floating_pnl=floating, equity=cash+floating,
                           position=position["direction"] if position else 0))
    if terminal:
        assert position is None and pending is None
        stats = base.summarize(trades,equity)
        assert stats["net_pnl"] == stats["gross_pnl"]-stats["slippage_cost"]-stats["fees"] == cash == equity[-1]["equity"]
    else:
        stats = base.summarize(trades,equity)
    stats["terminal_trades"] = sum(t["exit_reason"] == "terminal" for t in trades)
    stats["terminal_trade_pnl"] = sum(t["net_pnl"] for t in trades if t["exit_reason"] == "terminal")
    stats["max_holding_elapsed_minutes"] = max((t["holding_elapsed_minutes"] for t in trades),default=None)
    stats["cross_session_trades"] = sum((datetime.fromisoformat(t["entry_bar"]).date(),datetime.fromisoformat(t["entry_bar"]).hour>=21) !=
                                         (datetime.fromisoformat(t["exit_bar"]).date(),datetime.fromisoformat(t["exit_bar"]).hour>=21) for t in trades)
    if cfg.intraday:
        assert stats["cross_session_trades"] == 0
    return stats,trades,equity,audit


def f(x,n=2):
    return "N/A" if x is None else f"{x:,.{n}f}"


def report(results, stress):
    lines = ["# 按百家号文章截图规则测试SR", "",
             "日期：2026-09-21—10-20，16个交易日、5280分钟。实际合约SR701（SR2701），固定1手；每次开/平1跳不利滑点、3元手续费。未调参。",
             "", "## 结果", "", "| 版本 | 笔数 | 净胜率 | 盈亏比 | 净盈亏/元 | 分钟权益回撤/元 | 跨时段持仓笔数 |", "|---|---:|---:|---:|---:|---:|---:|"]
    for label,s in results.items():
        lines.append(f"| {label} | {s['trades']} | {f(s['win_rate']*100 if s['win_rate'] is not None else None)}% | {f(s['payoff_ratio'])} | {f(s['net_pnl'])} | {f(s['minute_close_max_drawdown'])} | {s['cross_session_trades']} |")
    lines += ["", "ks3/kx2是代码截图硬编码；ks1/kx2是让回测界面参数真正生效的对照，不声称就是原曲线版本。carry保留跨日持仓；intraday额外14:58/22:58清仓。盈亏比=平均净盈利÷平均净亏损绝对值；无盈利或无亏损时不强行给出比值。",
              "", "## 对原曲线的核对", "",
              f"- 用户提供的[百家号文章]({ARTICLE})发表于2025-12-05，已保存公开HTML和原始截图及哈希。",
              f"- 找到可核对的[2019年公开版本]({EARLY_ARTICLE})及[可见源码]({EARLY_CODE})。原示例为螺纹rb000、一小时周期，2015-02-22—2019-12-06，截图本金100万元、净收益43,987.302元。曲线标题显示累计4.399%、年化(252天)0.634%。这些是作者截图上的数值，没有复现其2015—2019年螺纹数据。",
              "- 当前代码快照的回测注释另写2019—2021、初始10000；文章代码截图与回测界面的Ks也不同，不能证明展示曲线由所贴代码原样生成。可确认不一致，不能据此断定作者造假。",
              "- 代码按新的一小时K线刷新上下轨，不是每天只刷新一次。Cycle=5对应当前小时刚开盘的形成中K线加前4根已完成小时K线；当前小时开盘时未知的高低收绝不提前加入。",
              "- 代码没有主动日内清仓，只在价格达到反方向轨道时退出。标题虽写日内，实际可跨夜、周末与长假持仓。不能把原曲线直接当作SR日内成绩。",
              "", "## 本地复现边界", "",
              f"[FMZ GetRecords文档]({FMZ_DOC})说明末根K线是当前K线、默认周期由界面决定。本地按北京时间自然小时聚合，有休息时段的小时保持真实30/45分钟片段；没有核实FMZ商品期货历史源的小时边界是否完全相同。",
              "原程序每秒检查价格并用当时最新形成中K线的Close作为报价；本地只有一分钟OHLC，采用完成分钟确认、次分钟开盘成交，可能漏过分钟内突破或延迟反向开仓，不冒充平台模拟Tick逐笔重现。原代码先平仓、下次回调再开仓，本地也分开两次意图处理。",
              "原订单的Sell(close−1)是卖出限价，本地没有逐笔盘口与排队模型，以统一1跳不利成交近似；没有把限价报价偏移再重复当作第二份滑点。",
              "源码在下单后立即修改mp虚拟持仓，没有等待/核对真实成交回报。本地引擎只在假定成交时更新持仓，但仍是假设可成交，不能验证作者原模拟器的真实订单状态。",
              "保留原始形成中K线的首次开盘快照，不在小时收盘后回填小时开盘信号。月份起点空仓、此前分钟只做热身；月份终点预定最后一分钟14:59开盘清仓，terminal标记与盈亏单列。持仓模式的收益包含该终点结算。",
              "", "## 成本压力（不改交易时间与方向）", "",
              "| 版本 | 每边滑点 | 笔数 | 净盈亏/元 |", "|---|---:|---:|---:|"]
    for row in stress:
        lines.append(f"| {row['label']} | {row['slip_ticks']} | {row['trades']} | {f(row['net_pnl'])} |")
    lines += ["", "完整来源和参数见analysis_plan.json、article_source.json、image_sources.json；逐笔交易、分钟权益和小时轨道分别为*_trades.csv、*_equity.csv、*_bands.csv。",
              "", "```powershell", "python scripts/backtest_sr_dual_thrust_article.py",
              "python -m unittest discover -s tests -p test_sr_dual_thrust*.py", "```", ""]
    (OUT/"REPORT.md").write_text("\n".join(lines),encoding="utf-8")


def main():
    plan()
    minutes=prepare()
    results={};stress=[]
    for cfg in CONFIGS:
        s,ts,eq,bands=replay(minutes,cfg)
        results[cfg.label]=s
        write_csv(cfg.label+"_trades.csv",ts)
        write_csv(cfg.label+"_equity.csv",eq)
        write_csv(cfg.label+"_bands.csv",bands)
        for slip in [1,2]:
            other,ots,*_=replay(minutes,replace(cfg,slip_ticks=slip))
            assert [(t['entry_bar'],t['exit_bar'],t['direction']) for t in ots] == [(t['entry_bar'],t['exit_bar'],t['direction']) for t in ts]
            stress.append(dict(label=cfg.label,slip_ticks=slip,**other))
    save("results.json",dict(results=results,stress=stress))
    report(results,stress)
    save("verification.json",dict(passed=True,checks=["frozen source hashes","5280 complete scored minutes","causal hourly opening snapshot",
         "next-minute opening fills","entry/exit costs counted once","trade/cash/equity reconciliation","intraday variants zero session carry","cost-only changes preserve trades"]))
    print(json.dumps(results,ensure_ascii=False,indent=2))
    print("Report:",OUT/"REPORT.md")


if __name__=="__main__":
    main()
