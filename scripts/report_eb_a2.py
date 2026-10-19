"""Report the frozen EB transfer, including failed cost and period scenarios."""
import csv
import json
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt

import backtest_eb_a2 as test
import discover_eg_swing as engine

OUT = test.OUT
ROOT = test.ROOT


def read(name):
    return json.loads((OUT/name).read_text(encoding="utf-8"))


def load(folder,sym):
    before=engine.extended.OUT;engine.extended.OUT=folder
    try:return engine.extended.load_minutes(sym)
    finally:engine.extended.OUT=before


def market_comparison():
    results={}
    for product,multiplier in [("eb",5),("eg",10)]:
        if product=="eb":
            folder=OUT
            sources=read("sources.json")
            syms=[s["name"] for s in sources if s.get("period")==60 and s["name"]!="main"]
            fallbacks={x["dt"]:x["contract"] for x in read("data_verification.json")["fallbacks"]}
        else:
            folder=ROOT/"reports"/"eg_ten_months_20261019"
            syms=["eg2601","eg2605","eg2609","eg2610","eg2611"]
            fallbacks=json.loads((folder/"main_contract_mapping.json").read_text(encoding="utf-8"))
        main=engine.aggregate(load(folder,"main"),15)
        bs={s:engine.aggregate(load(folder,s),15) for s in syms}
        for s,v in bs.items():engine.enrich(v)
        ix={s:{b["dt"]:b for b in v} for s,v in bs.items()}
        daily=defaultdict(list);atrs=[]
        for b in main:
            if not test.START<=b["dt"]<=test.END:continue
            matches=[s for s,idx in ix.items() if b["dt"] in idx and all(idx[b["dt"]][k]==b[k] for k in ["c","p"])]
            sym=matches[0] if len(matches)==1 else fallbacks[b["dt"]]
            own=ix[sym][b["dt"]];atrs.append(own["atr"])
            if own["dt"][11:13]<"21":daily[(own["dt"][:10],sym)].extend(own["minutes"])
        point=[];cash=[];pct=[]
        for key,ms in daily.items():
            if len(ms)!=225:continue
            r=max(b["h"] for b in ms)-min(b["l"] for b in ms)
            point.append(r);cash.append(r*multiplier);pct.append(r/ms[0]["o"]*100)
        results[product]={"multiplier":multiplier,"full_day_sessions":len(point),
            "median_day_range_points":statistics.median(point),"median_day_range_one_lot_cny":statistics.median(cash),
            "median_day_range_pct":statistics.median(pct),"median_atr20_points":statistics.median(atrs),
            "median_atr20_one_lot_cny":statistics.median(atrs)*multiplier,
            "definition":"complete225minute09--15day sessions, concrete member prices;night excluded;no splicejump"}
    test.save("market_comparison.json",results)
    return results


def curve(path):
    with path.open(encoding="utf-8-sig",newline="") as f:rows=list(csv.DictReader(f))
    return [datetime.fromisoformat(r["dt"]) for r in rows],[float(r["equity"]) for r in rows]


