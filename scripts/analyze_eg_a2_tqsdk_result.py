"""Analyze completed user-run TqSDK A2 results, without new strategy selection."""
from collections import Counter
import csv
from datetime import datetime
import hashlib
import json
from pathlib import Path
import statistics

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/"reports"/"eg_a2_tqsdk_older_four_20261019"
OLD=ROOT/"reports"/"eg_factor_research_20261019"


def read(folder,name):
    return json.loads((folder/name).read_text(encoding="utf-8"))


def main():
    assert read(OUT,"run_status.json")["completed"]
    assert read(OUT,"verification.json")["full_older_four_months"]
    rows=read(OUT,"all_results.json");months=read(OUT,"monthly_equity_changes.json")
    oldrows=read(OLD,"all_core_results.json")+read(OLD,"three_tick_audit.json")
    plan=read(OUT,"frozen_plan.json")
    hashes={f:hashlib.sha256((ROOT/"scripts"/f).read_bytes()).hexdigest()==sha for f,sha in plan["code_sha256"].items()}
    assert all(hashes.values())
    def stat(family,slip):
        rs=[r for r in rows if r["family"]==family and r["slip"]==slip]
        assert len(rs)==2
        assert all(rs[0][k]==rs[1][k] for k in ["net_pnl","minute_close_max_drawdown","trades"])
        return next(r for r in rs if r["mode"]=="ohlc")
    def m(v):return f"{v:+,.0f}"
    stats={};monthly_diagnostics=[]
    for label,folder,file in [("older_four",OUT,"season_volume_2_slip1_ohlc_trades.json"),
                              ("previous_ten",OLD,"continuous_season_volume_2_trades.json")]:
        ts=read(folder,file);vs=[t["net_pnl"] for t in ts]
        wins=[v for v in vs if v>0];losses=[v for v in vs if v<0]
        aw=statistics.mean(wins);al=-statistics.mean(losses)
        longest=run=0
        for v in vs:run=run+1 if v<0 else 0;longest=max(longest,run)
        stats[label]={"trades":len(ts),"net_pnl":sum(vs),"gross_before_fees_after_slippage":sum(t["gross_pnl"] for t in ts),
            "fees":sum(t["fees"] for t in ts),"wins":len(wins),"losses":len(losses),"win_pct":100*len(wins)/len(ts),
            "average_win":aw,"average_loss_abs":al,"descriptive_break_even_win_pct":100*al/(aw+al),
            "profit_factor":sum(wins)/-sum(losses),"median_signal_atr20":statistics.median(t["signal_atr"] for t in ts),
            "max_consecutive_losses":longest,"worst_trade":min(vs),"exit_counts":dict(Counter(t["exit_reason"] for t in ts)),
            "max_margin_assumption":max(t["margin_estimate"] for t in ts),
            "entry_contract_counts":dict(Counter(t["contract"] for t in ts)),
            "long_net":sum(t["net_pnl"] for t in ts if t["direction"]==1),
            "short_net":sum(t["net_pnl"] for t in ts if t["direction"]==-1)}
        if label=="older_four":
            for month in sorted({t["entry_dt"][:7] for t in ts}):
                sub=[t for t in ts if t["entry_dt"].startswith(month)]
                monthly_diagnostics.append({"month":month,"trades":len(sub),"entry_cohort_net":sum(t["net_pnl"] for t in sub),
                    "win_pct":100*sum(t["net_pnl"]>0 for t in sub)/len(sub),
                    "median_signal_atr20":statistics.median(t["signal_atr"] for t in sub),
                    "exit_counts":dict(Counter(t["exit_reason"] for t in sub))})
    # All accounting is rechecked from the saved trades, not the formatted report.
    for r in rows:
        ts=read(OUT,f"{r['family']}_slip{r['slip']}_{r['mode']}_trades.json")
        assert len(ts)==r["trades"] and sum(t["net_pnl"] for t in ts)==r["net_pnl"]
        assert sum(t["fees"] for t in ts)==r["fees"]
        assert all((t["exit"]-t["entry"])*t["direction"]*10-t["fees"]==t["net_pnl"] for t in ts)
    diagnostics={"stats":stats,"monthly_trade_diagnostics":monthly_diagnostics,"frozen_hashes_unchanged":hashes,
        "all_12_scenarios_trade_accounting_reconciled":True,"new_factor_trials":0}
    (OUT/"analysis_diagnostics.json").write_text(json.dumps(diagnostics,ensure_ascii=False,indent=2),encoding="utf-8")
    q=stats["older_four"];old=stats["previous_ten"]
    text=["# A2新增四个月结果分析","", "结论：**A2仍比不加量比的A基线少亏，但更早四个月净亏2,096元，不能据此前十个月盈利就认为它稳定。** 本轮保持全部参数不变，没有用新亏损区间继续筛选阈值。", "",
          "区间为2025-08-16—12-15，实际有交易从8月18日开始；热身从7月16日开始。4万元、原生1手、每边3元费用，普通隔夜允许，周末和长假前清仓。", "",
          "## 同口径的四个月结果","", "| 策略 | 1跳净收益 | 1跳分钟权益回撤 | 笔数 | 2跳净收益/回撤 | 3跳净收益/回撤 |",
          "|---|---:|---:|---:|---:|---:|"]
    for family,label in [("base","A基线"),("season_volume_2","A2同时段量比≥1.5")]:
        a,b,c=[stat(family,s) for s in [1,2,3]]
        text.append(f"| {label} | {m(a['net_pnl'])} | {a['minute_close_max_drawdown']:,.0f} | {a['trades']} | {m(b['net_pnl'])} / {b['minute_close_max_drawdown']:,.0f} | {m(c['net_pnl'])} / {c['minute_close_max_drawdown']:,.0f} |")
    text += ["", "A2相对基线1跳少亏1,282元、分钟回撤减少1,300元，交易从88笔减少至66笔；它有过滤作用，但没有建立正收益。1跳净亏相当于初始资金5.24%，3跳净亏9.64%。这些是历史实际结果，不是未来账户风险上限。", "",
        "## 亏损集中在什么地方","", "自然月权益变化取连续回放，8月和12月是残月，没有在月末重开账户。", "",
        "| 自然月 | 1跳净收益 | 2跳净收益 | 3跳净收益 | 入场笔数 | 1跳胜率 | 1跳普通止损笔数 |",
        "|---|---:|---:|---:|---:|---:|---:|"]
    for diag in monthly_diagnostics:
        month=diag["month"]
        vals=[next(r["net_equity_change"] for r in months if r["family"]=="season_volume_2" and r["slip"]==s and r["month"]==month) for s in [1,2,3]]
        text.append(f"| {month} | {m(vals[0])} | {m(vals[1])} | {m(vals[2])} | {diag['trades']} | {diag['win_pct']:.1f}% | {diag['exit_counts'].get('stop',0)} |")
    text += ["", "9月和10月合计亏2,102元，其余三个含残月区间合计赚6元。9月13笔中10笔止损、没有一笔达到2R目标；10月14笔中11笔止损。2/3跳情况下，每个含残月的自然月都亏损，11月1跳的小幅盈利也消失。", "",
        "## 不只是手续费的问题","", "1跳场景未扣手续费的交易盈亏已经是−1,700元，再扣66笔×6元=396元，得到−2,096元。这里的−1,700元仍包含1跳滑点，不能称为零交易成本收益。", "",
        f"66笔中22赢、44亏；平均盈利单{q['average_win']:.2f}元、平均亏损单{q['average_loss_abs']:.2f}元，按这段实际平均盈亏反推的打平胜率约{q['descriptive_break_even_win_pct']:.1f}%，实际只有33.3%。虽然目标为2R，时间/日历退出和各笔风险距离不同，实际平均盈亏比约1.29，不能直接套用理论2比1的33.3%打平线。", "",
        "退出分布：41笔止损合计−5,856元；12笔目标退出合计+2,948元；12笔时间退出合计+808元；1笔日历退出+4元。没有跳空止损，最大单笔亏186元，但最长10笔连亏。亏损由多笔普通止损累积，普通隔夜偶发跳空并非这段亏损的主要来源。", "",
        "多头合计亏682元、空头合计亏1,414元；不能简单通过只做一个方向就宣称解决问题。最高估算保证金11,262.5元，仍使用25%名义金额假设，未核验真实账户逐日标准。", "",
        "## 与此前十个月为何不同","", "| 1跳交易统计 | 新增四个月 | 此前十个月 |", "|---|---:|---:|"]
    for key,label,fmt in [("win_pct","胜率，%",".1f"),("median_signal_atr20","入场信号ATR20中位数，点",".3f"),
            ("average_win","平均盈利单，元",".2f"),("average_loss_abs","平均亏损单绝对值，元",".2f"),
            ("profit_factor","盈利因子",".2f"),("max_consecutive_losses","最长连亏笔数",".0f")]:
        text.append(f"| {label} | {q[key]:{fmt}} | {old[key]:{fmt}} |")
    text += ["", "较早交易的ATR明显更小，盈利单幅度下降，而亏损没有同比例下降；目标更少达成、胜率下降，构成负期望。这里比较的是已入场交易的ATR，不能把它直接当成全部市场波动；它也不能单独证明‘低ATR导致亏损’。量比只说明当前比自身历史同时段更活跃，低绝对波动下仍可能相对放量，所以1.5倍量比没有保证足够的趋势延续。", "",
        "完整主力成员核对：8月18日日盘为EG2509，当晚转EG2601；66笔中1笔EG2509、65笔EG2601。每笔都是当时主力，不是拿不活跃远月替代主力，仍然出现亏损。", "",
        "各区间独立起始空仓、终点结算，以下不是14个月连续收益：", "",
        "| A2独立区间 | 1跳净收益 | 2跳净收益 | 3跳净收益 |", "|---|---:|---:|---:|"]
    text.append(f"| 新增2025-08-16—12-15 | {m(stat('season_volume_2',1)['net_pnl'])} | {m(stat('season_volume_2',2)['net_pnl'])} | {m(stat('season_volume_2',3)['net_pnl'])} |")
    for w,label in [("f1_train","2025-12-16—2026-04-15"),("validate_four","2026-04-16—08-15"),("f3_test","2026-08-16—10-16")]:
        vals=[]
        for slip in [1,2,3]:
            window={"f1_train":"initial_train","f3_test":"later"}.get(w,w) if slip==3 else w
            vals.append(next(r["net_pnl"] for r in oldrows if r["name"]=="season_volume_2" and r["window"]==window and r["slip"]==slip and r["mode"]=="ohlc"))
        text.append(f"| {label} | "+" | ".join(m(v) for v in vals)+" |")
    text += ["", "此前十个月漂亮、新增四个月亏损，加上EB迁移较弱，说明目前优势存在行情依赖或选型偏差的可能。它并非因一次失败就证明永远无效，也没有通过更广区间的稳健性验证。重复筛选会增加选中偶然优势的风险，参考[Bailey等的回测过拟合研究](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf)。本次不根据这些亏损月份再挑一个刚好避开的ATR阈值。若继续研究择时，应先固定规则，再用未参与阈值选择的数据检验。", "",
        "## 数据与此前短段为何多一笔","", "测试区间27,480个预期分钟全部存在，未缺分钟；主力识别无未解决记录、无价格修补。SDK与原EDB快照重叠的13,110个分钟，在主连和EG2601上，OHLC、成交量、持仓量均逐字段一致。当前源代码哈希与回测前冻结记录一致，12个费用/路径/策略场景的逐笔费用及净利重算对账通过。", "",
        "此前仅有10月20日起的历史。到10月28日14:15信号时，旧短段只包含6个历史14:15量样本，中位数5,982.5手，本根6,599手，量比约1.103，小于1.5，被过滤。完整回测拥有此前10个同时段历史，接受了一笔当日14:16开空、21:01止盈的交易，净赚234元。完整回测同段入场交易合计+144元，旧短段−90元，差额正好234元，其余35笔成交与盈亏一致。完整运行的精确历史分母没有保存，不能编造数值；已确认原短段未通过量比条件、完整运行通过，基础空头信号在短段中也成立。", "",
        "这是历史热身长度改变了量比条件，不是行情改价或少扣费用。当前完整四个月66笔及−2,096元不作人工删改。后续跨窗口比较应统一充分热身；已确认10月28日之前只有6条时，不能把‘至少3条可计算’当作‘已拥有完整10条历史’。详见overlap_trade_reconciliation.json。", "",
        "原始用户回测报告保持不变，本分析只增加诊断和图；没有重新选择或优化策略。结果仍是分钟路径成交近似、固定费用/保证金假设，不是实盘成交保证。", "",
        "![四个月权益与逐月成本压力](analysis_overview.png)", ""]
    (OUT/"ANALYSIS.md").write_text("\n".join(text),encoding="utf-8")
    plt.rcParams.update({"font.sans-serif":["Microsoft YaHei","DejaVu Sans"],"axes.unicode_minus":False,"font.size":10})
    fig,axes=plt.subplots(2,1,figsize=(11.5,7.5),constrained_layout=True)
    for family,label,color in [("base","A基线","#9ca3af"),("season_volume_2","A2放量过滤","#2563eb")]:
        with (OUT/f"{family}_slip1_equity.csv").open(encoding="utf-8-sig",newline="") as f:curve=list(csv.DictReader(f))
        dates=[datetime.fromisoformat(r["dt"]) for r in curve];vals=[float(r["equity"]) for r in curve]
        assert vals[-1]==stat(family,1)["net_pnl"]
        axes[0].plot(dates,vals,color=color,label=f"{label}  {m(vals[-1])}元",lw=1.6)
    axes[0].set_title("新增四个月连续权益｜2025-08-16—12-15，1跳",loc="left",fontweight="bold")
    axes[0].xaxis.set_major_locator(mdates.MonthLocator());axes[0].xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    axes[0].set_ylabel("累计净收益（元）");axes[0].legend(frameon=False,loc="lower left")
    mm=[d["month"] for d in monthly_diagnostics]
    for j,(slip,color) in enumerate([(1,"#2563eb"),(2,"#f59e0b"),(3,"#dc2626")]):
        vals=[next(r["net_equity_change"] for r in months if r["family"]=="season_volume_2" and r["slip"]==slip and r["month"]==month) for month in mm]
        axes[1].bar([i+(j-1)*.24 for i in range(len(mm))],vals,width=.23,label=f"{slip}跳",color=color)
    axes[1].set_xticks(range(len(mm)),[x[2:] for x in mm]);axes[1].set_ylabel("A2月度净收益（元）")
    axes[1].set_title("亏损集中9月、10月；2/3跳每个月都亏损（8月、12月为残月）",loc="left",fontweight="bold")
    axes[1].legend(frameon=False,ncol=3,loc="lower right")
    for ax in axes:ax.axhline(0,color="#64748b",lw=.8);ax.grid(axis="y",alpha=.15)
    fig.suptitle("固定A2较早区间复核｜实际1手、4万元、每边3元手续费\n普通隔夜允许，周末/长假清仓；分析不增加新的参数筛选",fontsize=11)
    fig.savefig(OUT/"analysis_overview.png",dpi=150,facecolor="white");plt.close(fig)
    print("Written ANALYSIS.md, analysis_diagnostics.json, analysis_overview.png")


if __name__=="__main__":main()
