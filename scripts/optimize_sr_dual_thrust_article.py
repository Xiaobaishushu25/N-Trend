#!/usr/bin/env python3
"""Bounded parameter search for the article's hourly strategy on frozen SR data.

Search/report only. Does not connect to a broker, change lots, or deploy a model.
The compact evaluator must match the detailed chronological replay before use.
"""
from __future__ import annotations

import bisect
import csv
import hashlib
import json
import statistics
from dataclasses import asdict, replace
from datetime import date, datetime

import backtest_sr_dual_thrust_article as article

base = article.base
OUT = base.ROOT / "reports" / "sr_dual_thrust_article_optimization_20261021"
CYCLES = [2, 3, 4, 5, 6, 8, 12, 16]
COEFFICIENTS = [.1, .2, .3, .5, .75, 1., 1.5, 2., 3.]
TRAIN_END = date(2026, 10, 8)
VALIDATION_START = date(2026, 10, 9)


def save(name, obj):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT/name).write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def write_csv(name, rows):
    if rows:
        with (OUT/name).open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)


def label(cfg):
    return f"{'intraday' if cfg.intraday else 'carry'}_C{cfg.cycle}_ks{cfg.ks:g}_kx{cfg.kx:g}"


def key(cfg):
    return cfg.intraday, cfg.cycle, cfg.ks, cfg.kx


def grid():
    return [article.Config(cycle=n, ks=ks, kx=kx, intraday=mode)
            for mode in [False,True] for n in CYCLES for ks in COEFFICIENTS for kx in COEFFICIENTS]


def freeze_plan():
    plan = dict(source_strategy=str(article.ROOT) if hasattr(article,"ROOT") else str(article.__file__),
                source_report=str(article.OUT), period="1hour fixed; Cycle is lookback bar count, not candle duration",
                start=str(base.START), end=str(base.END), cycles=CYCLES, ks=COEFFICIENTS, kx=COEFFICIENTS,
                modes=["carry","intraday14:58/22:58"], combinations=len(grid()),
                lots=1, multiplier=10, fee_per_side=3, adverse_slip_ticks_per_side=1,
                train_end=str(TRAIN_END), validation_start=str(VALIDATION_START),
                selection=dict(minimum_training_trades=8, training_net_positive=True,
                               neighborhood="center and immediate grid neighbors along ONE parameter axis, same holding mode",
                               neighborhood_profitable_fraction_at_least=.5,
                               score="median(neighborhood training net PnL) -0.5*own training minute drawdown",
                               tie_break="own training net PnL, lower drawdown, label"),
                hindsight_rank="full-month net PnL, lower drawdown, more trades, label; no minimum trades; disclose separately",
                protocol="Evaluate training grid -> freeze selections -> evaluate full grid -> validation diagnostics; no selection based on validation results",
                disclosure="This same month has already been read and tested repeatedly. Chronological split is reused-data validation, NOT pristine unseen evidence.",
                accounting="Every independent window begins flat and liquidates at final minute open; carry full-month PnL need not equal train+validation because of split close/reopen.",
                freeze="No extra stop, take profit, volume filter, trading-time search or lot scaling. No fine-grid extension after viewing results.")
    p=OUT/"analysis_plan.json"
    if p.exists():
        assert json.loads(p.read_text(encoding="utf-8")) == plan
    else:
        save("analysis_plan.json", plan)


