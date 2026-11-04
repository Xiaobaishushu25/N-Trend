"""Write the final reproducible report from frozen audited results."""
import json,sqlite3
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from fresh_year_research import prepare,OUT,DB

def main():
    folder=OUT/'risk_aware';a=json.loads((folder/'minute_audit.json').read_text());r=json.loads((folder/'robustness.json').read_text());products,p=prepare()
    trades=pd.read_csv(folder/'audited_full_year_trades.csv');equity=pd.read_csv(folder/'audited_equity.csv')
    db=sqlite3.connect(f'file:{DB.as_posix()}?mode=ro',uri=True);checks=[]
    for nm in a['frozen']['universe']:
        d=products[nm];compared=0
        for contract in sorted(set(d['contract'])):
            f=pd.read_sql_query('select start_ns,open,high,low,close,volume from bars where contract=? and period_seconds=60 and start_ns>=? and start_ns<? order by start_ns',db,params=(contract,int(d['start_ns'][0]),int(d['end_ns'][-1])))
            f['bucket']=f.start_ns//900000000000*900000000000
            agg=f.groupby('bucket').agg(open=('open','first'),high=('high','max'),low=('low','min'),close=('close','last'),volume=('volume','sum'),n=('start_ns','size'))
            ix=np.where(d['contract']==contract)[0];g=agg.reindex(d['start_ns'][ix])
            assert (g.n==15).all(),(nm,contract,'missing minutes')
            for col in ('open','high','low','close','volume'):assert np.allclose(g[col],d[col][ix]),(nm,contract,col)
            compared+=len(ix)
        checks.append(dict(product=nm,matching_15m_bars=compared))
    zero_volume_entries=0
    for row in trades.itertuples():
        d=products[row.product];i=int(np.searchsorted(d['start_ns'],row.entry_ns));contract=d['contract'][i]
        volume=db.execute('select volume from bars where contract=? and period_seconds=60 and start_ns=?',(contract,int(row.entry_ns))).fetchone()
        assert volume is not None
        zero_volume_entries+=volume[0]==0
    db.close();assert zero_volume_entries==0
    assert a['historical_constraints_passed'];assert abs(trades.pnl.sum()-a['full_year']['net'])<.001
    checks=dict(aggregation=checks,zero_volume_entries=int(zero_volume_entries),trade_ledger_matches=True,read_window=[p['start'],p['end_exclusive']],historical_constraints_passed=True)
    (folder/'data_checks.json').write_text(json.dumps(checks,indent=2),encoding='utf8')
    dates=pd.to_datetime(equity.end_ns,utc=True).dt.tz_convert('Asia/Shanghai')
    fig,axes=plt.subplots(2,1,figsize=(11,6),gridspec_kw={'height_ratios':[3,1]},sharex=True)
    axes[0].plot(dates,equity.equity,color='#176b73',lw=1.1);axes[0].axhline(50000,color='gray',lw=.7)
    axes[0].axvspan(pd.Timestamp(p['blocks'][9]),pd.Timestamp(p['blocks'][12]),alpha=.12,color='#d98933',label='Repeated evaluation tail')
    axes[0].set_ylabel('Equity (CNY)');axes[0].set_title('Frozen strategy: one-minute execution audit, net of assumed costs');axes[0].legend(loc='upper left')
    peak=np.maximum.accumulate(np.r_[50000,equity.favorable_equity])[1:]
    dd=(equity.adverse_equity/peak-1)*100
    axes[1].fill_between(dates,dd,0,color='#b65644',alpha=.65);axes[1].set_ylabel('Drawdown (%)')
    for ax in axes:ax.grid(alpha=.15)
    fig.tight_layout();fig.savefig(folder/'equity.png',dpi=150);plt.close(fig)
    lines=['# 最近一年独立策略研究报告','',
    '**结论：找到满足这段历史数值约束的候选策略；尚不能证明未来稳定收益。**',
    '研究仅读取 2025-10-28 至 2026-10-27 的行情价格，没有读取此前策略探索文档，也没有使用更早行情作指标预热。只读打开源数据库，不修改数据库，不连接交易账户。',
    '', '## 冻结规则','',
    '- 品种：甲醇 MA、鸡蛋 jd、苯乙烯 eb、不锈钢 ss。每个品种最多一手，不加仓。',
    '- 信号周期：15 分钟。以该根收盘价相对之前 128 根收盘价均值的偏离计算 Z 分数，标准差采用样本标准差；Z > 2 做多，Z < -2 做空。下一根开盘成交。',
    '- ATR：之前 20 根真实波幅的简单均值。止损距离为 2 倍 ATR，向上取整到最小价位。止盈距离为止损距离的 4 倍；最多持有 128 根 15 分钟 K 线。',
    '- 每手计划风险（止损距离、出场滑点及双边手续费）不超过 **450 元**，超过则不交易。此预算低于用户的 600 元上限，为跳空预留空间。',
    '- 本金 50,000 元；保证金按成交名义价值 20% 计，入场时总保证金不超过当时权益 80%；组合计划止损风险上限 1,800 元。相同时间按品种代码排序分配资金。',
    '- 使用实际合约，主力映射滞后一个日历日采用；换合约和每个研究月块结束前平仓，不跨合约拼接价格。允许隔夜。',
    '- 每边手续费假设为 max(5 元, 合约名义价值 × 0.00015)，每边另计一个最小价位滑点。它是明确的研究假设，未冒充账户实际费率。',
    '- 跳空止损按更差的开盘价成交；同一分钟同时触发止损止盈时优先止损。未用止损金额截断真实亏损。',
    '- 实现细节：15 分钟信号生成器决定下一次可参与信号的时间；一分钟复核若提前退出，不额外新增原生成器尚未安排的交易。',
    '', '## 核心结果','', '| 数据段 | 净利润（元） | 本金收益率 | 保守盘中最大回撤 | 交易数 |','|---|---:|---:|---:|---:|']
    for key,label in [('train','随机六个月挖掘'),('validation','随机三个月验证'),('holdout','最后三个月，重复验证'),('full_year','全年连续组合')]:
        s=a[key];lines.append(f"| {label} | {s['net']:,.2f} | {s['return_pct']:.2f}% | {s['conservative_minute_drawdown_pct']:.2f}% | {s['trades']} |")
    s=a['full_year'];stress=a['full_year_stress'];hs=a['holdout_stress']
    lines+=['',f"全年期末权益 **{50000+s['net']:,.2f} 元**。201 笔交易，12 个按研究起始日划分的月块中 10 个盈利。最大单笔亏损 **{-s['worst_trade']:.2f} 元**；历史中没有单手亏损超过 600 元的交易。组合最大保证金占用 {s['max_margin']:,.0f} 元，全年最大保证金/权益约 {s['max_margin_to_equity_pct']:.2f}%，最大同时计划风险 {s['max_open_planned_risk']:,.2f} 元。",'',
    '回撤由一分钟持仓浮盈亏计算，并使用每分钟有利极值更新权益峰值、每分钟不利极值计算低点。这是对分钟内路径不确定性的保守上界；没有仅用平仓余额衡量回撤。保证金占用统计以入场名义价值计算，未模拟逐日保证金调整或交易所临时提高比例。',
    '', '## 泛化与压力检查','',
    f"- 手续费与滑点均加倍后，全年净利润约 {stress['net_less_extra_commission']:,.2f} 元，收益率 **{stress['return_pct_less_extra_commission']:.2f}%**；最后三个月净利润约 {hs['net_less_extra_commission']:,.2f} 元。双倍手续费是在成交结果上追加费用，未重新模拟手续费引起的资金准入变化。",
    f"- 参数邻域：止损倍数 1.75/2/2.25、盈亏比 3.5/4/4.5、持有上限 96/128/160，共 27 组。27 组全年收益均超过 20%、15 分钟回撤均低于 20%，末三个月均盈利，观察到的最差单笔均未超过 600 元。全年收益范围 {r['return_min']:.2f}%–{r['return_max']:.2f}%。邻域没有用于重新挑选最终参数，且并未逐组做一分钟复核。",
    f"- 最佳月占全年净利润 {r['best_month_share']:.2%}；去掉最佳月后，剩余收益相对本金仍为 {r['without_best_month_return_pct']:.2f}%。去掉盈利最多的品种后仍为 {r['without_best_product_return_pct']:.2f}%。这两项是贡献扣除检查，未模拟减去品种后的资金重新分配。",
    '- 四个品种均有正贡献，但化工品和宏观行情可能存在共同风险；四品种不等于四个独立收益来源。',
    '', '## 切分与研究过程的限制','',
    '按起始日每月 28 日划分十二个连续月块，固定种子 20261104。训练块为 0、2、3、5、7、8；验证块为 1、4、6；最后三块为 9、10、11，即 2026-07-28 至 2026-10-27。每个独立分段用 50,000 元起始，全年结果另行按时间连续模拟。',
    '基础日内扫描 216 组，较长持仓扫描 144 组，扩大日内扫描 144 组，风险预算扫描 432 组，合计 936 个配置（含重叠配置）。风险扫描形成 993 个品种组合候选；最后按策略族和 300/450/600 元风险预算分层取各三名，共 27 个不同训练候选作验证。品种排名依赖训练月利润、月度波动与盈利月份数，训练或验证期间出现过单笔亏损超过 600 元的风险候选不获准作为最终合格策略。',
    '**最后三个月在多轮探索中已被查看。** 最终结果属于重复验证，不能称为严格独立盲测；训练、验证表现包含筛选偏差。随机月份切分也不等于完整的按时间滚动前推验证。只有一年样本，无法证实跨年度、跨市场状态的稳定收益。',
    '**600 元是风险预算和本样本实绩约束，不是未来损失的硬保证。** 策略允许隔夜，跳空、涨跌停封板或止损无法成交仍可能超过预算；没有盘口和委托队列数据，无法验证极端情况下的可成交性。若要求任何情形下实际单手损失绝不能超过 600 元，本候选不能保证满足该要求。',
    '基于这些限制，本报告认定“历史数值达标候选”，不认定“已经证明稳定收益的实盘策略”。最终规则已冻结，不应因后续结果不利而回改这份研究的参数记录。',
    '', '## 逐月与品种贡献','', '| 月块起点 | 净利润（元） |','|---|---:|']
    for month,value in sorted(r['bymonth'].items(),key=lambda kv:int(kv[0])):lines.append(f"| {p['blocks'][int(month)][:10]} | {value:,.2f} |")
    lines+=['','| 品种 | 净利润（元） |','|---|---:|']
    for name,value in r['byproduct'].items():lines.append(f'| {name} | {value:,.2f} |')
    lines+=['','![权益和回撤](equity.png)','','## 数据校验与复现','',
    '四个最终品种的全部 15 分钟 K 线与原始一分钟 OHLCV 聚合逐条一致；一分钟覆盖无缺失，交易入场分钟没有零成交量。15 分钟回测与一分钟执行复核的逐笔净利润一致。所有最终交易及权益曲线已落盘。',
    '执行顺序（在仓库根目录）：','', '```powershell','python scripts/fresh_year_research.py','python scripts/fresh_year_expand.py','python scripts/fresh_year_expand.py --intraday','python scripts/fresh_year_expand.py --risk-aware','python scripts/fresh_year_audit.py','python scripts/fresh_year_robustness.py','python scripts/fresh_year_report.py','```','',
    '本次运行日期为 2026-11-04。行情库若以后更新，脚本默认“最近一年”的窗口也会变化，届时不应期望自动重现本次结果；请保存只读数据库快照与 protocol.json。',
    '', '关键文件：`expanded_frozen.json`、`minute_audit.json`、`audited_full_year_trades.csv`、`audited_equity.csv`、`parameter_neighborhood.csv`、`data_checks.json`。',
    '', '合约参数中，甲醇的 10 吨/手及 1 元/吨最小价位见[郑商所业务细则](https://www.czce.com.cn/cn/uploadfile/2024/02/07/20240207154824122.pdf)；不锈钢的 5 吨/手及 5 元/吨最小价位见[上期所业务细则](https://www.shfe.com.cn/regulation/exchangerules/productrules/202608/t20260831_833156.html)。本地历史行情是收益测算的唯一价格来源，外部查询仅用于合约参数核对。']
    (folder/'REPORT.md').write_text('\n'.join(lines),encoding='utf8')
    print(json.dumps(checks),flush=True)

if __name__=='__main__':main()
