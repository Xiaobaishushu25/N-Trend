"""Create a reviewable report and freeze the chosen observation specification."""
import hashlib
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

import research_swing_expansion_20261104 as x
import research_swing_year_20261104 as b


def read(path):return json.loads(path.read_text(encoding='utf8'))


def anchored_blocks(eq):
    boundaries=['2025-10-28','2025-11-28','2025-12-28','2026-01-28','2026-02-28','2026-03-28','2026-04-28','2026-05-28','2026-06-28','2026-07-28','2026-08-28','2026-09-28','2026-10-28']
    return {boundaries[i]:b.metrics(eq,[],boundaries[i],boundaries[i+1])['net'] for i in range(12)}


def main():
    comp=read(x.OUT/'comparison.json');audit=read(x.OUT/'robustness.json')
    # Calendar is inferred identically to the evaluator, including Friday nights.
    import sqlite3
    c=sqlite3.connect(f'file:{b.DB.as_posix()}?mode=ro',uri=True)
    b.CALENDAR=[r[0] for r in c.execute("SELECT DISTINCT date(start_ns/1000000000,'unixepoch','+8 hours') FROM bars WHERE period_seconds=3600 AND cast(strftime('%H',start_ns/1000000000,'unixepoch','+8 hours') AS int) BETWEEN 9 AND 14 ORDER BY 1")];c.close()
    names={comp['selected']['id']:'A: MTF pullback completion', 'oi_drive_15_32_target3_r500':'B: OI-confirmed breakout'}
    details={};reconciliations=[];fig,axes=plt.subplots(2,1,figsize=(11,7),sharex=True,gridspec_kw={'height_ratios':[2,1]})
    for name,r in audit.items():
        ts=pd.read_csv(x.OUT/f'audit-{name}-base-trades.csv')
        eq=pd.read_csv(x.OUT/f'audit-{name}-base-equity.csv',index_col=0).iloc[:,0]
        meq=pd.read_csv(x.OUT/f'{name}-minute-equity.csv',index_col=0).iloc[:,0]
        assert abs(sum(ts.net)-eq.iloc[-1])<1e-6
        assert abs(sum(r['scenarios']['base'][part]['net'] for part in ['train','validation','test'])-r['scenarios']['base']['full']['net'])<.021
        for label,scenario in r['scenarios'].items():
            ledger=pd.read_csv(x.OUT/f'audit-{name}-{label}-trades.csv')
            curve=pd.read_csv(x.OUT/f'audit-{name}-{label}-equity.csv',index_col=0).iloc[:,0]
            err=float(abs(ledger.net.sum()-curve.iloc[-1]));assert err<1e-6
            assert (ledger.planned_risk<=r['model']['risk']+1e-6).all()
            reconciliations.append(dict(model=name,scenario=label,ledger_error=err,trades=len(ledger)))
        ordered=ts.sort_values('exit_ns');streak=worst=0
        for net in ordered.net:
            streak=streak+1 if net<0 else 0;worst=max(worst,streak)
        wins=ts.loc[ts.net>0,'net'];losses=ts.loc[ts.net<=0,'net']
        details[name]=dict(anchored_months=anchored_blocks(eq),worst_loss_streak=worst,biggest_loss=float(ts.net.min()),biggest_win=float(ts.net.max()),mean_winner=float(wins.mean()),mean_loser=float(losses.mean()),active_products=sorted(ts.symbol.unique()),trade_risk_max=float(ts.planned_risk.max()),minute_drawdown=r['minute_audits']['minute']['full']['max_drawdown'])
        ix=pd.to_datetime(meq.index.astype('int64'),utc=True).tz_convert('Asia/Shanghai')
        axes[0].plot(ix,50000+meq.values,label=names[name],linewidth=1.2)
        dd=meq.values-np.maximum.accumulate(np.r_[0,meq.values])[1:]
        axes[1].plot(ix,dd,label=names[name],linewidth=1)
    axes[0].set(title='Second-round swing candidates | CNY 50,000 | 1-tick fills + modeled fees',ylabel='Minute-close marked equity (CNY)')
    axes[1].set(ylabel='Drawdown from running peak (CNY)');axes[1].axhline(-7500,color='#c43b3b',ls='--',label='CNY 7,500 drawdown target')
    for ax in axes:ax.grid(alpha=.2);ax.legend(fontsize=8)
    fig.autofmt_xdate();fig.tight_layout();fig.savefig(x.OUT/'candidates-equity.png',dpi=170);plt.close(fig)
    a=audit[comp['selected']['id']];other=audit['oi_drive_15_32_target3_r500']
    metricrows=[]
    for title,key in [('全年净收益','net'),('收盘权益最大回撤','max_drawdown'),('交易数','trades'),('胜率','win_rate')]:
        av=a['scenarios']['base']['full'][key];bv=other['scenarios']['base']['full'][key]
        metricrows.append(f'|{title}|{av:,.2f}|{bv:,.2f}|')
    def row(label,stats):return f"|{label}|{stats['full']['net']:+,.2f}|{stats['full']['max_drawdown']:,.2f}|{stats['validation']['net']:+,.2f}|{stats['test']['net']:+,.2f}|{stats['full']['trades']}|"
    pressure='\n'.join(row(label,a['scenarios'][key]) for label,key in [('每边1跳','base'),('每边2跳，固定计划筛选','slip2'),('每边3跳，固定计划筛选','slip3'),('手续费翻倍，固定计划筛选','fee2'),('保证金假设20%','margin20'),('同时信号反向排序','reverse')])
    cohort='\n'.join(row(label,a['minute_audits'][key]) for label,key in [('原始1分钟复核，1跳','minute'),('冻结原43笔及订单价位，3跳','minute_slip3'),('冻结原43笔，入场延迟1分钟','minute_delay1')])
    neighborhood=[r for r in comp['results'] if r['model']['family']=='pullback_end' and r['model']['tf']==60 and r['model']['risk']==250]
    near='\n'.join(f"|{r['model']['n']}|{r['model']['exit']}|{r['full']['net']:+,.2f}|{r['validation']['net']:+,.2f}|{r['test']['net']:+,.2f}|" for r in neighborhood)
    familyrows=[]
    for family in x.FAMILIES:
        rows=[r for r in comp['results'] if r['model']['family']==family and r['train']['trades']>=5]
        best=max(rows,key=lambda r:r['train']['net']/max(r['train']['max_drawdown'],250))
        familyrows.append(f"|{family}|{best['model']['id']}|{best['train']['net']:+,.2f}|{best['validation']['net']:+,.2f}|{best['test']['net']:+,.2f}|")
    report='''# 波段策略第二轮：多周期结构入场（2026-11-04）

**找到值得前向观察的候选A：日线顺势 → 约一小时回调结束 → 结构止损 → 固定3R退出。** 本轮5类机制、80个规则/风险组合中，7个通过事先记录的开发/验证门槛。A按开发期收益/回撤排序并通过验证后选出，最后三个月不参与选取。

它在开发、验证、最后测试段均为正，参数邻域和成本压力下仍有收益；这比首轮的失败对照明显改善。**但全部仍来自反复观察过的同一年历史，不能称为已证明稳定的实盘策略。**

## 范围和新增思路

仍使用2025-10-28—2026-10-27的本地22品种真实月份合约，预热从2025-09-01开始。初始本金50,000元，目标回撤7,500元，不使用另外两年的盈利选参。

这轮把三件事分开：日线判背景、较小周期找风险边界、退出规则匹配持仓目标。首轮日线ATR止损使多数品种一手风险过大，短周期持续追踪又容易提前退出；新规则用结构失效位约束风险，允许盈利发展到3R。信号涉及五类：波动收缩突破、回调结束、增仓突破、区间假突破和突破后回踩。

共同网格：15分钟或4根15分钟组成的交易时长小时；参数16/32；盈利2R后才启用3ATR追踪或固定3R；单手计划风险250/500元。共5×2×2×2×2=80个组合。未按全年收益挑选盈利品种。

回测改为账户状态直接驱动：拒绝的开仓不会形成隐藏持仓，后续信号仍能竞争；每次入场均检验资金、手续费及止损滑点后的计划风险。旧轮与新轮同时改变了入场、退出和机会处理，因此收益差不能全部归因于单个因素。

|每类机制按开发期排序领先的对照|规则ID|开发段|验证段|最后测试段|
|---|---|---:|---:|---:|
'''+ '\n'.join(familyrows)+'''

此表用来比较思路，不以最后测试段重新选择家族。增仓模型是同样通过原始门槛的机制对照B，随后成本压力表现不合格。

## 两个候选的基准账户结果

每边1跳，已扣冻结手续费。金额人民币；百分比均相对初始5万元，不年化。

|指标|A：多周期回调结束|B：增仓突破|
|---|---:|---:|
'''+ '\n'.join(metricrows)+'''
|收益/初始本金|8.98%|23.84%|
|15分钟收盘回撤/初始本金|1.97%|8.52%|
|1分钟收盘复核回撤|1,004.87|4,350.26|
|持仓自然小时中位数|139（约5.8天）|见逐笔及JSON|
|单笔计划风险上限|250元|500元|

基准账户权益含持仓浮动损益。1分钟复核固定原先获准的交易集合，不因更早退出追加新交易；所以B的一分钟净额13,006.69元不能替代完整账户重算结果11,921.71元。A的43笔原始分钟复核没有改变任何一笔净额。

![两个候选的一分钟收盘权益与回撤](candidates-equity.png)

## 优先保留A：完整可复核规则

1. **日线背景**：按推断交易日合并真实合约15分钟线，夜盘归后续日盘交易日。每个合约独立计算EMA10和EMA30，至少30个日线观测后才允许信号。只使用信号时已经闭合的日线；EMA10>EMA30只做多，反之只做空。
2. **约一小时信号线**：同一真实合约、同一交易日内，按时间把15分钟线每4根合并；不足4根的尾组不发信号。可以跨午休拼接，代表60个交易分钟，不能直接替换为软件原生自然小时线。
3. **多头回调结束**：小时EMA8>EMA32，前3根小时线的最低价小于当前EMA8；当前小时收盘突破前3根最高价，且收盘>开盘。空头完全镜像。小时EMA至少32根预热。
4. **入场**：信号闭合后，在紧接着的15分钟线开盘加不利1跳入场；若该时刻休市、没有连续下一根，信号失效。每次一手，不加仓。
5. **结构止损**：做多为当前及此前4根小时线的最低价再减1tick，做空为这5根最高价加1tick，均按tick向不利方向取整。止损固定，不跟随ATR收紧。
6. **入场门槛**：成交至止损至少4tick；不超过信号收盘至止损距离的1.5倍（比较基准至少4tick）。含开平手续费、入场滑点和止损退出滑点的计划风险≤250元；估算往返手续费及2边滑点≤初始价格风险15%。因此不是把所有品种硬压成小止损。
7. **退出**：从实际入场向有利方向设3倍初始价格风险距离的目标。目标要求价格至少穿过1个滑点距离才计成交，再计不利滑点。跳空止损按更差开盘价成交。15分钟内止损止盈皆触及则先按止损，随后另用分钟线核查顺序。
8. **时间及换月**：持有192根实际15分钟线仍未退出，下一根开盘平仓；离场后冷却60个自然分钟。主力切换时在旧合约当时真实开盘价平仓；样本末端强制平仓。允许隔夜、周末和长假持仓，存在跳空。
9. **账户约束**：最多4个持仓、同大类最多2个；初始计划风险之和≤1,000元；保证金按名义金额15%估算，入场时总保证金≤账户权益40%。总资金不足或一手超风险预算就跳过。

峰值入场保证金约17,306元；8个品种实际有成交（玉米、塑料、甲醇、聚丙烯、螺纹钢、锰硅、白糖、PVC）。多头净额约1,943元，空头约2,545元；并非单纯押一个方向。

## A的时间分段与成本压力

开发：2025-10-28—2026-04-27；验证：2026-04-28—2026-07-27；最后测试：2026-07-28—2026-10-27。基准分别为 **+2,792.58 / +483.04 / +1,212.19元**，平仓21/11/11笔。

验证段按28日至次月27日再分三个等长月份，分别+1,019.97、+81.02、−617.95元；说明中间后半段仍有亏损，不能把分段总额为正理解为每月上涨。

|完整账户情景|全年净额|收盘浮动回撤|验证段|最后测试段|交易数|
|---|---:|---:|---:|---:|---:|
'''+pressure+'''

成本加压时，入场计划的风险/摩擦筛选固定为基准1跳费率，不因成本增加事后剔除交易；实际成交价、费用、目标与占用按新情景重算。实际止损风险因此可略超250元计划预算。重新计算后A的三个时段仍为正。

|原获准交易集合的1分钟检验|全年净额|分钟收盘回撤|验证段|最后测试段|笔数|
|---|---:|---:|---:|---:|---:|
'''+cohort+'''

冻结交易集合的3跳检验只提高成交成本，保留原订单价位和基准触发阈值；它隔离了纯成本影响，不是另一套账户模拟。延迟1分钟检验也固定原交易集合及价位，只是执行敏感性。A基准43笔的首分钟均有实际成交量，未发现被动延迟入场。

验证段在冻结交易集合3跳下只剩+63.04元，优势较薄。**这是优先继续观察的理由，而不是直接实盘的充分证据。**

## 邻域、盈利集中度与统计边界

|小时参数|退出|全年净额|验证段|最后测试段|
|---:|---|---:|---:|---:|
'''+near+'''

上述4个相邻版本均全年、验证及最后测试为正，说明结果并非只在单一参数点成立；它们共享大量交易，不能视为4个独立样本。

A剔除最大单笔盈利仍+3,804.80元，剔除前三盈利交易仍+2,450.80元。删除全年最大盈利品种螺纹钢并重算账户，全年+3,482.25元、最后测试段+410.40元，但验证段转为−105.60元，分散稳健性仍不完整。

A的一分钟周权益变动按4周循环区块重采样4,000次，描述性全年净额95%区间约[1,434.81,8,042.41]元。此区间**没有修正80组合筛选、此前研究和事后观察偏差**，不能据此声称95%概率未来盈利。

## B：增仓突破为何暂列次级

B要求已闭合日线趋势同向、15分钟收盘突破此前32根区间、持仓量较32根前增长>1%、当根成交量高于此前20根均量；止损取最近5根结构极值外1tick，目标3R，单笔计划风险500元。其余账户约束相同。

基准+11,921.71元，开发/验证/最后测试分别+6,947.98、+1,353.12、+3,620.60元；每边2跳完整账户仍+9,316.90元。但每边3跳重算为**−2,470.95元、回撤8,459.88元**，超过7,500元目标。即使冻结原交易集合3跳仍有盈利，也不能覆盖完整账户路径恶化的问题。

B适合保留为机制对照；目前不符合本次要求的成本压力与回撤筛选。不能因为基准收益更高就替代A。

## 下一步与证据边界

优先冻结A参数进入前向模拟，记录每次信号、拒绝原因、真实可成交价、手续费和实际滑点，积累新的完整交易后再判断。不要按已经看过的最后三个月继续精调；本轮没有启动交易程序，也没有修改原生产策略。

- 数据覆盖为242个推断交易日/品种；官方交易日历、夜盘换月语义、逐日券商费率和保证金尚未完整重建。
- 止损假设可成交，未模拟涨跌停封板和盘口队列。分钟收盘回撤仍不是逐笔或盘中最坏回撤，不能保证实际损失封顶7,500元。
- 日线和小时指标均独立按真实月份合约预热；新主力历史不足时不发信号。历史主力成员使用前一个已观察交易日已知的映射。
- 5类机制不是唯一可能的策略；80组合是本轮有界探索，并未证明全局最优。此前首轮36规则也增加了同一年历史的研究暴露。

## 复现

```powershell
python scripts/research_swing_expansion_20261104.py
python scripts/audit_swing_expansion_20261104.py
python -m unittest discover -s tests -p test_swing_expansion_20261104.py -v
python scripts/report_swing_expansion_20261104.py
```

`comparison.json`保留全部80组合，`protocol.json`保留筛选条件，`robustness.json`保留两个候选的成本及分钟核验。各场景CSV包含逐笔交易和浮动权益；`reconciliation.json`记录现金核对；`detail-statistics.json`记录连亏、单笔损失和完整月块。冻结观察规范位于仓库 `research-strategies/mtf-pullback-observation-20261104-v1`。

核验测试覆盖未来OHLC/持仓量不改变既有特征、拒绝开仓不占用后续信号、风险预算包含费用、同柱止损止盈冲突按止损。12组账户逐笔净额与权益终值一致，两个基准重跑与原筛选结果一致。

方法来源：[CME持仓量说明](https://www.cmegroup.com/education/lessons/open-interest)将持仓量作为趋势强弱的辅助观察；本文将其作为待检验假设，未把它视为必然预测。[时间序列动量原始研究](https://www.aqr.com/Insights/Research/Journal-Article/Time-Series-Momentum)提供了跨市场趋势研究背景，其更长周期和跨资产结果不等于本轮短波段策略得到验证。
'''
    (x.OUT/'REPORT.md').write_text(report,encoding='utf8')
    x.write('detail-statistics.json',details);x.write('reconciliation.json',reconciliations)
    specdir=b.ROOT/'research-strategies/mtf-pullback-observation-20261104-v1';specdir.mkdir(parents=True,exist_ok=True)
    spec=dict(id=specdir.name,status='observation_candidate_not_live',model=comp['selected'],capital=50000,maximum_drawdown_target=7500,minute_close_drawdown=1004.87,rules_report='reports/swing-expansion-20261104/REPORT.md',rules_sections='优先保留A：完整可复核规则',frozen_source_hashes=read(x.OUT/'audit-implementation.json'),research_period=[b.START,b.END],prior_history_exposed=True,production_changed=False,forward_test_started=False)
    (specdir/'protocol.json').write_text(json.dumps(spec,ensure_ascii=False,indent=2),encoding='utf8')
    for source in [Path(x.__file__),Path(b.__file__),b.FEES]:
        (specdir/source.name).write_bytes(source.read_bytes())
    (specdir/'README.md').write_text('# 多周期回调结束观察候选\n\n冻结日期：2026-11-04。仅研究，不接入交易。\n\n'+report.split('## 优先保留A：完整可复核规则')[1].split('## A的时间分段')[0]+'\n源文件仅作冻结审阅快照，直接运行请使用仓库scripts目录中的原文件。\n',encoding='utf8')
    manifest={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in specdir.iterdir() if p.is_file() and p.name!='manifest.json'}
    (specdir/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf8')
    print('Report, 12 reconciliations and frozen observation specification written.')


if __name__=='__main__':main()