class Features:
    """Causal hourly opening snapshots, reused across Ks/Kx and holding modes."""
    def __init__(self, minutes, period_minutes=60):
        self.minutes=minutes
        self.period_minutes=period_minutes
        self.tds=[base.trading_date(b.dt) for b in minutes]
        self.opens=[b.o for b in minutes]
        self.closes=[b.c for b in minutes]
        self.flat=[article.flat_time(b) for b in minutes]
        self.anchor=[]
        self.spans={n:[] for n in CYCLES}
        completed=[]; current=None; anchor=None; spans={}
        for b in minutes:
            hk=article.period_key(b.dt,period_minutes)
            if current is None or hk != current[0]:
                if current is not None:
                    completed.append(tuple(current[1:]))
                anchor=b.o
                current=[hk,b.o,b.o,b.o]
                for n in CYCLES:
                    bounds=article.make_bands(completed,anchor,article.Config(cycle=n))
                    spans[n]=bounds[0] if bounds is not None else None
            self.anchor.append(anchor)
            for n in CYCLES:self.spans[n].append(spans[n])
            current[1]=max(current[1],b.h)
            current[2]=min(current[2],b.l)
            current[3]=b.c

    def bounds(self,start,end):
        first=bisect.bisect_left(self.tds,start)
        stop=bisect.bisect_right(self.tds,end)
        assert stop>first and all(v is not None for v in self.spans[max(CYCLES)][first:stop])
        return first,stop


def compact(features,cfg,start=base.START,end=base.END):
    """Same state transitions as article.replay, without allocating trade/equity rows."""
    first,stop=features.bounds(start,end)
    opens,closes=features.opens,features.closes
    spans=features.spans[cfg.cycle]
    pos=0; pending=0; entry=0.; entry_fill=0.; cash=0.; peak=0.; dd=0.
    pnl=[]; gross_total=0.; terminal_pnl=0.; terminal_n=0
    slip=cfg.slip_ticks
    for i in range(first,stop):
        opening,closing=opens[i],closes[i]
        forced=(cfg.intraday and features.flat[i]) or i==stop-1
        if forced:
            pending=0
        if pos and (forced or pending==2):
            gross=(opening-entry)*pos*10
            net=gross-20*slip-6
            pnl.append(net);gross_total+=gross
            cash+=(opening-pos*slip-entry_fill)*pos*10-3
            if i==stop-1:terminal_pnl+=net;terminal_n+=1
            pos=0
        elif not forced and pending in [-1,1]:
            assert pos==0
            pos=pending;entry=opening;entry_fill=opening+pos*slip;cash-=3
        pending=0
        if not forced:
            upper=round(features.anchor[i]+cfg.ks*spans[i],2)
            lower=round(features.anchor[i]-cfg.kx*spans[i],2)
            if pos==0:
                if closing>=upper:pending=1
                elif closing<=lower:pending=-1
            elif (pos==1 and closing<=lower) or (pos==-1 and closing>=upper):pending=2
        eq=cash+(closing-entry_fill)*pos*10 if pos else cash
        peak=max(peak,eq);dd=max(dd,peak-eq)
    assert pos==0 and cash==sum(pnl)
    wins=[p for p in pnl if p>0];losses=[p for p in pnl if p<0]
    aw=statistics.mean(wins) if wins else None
    al=-statistics.mean(losses) if losses else None
    return dict(trades=len(pnl), wins=len(wins), losses=len(losses), breakeven=len(pnl)-len(wins)-len(losses),
                win_rate=len(wins)/len(pnl) if pnl else None,
                average_win=aw,average_loss=al,payoff_ratio=aw/al if aw is not None and al else None,
                profit_factor=sum(wins)/-sum(losses) if losses else None,
                gross_pnl=gross_total,slippage_cost=len(pnl)*20*slip,fees=len(pnl)*6,net_pnl=cash,
                average_net_pnl=statistics.mean(pnl) if pnl else None,
                best_trade=max(pnl) if pnl else None,worst_trade=min(pnl) if pnl else None,
                minute_close_max_drawdown=dd,terminal_trades=terminal_n,terminal_trade_pnl=terminal_pnl)


def neighbors(cfg, training):
    keys={key(cfg)}
    for axis,values in [(1,CYCLES),(2,COEFFICIENTS),(3,COEFFICIENTS)]:
        index=values.index(key(cfg)[axis])
        for j in [index-1,index+1]:
            if 0<=j<len(values):
                candidate=list(key(cfg));candidate[axis]=values[j];keys.add(tuple(candidate))
    return [training[k]["net_pnl"] for k in sorted(keys)]


