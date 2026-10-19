"""Publish the bounded discovery results, including failed gates and all40 trials."""
import csv
import json
from datetime import datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt

import discover_eg_swing as discovery

OUT = discovery.OUT.parent / "eg_swing_calendarflat_20261019"
A = "channel_15m_r1_target2R"
B = "failed_break_15m_r1_trail2ATR"


def load(name):
    return json.loads((OUT/name).read_text(encoding="utf-8"))


def money(x):
    return f"{x:+,.0f}"


def main():
    results = load("all_results.json")
    cs = load("analysis_plan.json")["configs"]
    selection = load("training_selection.json")
    details = {A: load("channel_audit.json"), B: load("exploratory_audit.json")}
    more = load("exploratory_3tick_stress.json")["results"]

    def result(name, window="continuous", slip=1, mode="ohlc"):
        return next(r for r in results+more if (r["name"], r["window"], r["slip"], r["mode"]) == (name, window, slip, mode))

    reps = []
    for family in discovery.FAMILIES:
        names = {c["name"] for c in cs if c["family"] == family}
        diag = max((r for r in selection["diagnostics"] if r["name"] in names), key=lambda r: r["score"])
        reps.append({"family": family, "name": diag["name"], "train_gate_passed": diag["all_passed"]})
    discovery.OUT = OUT
    discovery.save("family_representatives.json", reps)
    # Calendar metadata never uses future price values. Check every non-weekend boundary.
    cal = load("calendar_boundaries.json")["boundaries"]
    assert cal["5"] == cal["15"]
    holidays = {"2025-12-31 15:00:00", "2026-02-13 15:00:00", "2026-04-03 15:00:00", "2026-04-30 15:00:00", "2026-06-18 15:00:00", "2026-09-24 15:00:00", "2026-09-30 15:00:00"}
    observed = set()
    for row in cal["15"]:
        d = datetime.fromisoformat(row["close"])
        if d.weekday() == 4 and d.hour == 23:
            continue
        assert row["close"] in holidays
        observed.add(row["close"])
    assert observed == holidays
    discovery.save("final_audit.json", {"passed": True, "declared_parameter_sets": 40,
         "calendar_policy_scenarios": len(results), "extra_failed_break_3tick_scenarios": len(more),
         "independent_price_walker_random_cases": 3000, "calendar_boundaries": len(cal["15"]), "seven_holiday_boundaries_checked": sorted(holidays),
         "risk_metric": "one-minute CLOSE mark-to-market DD for all final headline tables; intraminute worst DD remains unknown",
         "formal_qualification": "none passed all training-neighborhood and subsequent-window cost gates; do not deploy as validated"})
    a, b = result(A), result(B)
    lines = ["# EG小波段策略发掘：40组规则及周末/长假风控", "",
             "完成日期：2026-10-19。用户约束：总本金4万元、最大可接受账户回撤约1万元、每手保证金不超过2万元、抓小波段；普通交易日不强制15:00/23:00平仓，周末和长假前清仓。原策略和实盘代码未修改。",
             "", "## 结论", "",
             "本轮发现两个可继续研究的版本：A为15分钟通道突破，B为15分钟假突破反转。A的参数邻域较好，但成本提高后中间验证区间转负；B的邻域和训练段成本压力更弱。正式流程选出的A及开盘区间版本均未完整通过后续检验，**没有一组达到本轮预设的全部稳健性门槛**。下面的盈利数字不是已证实的稳定实盘收益。",
             "", "| 最终规则，实际1手 | 十个月净利 | 分钟收盘最大回撤 | 笔数 | 2跳净利 | 3跳净利 |", "|---|---:|---:|---:|---:|---:|"]
    for name, label in [(A, "A：20根通道突破＋2R目标"), (B, "B：20根假突破反转＋ATR跟踪")]:
        r = result(name)
        lines.append(f"| {label} | {money(r['net_pnl'])} | {r['minute_close_max_drawdown']:,.0f} | {r['trades']} | {money(result(name, slip=2)['net_pnl'])} | {money(result(name, slip=3)['net_pnl'])} |")
    lines += ["", "主表为1跳场景，手续费每手每边3元，每笔完整1手为6元。2/3跳数字重新回放，不能当作固定交易组简单加扣成本；不同成本也可能改变占仓和风控触发。所有数字都是原生1手交易，不是把旧2手分批退出结果除以2。",
              "", "## 数据、时间顺序与试验次数", "",
              "核心数据沿用天勤EDB的2025-12-16—2026-10-16主力成员及具体合约1分钟快照。5/15分钟信号均由连续分钟聚合，指标、形态和成交使用具体合约，不在拼接价差上直接模拟成交。主力按供应商定义，不等同新浪EG0换月规则。原始请求、哈希和此前一处HLC/邻居成员确认在 ../eg_ten_months_20261019/。",
              "额外下载2025-10-20—11-17的主连与EG2601分钟数据，取10月23日—11月16日为较早历史复核，前面留出指标热身。与既有快照重叠的345分钟逐字段一致。新增5分钟映射有11月4日09:05一处持仓量差异，按唯一HLC及前后同成员识别，保留具体合约原始价格。见reserve_sources.json、data_verification.json。",
              "先用2025-12-16—2026-4-15作训练；4月16日—8月15日作中间验证；8月16日—10月16日作后段诊断；另跑十个月连续和较早复核段。窗口独立起始空仓、研究终点结算，连续结果另算，不能通用地将独立段相加。",
              "五类逻辑×两个周期（5/15分钟）×两个初始止损尺度（1/1.5ATR）×两个退出（2R固定目标/2ATR收盘跟踪）=40组。每组报告1/2跳、分钟内OHLC/OLHC两种路径。只按训练段门槛挑最多3个不同家族，再看后续结果；本次入围A和开盘区间15分钟ATR跟踪。全40组及失败结果保留，未删除不利方案。",
              "用户随后确认周末和长假前清仓，因此对相同40组规则在新日历约束下重新运行，原先允许跨周末的结果保留在 ../eg_swing_discovery_20261019/；这是两种持仓政策，不能假装第二轮数据未被看过。最终政策共820个成本/路径/窗口场景，B额外10个3跳诊断。",
              "已有十个月行情及多轮旧策略结果早已被观察，后段只能称时间顺序诊断，不能称完全未触碰留出集。较早复核也不是未来样本。B未通过预设训练邻域门槛，只作为后续诊断发现的探索对象，不提升为正式入围。反复试验产生假阳性的风险见[Bailey等的回测过拟合研究](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf)。",
              "公开[中国商品期货日内动量研究](https://www.sciencedirect.com/science/article/pii/S0275531919311328)及[日内反转研究](https://www.sciencedirect.com/science/article/pii/S0927538X24002865)用于支持把趋势和反转两类放入假设池，不证明本次具体参数盈利。",
              "", "## 五类逻辑的对照", "",
              "每类取训练段评分最高的一组作诊断展示，不能理解成每类全样本最优，也不代表其通过筛选。全部40组见comparison.csv。以下均为最终周末/长假清仓政策，实际1手，1跳。",
              "", "| 家族 | 训练最高评分版本 | 十个月净利 | 分钟回撤 | 训练门槛 |", "|---|---|---:|---:|---|"]
    for rep in reps:
        r = result(rep["name"])
        lines.append(f"| {discovery.NAMES[rep['family']]} | {rep['name']} | {money(r['net_pnl'])} | {r['minute_close_max_drawdown']:,.0f} | {'通过' if rep['train_gate_passed'] else '未通过'} |")
    lines += ["", "## 候选A：通道突破", "",
              "15分钟收盘高于此前20根最高价，且EMA12>EMA36做多；收盘低于此前20根最低价，且EMA12<EMA36做空。此前20根不含当前K线。下一根开盘按市价加不利滑点入场。初始止损为max(12,ceil(ATR20))点，超过46点则跳过；以实际入场价为基准设置止损及2倍风险固定限价目标。最多持有120个正在交易的分钟，其后按市价退出。",
              "", "| A区间 | 1跳净利 | 2跳净利 | 3跳净利 | 1跳分钟回撤 |", "|---|---:|---:|---:|---:|"]
    for w, label in [("train", "训练四个月"), ("validate", "中间四个月"), ("later", "最近两个月"), ("continuous", "十个月连续"), ("reserve", "较早历史复核")]:
        r = result(A, w)
        lines.append(f"| {label} | {money(r['net_pnl'])} | {money(result(A,w,2)['net_pnl'])} | {money(result(A,w,3)['net_pnl'])} | {r['minute_close_max_drawdown']:,.0f} |")
    lines += ["", "A的同家族15分钟4组在训练段1跳下均为正，但中间四个月提高至2跳亏478元，较早20笔复核2跳亏30元，未完整通过成本稳定性要求。3跳中间段也为负。不能因为十个月总盈利就将它标成稳定。",
              "", "## 候选B：假突破反转", "",
              "15分钟最低价跌破此前20根最低价，但收盘重新站回该最低价之上且收阳，下一根做多；最高价突破此前20根最高价，但收盘跌回该最高价之下且收阴，下一根做空。初始止损同A，不设固定盈利目标；每根15分钟收盘后，把保护价向盈利方向更新为收盘价外侧2ATR，下一根才生效，只收紧不放宽；仍最多120个交易分钟。",
              "B十个月123笔净利4652元，1跳分钟回撤3248元；2跳净利2220元、回撤3954元，3跳净利430元、回撤4624元。训练段1跳仅赚446元，2跳亏310元，且同家族15分钟训练邻域仅1/4为正；不能靠这次后来发现的总盈利绕过原筛选门槛。1.5ATR版本还会因相同风险上限排除更多高波动信号，邻域变化包含机会集合变化，不只是扩大止损。",
              "", "## 盈利分布与不确定性", "", "| 指标 | A | B |", "|---|---:|---:|"]
    for label, key, fmt in [("胜率", "win_rate_pct", lambda x: f"{x:.1f}%"), ("盈利因子", "profit_factor", lambda x: f"{x:.2f}"),
                            ("最大单笔亏损", "worst_trade", money), ("持仓交易分钟中位数", "median_holding_trading_minutes", lambda x: f"{x:g}"),
                            ("跨15/23持仓笔数", "cross_15_or_23", str)]:
        lines.append(f"| {label} | {fmt(a[key])} | {fmt(b[key])} |")
    for label, key in [("最长连续亏损笔数", "max_consecutive_losses"), ("盈利月份（共11个含残月）", "positive_months"), ("前三大盈利单合计", "top3_trade_profit"), ("最长自然持仓小时", "max_elapsed_holding_hours")]:
        lines.append(f"| {label} | {details[A][key]:,.2f} | {details[B][key]:,.2f} |")
    lines += ["", "A的前三笔盈利2622元，占总净利约39%；B前三笔4812元，超过总净利4652元，其余交易合计为负。两者都不是高胜率平滑盈利模式。43个日历周权益增量作两周循环区块自助抽样，A平均周盈亏95%描述区间约-113至446元，B约-110至351元，均包含0；该计算未校正选型偏差，不是显著盈利的证明。",
              "", "## 风险边界与日历", "",
              "全账户同时最多1手，按北京时间自然日最多3次入场，日内损失触及800元后停止新开仓，不保证跳空时当日亏损不越线。单笔止损点数12—46，按3跳止损与6元费用校验计划风险不超过500元；跳空可能超过计划，A曾发生单笔亏756元。",
              "保证金使用合约名义金额的25%作研究假设，同时限制每手≤2万元、占当前账户权益≤40%；不是逐日已验证的期货公司真实历史标准。A样本最高估算14820元，B14590元。未来价格和比例变化仍需重新核验。",
              "普通日盘15:00、夜盘23:00不强制清仓，午休可持有。按提前已知的交易日历，在下一开盘距离超过24小时的最后收盘前清仓，覆盖周末和长假；仅依赖开闭市时间，不使用未来价格。5/15分钟日历边界一致，共51个含较早复核期，非周五23点的7个边界逐项核对元旦、春节、清明、五一、端午、中秋、国庆。春节等夜盘停开安排参考[大商所春节通知](https://www.gzjkqh.com/col13/13122)、[清明通知](https://www.ccbfutures.com/main/a/20260326/77468.shtml)、[五一通知](https://www.zjncf.com.cn/customer/info/6081.html)。清仓仍以收盘价加滑点近似，真实执行需要提前发单，无法保证最后成交价。",
              "120分钟指正在交易的分钟数，不是自然两小时；跨午休、普通隔夜后自然时间会更长。最终样本A最长约12小时、B约12.2小时，已没有跨周末/长假的仓位。持仓跨主力切换时继续使用原合约退出，不凭主连跳变制造盈亏。",
              "", "## 验证、复现与使用限制", "",
              "独立分钟退出引擎与既有价格路径引擎在3000个随机多空/止损/止盈/跳空案例上逐项一致；原生1手费用、时间因果、主力入场、单仓、风险上限和权益对账均检查。最大回撤主表使用分钟收盘持仓市值，较5/15分钟收盘更细，但仍非逐笔或分钟内极端回撤。成交未模拟排队、价差动态、涨跌停无法成交，费用为假设值。",
              "命令：`python scripts/discover_eg_swing_calendar.py`；`python scripts/audit_eg_swing_discovery.py --calendar --name channel_15m_r1_target2R`；`python scripts/audit_eg_swing_discovery.py --calendar`；`python scripts/report_eg_swing_discovery.py`。规则明细同时保存到项目根目录 STRATEGY_EG_SWING_RESEARCH.md，原策略和实盘程序不变。",
              "冻结A作优先模拟观察候选，B保留为探索对照。当前证据不足以称任何一个为稳定高效策略，继续实盘前需要独立的新数据和真实费用/成交验证。本轮不继续反复调这批已看过的数据来满足全部门槛。", "", "![权益与月度对照](overview.png)", ""]
    (OUT/"REPORT.md").write_text("\n".join(lines), encoding="utf-8")

    plt.rcParams.update({"font.sans-serif": ["Microsoft YaHei"], "axes.unicode_minus": False, "font.size": 10})
    fig, axs = plt.subplots(2, 1, figsize=(13, 8.5), layout="constrained")
    colors = {A: "#2563eb", B: "#d97706"}; labels = {A: "A：15分钟通道突破", B: "B：15分钟假突破反转（探索）"}
    for name in [A, B]:
        with (OUT/f"continuous_{name}_equity.csv").open(encoding="utf-8-sig") as f:
            curve = list(csv.DictReader(f))
        axs[0].plot([datetime.fromisoformat(e["dt"]) for e in curve], [float(e["equity"]) for e in curve], lw=1.3, color=colors[name], label=labels[name])
    axs[0].axhline(0, color="#64748b", lw=.8)
    axs[0].set(title="EG小波段候选：实际1手，周末和长假前清仓", ylabel="累计净盈亏（元）")
    axs[0].xaxis.set_major_locator(mdates.MonthLocator())
    axs[0].xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    axs[0].legend(); axs[0].grid(alpha=.2)
    ma, mb = load("channel_monthly.json"), load("exploratory_monthly.json")
    all_months = sorted({m["month"] for m in ma+mb}); x = list(range(len(all_months)))
    for name, rows, shift in [(A, ma, -.19), (B, mb, .19)]:
        by_month = {r["month"]: r["net_pnl"] for r in rows}
        axs[1].bar([v+shift for v in x], [by_month.get(m, 0) for m in all_months], width=.36, color=colors[name], label=labels[name])
    axs[1].axhline(0, color="#64748b", lw=.8)
    axs[1].set_xticks(x, [m+("*" if i in [0, len(x)-1] else "") for i, m in enumerate(all_months)], rotation=20)
    axs[1].set(title="月度交易组净收益仍有明显起伏；均未通过全部稳健性门槛", ylabel="净收益（元）")
    axs[1].legend(); axs[1].grid(axis="y", alpha=.2)
    fig.supxlabel("每手每边手续费3元 · 额外滑点1跳 · 不在普通日15:00/23:00强平\n月度按入场自然月归属整笔交易；*为残月；展示曲线为15分钟收盘，风险表另计分钟收盘回撤", fontsize=9)
    fig.savefig(OUT/"overview.png", dpi=150); plt.close(fig)
    print(OUT/"REPORT.md")
    print(OUT/"overview.png")


if __name__ == "__main__":
    main()
