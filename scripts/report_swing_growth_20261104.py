"""Reviewable third-round study, archive verification and frozen allocation spec."""
import hashlib
import json
import shutil
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

import research_swing_growth_20261104 as g
import research_swing_year_20261104 as b


def read(p):return json.loads(p.read_text(encoding='utf8'))


def main():
    comp=read(g.OUT/'comparison.json');short=read(g.OUT/'shortlist.json');minute=read(g.OUT/'minute-replay.json')
    archive=b.ROOT/'research-archives/swing-ab-complete-20261104-v1';complete=read(archive/'COMPLETE.json')
    assert complete['complete'] and complete['database_quick_check']=='ok'
    assert hashlib.sha256((archive/'manifest.json').read_bytes()).hexdigest()==complete['manifest_sha256']
    archived=read(archive/'reports/swing-expansion-20261104/robustness.json')
    # Regression controls demonstrate that the new account engine retains old
    # one-lot semantics before changing risk, margin capacity or entry logic.
    results={r['config']['id']:r for r in comp['results']}
    assert abs(results['A_original_20margin']['full']['net']-archived['pullback_end_60_32_target3_r250']['scenarios']['margin20']['full']['net'])<.011
    assert abs(results['B_original_20margin']['full']['net']-archived['oi_drive_15_32_target3_r500']['scenarios']['margin20']['full']['net'])<.011
    primary='A_scale_1250_m80';balanced='A_scale_750_m60';bigger='A_scale_1500_m80'
    eq=pd.read_csv(g.OUT/f'minute-{primary}-base-equity.csv',index_col=0).iloc[:,0]
    ts=pd.read_csv(g.OUT/f'minute-{primary}-base-trades.csv')
    wins=sorted(ts.loc[ts.net>0,'net'].tolist(),reverse=True)
    diagnostics=dict(primary=primary,remove_largest_winner=float(ts.net.sum()-wins[0]),remove_top3_winners=float(ts.net.sum()-sum(wins[:3])),mean_lots=float(ts.lots.mean()),max_campaign_loss=float(ts.net.min()),max_campaign_profit=float(ts.net.max()),trades=int(len(ts)),archived_originals_regression_passed=True)
    g.write('report-checks.json',diagnostics)
    fig,axes=plt.subplots(2,1,figsize=(11,7),sharex=True,gridspec_kw={'height_ratios':[2,1]})
    for name,label,scenario in [(balanced,'Balanced: CNY750 campaign / 60% entry margin','base'),(primary,'Growth: CNY1,250 campaign / 80% entry margin','base'),(primary,'Growth stress: 3 ticks per side','slip3')]:
        series=pd.read_csv(g.OUT/f'minute-{name}-{scenario}-equity.csv',index_col=0).iloc[:,0]
        ix=pd.to_datetime(series.index.astype('int64'),utc=True).tz_convert('Asia/Shanghai')
        axes[0].plot(ix,50000+series.values,label=label,lw=1.15)
        axes[1].plot(ix,series.values-np.maximum.accumulate(np.r_[0,series.values])[1:],label=label,lw=1)
    axes[0].set(title='Higher capital utilization | 20% notional margin | Initial capital CNY50,000',ylabel='Minute-close account equity (CNY)')
    axes[1].set(ylabel='Drawdown from peak (CNY)');axes[1].axhline(-10000,color='#b32d35',ls='--',label='20% of initial capital drawdown target')
    for ax in axes:ax.grid(alpha=.2);ax.legend(fontsize=8)
    fig.autofmt_xdate();fig.tight_layout();fig.savefig(g.OUT/'growth-equity.png',dpi=170);plt.close(fig)
    def summaryrow(label,name):
        r=minute[name]['base'];stress=minute[name].get('slip3',{});f=r['full']
        return f"|{label}|{f['net']:+,.2f}|{f['net']/500:.2f}%|{f['max_drawdown']:,.2f} ({f['max_drawdown']/500:.2f}%)|{stress['full']['net']:+,.2f} / {stress['full']['max_drawdown']:,.2f}|"
    overview='\n'.join(summaryrow(label,name) for label,name in [('A均衡配置',balanced),('A增长配置（开发段排序优先）',primary),('A更大预算对照',bigger)])
    pressure=[]
    for scenario,label in [('base','每边1跳'),('slip2','每边2跳'),('slip3','每边3跳'),('fee2','手续费翻倍'),('margin25','保证金25%'),('reverse','同时信号反向排序')]:
        s=short[primary]['scenarios'][scenario]
        pressure.append(f"|{label}|{s['full']['net']:+,.2f}|{s['full']['max_drawdown']:,.2f}|{s['train']['net']:+,.2f}|{s['validation']['net']:+,.2f}|{s['test']['net']:+,.2f}|")
    other=[]
    for name,label in [('A_pyramid','A盈利后加仓'),('B_target2_guard3','B按3跳成本选机会、2R退出')]:
        row=minute[name]['base'];s=short[name]['scenarios']['slip3']
        other.append(f"|{label}|{row['full']['net']:+,.2f}|{row['full']['max_drawdown']:,.2f}|{s['full']['net']:+,.2f}|{s['full']['max_drawdown']:,.2f}|")
    report='''# 更高收益波段研究：20%保证金与20%回撤目标（2026-11-04）

**主要结果：保留A原有机会资格，改善仓位分配后，5万元初始本金的样本年收益约37.88%；一分钟完整账户回放最大收盘浮动回撤9.70%。实际每边3跳加压后仍盈利24.34%，回撤16.90%，在用户更新后的20%目标内。**

这是**已有信号的资金配置提升**，不是发现了38%的独立新信号优势。新的高收益信号也做了研究，但其中46.58%收益的配置在成本加压后回撤接近30%，仍不合格。所有结果均来自同一年反复观察的历史，不能证明未来年收益或回撤。

## 1. 两个原候选已完整保存

归档目录：`research-archives/swing-ab-complete-20261104-v1`。73个文件、约2.465GB，含：

- A/B完整规则、原始80组合、源代码、测试、手续费快照；
- 逐笔交易、权益、成本和排序压力、分钟复核及全部研究报告；
- SQLite backup API生成的完整研究行情数据库一致性副本；`quick_check=ok`；
- 每个文件的SHA256及完成标记。原有代码与结果没有被本轮配置覆盖。

原A和B按20%保证金的一手对照，在新引擎中分别复现+4,224.04元、+11,353.82元，与已归档结果一致。

## 2. 本轮研究范围与用户风险更新

- 资金50,000元；保证金按合约名义金额20%估算；研究期2025-10-28—2026-10-27，22品种。收益率以5万元总本金为分母，不以已用保证金为分母，也不再把这一年收益外推年化。
- 用户最初目标回撤15%（7,500元），研究中补充“20%也能接受”，本轮按最大账户回撤20%（10,000元）更新，并保留原15%筛选记录。
- 首批74个组合包含新信号、混合策略、成本资格、退出与仓位；用户更新后追加4个更高预算配置，共78个。新增信号是增仓回调结束、增仓突破后回踩、减仓假突破收回、放量增仓强K线；同时研究A/B单账户组合及盈利后加仓。
- 新信号要求开发段至少25笔；已有A的仓位对照沿用其先前至少20笔的门槛，后者没有增加独立信号样本。20%扩展筛选允许开发/验证回撤≤10,000元；验证至少10笔且净收益为正；开发收益排序，最后测试不参与排序。该更新发生在研究过程中，不能称为完全预先固定的样本外筛选。
- 完整账户模拟所有机会竞争、资金和多手占用；不把多个品种或策略收益简单相加。所有成本压力固定原计划筛选门槛，实际成本变化后重算整个账户。

## 3. 更高资金利用率配置

以下采用原始一分钟行情完整账户重放，止损止盈逐分钟判断；信号、保护更新及持仓时间仍只按原15分钟/小时闭合时点推进。不是冻结原平仓单的简单收益乘倍数。保证金20%，模型手续费已扣。

|配置|样本年净收益|本金收益率|分钟收盘最大浮动回撤|3跳重算：净收益 / 回撤|
|---|---:|---:|---:|---:|
'''+overview+'''

![增长配置与均衡配置权益及压力回撤](growth-equity.png)

### 优先观察：A增长配置

信号保持A原规则：日线EMA10/30定方向；4根15分钟组成交易小时；小时EMA8/32顺向，回调后收盘突破此前3根高/低点；止损取当前及此前4根小时极值外1tick；下一连续15分钟开盘入场，固定3R目标，最多192根15分钟持仓，冷却60个自然分钟。允许隔夜、周末和长假持仓；主力切换在旧合约实际开盘价平仓。

变化只在资金配置：

- **机会资格仍要求一手含计划手续费和滑点的风险≤250元**，不放宽止损或混入高风险机会。
- 一次交易总计划风险预算1,250元（初始本金2.5%），最多6手；实际手数取风险预算、剩余保证金和总风险额度三者允许的整数最小值。
- 全账户计划初始风险之和≤3,000元（初始本金6%）；最多4个品种，同大类最多2个。
- 入场时保证金总占用≤当前权益80%；保证金率按名义金额20%。这对应最高约4倍名义资金敞口，是较积极的仓位配置。
- 单笔预算固定，不因盈利同比提高；保证金额度随当前权益变化。历史峰值入场保证金约52,720元发生在账户已有盈利后，不需要追加本金。

基准42个交易周期，持仓中位数约139.9小时（约5.8天）。开发/验证/最后测试收益分别 **+10,761.98 / +3,219.60 / +4,959.48元**；对应一分钟回放同样一致。增长版选择依据是开发段排序，不是因为更大预算版全年多赚938元就改选后者。

### 完整账户成本及资金压力

|情景|全年净额|15分钟收盘回撤|开发段|验证段|最后测试段|
|---|---:|---:|---:|---:|---:|
'''+ '\n'.join(pressure)+'''

3跳情景的一分钟完整账户重放为+12,172.32元，最大收盘浮动回撤8,450.03元，开发/验证/最后测试仍都为正。手续费翻倍、保证金25%、反向排序对照也保留正收益；它们不是独立样本。

一分钟复核中，基准及3跳情景最低“权益减估算保证金”分别约10,712元及10,592元，没有观察到保证金不足。入场80%占用不是持续硬上限；市值波动后占用比例可以升高。20%保证金是统一研究假设，不是每个合约逐日核验的券商保证金。

### 更温和的A均衡配置

单周期风险预算750元、最多4手、总风险2,000元、保证金占用上限当前权益60%，其他信号相同。样本年收益26.96%，分钟收盘回撤5.57%；3跳压力收益17.62%、回撤8.40%。相较增长配置，它保留更多空闲资金，收益目标也相应较低。

## 4. 新退出、加仓及信号的结果

|机制|一分钟基准净收益|分钟收盘回撤|3跳完整账户净收益（15分钟执行）|3跳回撤|
|---|---:|---:|---:|---:|
'''+ '\n'.join(other)+'''

A盈利加仓配置只在已闭合15分钟达到1.5R浮盈后，把保护位提高至原入场有利方向0.5R，下一根开盘再申请加1手；受单笔、总风险和保证金约束，不在亏损时补仓。44个周期中发生22次加仓；所有原始开仓、加仓及平仓都有独立成交明细，现金对账通过。其样本收益21.06%，没有胜过更积极的直接仓位配置，但提供了另一种资金使用方式。

B的改良方向是**按每边3跳预估成本提前筛选机会，固定2R退出**。它用不同的资格和退出改善原B对成本的敏感性；基准收益18.48%，3跳仍约11.86%，但利润较集中在最后测试段。本轮完整一分钟基准回放与原15分钟净额一致。

增仓回调结束、5R目标的C配置有+23,288.43元（46.58%）基准收益，但基准回撤9,794.47元，2跳回撤12,258.43元，3跳回撤14,748.30元。即使用户接受20%回撤，这个配置也未通过成本压力。**不把它作为合格高收益策略推荐。**

混合A与新信号不必然更稳：有组合开发和验证盈利，最后测试转亏。收益来源需要分开核查，不能把本轮提高收益全部解释为新逻辑优势。

## 5. 实际决策与局限

本轮优先保留A增长配置作前向观察，A均衡配置作较低风险对照；原A/B继续保存在完整归档。新的执行引擎、配置及成交证据另存，不接入生产交易。

这次更高收益主要来自**利用已有信号承担更多现金风险和保证金占用**。其收益和亏损会同步放大；一分钟收盘回撤仍不是盘中或逐报价最坏回撤，封板及跳空可能令止损无法按模型价格成交。保证金作为履约担保，不是损失上限，参考[CFTC期货保证金说明](https://www.cftc.gov/LearnAndProtect/AdvisoriesAndArticles/economicpurpose.html)。

同一年数据先后经历首轮36规则、第二轮80组合和本轮78配置；最后三个月早已被观察，不能再称独立保留集。低回撤和高收益可能被多重筛选夸大。用户可接受20%是研究目标，不是本系统对实际回撤的保证。

较高预算仍只得到42个交易周期，没有比一手A增加独立样本。前向检验应固定规则，核实真实手续费、滑点、保证金和跳空执行，不能根据这张历史曲线承诺未来38%。

## 6. 复现与核验

```powershell
python scripts/archive_swing_ab_20261104.py
python scripts/research_swing_growth_20261104.py
python scripts/audit_swing_growth_20261104.py
python -m unittest discover -s tests -p 'test_swing*20261104.py' -v
python scripts/report_swing_growth_20261104.py
```

输入分钟覆盖1,705,500根，与选中15分钟线的组成分钟数完全一致。完整一分钟账户复核8个配置/成本情景；逐笔开仓、加仓、平仓净额均与账户权益终值一致；原A/B一手20%保证金控制结果与归档一致。4项新增测试覆盖多手现金、只在已闭合浮盈后加仓、亏损不能加仓、20%保证金约束，连同此前9项测试均通过。

`comparison.json`保留全部78组合及15%/20%门槛结果；`shortlist.json`保留48个压力账户；`minute-replay.json`为8个分钟重放；逐周期和完整成交明细分别在CSV/JSON中；`fill-reconciliation.json`记录现金核对。源文件SHA256见implementation及audit-implementation。

本轮观察规范另存为 `research-strategies/mtf-pullback-growth-observation-20261104-v1`。核心数据副本复用原A/B完整归档中的一致性快照，不重复拷贝整个数据库。
'''
    (g.OUT/'REPORT.md').write_text(report,encoding='utf8')
    spec=b.ROOT/'research-strategies/mtf-pullback-growth-observation-20261104-v1';spec.mkdir(parents=True,exist_ok=True)
    selected=dict(id=spec.name,status='observation_not_live',capital=50000,margin=.2,drawdown_target=10000,primary_config=short[primary]['config'],balanced_config=short[balanced]['config'],primary_results=minute[primary],balanced_results=minute[balanced],data_archive=str(archive.relative_to(b.ROOT)),evidence_report=str((g.OUT/'REPORT.md').relative_to(b.ROOT)),selection_amended_after_user_risk_update=True,production_changed=False)
    (spec/'protocol.json').write_text(json.dumps(selected,ensure_ascii=False,indent=2),encoding='utf8')
    for name in ['research_swing_growth_20261104.py','research_swing_expansion_20261104.py','research_swing_year_20261104.py','audit_swing_growth_20261104.py','exchange_fees_20261022.json']:
        shutil.copy2(b.ROOT/'scripts'/name,spec/name)
    shutil.copy2(g.OUT/'REPORT.md',spec/'README.md')
    shutil.copy2(g.OUT/f'minute-{primary}-base-fills.json',spec/'primary-fills.json')
    shutil.copy2(g.OUT/f'minute-{primary}-slip3-fills.json',spec/'primary-stress-fills.json')
    (spec/'manifest.json').write_text(json.dumps({p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in spec.iterdir() if p.is_file() and p.name!='manifest.json'},indent=2),encoding='utf8')
    print('Growth report and frozen observation package written.')


if __name__=='__main__':main()