def select(training, candidates=None):
    candidates=grid() if candidates is None else candidates
    selected={};diagnostics={}
    for mode in [False,True]:
        eligible=[]
        for cfg in candidates:
            if cfg.intraday != mode:continue
            stats=training[key(cfg)]
            ns=neighbors(cfg,training)
            median=statistics.median(ns)
            positive=sum(x>0 for x in ns)/len(ns)
            score=median-.5*stats["minute_close_max_drawdown"]
            diagnostics[key(cfg)]=dict(neighbor_median_training_net=median,neighbor_positive_fraction=positive,training_score=score)
            if stats["trades"]>=8 and stats["net_pnl"]>0 and positive>=.5:
                eligible.append((score,stats["net_pnl"],-stats["minute_close_max_drawdown"],label(cfg),cfg))
        selected[mode]=max(eligible,key=lambda x:x[:-1])[-1] if eligible else None
    return selected,diagnostics


def assert_reference(features,minutes,cfg,start,end):
    fast=compact(features,cfg,start,end)
    detailed=article.replay(minutes,cfg,start=start,end=end,period_minutes=features.period_minutes)
    for k,v in fast.items():
        assert v==detailed[0][k], (label(cfg),start,end,k,v,detailed[0][k])
    return detailed


def dump_run(name,result):
    s,ts,eq,bands=result
    save(name+"_summary.json",s)
    write_csv(name+"_trades.csv",ts)
    write_csv(name+"_equity.csv",eq)
    write_csv(name+"_bands.csv",bands)


def full_rank(cfg,stats):
    return stats["net_pnl"],-stats["minute_close_max_drawdown"],stats["trades"],label(cfg)


def flat_row(cfg,training,full,diag):
    return dict(label=label(cfg),mode="intraday" if cfg.intraday else "carry",cycle=cfg.cycle,ks=cfg.ks,kx=cfg.kx,
                train_trades=training["trades"],train_net=training["net_pnl"],train_drawdown=training["minute_close_max_drawdown"],
                **diag,full_trades=full["trades"],full_win_rate=full["win_rate"],full_payoff_ratio=full["payoff_ratio"],
                full_net=full["net_pnl"],full_drawdown=full["minute_close_max_drawdown"])


def f(x,n=2):return "N/A" if x is None else f"{x:,.{n}f}"


def row(title,cfg,stats):
    return f"| {title} | {cfg.cycle} | {cfg.ks:g}/{cfg.kx:g} | {stats['trades']} | {f(stats['win_rate']*100 if stats['win_rate'] is not None else None)}% | {f(stats['payoff_ratio'])} | {f(stats['net_pnl'])} | {f(stats['minute_close_max_drawdown'])} |"


