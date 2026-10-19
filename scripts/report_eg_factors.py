"""Render the completed, bounded EG factor experiment; never rerun selection."""
import csv
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "reports" / "eg_factor_research_20261019"


def read(name):
    return json.loads((OUT / name).read_text(encoding="utf-8"))


def money(x):
    return f"{x:+,.0f}"


def main():
    core = read("all_core_results.json")
    stress = read("three_tick_audit.json")
    audit = read("volume_roll_audit.json")
    walk = read("walk_forward_results.json")
    plan = read("analysis_plan.json")
    specs = plan["specifications"]
    labels = {s["id"]: s["label"] for s in specs}
    labels.update({"base": "A基线", "day_only": "A1：仅日盘入场", "season_volume_2": "A2：同时段量比≥1.5", "ema_slope_2": "A3：慢均线斜率≥0.2ATR"})

    def result(name, window="continuous", slip=1):
        w = "initial_train" if window == "f1_train" and slip == 3 else "later" if window == "f3_test" and slip == 3 else window
        rows = [r for r in (stress if slip == 3 else core) if r["name"] == name and r["window"] == w and r["slip"] == slip]
        assert len(rows) == 2, (name, window, slip)
        # Report conservative path envelopes rather than silently choose a favorable path.
        r = next(r for r in rows if r["mode"] == "ohlc").copy()
        r["net_pnl"] = min(x["net_pnl"] for x in rows)
        r["minute_close_max_drawdown"] = max(x["minute_close_max_drawdown"] for x in rows)
        return r

    def load_curve(name):
        with (OUT / name).open(encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        return [(datetime.fromisoformat(r["dt"]), float(r["equity"])) for r in rows]

    monthly = {}
    trade_summary = {}
    names = ["base", "day_only", "season_volume_2", "ema_slope_2"]
    for name in names:
        curve = load_curve(f"continuous_{name}_equity.csv")
        ends = {}
        for dt, value in curve:
            ends[dt.strftime("%Y-%m")] = value
        previous = 0.
        monthly[name] = {}
        for month, value in ends.items():
            monthly[name][month] = value - previous
            previous = value
        ts = read(f"continuous_{name}_trades.json")
        assert abs(sum(t["net_pnl"] for t in ts) - result(name)["net_pnl"]) < 1e-8
        assert abs(sum(monthly[name].values()) - result(name)["net_pnl"]) < 1e-8
        longest = run = 0
        for t in ts:
            run = run + 1 if t["net_pnl"] < 0 else 0
            longest = max(longest, run)
        trade_summary[name] = {
            "max_margin_assumption": max(t["margin_estimate"] for t in ts),
            "max_consecutive_losses": longest,
            "profitable_calendar_months": sum(v > 0 for v in monthly[name].values()),
            "calendar_months_including_partial": len(monthly[name]),
            "largest_three_winners": sum(sorted((t["net_pnl"] for t in ts), reverse=True)[:3]),
            "net_per_trade": sum(t["net_pnl"] for t in ts) / len(ts),
        }
    (OUT / "monthly_equity_changes.json").write_text(json.dumps(monthly, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "trade_summary.json").write_text(json.dumps(trade_summary, ensure_ascii=False, indent=2), encoding="utf-8")

    plt.rcParams.update({"font.sans-serif": ["Microsoft YaHei", "DejaVu Sans"], "axes.unicode_minus": False, "font.size": 10})
    fig, axes = plt.subplots(2, 1, figsize=(12, 8.2), constrained_layout=True)
    colors = {"base": "#6b7280", "day_only": "#2563eb", "season_volume_2": "#059669"}
    for name in ["base", "day_only", "season_volume_2"]:
        pts = load_curve(f"continuous_{name}_equity.csv")
        axes[0].plot([p[0] for p in pts], [p[1] for p in pts], label=f"{labels[name]}  {money(result(name)['net_pnl'])}元", color=colors[name], lw=1.6)
    axes[0].set_title("固定规则十个月连续回放｜2025-12-16—2026-10-16", loc="left", fontweight="bold")
    for policy, label, color in [("base", "A基线", "#6b7280"), ("adaptive", "按过去数据每两个月筛选", "#7c3aed")]:
        pts = load_curve(f"walk_{policy}_equity.csv")
        s = next(r for r in walk if r["policy"] == policy and r["slip"] == 1 and r["mode"] == "ohlc")
        axes[1].plot([p[0] for p in pts], [p[1] for p in pts], label=f"{label}  {money(s['net_pnl'])}元", color=color, lw=1.7)
    axes[1].set_title("按历史顺序筛选的六个月回放｜2026-04-16—2026-10-16", loc="left", fontweight="bold")
    for ax in axes:
        ax.axhline(0, color="#cbd5e1", lw=.8)
        ax.grid(alpha=.15)
        ax.set_ylabel("累计净收益（元）")
        ax.legend(loc="upper left", frameon=False)
        ax.xaxis.set_major_locator(mdates.MonthLocator())
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    fig.suptitle("EG因子研究：实际1手，手续费3元/边，市价成交不利滑点1跳\n允许普通隔夜，周末/长假清仓；历史重复研究，不是独立未来验证", fontsize=11)
    fig.savefig(OUT / "overview.png", dpi=160, facecolor="white")
    plt.close(fig)

    text = ["# EG小波段因子扩展研究", "", "完成日期：2026-10-19。基于已冻结的A通道突破策略，保持原生1手、退出、风险预算和持仓政策，只研究入场过滤。没有修改生产交易程序。", "", "## 结论", "", "本轮有明显的历史改善：同时段成交量过滤将十个月净利由6,726元提高到12,890元，分钟收盘最大回撤由3,636元降到2,010元。训练段优先选出的仅日盘入场版本，十个月净利8,716元。按既定规则每两个月只使用过去数据筛选，后六个月净利由基线2,590元提高到5,732元。三个数字对应不同规则或期间，不能相加或混用。", "", "**这些是继续模拟验证的研究候选，尚不能称为稳定获利策略。** 量比版本在较早14笔复核中仍亏损；本批34个附加方案的多重比较诊断未显示显著优势，且这些行情已经被前几轮研究查看。", "", "## 统一口径与数据", "", "- 本金40,000元，具体主力合约实际1手，每点10元。净利已扣每边3元费用。1/2/3跳均重新逐分钟回放；入场、止损、时间/日历退出计不利滑点，固定限价目标不额外计滑点。", "- 十个月：2025-12-16—2026-10-16；训练四个月至2026-04-15，中间四个月2026-04-16—08-15，后两个月2026-08-16—10-16。较早复核为2025-10-23—11-16，前3个交易日数据热身。", "- 沿用天勤EDB主连和具体合约1分钟原始快照，15分钟信号由具体合约分钟数据聚合，主连仅识别成员。供应商主力定义不保证与新浪EG0完全一致。没有拼接主连价差来成交。来源、哈希及成员异常说明保留在[上一轮数据审计](../eg_swing_calendarflat_20261019/REPORT.md)及[原十个月快照](../eg_ten_months_20261019/)。接口说明见[天勤EDB](https://doc.shinnytech.com/edb/latest/md_server.html)。", "- 普通日15:00/23:00不强平，午休可持仓；周末及长假前清仓。使用已知休市日历，不用未来价格，沿用已核对的51个日历边界。120分钟持仓上限按交易中的分钟计时。", "- 主表回撤为一分钟收盘持仓权益回撤，含浮动盈亏；不是逐笔最坏回撤。净利取OHLC/OLHC两种分钟路径较低值，回撤取较大值；展示的核心候选在这些口径下两条路径一致。图为15分钟采样权益，仅作展示，回撤来自分钟引擎。", "- 各独立窗口起始空仓并在终点结算；十个月连续结果单独运行。不同独立区间不能普遍直接相加。六个月滚动模拟两种策略均在每两个月预定换模型边界结算，收费、滑点和账户资金承接完全相同。", "", "## 固定因子版本：十个月连续结果", "", "本表不是把全样本最优提升为事先选定。A1/A2/A3来自最初四个月训练入围前三名；A2后来恰为全样本最优。", "", "| 版本 | 1跳净利 | 1跳分钟回撤 | 笔数 | 胜率 | 盈利因子 | 2跳净利 | 3跳净利 |", "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for name in names:
        s = result(name)
        text.append(f"| {labels[name]} | {money(s['net_pnl'])} | {s['minute_close_max_drawdown']:,.0f} | {s['trades']} | {s['win_rate_pct']:.1f}% | {s['profit_factor']:.2f} | {money(result(name, slip=2)['net_pnl'])} | {money(result(name, slip=3)['net_pnl'])} |")
    text += ["", "A2净利增加6,164元（约91.6%），交易由199笔降到135笔；没有加手数或放宽止损。1跳净利相当于4万元初始本金的32.2%，这是十个月简单收益率，不作年化。过滤后可能允许出现基线因占仓而错过的新交易，因此结果来自完整重跑，不能用基线交易删行替代。", "", "| 版本 | 2跳分钟回撤 | 3跳分钟回撤 | 最大单笔亏损（1跳） | 最长连亏 | 盈利月/含残月总月数 | 最高估算保证金 |", "|---|---:|---:|---:|---:|---:|---:|"]
    for name in names:
        s = result(name); t = trade_summary[name]
        text.append(f"| {labels[name]} | {result(name, slip=2)['minute_close_max_drawdown']:,.0f} | {result(name, slip=3)['minute_close_max_drawdown']:,.0f} | {money(s['worst_trade'])} | {t['max_consecutive_losses']} | {t['profitable_calendar_months']}/{t['calendar_months_including_partial']} | {t['max_margin_assumption']:,.1f} |")
    text += ["", "保证金以名义金额25%为研究假设，逐笔限制不超过2万元与当前账户权益40%，未替代期货公司的真实历史标准。计划单笔风险≤500元，跳空实际曾亏756元；普通隔夜仍有跳空风险。", "", "## 新因子是什么、为什么有改善", "", "### A2：相对历史同时段成交量", "", "`量比_t = 本合约当前完整15分钟成交量 / median(本合约此前最多10个交易日相同钟点的15分钟成交量)`，至少3个历史观测，历史不含当前K线。在A的多空条件之外，要求量比≥1.5。", "", "例如09:45仅与以前09:45比较，不直接与午后、夜盘等不同活跃度时段混比。当前K线收盘后才知道完整成交量，下一根开盘交易，避免盘中尚未放量就按事后放量成交。成交量提供当前活跃程度，不能解释成净买入或净流入。对成交量和持仓量的定义见[CME说明](https://www.cmegroup.com/education/courses/introduction-to-futures/open-interest)。", "", "这一过滤在本样本减少了低活跃度突破的成本消耗，1跳净利/交易由约34元升到约95元。这是交易统计支持的现象；‘大资金推动趋势’只是可能的机制，分钟量价数据不能证明。较宽阈值量比≥1.0也有改善：十个月1跳9,166元、2跳6,016元，未出现只有1.5一个孤立点盈利的情形。", "", "### A1：只在日盘生成入场", "", "在A上增加信号时刻属于日盘的要求，夜盘不发新入场信号；原有持仓仍按止损/止盈/120交易分钟/周末长假规则退出，**不恢复每日15:00或23:00强平**。样本中夜盘单独入场十个月净亏2,030元，日盘单独入场净赚8,716元；由于占仓和每日次数路径不同，两者不能机械相加为A。", "", "A1规则简单，训练评分第一，在中间四个月、后两个月、较早复核的1/2跳均盈利并高于基线，是本轮优先冻结的模拟对照。3跳中间四个月只剩62元，成本余量仍薄；全样本1跳回撤3,678元，也没有比A更低。", "", "### A3与其他因子的证据", "", "A3要求 `方向×(EMA36_t−EMA36_t−4)/ATR20_t ≥0.2`，十个月提升但中间四个月3跳仍亏672元；不与量比再任意拼接制造更高历史收益。三根持仓量减少在中段有效，最近两个月却亏1,028元；‘增仓才是真突破’在本样本也不成立。持仓量不等于多空净资金。", "", "ATR只使用已完成K线就能计算，并不需要等未来波动走完；它确有滞后，但不构成未来函数。本轮ATR分位限制十个月净利分别3,048元、−316元，较紧的扩张阈值亏542元，没有证据表明仅靠ATR择时便能提高收益。量比同样要等15分钟收盘，不能消除所有滞后。", "", "## 分段与月度：避免总收益掩盖弱点", "", "每格为1跳净利 / 2跳净利（元）。", "", "| 版本 | 最初训练四个月 | 中间四个月 | 后两个月 | 较早历史复核 |", "|---|---:|---:|---:|---:|"]
    for name in names:
        vals = [f"{money(result(name,w,1)['net_pnl'])} / {money(result(name,w,2)['net_pnl'])}" for w in ["f1_train", "validate_four", "f3_test", "reserve"]]
        text.append(f"| {labels[name]} | " + " | ".join(vals) + " |")
    text += ["", "A2较早复核只有14笔，1跳−44元、2跳−164元、3跳−284元，既不能据此证明长期失效，也不能省略。A1及A3通过本轮最初训练入围后的1/2跳盈利改善门槛；这只是内部研究门槛，历史已被查看，不能升级为独立未来验证。", "", "下表按十个月连续权益的月末水平差计算，不在每月重置仓位；12月和10月是残月。", "", "| 自然月 | A | A1日盘 | A2量比 | A3斜率 |", "|---|---:|---:|---:|---:|"]
    for month in monthly["base"]:
        text.append(f"| {month} | " + " | ".join(money(monthly[n][month]) for n in names) + " |")
    contribution = monthly["season_volume_2"]["2026-03"] + monthly["season_volume_2"]["2026-08"]
    text += ["", f"A2的3月和8月合计净利{contribution:,.0f}元，占十个月净利约{100*contribution/result('season_volume_2')['net_pnl']:.1f}%；11个含残月的自然月仅6个月盈利，最长7笔连亏，9月和10月残月均亏损。它减少了部分弱市损失，但盈利依然依赖少数有利月份，不能描述为月月稳定。"]
    text += ["", "## 六个月按历史顺序筛选", "", "每次只用当时已结束的训练数据，先将选择保存到`f*_selection_before_test.json`，再计算随后两个月。训练门槛：1/2跳两路径盈利；交易数≥30且≥基线35%；1跳盈利因子≥1.1；2跳比基线改善≥max(500元,基线净利绝对值20%)；分钟回撤≤min(8,000元,基线2跳回撤1.25倍)；同组相邻阈值2跳盈利。按较差2跳净利/回撤排序，无入围则回退A。", "", "| 下一段测试期 | 只用截至何时训练 | 选中因子 | 优化净利1跳 | 基线净利1跳 | 优化净利2跳 |", "|---|---|---|---:|---:|---:|"]
    a1 = next(r for r in walk if r["policy"] == "adaptive" and r["slip"] == 1 and r["mode"] == "ohlc")
    a2 = next(r for r in walk if r["policy"] == "adaptive" and r["slip"] == 2 and r["mode"] == "ohlc")
    b1 = next(r for r in walk if r["policy"] == "base" and r["slip"] == 1 and r["mode"] == "ohlc")
    for i, fold in enumerate(plan["folds"]):
        s = a1["segments"][i]
        text.append(f"| {fold['test'][0][:10]}—{fold['test'][1][:10]} | {fold['train'][1][:10]} | {labels[s['factor']]} | {money(s['net_pnl'])} | {money(b1['segments'][i]['net_pnl'])} | {money(a2['segments'][i]['net_pnl'])} |")
    text += ["", "| 2026-04-16—10-16，同一六个月 | 1跳净利 | 1跳分钟回撤 | 笔数 | 2跳净利/回撤 | 3跳净利/回撤 |", "|---|---:|---:|---:|---:|---:|"]
    for policy, label in [("base", "A基线"), ("adaptive", "过去数据筛选")]:
        rs = [next(r for r in walk if r["policy"] == policy and r["slip"] == s and r["mode"] == "ohlc") for s in [1,2,3]]
        s = rs[0]
        text.append(f"| {label} | {money(s['net_pnl'])} | {s['minute_close_max_drawdown']:,.0f} | {s['trades']} | {money(rs[1]['net_pnl'])} / {rs[1]['minute_close_max_drawdown']:,.0f} | {money(rs[2]['net_pnl'])} / {rs[2]['minute_close_max_drawdown']:,.0f} |")
    text += ["", "滚动收益承接账户资金，第一段40,000元；1跳优化各段起始资金40,000、42,132、43,340元。每两个月预定模型切换时结算存量仓位并计成本，普通日并不强平。拼接回撤使用分钟权益轨迹重新计算，不能取各段回撤最大值替代。该流程避免用下一段成绩选模型，但假设池来自已反复查看的同一批行情，仍是回溯诊断。", "", "## 换月与成交量分母的追加审计", "", "同一合约成为主力前可能较冷清，使自己的同时段分母偏小。因此在初次筛选后追加了3种定义敏感性诊断，每种5个窗口×3档滑点×2条路径，共90个场景。它们没有进入最初35组选择，也没有用来替换最初选出的A2。", "", "| 十个月诊断定义 | 1跳净利 | 1跳分钟回撤 | 2跳净利 | 3跳净利 |", "|---|---:|---:|---:|---:|"]
    alabels = {"main_volume15": "同时段历史分母串接当时历任主力", "same_volume_skip5sessions": "原A2跳过换主力后前5个日/夜时段", "same_volume_skip10sessions": "原A2跳过换主力后前10个日/夜时段"}
    for name, label in alabels.items():
        rs = [next(r for r in audit["results"] if r["name"] == name and r["window"] == "continuous" and r["slip"] == s and r["mode"] == "ohlc") for s in [1,2,3]]
        text.append(f"| {label} | {money(rs[0]['net_pnl'])} | {rs[0]['minute_close_max_drawdown']:,.0f} | {money(rs[1]['net_pnl'])} | {money(rs[2]['net_pnl'])} |")
    text += ["", "原A2中，换主力后前10个日/夜时段共16笔净亏606元，其余119笔净赚13,496元。放量改善没有集中在换月初期；改为历任主力同时段分母后仍有改善。但追加定义的13,978元属于事后敏感性结果，不冒充预先筛选成绩；较早复核该定义仍亏44/164/284元。", "", "## 多因子筛选与偶然性", "", "本轮先保存假设与门槛，再做13组×2阈值、4个时段方案、4个事先列出的组合，加A共35组。主流程1,260个成本/路径/窗口场景；初始入围及滚动选中方案另50个3跳场景，滚动执行12个场景，换月诊断90个场景。这里的场景不是独立样本数。先前还已试过40组策略×两种持仓政策。", "", "用43个日历周的策略−基线权益增量，共同两周循环区块、2,000次中心化自助抽样，检查34个附加方案的最大平均改善。1跳p≈0.341，2跳p≈0.285，**不能排除从很多方案中偶然选中漂亮结果**。这是自定义的简化最大均值诊断，不是完整White Reality Check或SPA；没有校正此前所有研究及追加90场景，不构成收益显著性的证明。详见[multiple_testing_audit.json](multiple_testing_audit.json)及[回测过拟合研究](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf)。", "", "量价衍生指标只能挖掘当前数据已有信息。本轮没有订单簿、逐笔主动买卖、可对齐的现货基差或产业库存数据，因此没有编造订单流、净资金或基本面因子。", "", "## 全部35组，含不利结果", "", "十个月连续1/2跳；不是每组均通过训练门槛。全部窗口、成本、路径另见[comparison.csv](comparison.csv)。", "", "| ID与条件 | 1跳净利 | 1跳分钟回撤 | 2跳净利 |", "|---|---:|---:|---:|"]
    for spec in specs:
        s = result(spec["id"])
        text.append(f"| {spec['id']}：{spec['label']} | {money(s['net_pnl'])} | {s['minute_close_max_drawdown']:,.0f} | {money(result(spec['id'],slip=2)['net_pnl'])} |")
    text += ["", "## 复现与验证", "", "```text", "python scripts/research_eg_factors.py", "python scripts/audit_eg_factors.py", "python scripts/report_eg_factors.py", "```", "", "本轮基线20个场景与上一轮逐项一致；对每个合约多个历史前缀重算全部因子和信号；检查单仓、主力入场、费用、计划风险、保证金、周末/长假空仓；滚动承接资金及分钟回撤逐段对账通过。见[verification.json](verification.json)、[analysis_plan.json](analysis_plan.json)、各窗口每笔交易JSON与权益CSV。月度权益增量和交易净利已与连续净利对账。", "", "成交仍使用分钟价格情景：未模拟限价排队、触价不成交、动态价差和涨跌停封板；周末清仓按最后收盘加滑点近似，需要提前下单的实际成交验证。分钟回撤小于1万元不保证未来回撤不超过1万元。研究使用固定手续费和25%保证金假设，不是账户实收核验。", "", "后续冻结A1作为简单对照、A2作为收益优先候选，只在独立新增数据及模拟真实成交上积累证据。本轮不自动启用实盘或提高仓位。完整条件见根目录[STRATEGY_EG_FACTOR_RESEARCH.md](../../STRATEGY_EG_FACTOR_RESEARCH.md)。", "", "![同一期间内比较权益，两个面板期间不同](overview.png)", ""]
    (OUT / "REPORT.md").write_text("\n".join(text), encoding="utf-8")
    print("Wrote REPORT.md, overview.png, monthly_equity_changes.json, trade_summary.json")


if __name__ == "__main__":
    main()