def main():
    rows=read("all_results.json");monthly=read("monthly_equity_changes.json");summary=read("summary.json")
    market=market_comparison()
    def s(profile,window="continuous",slip=1,fee=3):
        rs=[r for r in rows if r["profile"]==profile and r["window"]==window and r["slip"]==slip and r["fee_per_side"]==fee]
        assert len(rs)==2
        r=next(x for x in rs if x["mode"]=="ohlc").copy()
        r["net_pnl"]=min(x["net_pnl"] for x in rs);r["minute_close_max_drawdown"]=max(x["minute_close_max_drawdown"] for x in rs)
        return r
    def m(x):return f"{x:+,.0f}"
    labels={"risk_equivalent":"EB A2：按金额风险换算", "literal_points":"EB A2：直接保留EG点数", "base_risk_equivalent":"EB A：不加量比过滤"}
    text=["# A2迁移苯乙烯EB：固定规则十个月回测","", "日期：2026-10-19。用户在EG更早历史受数据权限限制后，要求换EB0检验。本轮已完成与此前EG相同的2025-12-16—2026-10-16十个月，普通隔夜允许、周末/长假前清仓。EG缺失四个月没有被补齐；[已完成的EG部分复核](../eg_a2_older_four_20261019/REPORT.md)单独保留。", "", "## 结果与判断", "", "**A2在EB上未显示稳健盈利。** 按相同金额风险迁移十个月1跳亏2,059元，2跳亏3,923元；直接沿用EG点数仅1跳微利846元，2跳转亏。最近两个月盈利，不能代表此前八个月；没有按这些结果重新调阈值或选出新的‘最优EB策略’。", "", "| 同一十个月、原生1手 | 1跳净利 | 1跳分钟权益回撤 | 1跳笔数 | 2跳净利 | 3跳净利 |", "|---|---:|---:|---:|---:|---:|", "| EG A2，先前冻结结果 | +12,890 | 2,010 | 135 | +10,190 | +8,304 |"]
    for profile in ["risk_equivalent","literal_points","base_risk_equivalent"]:
        r=s(profile);text.append(f"| {labels[profile]} | {m(r['net_pnl'])} | {r['minute_close_max_drawdown']:,.0f} | {r['trades']} | {m(s(profile,slip=2)['net_pnl'])} | {m(s(profile,slip=3)['net_pnl'])} |")
    text += ["", "主表均按单边3元手续费作相同成本口径的比较，并非逐日真实账户费用。每档滑点及每条分钟路径独立回放，不能把交易固定后简单扣差额。回撤是1分钟收盘持仓权益，含浮盈亏，仍非逐笔或分钟内最坏风险。图是15分钟采样，用于展示。", "", "## 迁移规则如何固定", "", "A2信号完全沿用：具体合约15分钟收盘突破此前20根通道，EMA12/36同向，同时段成交量/此前最多10次相同钟点量中位数≥1.5，至少3次历史。当前完整K线收盘后才生成信号，下一根开盘入场；ATR20为简单TR均值，固定2R限价目标、120个交易中分钟退出、不加仓，4万元原生1手。", "", "EB每手5吨、最小变动1元/吨，即一跳5元；EG每手10吨，一跳10元。合约依据见[大商所标准合约附件，期货公司官网保存](https://www.htfc.com/wz_upload/png_upload/20240207/17072856162152a5658.pdf)。为避免同样点数对应不同金额风险，事先声明以下两种迁移口径，并完整披露，不能事后只展示盈利版本：", "", "- **主口径，金额风险换算**：EG的最小12点/最大46点等于120/460元；EB换为最小24点/最大92点，仍R=max(下限,ceil(ATR20))，超过上限跳过。最小开盘跳变容忍从2点换为4点，其金额都是20元；0.25ATR限制不变。", "- **直接点数对照**：仍12—46点和2点最小跳变容忍，但EB每点只有5元，计划止损金额及允许的波动集合更小。这是机械迁移敏感性，不是与EG完全同风险。", "- 两者均最多同时1手，单笔计划风险500元以内，自然日最多3笔、日内损失800元停开新仓；研究保证金25%名义金额，≤2万元及当前权益40%。不能将保证金假设当成期货公司实收。", "", "量比过滤相对EB不加量比基线有改善（1跳−5,713元到−2,059元），但仍没有实现正收益。直接点数对照有143次风险距离拒绝，保留74笔；金额风险口径拒绝25次，保留154笔。两者交易集合不同，不应简单归因于止损更宽或更窄。", "", "## 分段表现", "", "每个独立段起始空仓、终点结算；十个月连续另算。", "", "| EB口径与期间 | 1跳净利 | 2跳净利 | 3跳净利 | 1跳分钟回撤 |", "|---|---:|---:|---:|---:|"]
    for profile in ["risk_equivalent","literal_points"]:
        for window,label in [("first_four","2025-12-16—2026-04-15"),("middle_four","2026-04-16—08-15"),("recent_two","2026-08-16—10-16")]:
            r=s(profile,window);text.append(f"| {labels[profile]}，{label} | {m(r['net_pnl'])} | {m(s(profile,window,2)['net_pnl'])} | {m(s(profile,window,3)['net_pnl'])} | {r['minute_close_max_drawdown']:,.0f} |")
    text += ["", "金额风险口径前八个月独立段合计亏5,276元，后两个月赚3,217元；这里只用于说明时间分布，连续结果仍以单独回放为准。这次分段合计恰与连续相同，不代表所有策略和窗口都能直接相加。", "", "## EB的日内波动是否更适合", "", "同属化工不代表行为一致。EB处于纯苯/乙烯和PS、EPS、ABS产业链；EG主要服务聚酯，并存在煤制与油制路线。相同原油冲击之外，供需、库存和装置变化各有影响，不能由名称相近推导策略可迁移。产业结构仅作背景，未作为本轮信号输入。", "", "比较同一期间完整09:00—15:00日盘高低差，不包含夜盘、不在换月拼接差上计算。", "", "| 市场波动中位数 | EB | EG |", "|---|---:|---:|"]
    for key,label,fmt in [("full_day_sessions","完整日盘样本",",.0f"),("median_day_range_points","日盘高低差，报价点",",.1f"),("median_day_range_one_lot_cny","日盘高低差×乘数，元/手",",.1f"),("median_day_range_pct","日盘高低差/开盘，%",".2f"),("median_atr20_one_lot_cny","15分钟ATR20×乘数，元/手",",.2f")]:
        text.append(f"| {label} | {market['eb'][key]:{fmt}} | {market['eg'][key]:{fmt}} |")
    text += ["", "EB报价点数更大，但乘数只有EG的一半；本样本按每手金额或百分比衡量，并未比EG更大。这些高低差是完整行情走完后的描述，不是可提前知道或可全额捕获的盈利。", "", "EB主口径154笔中86笔普通止损、3笔跳空止损，只有39笔达到2R；EG A2的135笔中50笔达到2R、59笔普通或跳空止损。EB胜率38.3%、平均盈利单约374元、平均亏损单约254元；EG分别46.7%、约480元和241元。失败来自这套入场/退出组合下的胜率及盈亏分布，不能因为波动活跃就推定突破会延续。", "", "## 月度与费用压力", "", "自然月权益增量来自连续回放，含两端残月，没有月末人为重置。", "", "| 自然月 | EB金额风险口径 | EB直接点数 |", "|---|---:|---:|"]
    months=[r["month"] for r in monthly if r["profile"]=="risk_equivalent"]
    for month in months:
        vals=[next(r["net_equity_change"] for r in monthly if r["profile"]==profile and r["month"]==month) for profile in ["risk_equivalent","literal_points"]]
        text.append(f"| {month} | {m(vals[0])} | {m(vals[1])} |")
    text += ["", "EB交易所费率在2026年7月9日夜盘起由3元/边降为1元/边，见[国海良时通知](https://www.ghlsqh.com.cn/company/news/show-28503.html)及[华泰期货通知](https://webtrade.htfc.com/main/a/20260709/80180995.shtml)。本轮为与此前EG比较，主表统一3元/边，同时测试较高公司收费；未声称这就是用户账户的真实费率，也未追溯逐日保证金。", "", "| EB金额风险口径，十个月 | 1跳净利 | 2跳净利 | 3跳净利 |", "|---|---:|---:|---:|"]
    for fee in [3,6,12]:text.append(f"| 假设每边{fee}元 | {m(s('risk_equivalent',slip=1,fee=fee)['net_pnl'])} | {m(s('risk_equivalent',slip=2,fee=fee)['net_pnl'])} | {m(s('risk_equivalent',slip=3,fee=fee)['net_pnl'])} |")
    text += ["", "主口径1跳最高估算保证金13,326.25元，低于2万元；但最长10笔连亏、最大单笔实际亏696元。7月20日14:46开多EB2608，计划止损金额306元，21:01跳空触发后实际亏696元，说明15:00—21:00休市也会产生跳空。3跳主口径分钟回撤8,521元，12元/边加3跳压力下回撤10,635元，超过用户1万元目标。", "", "## 数据与核验", "", "下载[天勤EDB公开分钟数据](https://doc.shinnytech.com/edb/latest/md_server.html)，主连74,?分钟的实际条数、所有具体合约、原始URL、时间和SHA256见[sources.json](sources.json)。2025-11-17开始留出热身；每日主连与14个具体月份日线先确定下载覆盖，实际回放每根15分钟通过具体合约close+OI匹配成员，没有用日线预测主力或制造成交。EB主力按月切换，实际成交始终使用具体合约，持仓换月不强行按主连平移。EDB成员定义不保证等于新浪EB0。", "", "有两根主连和具体合约的末分钟快照不同：2026-04-22 21:15、06-29 14:15。两根均有14/15个原始分钟收盘价与OI精确匹配唯一具体合约，其他备选为0/15，相邻成员确认；保留具体合约原始OHLC和成交量，未改价。全部日历、成员切换和两处识别依据见[data_verification.json](data_verification.json)。", "", "固定三种配置×四个期间×三档滑点×两条路径=72场景，主口径额外两档费用×三档滑点×两条路径=12，共84场景。三种配置为金额迁移、直接点数敏感性和去掉量比的基线，没有因观察EB收益再调信号或选择参数。分钟内路径OHLC/OLHC为情景近似，未模拟限价排队、触价不成交、动态价差和涨跌停封板。周末长假清仓按最后收盘加滑点近似，仍需真实成交验证。", "", "共3,000个独立价格路径案例、4个不同乘数/费用现金示例检查通过；修改研究引擎默认参数后，EG旧十个月与此前短复核的24个参照场景逐项完全复现。费用、原生1手、风险、保证金、日历空仓、因子前缀因果和权益对账通过，见[verification.json](verification.json)。", "", "```text", "python scripts/backtest_eb_a2.py --fetch", "python scripts/report_eb_a2.py", "```", "", "首次下载主连及每日成员范围探测的请求保存在sources.json与daily_member_probe.json；已存在快照可直接不加--fetch重跑。生产交易代码未修改。当前结果支持保留EG A2作研究候选，不支持把EB A2作为已验证的收益替代。", "", "![同一十个月累计权益与EB月度分布](overview.png)", ""]
    minute_count=next(x["rows"] for x in read("sources.json") if x["name"]=="main")
    text=[x.replace("74,?",f"{minute_count:,}") for x in text]
    text=[x.replace("EG主要服务聚酯，并存在煤制与油制路线。", "")
          .replace("产业结构仅作背景，未作为本轮信号输入。", "产业结构仅作背景，未作为本轮信号输入；参考[交易所EB交易指南，深圳期货业协会保存](https://www.szfachina.org/media/upload/aaa/%E8%8B%AF%E4%B9%99%E7%83%AF%E6%9C%9F%E8%B4%A7%E4%BA%A4%E6%98%93%E6%8C%87%E5%8D%97.pdf)。") for x in text]
    (OUT/"REPORT.md").write_text("\n".join(text),encoding="utf-8")
    plt.rcParams.update({"font.sans-serif":["Microsoft YaHei","DejaVu Sans"],"axes.unicode_minus":False,"font.size":10})
    fig,axes=plt.subplots(2,1,figsize=(11.5,8),constrained_layout=True)
    series=[(ROOT/"reports"/"eg_factor_research_20261019"/"continuous_season_volume_2_equity.csv","EG A2（先前冻结）", "#059669"),
        (OUT/"continuous_risk_equivalent_slip1_equity.csv","EB A2（金额风险换算）","#dc2626"),
        (OUT/"continuous_literal_points_slip1_equity.csv","EB A2（保留EG点数）","#2563eb")]
    for path,label,color in series:
        dates,vals=curve(path);axes[0].plot(dates,vals,label=f"{label}  {m(vals[-1])}元",color=color,lw=1.5)
    axes[0].set_title("同一十个月累计净收益｜2025-12-16—2026-10-16",loc="left",fontweight="bold")
    axes[0].xaxis.set_major_locator(mdates.MonthLocator());axes[0].xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    axes[0].legend(frameon=False,loc="upper left");axes[0].set_ylabel("累计净收益（元）")
    vals=[next(r["net_equity_change"] for r in monthly if r["profile"]=="risk_equivalent" and r["month"]==month) for month in months]
    axes[1].bar(range(len(vals)),vals,color=["#059669" if v>=0 else "#dc2626" for v in vals])
    axes[1].set_xticks(range(len(vals)),[m[2:] for m in months]);axes[1].set_ylabel("月度净权益变化（元）")
    axes[1].set_title("EB金额风险口径：前段弱，最近两个月改善（两端含残月）",loc="left",fontweight="bold")
    for ax in axes:ax.axhline(0,lw=.8,color="#94a3b8");ax.grid(axis="y",alpha=.15)
    fig.suptitle("A2跨品种迁移｜4万元、实际1手、每边3元费用、1跳滑点\n普通隔夜允许，周末/长假清仓；未重新优化信号",fontsize=11)
    fig.savefig(OUT/"overview.png",dpi=150,facecolor="white");plt.close(fig)
    print("Saved EB REPORT.md, overview.png, market_comparison.json")


if __name__=="__main__":main()