def report(best,selected,runs,stress,ranking):
    lines=["# SR文章小时策略参数优化", "",
           "2026-09-21—10-20，16个交易日，同一批SR701/SR2701一分钟快照。每次1手；开、平各1跳滑点、各3元手续费。",
           f"固定1小时策略，搜索Cycle={CYCLES}，Ks与Kx分别取{COEFFICIENTS}，两种持仓政策共{len(grid())}组。没有调仓位、手续费、时间、止损止盈或额外筛选指标。",
           "", "## 整月回看最高净收益（事后挑选）", "",
           "| 版本 | Cycle | Ks/Kx | 笔数 | 净胜率 | 盈亏比 | 净盈亏/元 | 分钟权益回撤/元 |", "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for mode in [False,True]:
        title="允许隔夜" if not mode else "日内清仓"
        lines.append(row(title,best[mode],runs[(mode,"hindsight_full")][0]))
    lines += ["", "以上直接回答这个月哪些参数赚得最多，包含从整月行情中挑选的偏差，不能称为预先验证通过。carry允许跨夜/周末/长假；intraday只在14:58/22:58清仓。", "",
              "## 只按前半段选参后的表现", "",
              "训练：9月21日—10月8日，8个交易日；验证：10月9日—20日，8个交易日。该月行情此前已经反复查看，属于复用数据的时间切分，不是完全未见样本。",
              "训练门槛：至少8笔且净利>0，并且相邻网格参数至少一半盈利。评分=相邻参数训练净利中位数−0.5×自身训练分钟回撤。选择先落盘，后计算全月及验证；验证结果不用于换参。", "",
              "| 区间/版本 | Cycle | Ks/Kx | 笔数 | 净胜率 | 盈亏比 | 净盈亏/元 | 分钟权益回撤/元 |", "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for mode in [False,True]:
        cfg=selected[mode]
        if cfg is None:
            lines.append(f"| {'日内' if mode else '隔夜'}无参数通过训练门槛 | — | — | — | — | — | — | — |")
        else:
            for name,title in [("selected_train","训练"),("selected_validation","验证"),("selected_full","整月展示")]:
                lines.append(row(("日内" if mode else "隔夜")+title,cfg,runs[(mode,name)][0]))
    lines += ["", "每个独立窗口起始空仓、终点结算。隔夜版全月持续持仓的盈亏不能直接按训练与验证两段相加；训练选择的全月展示含训练部分收益。", "",
              "## 成本压力", "", "| 版本 | 每边滑点 | 笔数 | 净盈亏/元 |", "|---|---:|---:|---:|"]
    for s in stress:lines.append(f"| {s['version']} | {s['slip_ticks']}跳 | {s['trades']} | {f(s['net_pnl'])} |")
    lines += ["", "## 全月前五名与参数邻域", ""]
    for mode in [False,True]:
        lines += ["日内清仓：" if mode else "允许隔夜：", "", "| 参数 | 笔数 | 全月净利 | 训练净利 | 训练邻域盈利比例 |", "|---|---:|---:|---:|---:|"]
        for r in ranking[mode][:5]:
            lines.append(f"| {r['label']} | {r['full_trades']} | {f(r['full_net'])} | {f(r['train_net'])} | {f(r['neighbor_positive_fraction']*100)}% |")
        lines.append("")
    lines += ["## 执行口径与复现", "",
              "沿用上一轮文章引擎：当前小时开盘时，用前Cycle−1根已完成小时K线加当前开盘快照计算Range；当小时高低收不会提前进入开盘信号。原截图的系数硬编码改为可配置参数。",
              "完成分钟收盘确认、下一可交易分钟开盘成交；相反信号先平仓，下次回调再开。与原平台每秒轮询及模拟Tick仍有差别，原平台小时边界和挂单成交没有复现。",
              "来源哈希、小时特征、逐笔成本与现金/权益均核对；快速统计引擎逐字段对照详细引擎，原先四个版本精确复现。固定1手未模拟保证金限制、限价排队或封板。回撤为分钟收盘权益口径。",
              "终点结算的交易在逐笔CSV中标记terminal；未平仓浮盈亏不隐藏。手续费及双边滑点包含在净利和净胜率中。",
              "", "```powershell", "python scripts/optimize_sr_dual_thrust_article.py",
              "python -m unittest discover -s tests -p test_sr_dual_thrust*.py", "```", "",
              "全部1296组训练/全月参数表：parameter_search.csv；冻结选择：training_selection.json；结果汇总：results.json。", ""]
    (OUT/"REPORT.md").write_text("\n".join(lines),encoding="utf-8")


def main():
    freeze_plan()
    minutes=article.prepare()
    features=Features(minutes)
    baseline=json.loads((article.OUT/"results.json").read_text(encoding="utf-8"))["results"]
    reference_checks=0
    for cfg in article.CONFIGS:
        detail=assert_reference(features,minutes,cfg,base.START,base.END)
        assert detail[0]==baseline[cfg.label]
        reference_checks+=1
    training={}
    candidates=grid()
    for i,cfg in enumerate(candidates,1):
        training[key(cfg)]=compact(features,cfg,base.START,TRAIN_END)
        if i%216==0:print("Training",i,"/",len(candidates),flush=True)
    selected,diagnostics=select(training)
    save("training_selection.json",dict(created_at=datetime.now(base.ZONE).isoformat(),
                                        choices={"intraday" if m else "carry":dict(config=asdict(c),label=label(c),stats=training[key(c)],**diagnostics[key(c)]) if c else None for m,c in selected.items()},
                                        rule="training only; no validation PnL used"))
    full={};rows=[]
    for i,cfg in enumerate(candidates,1):
        s=compact(features,cfg)
        full[key(cfg)]=s
        rows.append(flat_row(cfg,training[key(cfg)],s,diagnostics[key(cfg)]))
        if i%216==0:print("Full-month",i,"/",len(candidates),flush=True)
    write_csv("parameter_search.csv",rows)
    best={m:max((c for c in candidates if c.intraday==m),key=lambda c:full_rank(c,full[key(c)])) for m in [False,True]}
    ranking={m:sorted((r for r in rows if r['mode']==('intraday' if m else 'carry')),key=lambda r:(r['full_net'],-r['full_drawdown'],r['full_trades'],r['label']),reverse=True) for m in [False,True]}
    runs={};stress=[]
    for mode in [False,True]:
        configs=[("hindsight_full",best[mode],base.START,base.END)]
        if selected[mode] is not None:
            c=selected[mode]
            configs += [("selected_train",c,base.START,TRAIN_END),("selected_validation",c,VALIDATION_START,base.END),
                        ("selected_full",c,base.START,base.END)]
        for name,cfg,start,end in configs:
            result=assert_reference(features,minutes,cfg,start,end)
            runs[(mode,name)]=result;reference_checks+=1
            filename=("intraday_" if mode else "carry_")+name
            dump_run(filename,result)
        for version,cfg in [("hindsight",best[mode]),("training_selected",selected[mode])]:
            if cfg is None:continue
            original=article.replay(minutes,cfg)[1]
            for slip in [1,2,3]:
                c=replace(cfg,slip_ticks=slip)
                s,ts,*_=assert_reference(features,minutes,c,base.START,base.END);reference_checks+=1
                assert [(t['entry_bar'],t['exit_bar'],t['direction']) for t in ts]==[(t['entry_bar'],t['exit_bar'],t['direction']) for t in original]
                stress.append(dict(version=("intraday_" if mode else "carry_")+version,slip_ticks=slip,**s))
    summary=dict(
        hindsight={"intraday" if m else "carry":dict(config=asdict(best[m]),stats=runs[(m,"hindsight_full")][0]) for m in [False,True]},
        training_selected={"intraday" if m else "carry":dict(config=asdict(selected[m]),train=runs[(m,"selected_train")][0],
                            validation=runs[(m,"selected_validation")][0],full=runs[(m,"selected_full")][0]) if selected[m] else None for m in [False,True]},
        stress=stress)
    save("results.json",summary)
    report(best,selected,runs,stress,ranking)
    save("verification.json",dict(passed=True,grid_count=len(candidates),reference_checks=reference_checks,
                                  checks=["source hashes","prior4reference scenarios exactly reproduced","1296 training then1296 full evaluations",
                                          "training selection frozen before validation","compact/detailed replay agreement",
                                          "fees/slippage/cash/equity reconciliation","cost-only scenario fills identical"]))
    print(json.dumps({k:v for k,v in summary.items() if k!='stress'},ensure_ascii=False,indent=2,default=str),flush=True)
    print("Report:",OUT/"REPORT.md")


if __name__=="__main__":main()
