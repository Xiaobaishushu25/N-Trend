"""Render the completed fixed-wide-exit extension; no strategy selection or tuning."""
import csv
import json
from datetime import datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt

import extend_eg_backtest as extended

OUT = Path(__file__).resolve().parents[1] / "reports" / "eg_ten_months_20261019"


def read(name):
    return json.loads((OUT / name).read_text(encoding="utf-8"))


def write(name, data):
    (OUT / name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def money(v):
    return f"{v:+,.0f}"


def main():
    rows = read("monthly_attribution.json")
    results = read("all_results.json")
    trades = read("ten_months_baseline_slip1_adverse_first_trades.json")
    names = {
        "baseline": "旧宽退出，无额外择时",
        "atr_floor20_prev": "前一根 ATR≥20",
        "atr_band20_60_prev": "前一根 ATR 在20—60",
        "atr_rank_upper_half_prev": "前一根 ATR 位于近120根上半区",
        "atr_expanding5_prev": "前一根 ATR≥五根前 ATR",
        "atr_band20_60_current": "已收盘警告根 ATR 在20—60",
        "er025_prev": "前一根 ER20≥0.25",
        "atr_band20_60_prev_er025": "前一根 ATR20—60且 ER≥0.25",
    }

    def result(w, n="baseline", slip=1, mode="adverse_first"):
        return next(r for r in results if (r["window"], r["name"], r["slip"], r["mode"]) == (w, n, slip, mode))

    details = []
    for r in rows:
        ts = [t for t in trades if t["entry_dt"][:7] == r["month"]]
        details.append({"month": r["month"], "first_target_hits": sum(any(l["reason"] == "tp_limit" for l in t["legs"]) for t in ts),
                        "initial_stop_trades": sum(t["stop"] == -30 for t in ts),
                        "best_trade": max(t["net_pnl"] for t in ts),
                        "worst_trade": min(t["net_pnl"] for t in ts)})
    write("monthly_execution_details.json", details)

    # Inspect every simulated stop opening against the actual held-contract minute data.
    old_out = extended.OUT
    extended.OUT = OUT
    try:
        minutes = {sym: extended.load_minutes(sym) for sym in {t["contract"] for t in trades}}
    finally:
        extended.OUT = old_out
    index = {sym: {b["dt"]: i for i, b in enumerate(bs)} for sym, bs in minutes.items()}
    gaps = []
    for t in trades:
        for l in t["legs"]:
            if l["reason"] != "gap_stop":
                continue
            bs = minutes[t["contract"]]
            i = index[t["contract"]][l["dt"]]
            b, prev = bs[i], bs[i-1]
            assert l["price"] == b["o"] - t["direction"]
            gaps.append({"entry_dt": t["entry_dt"], "stop_dt_minute_end": l["dt"], "contract": t["contract"],
                         "direction": t["direction"], "entry": t["entry"], "stop_distance_at_exit": t["stop"],
                         "previous_minute": prev, "stop_minute": b, "execution": l,
                         "elapsed_minutes": (datetime.fromisoformat(b["dt"])-datetime.fromisoformat(prev["dt"])).total_seconds()/60,
                         "extra_loss_vs_stop_and_one_tick": max(0, (t["stop"]-1)*10*l["qty"]-l["pnl"]),
                         "whole_trade_net": t["net_pnl"]})
    write("gap_stop_audit.json", gaps)

    comments = {
        "2025-12": "仅后半月，6笔中5笔到达首目标；低ATR仍盈利，样本很少，不能推断低波动必然有利。",
        "2026-01": "12笔仅5笔到达首目标；平均盈利718元、平均亏损632元，41.7%胜率不足以覆盖损失。",
        "2026-02": "8笔仅3笔盈利，平均盈利625元低于平均亏损676元；开盘越过止损/保护价额外损失480元。",
        "2026-03": "31笔中22笔在首目标之前止损；空头净亏7532元，多头赚2630元。3月26日夜盘一笔跳空空单亏3632元，其中相对正常止损多损失3000元；即机械加回这3000元，月度交易组仍亏1902元，不是全由该笔跳空造成。",
        "2026-04": "19笔仅7笔盈利，但盈利单平均1121元、亏损单平均642元，较大的阶梯退出利润补偿低胜率，接近盈亏平衡。",
        "2026-05": "21笔仅6笔盈利、15笔未到首目标即止损；没有越价止损额外损失，亏损主要是信号后未能延续，不能归因于隔夜大跳空。",
        "2026-06": "27笔12胜15负；平均盈利833元、平均亏损659元，胜率44.4%与样本盈亏平衡胜率44.2%接近，净利很薄；越价额外损失540元。",
        "2026-07": "28笔12胜16负；盈利单平均951元略能覆盖损失，多头赚2754元、空头亏1950元，收益被方向差异抵消；越价额外损失520元。",
        "2026-08": "24笔13胜，胜率54.2%；多头贡献6668元、空头亏1426元。最大盈利单3078元，阶梯退出抓住较长延续行情。",
        "2026-09": "22笔13胜，胜率59.1%；多头赚1690元、空头赚4496元。胜率高于3月且两边都有正贡献，ATR高只是伴随现象，不能单独解释盈利。",
        "2026-10": "仅到16日，10笔4胜；胜率40%但盈利单平均1386元、亏损单632元，靠较大的单笔盈利覆盖亏损，最大盈利单2478元。",
    }
    report = ["# EG 15分钟旧宽退出：追加四个月与月度归因", "",
              "完成日期：2026-10-19。新增区间：2025-12-16 09:00—2026-04-15；十个月连续区间：2025-12-16 09:00—2026-10-16 23:00（北京时间）。12月和10月为非完整自然月。",
              "", "## 固定规则和可比口径", "",
              "采用旧宽退出：固定实际2手，10元/点/手；初始止损30点，第一手50点限价止盈，第二手先保护10点，再按30点阶梯跟踪。信号、警告K线过滤和下一根才触发的规则保持不变。常规初始风险600元，跳空可能远超此数。",
              "所有主表扣每手每边3元手续费；开仓和止损额外不利滑点1跳，止盈限价不额外滑点。共208笔手续费2496元。资金、保证金占用未指定，因此只报人民币盈亏，不计算收益率。没有采用半手风险归一化数值。",
              "15分钟收盘出信号，以分钟先后顺序回放成交；分钟内部仍假设价格路径。主表沿用 adverse_first 场景以便前后比较，该名字不代表收益下界。最大回撤以15分钟收盘持仓市值加已实现盈亏、已付费用计算，并非逐笔行情或分钟内最大回撤。",
              "", "## 数据与主力合约", "",
              "公开数据：[天勤 EDB 行情历史服务](https://doc.shinnytech.com/edb/latest/md_server.html)。新增1分钟原始数据、请求URL与SHA256保存在 sources_older.json；与既有数据逐时间戳合并的来源、重叠完全一致证明在 merged_inputs.json。主连共75405条1分钟数据，每根信号K线都由15条连续分钟组成，未发现不完整聚合组；这是聚合完整性检查，不等同交易所逐笔数据全量审计。",
              "主连只识别供应商主力成员，指标及成交均在具体合约上计算，避免拼接价差产生虚假交易。信号和开仓时都必须是供应商当时的主力，已有仓位继续持有原合约至退出。不是用某一远月合约倒推整个历史。",
              "本窗口供应商主力映射：12月16日日盘EG2601 → 当晚EG2605 → 4月17日夜盘EG2609 → 8月21日夜盘EG2610 → 9月28日夜盘EG2611。主力定义遵循[供应商说明](https://doc.shinnytech.com/tqsdk/latest/usage/mddatas.html)，没有独立重建全交易所所有合约的逐日成交量/持仓量排名。4月22日21:15沿用已披露的一处唯一HLC加相邻成员确认，未修改价格。",
              "", "## 追加结果", "", "| 独立运行区间 | 笔数 | 净盈亏（元） | 最大回撤（元） |", "|---|---:|---:|---:|"]
    labels = {"new_older_four": "新增四个月 12/16—4/15", "previous_four": "此前四个月 4/16—8/15", "recent_two": "最近两个月 8/16—10/16", "ten_months": "十个月连续运行"}
    for w, label in labels.items():
        r = result(w)
        report.append(f"| {label} | {r['trades']} | {money(r['net_pnl'])} | {r['max_drawdown']:,.0f} |")
    report += ["", "各独立窗口起始空仓、末尾按规则结算，不能直接相加当成连续回测。此次新增四个月-4530元，加既有六个月连续11274元，恰好等于十个月6744元；但三个独立窗口相加只有5622元，与连续值差1122元，仍来自8月边界持仓的不同处理。逐笔记录和连续权益已核对。",
               "新增四个月按入场月拆分：12月16—31日+3458元、1月-834元、2月-1506元、3月-4902元、4月1—15日-746元，共-4530元。下表4月为完整自然月，故数值不同。十个月最大回撤从3月11日11:15累计权益6501元，降至6月12日11:00的-11334元，幅度17835元。",
               "", "## 每月收益变化", "",
               "交易组盈亏：按**入场自然月**归属整笔最终净收益，便于把开仓时波动状态和后续结果配对；月末权益增量：按真实月末持仓市值分配跨月盈亏，不强制每月平仓。两列各自加总均为6744元。夜盘按北京时间自然月，不按交易日重新归月。ATR取每根15分钟K线前一根已完成的ATR20，表内为整个月主力K线的中位数，单位为价格点；不是只对成交或警告K线取中位数。",
               "", "| 月份 | 笔数 | 胜率 | 交易组净利 | 月末权益增量 | ATR中位数 | 首目标前止损 |", "|---|---:|---:|---:|---:|---:|---:|"]
    for r, d in zip(rows, details):
        report.append(f"| {r['month']} | {r['trades']} | {r['win_rate_pct']:.1f}% | {money(r['cohort_net_pnl'])} | {money(r['month_close_equity_change'])} | {r['atr_prev_median']:.1f} | {d['initial_stop_trades']} |")
    report += ["", *[f"- **{r['month']}**：{comments[r['month']]}" for r in rows], "",
               "这些是成交结果的会计分解和行情特征对照，不是因果识别。样本中ATR月中位数与交易组净利的Pearson相关仅0.056；与每笔净利相关0.005。仅11个自然月且包含两个残月，不能据此推断未来稳定关系。",
               "3月ATR中位数60.7、9月55.5，前者亏4902元、后者赚6186元；12月ATR仅14.8仍赚3458元。固定30点止损面对3月波动，相对ATR约0.49倍，9月约0.54倍，但两个月成败率明显不同，说明波动相对止损的尺度不是充分的择时条件。",
               "", "## 跳空核对", "",
               "3月26日空单EG2605：14:31分钟标签入场5046，止损价5076；日盘最后分钟收盘5058，夜盘21:00开盘5226，按1跳不利滑点在5227买平，两手毛亏3620元，加手续费后亏3632元。数据中的21:01为21:00—21:01分钟的结束标签。两侧都是EG2605，主力尚未换月，这个跳变不是合约拼接。正常止损加1跳预计亏632元，跳空额外损失3000元。原始分钟对照见 gap_stop_audit.json。",
               "所有 gap_stop 均已对照实际持仓合约分钟开盘价；此次13例的前后分钟间隔均大于1分钟，属于休市间隔后的越价（含日内休市，不全是隔夜），相对保护价加1跳总计额外损失6020元。该标记在代码中也可以涵盖连续交易中的分钟开盘越价。每月额外损失按交易入场月归类。损失加回仅为会计分解，不是可实现的消除跳空策略。",
               "", "## ATR能否择时：按当时可知数据实际重跑", "",
               "ATR不需要等整个月结束。警告K线t收盘后，ATR_t已经确定，可用于t+1开仓；前一根ATR_(t-1)更早已知。滞后是对变化反应慢，不等于使用未来数据。ATR衡量波动幅度，不包含方向或信号胜率信息：[Fidelity指标说明](https://www.fidelity.com/learning-center/trading-investing/technical-analysis/technical-indicator-guide/atr)。本策略延续原代码的20根TR简单均值，不是该说明中的Wilder递推平滑，跨平台比较要统一算法。",
               "运行前固定粗粒度假设，不扫描最优阈值：20点为固定30点止损的2/3，60点为2倍止损；相对分位用近120个已知ATR的50%分位；ER采用前20根净变动/总绝对变动，阈值0.25。所有版本仅附加开仓筛选，退出仍是旧30/50规则。测试覆盖160种窗口/费用/路径组合。",
               "", "| 附加开仓规则 | 新增四个月 | 此前四个月 | 最近两个月 | 十个月连续 | 十个月回撤 |", "|---|---:|---:|---:|---:|---:|"]
    for name, label in names.items():
        vals = [result(w, name) for w in labels]
        report.append(f"| {label} | " + " | ".join(money(r['net_pnl']) for r in vals) + f" | {vals[-1]['max_drawdown']:,.0f} |")
    report += ["", "不能用本表选一个全样本最好参数再称其已验证。前一根ATR分位筛选把新增四个月亏损缩至882元，但把此前四个月从赚756变为亏5830，十个月也变为亏202；仅做ATR扩张十个月亏15424元。ATR20—60能降低部分回撤，但十个月少赚1874元、新增四个月反而多亏998元，没有跨阶段一致改善。ER或ATR加ER同样未通过这次检验。",
               "基线成交按开仓前ATR分桶：ATR<20的28笔共赚3074元，ATR≥60的27笔共亏2464元。但这是既有成交的描述分组；真正过滤开仓会改变后续占仓和机会，因此必须以重新运行的表格评判，不能把分桶利润直接删减。",
               "", "## 成交路径与成本敏感性", "", "| 窗口 | 额外滑点 | 四种分钟内路径净利范围 | adverse_first净利 |", "|---|---:|---:|---:|"]
    for w in ["new_older_four", "ten_months"]:
        for slip in [0, 1, 2]:
            rs = [r for r in results if r["window"] == w and r["name"] == "baseline" and r["slip"] == slip]
            report.append(f"| {labels[w]} | {slip}跳 | {money(min(r['net_pnl'] for r in rs))} 至 {money(max(r['net_pnl'] for r in rs))} | {money(result(w, slip=slip)['net_pnl'])} |")
    report += ["", "四种路径仅是情景对照，未覆盖所有可能成交顺序，不是严格收益上下界。2跳时十个月四场景仅赚98—2498元；1跳为4644—6744元。成本变化也改变触发后路径、持仓和后续机会，净利差不能直接解释为同一组交易多付的滑点。",
               "", "## 研究结论与复现", "",
               "维持旧宽退出作为基线，不增加ATR择时。现有证据显示信号后延续/反转、盈利尾部和跳空共同改变月度表现；ATR可描述风险环境，但此次简单ATR择时没有显示稳定增益。若继续研究，可先冻结防隔夜跳空或风险缩放的具体规则，再检验新的未看过数据；不能因3月单笔损失就在这批数据上反复调到盈利。本次不修改实盘策略及原策略文档。",
               "新增四个月包含65笔、十个月208笔，仍是单品种有限历史；前六个月已有多轮研究，此次结果没有独立未触碰留出集。费用是假设值；分钟内路径、价差和排队未由逐笔成交验证，亦未模拟保证金不足/涨跌停无法成交。正净利不足以据此判断可实盘稳定获利。",
               "", "复现：`python scripts/analyze_eg_ten_months.py`，生成报告与图片：`python scripts/report_eg_ten_months.py`。原始请求清单用于复取，默认分析使用已保存且有哈希的原始快照。verification.json核验通过：原有区间逐场景复现、指标前缀一致性抽查、基线全部开仓主力匹配、费用/持仓/月度权益对账、原始重叠数据一致。",
               "", "![连续权益与月度比较](overview.png)", ""]
    (OUT / "REPORT.md").write_text("\n".join(report), encoding="utf-8")

    plt.rcParams.update({"font.sans-serif": ["Microsoft YaHei"], "axes.unicode_minus": False, "font.size": 10})
    fig, axs = plt.subplots(2, 1, figsize=(14, 9.5), gridspec_kw={"height_ratios": [1.15, 1]}, layout="constrained")
    for name, color in [("baseline", "#1d4ed8"), ("atr_band20_60_prev", "#d97706"), ("atr_rank_upper_half_prev", "#6b7280")]:
        with (OUT / f"ten_months_{name}_slip1_adverse_first_equity.csv").open(encoding="utf-8-sig") as f:
            es = list(csv.DictReader(f))
        axs[0].plot([datetime.fromisoformat(e["dt"]) for e in es], [float(e["equity"]) for e in es], label=names[name], color=color, lw=1.3)
    axs[0].axhline(0, color="#94a3b8", lw=.8)
    axs[0].set(title="旧宽退出追加历史：连续权益与 ATR 择时对照", ylabel="累计净盈亏（元）")
    axs[0].legend(loc="upper left", fontsize=9)
    axs[0].xaxis.set_major_locator(mdates.MonthLocator())
    axs[0].xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    axs[0].grid(alpha=.18)
    x = list(range(len(rows)))
    axs[1].bar([v-.19 for v in x], [r["cohort_net_pnl"] for r in rows], width=.36, color="#2563eb", label="入场月交易组净利")
    axs[1].bar([v+.19 for v in x], [r["month_close_equity_change"] for r in rows], width=.36, color="#93c5fd", label="月末权益增量")
    axr = axs[1].twinx()
    axr.plot(x, [r["atr_prev_median"] for r in rows], color="#c2410c", marker="o", label="开仓前可知 ATR20（月中位数）")
    axr.set_ylabel("ATR（价格点）", color="#c2410c")
    axr.set_ylim(0, 78)
    axs[1].set_xticks(x, [r["month"] + ("*" if i in [0, len(rows)-1] else "") for i, r in enumerate(rows)], rotation=20)
    axs[1].set(title="3 月波动更大却亏损，9 月盈利；ATR 不能单独解释收益", ylabel="月度盈亏（元）")
    axs[1].axhline(0, color="#94a3b8", lw=.8)
    axs[1].grid(axis="y", alpha=.18)
    h, l = axs[1].get_legend_handles_labels()
    hr, lr = axr.get_legend_handles_labels()
    axs[1].legend(h+hr, l+lr, loc="upper left", fontsize=8)
    fig.supxlabel("固定2手 · 手续费3元/手/边 · 额外滑点1跳 · 分钟内 adverse_first 情景\n* 12月和10月为残月；月度关系是描述统计；交易组按入场月归属整笔最终盈亏", fontsize=9)
    fig.savefig(OUT / "overview.png", dpi=150)
    plt.close(fig)
    print(OUT / "REPORT.md")
    print(OUT / "overview.png")


if __name__ == "__main__":
    main()
