"""Render the saved frozen baseline results, without reselecting parameters."""
import csv
import json
import os
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/"reports/sr_donchian_atr_20261021"
os.environ.setdefault("MPLCONFIGDIR",str(OUT/"matplotlib-cache"))
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

plt.rcParams["font.sans-serif"]=["Microsoft YaHei","SimHei","DejaVu Sans"]
plt.rcParams["axes.unicode_minus"]=False


def read_csv(name):
    with (OUT/name).open(encoding="utf-8-sig",newline="") as f:return list(csv.DictReader(f))


def fmt(value,decimal=2):
    return "—" if value is None else f"{value:,.{decimal}f}"


def main():
    ss=json.loads((OUT/"summary.json").read_text(encoding="utf-8"))
    sensitivity=json.loads((OUT/"sensitivity.json").read_text(encoding="utf-8"))
    verification=json.loads((OUT/"data_verification.json").read_text(encoding="utf-8"))
    fig,axes=plt.subplots(2,2,figsize=(14,9),layout="constrained")
    fig.suptitle("白糖 SR2701｜唐奇安突破 + ATR 仓位管理\n2026-09-21—2026-10-20（交易日）；初始资金 100,000 元",fontsize=15)
    colors={"daily":"#5974a5","15m":"#0c8d7c"}
    for s,name in zip(ss,["daily","15m"]):
        curve=read_csv(name+"_equity.csv")
        axes[0,0].plot(range(len(curve)),[float(r["net_pnl"]) for r in curve],color=colors[name],lw=1.5,label="日线经典参数" if name=="daily" else "15 分钟对照")
    axes[0,0].axhline(0,color="#777",lw=.7)
    axes[0,0].set(title="累计净盈亏（含持仓浮盈亏）",xlabel="已完成的交易分钟（跳过休市）",ylabel="元")
    axes[0,0].legend(frameon=False)
    ts=read_csv("15m_trades.csv");p=[float(t["net_pnl"]) for t in ts]
    axes[0,1].bar(range(1,len(p)+1),p,color=["#0c8d7c" if x>0 else "#c66360" for x in p])
    axes[0,1].axhline(0,color="#777",lw=.7)
    axes[0,1].set(title="15 分钟逐笔净盈亏",xlabel="交易编号（开仓顺序）",ylabel="元")
    axes[0,1].set_xticks(range(1,len(p)+1))
    w=sorted([x for x in p if x>0],reverse=True)
    shares=[100*sum(w[:i])/sum(w) for i in range(1,len(w)+1)]
    axes[1,0].bar(range(1,len(w)+1),shares,color="#0c8d7c")
    axes[1,0].set(title="盈利集中度：最大几笔占全部盈利单净利润",xlabel="累计纳入的最大盈利单数",ylabel="占比 %",ylim=(0,110))
    for i,y in enumerate(shares,1):axes[1,0].text(i,y+2,f"{y:.1f}%",ha="center",fontsize=9)
    days=read_csv("15m_days.csv");daily=[float(d["net_change"]) for d in days]
    axes[1,1].bar(range(len(days)),daily,color=["#0c8d7c" if x>0 else "#c66360" for x in daily])
    axes[1,1].axhline(0,color="#777",lw=.7)
    axes[1,1].set(title="15 分钟每日权益变化（按交易日，含浮盈亏）",ylabel="元")
    axes[1,1].set_xticks(range(len(days)),[d["trading_date"][5:] for d in days],rotation=60,fontsize=8)
    for ax in axes.flat:
        ax.grid(axis="y",alpha=.2);ax.spines[["top","right"]].set_visible(False)
    fig.savefig(OUT/"performance.png",dpi=160,facecolor="white")
    fig.savefig(OUT/"performance.svg",facecolor="white")
    plt.close(fig)

    a,b=ss;na=a["natural_closed"];nb=b["natural_closed"]
    lines=["# 白糖唐奇安突破＋ATR：近一个月复现报告", "",
        "日线主测试亏损 1,096 元，仅有 1 笔交易。15 分钟独立对照盈利 1,742 元，但依赖少数盈利单，成本敏感，尚不足以支持实盘。", "",
        "## 固定口径", "",
        "- 品种：郑商所白糖 SR2701，数据源代码 CZCE.SR701。测试前按已有完整行情及近期主力身份选定，没有按本策略收益挑选。",
        "- 区间：2026-09-21 至 2026-10-20，共 16 个交易日、5,280 根完整分钟线。未使用 10 月 21 日未完成交易日。交易日期口径包含 9 月 18 日夜盘，结束于 10 月 20 日 15:00；不含 10 月 20 日夜盘。",
        f"- 指标预热：日线从 {verification['warmup_start'][:10]} 开始，测试前 {verification['daily_warmup']} 根日线；预热仅计算指标，9 月 21 日交易日起空仓启动。",
        "- 20 根 K 线最高价上方 1 跳做多、最低价下方 1 跳做空；10 根反向通道退出；Wilder ATR20；入场时固定 2 ATR 止损。价格均使用当前分钟之前已完成信号 K 线计算。",
        "- 初始资金 100,000 元。单位手数=floor(权益×1%÷(ATR×10))。这里的 1% 是每 N 的资金波动预算，2N 止损对应约 2% 的预算，取整及保证金上限会降低实际初始风险。",
        "- 假设保证金率 12%、初始保证金使用不超过权益 50%；它们是建模假设，不是历史账户实际保证金率。15 分钟版本有 16/19 笔受到该上限限制，实际为 7—8 手。日线为 1 手。",
        "- 沿用前文建议的不加仓简化版：没有海龟的 0.5N 加仓、上次盈利跳过信号、55 日保障突破和组合头寸限制，不能称为完整原版海龟 S1。",
        "- 日线主测试与 15 分钟对照共用 20/10/20/2 参数，均未优化。15 分钟是独立短波段实验，允许隔夜、周末和节假日持仓，不是纯日内策略。",
        "- 每边每手假设手续费 3 元、逆向滑点 1 跳（白糖每跳每手 10 元）。3 元为假设的客户总费用，不能当作交易所实时费率；实际账户需替换。",
        "- 触价使用分钟 OHLC 近似；跳空按更差的实际开盘价成交，再加滑点。同一分钟内允许开仓后止损，但平仓后下个分钟才可重入。零成交量分钟不成交。", "",
        "## 结果（均扣除上述成本）", "",
        "| 指标 | 日线主测试 | 15 分钟对照 |", "|---|---:|---:|"]
    rows=[("完成交易",na["trades"],nb["trades"]),("盈利／亏损笔数",f"{na['wins']} / {na['losses']}",f"{nb['wins']} / {nb['losses']}"),
          ("胜率",fmt(na["win_rate_pct"])+"%",fmt(nb["win_rate_pct"])+"%"),
          ("未扣手续费及滑点毛盈亏",fmt(na["gross_pnl"]),fmt(nb["gross_pnl"])),
          ("滑点成本",fmt(na["slippage_cost"]),fmt(nb["slippage_cost"])),("手续费",fmt(na["fees"]),fmt(nb["fees"])),
          ("净盈亏",fmt(na["net_pnl"]),fmt(nb["net_pnl"])),("本金收益率",fmt(a["return_pct"],3)+"%",fmt(b["return_pct"],3)+"%"),
          ("分钟收盘权益最大回撤",fmt(a["minute_close_max_drawdown"]),fmt(b["minute_close_max_drawdown"])),
          ("最大回撤比例",fmt(a["minute_close_max_drawdown_pct"])+"%",fmt(b["minute_close_max_drawdown_pct"])+"%"),
          ("平均盈利单",fmt(na["mean_win"]),fmt(nb["mean_win"])),("平均亏损单",fmt(na["mean_loss"]),fmt(nb["mean_loss"])),
          ("平均盈利／平均亏损绝对值",fmt(na["payoff_ratio"]),fmt(nb["payoff_ratio"])),
          ("盈利因子（全部盈利／全部亏损绝对值）",fmt(na["profit_factor"]),fmt(nb["profit_factor"])),
          ("单笔中位数",fmt(na["median_trade"]),fmt(nb["median_trade"])),
          ("最好一笔净盈亏",fmt(na["best_trade"]),fmt(nb["best_trade"])),("最差一笔净盈亏",fmt(na["worst_trade"]),fmt(nb["worst_trade"])),
          ("最大连续亏损笔数",na["max_consecutive_losses"],nb["max_consecutive_losses"])]
    lines += [f"| {label} | {x} | {y} |" for label,x,y in rows]
    lines += ["", "金额单位均为人民币元。两组期末都没有未平仓持仓，收益全部来自策略自然退出，没有通过强制平仓改变胜率。回撤使用每分钟收盘权益，不是仅平仓收益回撤，也不是逐 Tick 极值。", "",
        "日线唯一交易：10 月 9 日夜盘 21:04 做多 1 手（所属交易日 10 月 12 日），成交 5512；10 月 15 日 09:43 止损，成交 5403。成交价差亏损 1,090 元、另扣手续费 6 元，合计 -1,096 元。胜率 0% 只是 1 笔样本的描述。", "",
        f"![收益与集中度]({(OUT/'performance.png').as_posix()})", "", "## 盈亏是否集中", "",
        f"15 分钟共 6 笔盈利、13 笔亏损。全部盈利单净利润合计 {fmt(nb['positive_trade_total'])} 元，全部亏损单合计 -{fmt(nb['negative_trade_total'])} 元。", "",
        f"- 最大盈利单 {fmt(nb['best_trade'])} 元，占全部盈利单利润 {fmt(nb['top1_winner_share_pct'])}%。",
        f"- 最大三笔盈利合计 19,124 元，占全部盈利单利润 {fmt(nb['top3_winner_share_pct'])}%。分母是盈利单利润总额，避免用较小的净利润误算集中度。",
        f"- 直接从已实现流水中剔除最大盈利单后为 {fmt(nb['net_without_best_winner'])} 元；剔除最大三笔后为 {fmt(nb['net_without_top3_winners'])} 元。这只是流水贡献分析，没有重跑剔除交易后的权益与手数路径。",
        "- 最大盈利交易：9 月 30 日 11:28 做多 7 手，10 月 8 日 14:44 退出，跨国庆休市；它不能归为日内收益。另两笔大赢家为 10 月 14—15 日空单 +6,118 元、9 月 21—24 日多单 +3,808 元。",
        "- 大部分交易是亏损，单笔净盈亏中位数 -1,162 元。盈利依赖少数趋势行情，集中度明显。", "",
        "| 15 分钟单笔净盈亏区间 | 笔数 | 合计金额 |", "|---|---:|---:|"]
    labels=["低于 -2,000","[-2,000, -1,000)","[-1,000, 0)","(0, 1,000)","[1,000, 2,000)","至少 2,000"]
    lines += [f"| {label} | {r['count']} | {fmt(r['net_pnl'])} |" for label,r in zip(labels,nb["distribution"])]
    lines += ["", "## 成本与分钟内成交顺序敏感性", "",
        "| 场景（每边每手） | 日线净盈亏 | 15 分钟净盈亏 |", "|---|---:|---:|"]
    labels={"slip0_fee3":"0 跳滑点＋3 元手续费","slip2_fee3":"2 跳滑点＋3 元手续费", "slip3_fee3":"3 跳滑点＋3 元手续费", "slip1_fee6":"1 跳滑点＋6 元手续费", "slip1_fee9":"1 跳滑点＋9 元手续费"}
    lines += [f"| 基准：1 跳滑点＋3 元手续费 | {fmt(na['net_pnl'])} | {fmt(nb['net_pnl'])} |"]
    for scenario,label in labels.items():
        values={s["frame"]:s["net_pnl"] for s in sensitivity if s["scenario"]==scenario}
        lines.append(f"| {label} | {fmt(values['daily'])} | {fmt(values['15m'])} |")
    lines += ["", "这些是完整重放：滑点改变入场后的保护止损、费用改变权益与整数手数，因此不是简单从基准利润上再减固定成本。", "",
        "15 分钟有 1 根分钟 K 线存在路径歧义：9 月 28 日 09:00（中秋后开盘），无法仅凭 OHLC 确认先突破还是先止损。基准在发生歧义时采用当分钟收盘权益较低的路径；另按全程 O-H-L-C 与 O-L-H-C 两种路径重放，月度净盈亏分别为 +3,588 元（17 笔）和 +1,742 元（19 笔）。该区间是路径敏感性，不是实际盈亏的严格上下界。日线无该歧义。", "",
        "## 数据与验证", "",
        "- 读取项目已有的天勤 EDB 原始快照，不连接交易账户。sources.json 保留供应商 URL、抓取时间、文件 SHA256 及本地路径；全部校验通过。",
        "- 逐交易日确认分钟无缺失、无重复，并将分钟聚合 OHLC/收盘持仓量与实际合约日线核对。近期各日的日线 HLC/持仓量与供应商主连一致。",
        "- 在 SR611、SR701、SR705 三个候选中，逐日用前一完成交易日的成交量及持仓量核验 SR701 为最大者。这个选择范围及事后主连核验不能证明它在所有历史时点都是全市场唯一主力；信号与金额只使用真实 SR701，未拼接主连价格。",
        "- 日线指标只依赖当前交易日前已完成日线；15 分钟只依赖前一完成 15 分钟线。前缀截断权益对照及指标前缀因果检查通过。资金、逐笔毛利、费用、滑点对账通过。",
        "- 覆盖 ATR 递推、通道滞后、仓位风险、保证金上限、双向滑点、跳空止损、同分钟开平仓、路径歧义、零量不成交和交易日映射的单元测试通过。",
        "- 未模拟盘口排队、限价板无法成交、实际保证金逐日调整；日线结算采用收盘价盯市近似，没有使用结算价还原每日资金划转。", "",
        "## 判断", "",
        "日线只有一笔，不具备统计判断力。15 分钟有正收益，但净盈利远小于期间回撤，最大三笔盈利高度集中，而且每边多 1 跳滑点即转亏；当前只能作为继续做跨年份、跨品种验证的基准，不能据此认定策略已具备稳定优势。", "",
        "## 复现", "", "```powershell", "python -m unittest discover -s tests -p test_sr_donchian_atr.py", "python scripts/backtest_sr_donchian_atr.py", "python scripts/report_sr_donchian_atr.py", "```", "",
        "本目录同时保存冻结计划 analysis_plan.json、汇总 summary.json、敏感性 sensitivity.json、逐笔 *_trades.csv、分钟权益 *_equity.csv、每日权益 *_days.csv、指标审计 *_signals.csv 与路径歧义 *_ambiguity.csv。", ""]
    (OUT/"report.md").write_text("\n".join(lines),encoding="utf-8")
    print(str(OUT/"report.md"))
    print(str(OUT/"performance.png"))


if __name__=="__main__":main()
