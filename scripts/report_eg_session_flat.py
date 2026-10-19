"""Present the fixed session-liquidation experiment without selecting parameters."""
import csv
import json
from datetime import datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt

OUT = Path(__file__).resolve().parents[1] / "reports" / "eg_session_flat_20261019"


def load(name):
    return json.loads((OUT / name).read_text(encoding="utf-8"))


def money(x):
    return f"{x:+,.0f}"


def main():
    rs = load("all_results.json")
    months = load("monthly_comparison.json")
    attrib = load("trade_change_attribution.json")["summary"]

    def result(w="ten_months", name="session_flat", slip=1, mode="adverse_first"):
        return next(r for r in rs if (r["window"], r["name"], r["slip"], r["mode"]) == (w, name, slip, mode))

    old, new = result(name="original"), result()
    report = ["# EG旧宽退出：15:00与23:00强制平仓", "",
              "2026-10-19完成。完整回放区间为2025-12-16 09:00至2026-10-16 23:00，北京时间；另列新增四个月、此前四个月、最近两个月及六个月连续结果。只测试用户要求的固定时点规则，没有调优止损、止盈或ATR条件。",
              "", "## 执行口径", "",
              "旧宽退出保持实际2手：初始止损30点；第一手50点限价止盈，第二手先保护10点，后按30点阶梯跟踪。15分钟收盘判断原警告形态，下一根K线触发；原形态和ATR过滤保留，不附加ATR择时。没有改成前面讨论的1手日内策略。",
              "唯一主要变化：每个15:00、23:00，在正常回放该根最后一分钟后，将剩余全部手数按具体持仓合约收盘价加不利滑点平掉；午休和10:15休市不平仓。若原止损/止盈先成交，则优先采用正常成交。主表开仓、止损、强平额外不利滑点均1跳，首目标为限价不额外滑点；手续费每手每边3元，每笔完整2手共12元。",
              "本次保留原信号有效至下一根交易K线的规则，15:00或23:00收盘形成的警告可在下一交易时段触发，但没有仓位跨越这两个收盘时点。收盘信号也撤销的版本单列为敏感性对照，不混入主要结果。",
              "15:00/23:00成交以最终分钟收盘价加滑点建模，是预定收盘清仓的回测近似；无法凭分钟数据确认真实可在最后时点成交，实盘应提前安排订单并核验成交。尚未模拟涨跌停无法平仓、排队和保证金追缴。",
              "", "## 数据与EG0含义", "",
              "EG0按主力连续理解，本次继续使用已保存的天勤EDB KQ.m@DCE.eg快照与具体合约分钟数据，保证和上一轮同数据比较；不是新浪EG0原始序列，二者换月定义可能不同。信号、ATR、成交在当时主力具体月份合约上计算，避免主连拼接价差直接进入盈亏。主力成员及已披露的一处HLC/相邻成员确认沿用前报告。",
              "原始快照、请求URL、SHA256和换月映射位于 ../eg_ten_months_20261019/，详见其中 sources_older.json、merged_inputs.json、main_contract_mapping.json。供应商主力定义见[天勤文档](https://doc.shinnytech.com/tqsdk/latest/usage/mddatas.html)，公开分钟服务见[EDB文档](https://doc.shinnytech.com/edb/latest/md_server.html)。本次没有切换数据源。",
              "", "## 总体结果", "", "| 指标 | 原版允许跨收盘持仓 | 15:00/23:00强平 |", "|---|---:|---:|"]
    for label, key, fmt in [("净收益（元）", "net_pnl", money), ("交易笔数", "trades", str),
                            ("胜率", "win_rate_pct", lambda x: f"{x:.1f}%"), ("最大回撤（元）", "max_drawdown", lambda x: f"{x:,.0f}"),
                            ("最大单笔亏损（元）", "worst_trade", money), ("越价止损交易数", "gap_stop_trades", str),
                            ("手续费（元）", "fees", lambda x: f"{x:,.0f}")]:
        report.append(f"| {label} | {fmt(old[key])} | {fmt(new[key])} |")
    report += ["", "最大回撤基于每15分钟收盘的已实现盈亏、持仓市值和已付费用，不是逐笔或分钟内最大回撤；两个版本按相同频率计算。强平版本116笔含收盘强平（15:00为64笔、23:00为52笔），共强平216手；其他退出仍遵守原止损/止盈。",
               "以4万元初始本金比较，16669元回撤相当于本金的41.7%，超过用户可接受的1万元。此处没有以减半的风险归一化数值替代真实2手结果；原策略需要分批退出，不能将整张表简单除以2当作可执行1手策略。",
               "", "## 分段对比", "", "| 区间 | 原版净利 | 强平净利 | 强平交易数 | 强平最大回撤 |", "|---|---:|---:|---:|---:|"]
    labels = {"new_older_four": "2025/12/16—2026/4/15", "previous_four": "2026/4/16—8/15", "recent_two": "2026/8/16—10/16",
              "continuous_six": "2026/4/16—10/16连续", "ten_months": "2025/12/16—2026/10/16连续"}
    for w, label in labels.items():
        a, b = result(w, "original"), result(w)
        report.append(f"| {label} | {money(a['net_pnl'])} | {money(b['net_pnl'])} | {b['trades']} | {b['max_drawdown']:,.0f} |")
    assert result("new_older_four")["net_pnl"]+result("previous_four")["net_pnl"]+result("recent_two")["net_pnl"] == new["net_pnl"]
    report += ["", "强平版本这几个分段的边界均已空仓，因此-6966-6188+8434=-4720，与十个月连续值一致。原版各独立窗口仍会因边界持仓不同而不能直接相加为连续值。最近两个月仍盈利，但较早窗口亏损，不能据最近盈利判定规则稳定有效。",
               "", "## 月度对比", "",
               "交易组按入场自然月归属整笔最终净收益。原版有跨月持仓，月末市值增量另列；强平版没有跨月持仓，两种月度口径相同。两个版本分别合计6744元和-4720元。12月从16日起，10月截至16日。",
               "", "| 月份 | 原版交易组净利 | 强平净利 | 差额 | 原版月末权益增量 |", "|---|---:|---:|---:|---:|"]
    for r in months:
        report.append(f"| {r['month']} | {money(r['original_entry_month_net'])} | {money(r['flat_entry_month_net'])} | {money(r['entry_month_delta'])} | {money(r['original_calendar_equity_change'])} |")
    report += ["", "## 为什么跳空减少，整体收益却下降", "",
               "3月26日14:31入场的EG2605空单仍在两个版本中出现：入场5046，原版留到夜盘5227止损，净亏3632元；强平版15:00按5058收盘价加1跳，在5059平仓，净亏272元，这笔改善3360元。它确实消除了该笔跨日盘/夜盘的尾部风险。",
               "原版13笔越价止损相对正常止损/保护价加1跳，额外损失总计6020元；新版只剩7月22日午休后的1笔，额外损失20元。该笔整笔仍盈利558元，越价止损不等于整笔亏损。午休持仓按用户要求保留。",
               "减少尾部损失没有自动带来正期望，提前平仓也改变了盈利单后续空间和后续占仓机会。对两个版本完全相同入场的交易按合约、信号时间、入场时间、方向和入场价配对，差额可精确对账：",
               "", "| 变化来源 | 笔数 | 对新减旧净利的影响 |", "|---|---:|---:|"]
    report += [f"| 共同入场，后续退出变化 | {attrib['common_entries']} | {money(attrib['common_entry_pnl_delta'])} |",
               f"| 提前释放仓位后新增入场 | {attrib['new_entries']} | {money(attrib['new_entries_net'])} |",
               f"| 原有入场不再发生 | {attrib['removed_old_entries']} | {money(-attrib['removed_old_entries_net'])} |",
               f"| 合计 | — | {money(attrib['total_pnl_delta'])} |", "",
               "新增43笔共亏8856元，是这次差额中最大的项；该组费用共516元，说明不能仅归因于多交手续费。两个版本总费用仅相差504元。配对是路径变化后的会计分解，不代表可以事前识别并删除这些新增亏损交易。逐笔见 trade_change_attribution.json。",
               "", "## 滑点和分钟内路径敏感性", "", "| 额外滑点 | 强平版四路径净利范围 | 强平版adverse_first净利 |", "|---|---:|---:|"]
    for slip in [0, 1, 2]:
        a = [r for r in rs if r["window"] == "ten_months" and r["name"] == "session_flat" and r["slip"] == slip]
        report.append(f"| {slip}跳 | {money(min(r['net_pnl'] for r in a))} 至 {money(max(r['net_pnl'] for r in a))} | {money(result(slip=slip)['net_pnl'])} |")
    alt = result(name="flat_cancel_close_signal")
    report += ["", "0跳仍扣手续费；滑点同时影响触发、占仓与后续机会，因此不同滑点场景不是对同一组交易简单多扣成本。四种OHLC路径仅是分钟内部成交顺序的情景比较，不是严格上下界。主表沿用adverse_first以保持可比，这个名字不代表最低收益情景。",
               f"补充敏感性：若15:00和23:00生成的警告也撤销，1跳adverse_first净利为{money(alt['net_pnl'])}元，最大回撤{alt['max_drawdown']:,.0f}元。仍亏损，没有因该信号处理歧义改变主要判断。完整四路径、1/2跳结果均在all_results.json，未按结果挑选版本。",
               "", "## 核验与结论", "",
               "128种窗口/规则/成本/路径场景完成；其中60个原版场景逐项复现旧报告。逐场景验证15:00/23:00持仓为0、没有仓位跨这两个时点、强平价格等于实际持仓合约收盘价加指定滑点、入场对应供应商当时主力、每笔2手手续费与盈亏、月度权益及交易变化差额对账。核验结果见verification.json。",
               "固定时点清仓有效压低了这批数据中的跳空尾部损失，但旧宽退出在此限制下十个月净亏4720元，回撤仍超过1万元。因此这一版尚不满足4万元账户的收益与回撤要求。有限历史及分钟成交假设不能证明实盘风险上限，不修改实盘代码或原策略文件。",
               "", "复现：`python scripts/backtest_eg_session_flat.py`；图文：`python scripts/report_eg_session_flat.py`。", "", "![权益与月度对比](overview.png)", ""]
    (OUT / "REPORT.md").write_text("\n".join(report), encoding="utf-8")

    plt.rcParams.update({"font.sans-serif": ["Microsoft YaHei"], "axes.unicode_minus": False, "font.size": 10})
    fig, axs = plt.subplots(2, 1, figsize=(13, 8.5), layout="constrained")
    colors = {"original": "#2563eb", "session_flat": "#d97706"}
    legends = {"original": "旧宽退出：允许跨收盘持仓", "session_flat": "旧宽退出：15:00 / 23:00 全平"}
    for name in ["original", "session_flat"]:
        with (OUT / f"ten_months_{name}_slip1_adverse_first_equity.csv").open(encoding="utf-8-sig") as f:
            curve = list(csv.DictReader(f))
        axs[0].plot([datetime.fromisoformat(e["dt"]) for e in curve], [float(e["equity"]) for e in curve], color=colors[name], lw=1.3, label=legends[name])
    axs[0].axhline(0, color="#64748b", lw=.8)
    axs[0].set(title="EG旧宽退出：收盘强平减少跳空，但十个月净收益转负", ylabel="累计净盈亏（元）")
    axs[0].xaxis.set_major_locator(mdates.MonthLocator(interval=1))
    axs[0].xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    axs[0].legend(loc="lower right")
    axs[0].grid(alpha=.2)
    x = list(range(len(months)))
    axs[1].bar([a-.19 for a in x], [r["original_entry_month_net"] for r in months], width=.36, color=colors["original"], label="原版")
    axs[1].bar([a+.19 for a in x], [r["flat_entry_month_net"] for r in months], width=.36, color=colors["session_flat"], label="收盘强平")
    axs[1].axhline(0, color="#64748b", lw=.8)
    axs[1].set_xticks(x, [r["month"] + ("*" if i in [0, len(months)-1] else "") for i, r in enumerate(months)], rotation=20)
    axs[1].set(title="月度交易组净收益（按入场自然月归属整笔交易）", ylabel="净收益（元）")
    axs[1].legend(); axs[1].grid(axis="y", alpha=.2)
    fig.supxlabel("实际2手 · 30点止损 / 50点首目标 · 手续费3元/手/边 · 额外滑点1跳\n分钟回放，分钟内部采用adverse_first情景；午休保留持仓；*为残月", fontsize=9)
    fig.savefig(OUT / "overview.png", dpi=150)
    plt.close(fig)
    print(OUT / "REPORT.md")
    print(OUT / "overview.png")


if __name__ == "__main__":
    main()
