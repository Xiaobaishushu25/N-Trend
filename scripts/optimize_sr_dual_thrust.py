#!/usr/bin/env python3
"""Source-based SR Dual Thrust revision and predeclared chronological selection.

Uses immutable snapshots from backtest_sr_dual_thrust.py. No network, production
imports or live execution. Confirm a breakout on a completed one-minute close;
execute at the next available minute open, with both-side fees/slippage.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path

import backtest_sr_dual_thrust as base

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "reports" / "sr_dual_thrust_v2_20261021"
SOURCE_ARTICLE = "https://fudan.bigquant.com/wiki/doc/9zCGg7j7az"
SOURCE_CODE = "https://bigquant.com/wiki/doc/RBKegXJO9n"
TRAIN_END = date(2026, 10, 8)


@dataclass(frozen=True)
class Config:
    n: int = 5
    k: float = .2
    include_night: bool = True
    max_entries: int = 3  # A reversal consumes a new entry; closes never suppressed.
    slip_ticks: int = 1

    @property
    def label(self):
        return f"{'day_night' if self.include_night else 'day_only'}_N{self.n}_k{self.k:.1f}"


GRID = [Config(n=n, k=k/10) for n in range(1, 6) for k in range(1, 6)]


@dataclass(frozen=True)
class SourceCodeConfig(Config):
    """Fixed comparator added after reading the COMPLETE BigTrader example.

    Its rules differ from the separate article: no cap/14:00 cutoff, strict
    breakouts, trading-day opening anchor retained,14:58/22:58 liquidation.
    This comparator is outside the article25-grid; its own grid is a separate,
    explicitly disclosed second experiment and cannot alter the first selection.
    """
    max_entries: int | None = None
    day_entry_cutoff: time | None = None
    day_flat: time = time(14, 58)
    night_flat: time = time(22, 58)
    reset_day_anchor: bool = False
    strict_breakout: bool = True

    @property
    def label(self):
        return f"source_code_N{self.n}_k{self.k:.1f}"


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


def analysis_plan():
    return dict(
        created_at=datetime.now(base.ZONE).isoformat(), start=str(base.START), end=str(base.END),
        sources=[SOURCE_ARTICLE, SOURCE_CODE], baseline=asdict(Config()),
        original_article_day_only=asdict(Config(include_night=False)),
        grid=[asdict(c) for c in GRID], train_end=str(TRAIN_END),
        validation_start="2026-10-09", minimum_training_round_trips=6,
        selection="maximize training(net_pnl -0.5*minute_close_max_drawdown), then net_pnl, then lower drawdown, then label",
        acceptance="training net_pnl>0 and at least6 completed trades; otherwise no qualified candidate, retain fixed source defaults as comparison only",
        limits="This month was already examined in the prior chat; chronological split is a reuse-aware diagnostic, NOT pristine unseen out-of-sample evidence.",
        rules=[
            "Range=max(max(previousN daily highs)-min(previousN daily closes), max(previousN daily closes)-min(previousN daily lows)); current trading day excluded.",
            "Symmetric upper/lower=session open +/- k*Range; rounded outwards to tick. No fitted trend filters or take-profit.",
            "Confirm upper/lower breakout at CLOSED minute; next minute OPEN fills market intent. No same-close execution or high/low retroactive fill.",
            "A reversal always closes; reopen only below3 entries/day and before entry cutoff. Same-direction reentry is allowed.",
            "Max3 actual new entries per exchange trading day, shared between night and day; no separate once-per-direction limit.",
            "Day-only reference uses09:00 open,14:00 entry cutoff,14:55 flat.",
            "Night/day extension resets anchor at21:00 and09:00; night entries until22:55 flat; day entries stop14:00 and flat14:55.",
            "Night prior to current day is current-day information, NOT included in the daily lookback; range stays fixed during day/night.",
            "Pre-scheduled flat at14:55/22:55 minute open cancels pending new entries, all lunch/recess positions may remain.",
            "Fixed1lot; each execution1tick adverse slip plus3CNY fee. 10CNY/tick. Reversal pays close AND new-open costs.",
        ],
        unchanged="2026-09-21--10-20 scored dates, sameSR701 frozen data, same costs/lot and preclose times as prior report",
        post_selection_diagnostics="After freezing training selection, evaluate all25 full-month/validation results as diagnostics; full-month winner explicitly hindsight-only. Do not change selection.",
    )


def freeze_plan():
    plan = analysis_plan()
    path = OUT / "analysis_plan.json"
    if path.exists():
        old = json.loads(path.read_text(encoding="utf-8"))
        assert {k:v for k,v in old.items() if k != "created_at"} == {k:v for k,v in plan.items() if k != "created_at"}, "Frozen plan changed"
    else:
        save("analysis_plan.json", plan)


def prepare():
    manifest = json.loads((base.OUT / "sources.json").read_text(encoding="utf-8"))
    for src in manifest:
        assert hashlib.sha256((base.OUT / src["file"]).read_bytes()).hexdigest() == src["sha256"]
    groups = {}
    for b in base.load_csv("SR701_1m.csv"):
        groups.setdefault(base.trading_date(b.dt), []).append(b)
    dates = []
    d = base.START
    while d <= base.END:
        if base.is_trading_day(d):
            assert {b.dt for b in groups[d]} == base.expected_minutes(d)
            dates.append(d)
        d += timedelta(days=1)
    daily = {b.dt.date(): b for b in base.load_csv("SR701_daily.csv")}
    main = {b.dt.date(): b for b in base.load_csv("main_daily.csv")}
    warmup = []
    for d in dates:
        a, b = main[d], daily[d]
        assert (a.h, a.l, a.c, a.oi) == (b.h, b.l, b.c, b.oi)
        bs = groups[d]
        assert (bs[0].o, max(b.h for b in bs), min(b.l for b in bs), bs[-1].c) == (b.o, b.h, b.l, b.c)
        prior = history(daily, d, 5)
        assert len(prior) == 5 and all(b.dt.date() < d for b in prior)
        warmup.append(dict(trading_date=str(d), daily_history_dates=[str(b.dt.date()) for b in prior]))
    assert len(dates) == 16 and sum(len(groups[d]) for d in dates) == 5280
    save("inputs.json", dict(source_folder=str(base.OUT), manifest=manifest, scored_minutes=5280,
                             warmup=warmup, first_5_history_days=[str(b.dt.date()) for b in history(daily, dates[0], 5)],
                             note="Sep14 supplier daily includes preceding night even though minute download beginsSep14day; daily is a separate full trading-day observation."))
    return groups, daily, dates


def history(daily, d, n):
    keys = sorted(k for k in daily if k < d)[-n:]
    assert len(keys) == n, (d, n, keys)
    expected = []
    p = d
    for _ in range(n):
        p = base.previous_trading_day(p)
        expected.append(p)
    assert keys == sorted(expected), ("daily history hole", d, keys, expected)
    return [daily[k] for k in keys]


def classic_range(previous):
    return max(max(b.h for b in previous)-min(b.c for b in previous),
               max(b.c for b in previous)-min(b.l for b in previous))


def bands(opening, span, k, strict=False):
    if strict:
        # Source tests price>raw upper and price<raw lower (not>=/<=).
        return math.floor(opening+k*span+1e-10)+1, math.ceil(opening-k*span-1e-10)-1
    return math.ceil(opening+k*span-1e-10), math.floor(opening-k*span+1e-10)


def is_night(b):
    return b.dt.hour >= 21


def closed_to_entries(b, cfg):
    cutoff = getattr(cfg, "day_entry_cutoff", time(14))
    return cutoff is not None and not is_night(b) and b.dt.time() >= cutoff


def flat_time(b, cfg):
    return b.dt.time() >= (getattr(cfg, "night_flat", time(22, 55)) if is_night(b)
                           else getattr(cfg, "day_flat", time(14, 55)))


def entries_available(entries, cfg):
    return cfg.max_entries is None or entries < cfg.max_entries


def replay_day(bars, previous, cfg=Config()):
    d = base.trading_date(bars[0].dt)
    span = classic_range(previous)
    session = None
    opening = upper = lower = None
    pending = position = None
    entries = fills = 0
    cash = 0.
    trades, curve, sessions = [], [], []

    def enter(side, b, signal):
        nonlocal position, cash, entries, fills
        assert position is None and entries_available(entries, cfg)
        fill = b.o+side*cfg.slip_ticks
        position = dict(trading_date=str(d), contract="SR701", direction=side, lots=1,
                        entry_bar=str(b.dt), signal_bar=signal, entry_base=b.o, entry_fill=fill,
                        n=cfg.n, k=cfg.k, range=span, session_open=opening, upper=upper, lower=lower)
        entries += 1
        fills += 1
        cash -= 3

    def leave(b, reason):
        nonlocal position, cash, fills
        assert position
        p = position
        fill = b.o-p["direction"]*cfg.slip_ticks
        gross = (b.o-p["entry_base"])*p["direction"]*10
        cost = 2*cfg.slip_ticks*10
        net = gross-cost-6
        cash += (fill-p["entry_fill"])*p["direction"]*10-3
        fills += 1
        trades.append({**p, "exit_bar": str(b.dt), "exit_base": b.o, "exit_fill": fill,
                       "exit_reason": reason, "gross_pnl": gross, "slippage_cost": cost,
                       "fees": 6, "net_pnl": net})
        position = None

    for b in bars:
        assert base.trading_date(b.dt) == d
        new_session = (b.dt.date(), is_night(b))
        if session != new_session:
            assert position is None, ("position crossed session close", position)
            session = new_session
            pending = None
            if opening is None or getattr(cfg, "reset_day_anchor", True):
                opening = b.o
            upper, lower = bands(opening, span, cfg.k, getattr(cfg, "strict_breakout", False))
            sessions.append(dict(trading_date=str(d), start=str(b.dt), opening=opening,
                                 upper=upper, lower=lower, range=span, enabled=cfg.include_night or not is_night(b)))
        enabled = cfg.include_night or not is_night(b)
        if not enabled or flat_time(b, cfg):
            pending = None
            if position:
                leave(b, "night_flat" if is_night(b) else "day_flat")
        elif pending is not None:
            target, signal = pending
            assert datetime.fromisoformat(signal)+timedelta(minutes=1) <= b.dt
            if position and position["direction"] != target:
                leave(b, "opposite_breakout")
            # Entry cap/cutoff NEVER prevents an existing position from closing.
            if position is None and entries_available(entries, cfg) and not closed_to_entries(b, cfg):
                enter(target, b, signal)
        pending = None
        if enabled and not flat_time(b, cfg):
            target = 1 if b.c >= upper else -1 if b.c <= lower else 0
            if target and (position is None or target != position["direction"]):
                if position is not None or (entries_available(entries, cfg) and not closed_to_entries(b, cfg)):
                    pending = (target, str(b.dt))
        floating = (b.c-position["entry_fill"])*position["direction"]*10 if position else 0.
        curve.append(dict(bar_end=str(b.dt+timedelta(minutes=1)), trading_date=str(d),
                          realized_cash=cash, floating_pnl=floating, equity=cash+floating,
                          position=position["direction"] if position else 0))
    return trades, curve, dict(trading_date=str(d), range=span, entries=entries, fills=fills,
                              trades=len(trades), net_pnl=cash, open_position=position is not None), sessions


def replay(groups, daily, dates, cfg=Config()):
    trades, curve, days, sessions = [], [], [], []
    cash = 0.
    for d in dates:
        ts, eq, day, ss = replay_day(groups[d], history(daily, d, cfg.n), cfg)
        assert not day["open_position"] and day["entries"] == len(ts)
        assert cfg.max_entries is None or len(ts) <= cfg.max_entries
        assert day["fills"] == len(ts)*2
        assert abs(day["net_pnl"]-sum(t["net_pnl"] for t in ts)) < 1e-9
        for r in eq:
            r["realized_cash"] += cash
            r["equity"] += cash
        cash += day["net_pnl"]
        trades.extend(ts)
        curve.extend(eq)
        days.append(day)
        sessions.extend(ss)
    stats = base.summarize(trades, curve)
    assert stats["net_pnl"] == stats["gross_pnl"]-stats["slippage_cost"]-stats["fees"] == cash == curve[-1]["equity"]
    for t in trades:
        a, z = datetime.fromisoformat(t["entry_bar"]), datetime.fromisoformat(t["exit_bar"])
        assert a.date() == z.date() and (a.hour >= 21) == (z.hour >= 21)
    return stats, trades, curve, days, sessions


def training_rank(stats, cfg):
    return (stats["net_pnl"]-.5*stats["minute_close_max_drawdown"], stats["net_pnl"],
            -stats["minute_close_max_drawdown"], cfg.label)


def choose(training):
    eligible = [(c,s) for c,s in training if s["trades"] >= 6 and s["net_pnl"] > 0]
    return max(eligible, key=lambda pair: training_rank(pair[1], pair[0]))[0] if eligible else None


def dump_run(label, result):
    stats, trades, curve, days, sessions = result
    save(label+"_summary.json", stats)
    write_csv(label+"_trades.csv", trades)
    write_csv(label+"_equity.csv", curve)
    write_csv(label+"_daily.csv", days)
    write_csv(label+"_sessions.csv", sessions)


def source_search(groups, daily, dates, train_dates, validation_dates):
    """Separate25-group diagnostic after obtaining the complete source code."""
    grid = [SourceCodeConfig(n=n, k=k/10) for n in range(1, 6) for k in range(1, 6)]
    plan = dict(source=SOURCE_CODE, reason="Full code differs materially from article; same25 N/k combinations under complete-code rules",
                grid=[asdict(c) for c in grid], training_end=str(TRAIN_END),
                selection="Same>=6trades,net>0 gate; maximize training net-.5drawdown, no validation input",
                disclosure="Second25-group experiment after first25 groups and full-code default were inspected. The month has already been seen; reused-data diagnostic only.")
    path = OUT / "complete_code_search_plan.json"
    if path.exists():
        assert json.loads(path.read_text(encoding="utf-8")) == json.loads(json.dumps(plan, default=str))
    else:
        save("complete_code_search_plan.json", plan)
    training = [(cfg, replay(groups, daily, train_dates, cfg)[0]) for cfg in grid]
    cfg = choose(training)
    picked = dict(label=cfg.label, config=asdict(cfg), training=next(s for c,s in training if c == cfg)) if cfg else None
    save("complete_code_training_selection.json", dict(selection=picked, rule="training only; no qualification means no promoted candidate"))
    diagnostic = []
    for c, tr in training:
        val = replay(groups, daily, validation_dates, c)[0]
        full = replay(groups, daily, dates, c)[0]
        assert tr["net_pnl"]+val["net_pnl"] == full["net_pnl"]
        diagnostic.append(dict(label=c.label, n=c.n, k=c.k, train_trades=tr["trades"], train_net_pnl=tr["net_pnl"],
                               train_drawdown=tr["minute_close_max_drawdown"], train_score=training_rank(tr,c)[0],
                               validation_trades=val["trades"], validation_net_pnl=val["net_pnl"],
                               full_trades=full["trades"], full_win_rate=full["win_rate"], full_payoff_ratio=full["payoff_ratio"],
                               full_net_pnl=full["net_pnl"], full_drawdown=full["minute_close_max_drawdown"]))
    write_csv("complete_code_parameter_diagnostics.csv", diagnostic)
    results = {}
    if cfg:
        for label, span in [("code_selected_train", train_dates), ("code_selected_validation", validation_dates), ("code_selected_full", dates)]:
            run = replay(groups, daily, span, cfg)
            results[label] = run[0]
            dump_run(label, run)
    stress = []
    reference = replay(groups, daily, dates, cfg or SourceCodeConfig())[1]
    for slip in [1, 2, 3]:
        run = replay(groups, daily, dates, replace(cfg or SourceCodeConfig(), slip_ticks=slip))
        assert [(t['entry_bar'],t['exit_bar'],t['direction']) for t in run[1]] == [(t['entry_bar'],t['exit_bar'],t['direction']) for t in reference]
        stress.append(dict(slip_ticks=slip, **run[0]))
    result = dict(selection=picked, results=results, stress=stress, hindsight_best=max(diagnostic, key=lambda r:r["full_net_pnl"]))
    save("complete_code_search_results.json", result)
    return result


def f(x, n=2):
    return "N/A" if x is None else f"{x:,.{n}f}"


def stats_row(label, s):
    return f"| {label} | {s['trades']} | {f(s['win_rate']*100 if s['win_rate'] is not None else None)}% | {f(s['payoff_ratio'])} | {f(s['net_pnl'])} | {f(s['minute_close_max_drawdown'])} |"


def report(results, selection, train_dates, validation_dates, diagnostics, stress, code_search):
    lines = ["# SR Dual Thrust V2：按来源规则修订与有限选参", "",
             "同一批SR701（SR2701）一分钟快照；2026-09-21—10-20，共16个交易日、5280分钟。固定1手，每边1跳滑点和3元手续费；完整开平成本26元。",
             "", "## 主要比较", "",
             "| 版本 | 笔数 | 净胜率 | 盈亏比 | 净盈利/元 | 分钟权益最大回撤/元 |", "|---|---:|---:|---:|---:|---:|"]
    for label in ["old_vnpy", "source_code_fixed", "code_selected_full", "source_day_only", "source_day_night", "selected_full"]:
        if label in results:
            title = {"old_vnpy":"上一版简化DT", "source_code_fixed":"完整代码规则 N5/k0.2 日夜盘固定对照", "code_selected_full":"完整代码规则仅训练选参，整月展示", "source_day_only":"文章规则 N5/k0.2 日盘参考版",
                     "source_day_night":"N5/k0.2 日夜盘适配版", "selected_full":"仅训练选出的参数，整月展示"}[label]
            lines.append(stats_row(title, results[label]))
    lines += ["", "## 来源与准确复现边界", "",
              f"1. [BigQuant日内文章]({SOURCE_ARTICLE})明确N日高低收Range、反手、每天最多3笔、14点以后停止交易。文章没有给出全部夜盘、成交撮合细节。",
              f"2. [BigTrader文档例3]({SOURCE_CODE})给出N=5、k1=k2=0.2，以及可配置日/夜盘尾盘清仓。两份资料并非完全相同的版本；本次是有明确口径的本地实现，未冒称在BigQuant引擎里逐笔原样复现。",
              "完整代码规则对照：随后拿到文档例3完整代码，确认其没有每日3笔限制，也没有14点入场截止；按price>上轨/price<下轨严格比较，保留交易日首次开盘基准，14:58/22:58清仓。新增source_code_fixed作为固定参数对照，不加入25组搜索、不影响已经冻结的训练选择。先收盘确认、次分钟开盘成交仍为统一的本地因果成交模型，原平台撮合结果未复现。source_rules_addendum.json记录差异。以下第4—7项描述的是文章规则实验，不套用到完整代码规则对照。",
              "3. 修正核心Range为max(HH−LC, HC−LL)，只读此前N个完整交易日日线，当前夜盘数据也不放入本交易日的历史窗口。",
              "4. 不再按方向每天只准入一次：允许同方向重复开仓与反手，最多3次实际开仓/交易日；反手计1次新开，平仓始终允许。",
              "5. 日盘参考版只做09:00—14:55；14:00起禁止新开仓但允许退出，14:55分钟开盘清仓。夜盘适配版增加21:00—22:55交易，09:00重新以日盘开盘价设轨；3次额度仍由整个交易日共享。夜盘没有另加14点规则的机械时刻比较。",
              "6. 1分钟收盘确认突破，下一根可交易分钟开盘成交。第一分钟突破只能从第二分钟开始成交；同根K线不回填触发价。休息时间产生的意图可以在下一交易分钟开盘成交；预定尾盘平仓优先于所有意图。",
              "7. 没有额外趋势指标、止盈或追踪止损；主要改动是来源Range、开仓额度与日盘重设开盘基准。上下轨对称取k，减少自由度。",
              "", "## 先训练、后验证", "",
              f"- 训练：{train_dates[0]}—{train_dates[-1]}（{len(train_dates)}交易日）。验证：{validation_dates[0]}—{validation_dates[-1]}（{len(validation_dates)}交易日）。",
              "- 固定25组：N∈{1,2,3,4,5}，对称k∈{0.1,0.2,0.3,0.4,0.5}；只在日夜盘适配规则下选参。",
              "- 训练门槛：至少6笔、净盈利>0；评分=训练净盈利−0.5×训练分钟权益最大回撤。先保存training_selection.json，再计算验证结果；验证盈亏不参与筛选。",
              "- 上一轮已经看过整个月的行情与结果，因此这次时间切分只属于复用数据的诊断，不是完全未见样本。",
              f"- 选出参数：{selection['label'] if selection else '无参数通过门槛'}。", "",
              "| 版本/区间 | 笔数 | 净胜率 | 盈亏比 | 净盈利/元 | 分钟权益最大回撤/元 |", "|---|---:|---:|---:|---:|---:|"]
    for label in ["code_train", "code_validation", "fixed_train", "fixed_validation", "selected_train", "selected_validation"]:
        if label in results:
            lines.append(stats_row(label, results[label]))
    lines += ["", "## 成本压力（无训练合格参数时展示N5/k0.2文章适配版）", "",
              "| 每边滑点 | 笔数 | 净胜率 | 盈亏比 | 净盈利/元 | 分钟权益最大回撤/元 |", "|---|---:|---:|---:|---:|---:|"]
    for s in stress:
        lines.append(stats_row(str(s["slip_ticks"])+"跳", s))
    best = max(diagnostics, key=lambda r:r["full_net_pnl"])
    lines += ["", "## 全月回看最优：仅用于揭示选择偏差", "",
              f"在全部25组里，按整月净盈利事后挑选会得到{best['label']}，全月净盈利{f(best['full_net_pnl'])}元。它与训练选择分开，不能把这个值当成事先选好参数的结果。所有组合的训练、验证、全月结果见parameter_diagnostics.csv。",
              "", "## 数据、测试与限制", "",
              "沿用上一版冻结CSV；SHA256、评分分钟日历、合约成员及分钟/日线OHLC核验均重新检查。N日热身用独立的完整交易日日线，首日可用9月14—18日5根日线。inputs.json保留来源和每个交易日的历史日期。",
              "所有盈亏按具体SR701成交，未用主连价格撮合。现金、浮盈亏、逐笔成本、日总数、尾盘零仓均做对账；平仓不受开仓截止时间/次数限制。未模拟盘口排队、涨跌停无法成交、市场冲击或保证金约束。一个月的样本无法证明未来可盈利。",
              "", "```powershell", "python scripts/optimize_sr_dual_thrust.py",
              "python -m unittest discover -s tests -p test_sr_dual_thrust*.py", "```", ""]
    lines += ["## 完整代码规则的第二轮25组选参", "",
              "补充完整代码实验使用相同N/k范围再试25组，与文章规则的25组分开记录，共50组参数实验。来源差异是在首轮实验后发现，计划见complete_code_search_plan.json；这是复用数据的诊断，不是完全未见样本。",
              f"训练选择：{code_search['selection']['label'] if code_search['selection'] else '无合格参数'}；规则仍为至少6笔、净利>0，评分=净利−0.5回撤。全部组合见complete_code_parameter_diagnostics.csv。", "",
              "| 区间 | 笔数 | 净胜率 | 盈亏比 | 净盈利/元 | 分钟权益最大回撤/元 |", "|---|---:|---:|---:|---:|---:|"]
    for label, s in code_search["results"].items():
        lines.append(stats_row(label, s))
    lines += ["", "完整代码所选参数（无合格时为原默认参数）的成本压力：", "",
              "| 每边滑点 | 笔数 | 净胜率 | 盈亏比 | 净盈利/元 | 分钟权益最大回撤/元 |", "|---|---:|---:|---:|---:|---:|"]
    for s in code_search["stress"]:
        lines.append(stats_row(str(s["slip_ticks"])+"跳", s))
    h = code_search["hindsight_best"]
    lines += ["", f"完整代码25组整月回看最优：{h['label']}，{h['full_trades']}笔、净利{f(h['full_net_pnl'])}元。这是事后挑选，不替代训练选参结果。", ""]
    (OUT / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan-only", action="store_true")
    args = parser.parse_args()
    freeze_plan()
    if args.plan_only:
        print("Frozen plan:", OUT / "analysis_plan.json")
        return
    groups, daily, dates = prepare()
    train_dates = [d for d in dates if d <= TRAIN_END]
    validation_dates = [d for d in dates if d > TRAIN_END]
    assert len(train_dates) == len(validation_dates) == 8
    # No full-period or validation strategy evaluation takes place above selection.
    training = [(cfg, replay(groups, daily, train_dates, cfg)[0]) for cfg in GRID]
    selected = choose(training)
    selected_training = next(s for c,s in training if c == selected) if selected else None
    selection = dict(label=selected.label, config=asdict(selected), training=selected_training) if selected else None
    save("training_results.json", [dict(label=c.label, config=asdict(c), stats=s, rank=training_rank(s,c)) for c,s in training])
    save("training_selection.json", dict(created_at=datetime.now(base.ZONE).isoformat(), selection=selection,
                                         rule="training only; frozen before validation evaluation"))
    old = json.loads((base.OUT / "summary.json").read_text(encoding="utf-8"))["primary"]
    reproduced = base.replay(groups, dates)[0]
    assert reproduced == old, "The prior baseline must reproduce exactly"
    results = dict(old_vnpy=old)
    save("source_rules_addendum.json", dict(created_at=datetime.now(base.ZONE).isoformat(), url=SOURCE_CODE,
        page_sha256="9dd208922c35c9604564f6914526cd89c43fa0b59ac4cbb87228c0cc107262bd",
        reason="Complete example read after initial article-based experiment; fixed comparator only, no additional search",
        config=asdict(SourceCodeConfig()), execution="source close signal, local next-open fill; no claim of same BigTrader-engine fills"))
    runs = [("source_day_only", dates, Config(include_night=False)), ("source_day_night", dates, Config()),
            ("fixed_train", train_dates, Config()), ("fixed_validation", validation_dates, Config()),
            ("source_code_fixed", dates, SourceCodeConfig()),
            ("code_train", train_dates, SourceCodeConfig()), ("code_validation", validation_dates, SourceCodeConfig())]
    if selected:
        runs += [("selected_full", dates, selected), ("selected_train", train_dates, selected),
                 ("selected_validation", validation_dates, selected)]
    for label, span, cfg in runs:
        run = replay(groups, daily, span, cfg)
        results[label] = run[0]
        dump_run(label, run)
    diagnostics = []
    for cfg, tr in training:
        val = replay(groups, daily, validation_dates, cfg)[0]
        full = replay(groups, daily, dates, cfg)[0]
        assert tr["net_pnl"]+val["net_pnl"] == full["net_pnl"]
        diagnostics.append(dict(label=cfg.label, n=cfg.n, k=cfg.k,
                                train_trades=tr["trades"], train_net_pnl=tr["net_pnl"],
                                train_drawdown=tr["minute_close_max_drawdown"], train_score=training_rank(tr,cfg)[0],
                                validation_trades=val["trades"], validation_net_pnl=val["net_pnl"],
                                full_trades=full["trades"], full_win_rate=full["win_rate"],
                                full_payoff_ratio=full["payoff_ratio"], full_net_pnl=full["net_pnl"],
                                full_drawdown=full["minute_close_max_drawdown"]))
    write_csv("parameter_diagnostics.csv", diagnostics)
    stress = []
    cfg = selected or Config()
    reference = replay(groups, daily, dates, cfg)[1]
    for slip in [1, 2, 3]:
        r = replay(groups, daily, dates, replace(cfg, slip_ticks=slip))
        assert [(t['entry_bar'],t['exit_bar'],t['direction']) for t in r[1]] == [(t['entry_bar'],t['exit_bar'],t['direction']) for t in reference]
        stress.append(dict(slip_ticks=slip, **r[0]))
    code_search = source_search(groups, daily, dates, train_dates, validation_dates)
    results.update(code_search["results"])
    save("results.json", dict(selection=selection, results=results, stress=stress, complete_code_search=code_search,
                              hindsight_best=max(diagnostics, key=lambda r:r['full_net_pnl'])))
    report(results, selection, train_dates, validation_dates, diagnostics, stress, code_search)
    save("verification.json", dict(passed=True, article_grid_size=25, complete_code_grid_size=25,
                                   old_baseline_exactly_reproduced=True,
                                   checks=["source SHA256", "all scored session minutes", "complete priorN daily history",
                                           "no session carry", "article entries<=3; complete source uncapped; exits never capped", "next-open fills only",
                                           "trade/fee/slippage/cash/equity reconciliation", "training frozen before validation",
                                           "same fills under changed costs", "train+validation PnL=full PnL"]))
    print(json.dumps(dict(article_selection=selection, code_selection=code_search["selection"], results=results,
                          code_stress=code_search["stress"], code_hindsight=code_search["hindsight_best"]), ensure_ascii=False, indent=2, default=str))
    print("Report:", OUT / "REPORT.md")


if __name__ == "__main__":
    main()
